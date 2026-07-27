# Phase 3 — Creator Experience (Dashboard + Notifications + Scheduling) — GROUNDING

**Status:** Read-only grounding. No implementation, branches, commits, PRs, or code changes.
**Baseline:** `v0.4.39-phase3-alpha8.8` (version constant `0.4.39-phase3-alpha8.8`, `backend/app/main.py:103`).
**Scope of this document:** Facts verified from source, plus a factual assessment of what
already exists, what is reusable, the minimal additions implied, and whether a migration or
ADR is genuinely required. Design/rulings are deferred to the pre-flight.

The selected slice — *Creator Experience (Dashboard + Notifications + Scheduling)* — is a
**product-surface** slice: expose the already-built publishing/generation primitives as a
usable creator product. Per the directive it must be **strictly additive** and must not touch
orchestration/generation/render/export/publishing runtime execution models, the Asset
Promotion Bridge, AI providers, the Planner, or the verification pipeline.

---

## 0. Method

Every fact below was read directly from the repository at the frozen baseline. File paths are
given for verification. No behaviour was executed; no files were modified.

---

## 1. Existing notification projection

**Exists and is complete for the export context.**

- `backend/app/application/use_cases/notifications/notification_projection.py` —
  `NotificationProjection`. It is an `EventHandler` registered on the in-process
  `PublisherPort`. It handles **only** `ExportJobSucceeded` / `ExportJobFailed`
  (`_HANDLED_EVENT_TYPES`, importing the constants from
  `app.application.use_cases.export._events`).
- It maps each event to notification content (`kind`, `title`, `body`, `payload`) and calls a
  **freshly-built** `CreateNotification` (factory injection → one Unit of Work per event).
- Idempotency is DB-owned: it addresses `payload["requested_by_user_id"]` and dedupes on
  `event.id` via the partial-unique `(user_id, source_event_id)` index. Malformed payloads
  are logged and skipped (never park the relay); genuine DB failures propagate for redelivery.
- `CreateNotification` (`.../notifications/create_notification.py`) is the transactional
  writer; wired via `container.get_create_notification_use_case()`
  (`backend/app/core/container.py:1255`).

**Fact:** the projection listens to export events **only**. There is **no** projection for
publish terminal events (this is the deferred DQ7 / roadmap item α8.6d, see §11/§13).

---

## 2. Publish terminal events

**Exist and are fan-out ready.**

- `backend/app/application/use_cases/publishing/_events.py` defines:
  - `EVENT_PUBLISH_JOB_CREATED = "PublishJobCreated"`
  - `EVENT_PUBLISH_JOB_SUCCEEDED = "PublishJobSucceeded"`
  - `EVENT_PUBLISH_JOB_FAILED = "PublishJobFailed"`
  - `AGGREGATE_TYPE = "publish_job"`
- Payloads (`_base_payload`) carry: `publish_job_id`, `project_id`,
  **`requested_by_user_id`**, `social_account_id`, `platform`, `source_export_job_id`,
  `source_media_asset_id`, `status`, `version`. Success adds `platform_post_id`,
  `platform_post_url`, `published_at`; failure adds a neutral `error` dict. **No credential /
  bearer / bytes** (PUB-8 / ADR-0047 C8).
- Emitted inside the caller's UoW transaction (transactional outbox), consistent with
  `export._events`.
- The module docstring explicitly records: *"DQ7 (deferred): α8.6b emits terminal events
  only. There is **no** notification projection in this slice; a downstream `publish.*`
  consumer is a follow-up."*

**Fact:** `requested_by_user_id` is present on both terminal events, so a publish-notification
projection can address the creator with **no** event-schema change (mirrors the export
projection precisely).

---

## 3. Scheduled publishing support

Two distinct, already-existing scheduling concepts — **both partially present, neither
exposed at create time.**

### 3a. `scheduled_at` — worker-side deferred execution
- Column `publish_jobs.scheduled_at timestamptz` (migration `0014_publish_jobs.py:62`;
  ORM `backend/app/infrastructure/db/models/publishing.py:153`).
- Index `ix_publish_jobs_status_scheduled_at` on `(status, scheduled_at)`
  (`0014_publish_jobs.py:91`) — the claim scan.
- The claim scan already honours it: `INotificationRepository`/publish repo contract
  (`backend/app/application/interfaces/repositories.py:2416`) documents `list_claimable`
  returning `status='queued' AND (scheduled_at IS NULL OR scheduled_at <= now)`.
- `PublishWorker.run_once()` (`.../publishing/publish_worker.py:53`) calls
  `list_claimable(now=now, ...)`, so a job with a **future** `scheduled_at` is naturally
  deferred until due.
- Retry backoff already writes future `scheduled_at`: `reschedule(..., scheduled_at=...)`
  (`repositories.py:2462`), exercised by `ProcessPublishJob` (DQ6 capped exponential backoff).
- **BUT** `CreatePublishJob.execute()` hardcodes `scheduled_at=None`
  (`.../publishing/create_publish_job.py:164`), and the create API body has no schedule field
  (`backend/app/api/v1/schemas/publish_jobs.py`, `PublishJobCreateRequest`).

### 3b. `publish_at` — platform-side scheduled visibility
- `ContentPackage.publish_at: datetime | None` exists and round-trips through JSONB
  (`backend/app/domain/publishing/content_package.py:40`, `to_dict`/`from_dict`).
- `build_content_package(..., publish_at=...)` already accepts it
  (`content_package.py:86`).
- The YouTube destination already maps it: a `publish_at` forces `privacyStatus=private` +
  `status.publishAt` (YouTube schedules the public transition)
  (`backend/app/infrastructure/publishing/destinations/youtube.py:106-111`).
- **BUT** `CreatePublishJob` never passes `publish_at` into `build_content_package`
  (`create_publish_job.py:145-152`), and the API body has no such field.

**Fact:** deferred/scheduled publishing is **already fully supported by the persistence
layer, the worker claim scan, and the YouTube adapter**. The only missing link is accepting a
schedule at create time and threading it through `CreatePublishJob` → `repo.add(scheduled_at=)`
and/or `build_content_package(publish_at=)`. No new column and no runtime execution change is
implied by the storage/runtime facts.

---

## 4. Notification infrastructure

**In-app write + read surface is complete; delivery channels are not.**

- Table `notifications` (`backend/app/infrastructure/db/models/notifications.py`): `user_id`,
  `kind` (**free-form Text**), `title`, `body`, `payload` (JSONB), `source_event_id`,
  `delivered_in_app_at`, **`delivered_email_at`** (present in the table), `read_at`,
  `archived`, plus `created_at`/`updated_at`. Indexes: partial unread
  (`ix_notifications_user_id_unread`), feed (`ix_notifications_user_id_created_at`), and
  partial-unique `uq_notifications_user_id_source_event_id`.
- Migration `0009_notifications_source_event_id.py` added the dedupe column; the table itself
  originates in the baseline.
- Domain entity `backend/app/domain/notifications/notification.py` (frozen dataclass).
  Note: it models `delivered_in_app_at`, `read_at`, `archived` but **not** `delivered_email_at`.
- Repository `backend/app/infrastructure/repositories/notification_repository.py` —
  `add` (ConflictError on the unique index → idempotent no-op), `list_for_user` (keyset
  `created_at DESC, id DESC`), `count_unread`, `mark_read`, `mark_all_read`.
- Use cases: `CreateNotification`, `ListNotifications`, plus count/mark-read/mark-all-read
  (`backend/app/application/use_cases/notifications/`).
- API: `backend/app/api/v1/routers/notifications.py` —
  `GET /notifications` (keyset feed, `?limit`/`?cursor`), `GET /notifications/unread-count`,
  `POST /notifications/{id}/read`, `POST /notifications/read-all`. DTOs in
  `backend/app/api/v1/schemas/notifications.py` (`NotificationPublic`, `UnreadCountPublic`,
  `MarkAllReadResult`). `delivered_email_at` and `archived` are **deliberately not exposed**.

**Fact — no delivery channels exist.** There is **no** `INotifier` port, no email/SMTP/push
provider, no template/retry machinery. The `delivered_email_at` column is dormant. The
notifications-schema docstring records email as a later slice (α8.5b.4) and archive as
deferred. Because `kind` is free-form text, adding new notification categories (e.g.
`publish.*`) requires **no** schema change.

---

## 5. Analytics events

**Dormant baseline schema; no code path uses it.**

- `backend/app/infrastructure/db/models/analytics.py` — `AnalyticsEvent`
  (`analytics_events`), append-only, **partitioned monthly** by `occurred_at`
  (`postgresql_partition_by = "RANGE (occurred_at)"`), columns `tenant_id`, `user_id`,
  `session_id`, `event_name`, `properties` (JSONB + GIN), `ip`, `user_agent`, `occurred_at`.
  Schema ref: `docs/database/schema.md §26`, created in `0001_baseline.py`.
- **No repository, no use case, no API, and no writer/reader** reference `AnalyticsEvent`
  anywhere in `app/` (only the model, the model registry, ERD/validation scripts, and the
  baseline migration mention it).

**Fact:** there is an analytics *table* but no analytics *subsystem*. It is out of the
selected slice unless the pre-flight deliberately activates it; the slice can be delivered
without touching it.

---

## 6. Dashboard projections

**None exist.**

- No `dashboard` router, schema, use case, projection, repository, table, or migration exists
  (`ls backend/app/api/v1/routers/` and `schemas/` show none; no `dashboard` symbol in `app/`).
- The read primitives a dashboard would aggregate **already exist** as per-resource endpoints:
  - `GET /api/v1/publish-jobs` — owner list, newest first
    (`ListPublishJobs` → `list_for_owner`; **unpaginated**).
  - `GET /api/v1/publish-jobs/{id}` — single job.
  - `GET /api/v1/render-jobs` — owner list (`render_jobs.py:103`).
  - `GET /api/v1/workflow-runs` — owner list (`workflow_runs.py:148`).
  - `GET /api/v1/media` — owner list (media router).
  - `GET /api/v1/social-accounts` — owner list.
  - `GET /api/v1/notifications` + `/unread-count` — owner feed + an **aggregate-count
    precedent**.
- **Gap:** `export_jobs` has **no** owner-list endpoint — only `GET /export-jobs/{id}` and
  `GET /export-jobs/{id}/download` (`export_jobs.py`). A dashboard that lists exports would
  need a new owner-scoped list (read-only, additive).

**Fact:** a "dashboard" today would be a **new read/aggregation surface** composed over
existing owner-scoped reads. There is no existing aggregate/projection to reuse beyond
`count_unread`'s pattern.

---

## 7. API surface (current)

Routers under `backend/app/api/v1/routers/`: `auth`, `export_jobs`, `health`, `media`,
`notifications`, `projects`, `prompts`, `publish_jobs`, `render_jobs`, `scenes`,
`social_accounts`, `timeline`, `users`, `versions`, `webhooks`, `workflow_runs`.

- **No** `dashboard`, `analytics`, `scheduler`, or `feed` router.
- All authenticated endpoints follow ADR-0034 (the `CurrentUserDep` seam) and the envelope
  helper (`app/api/v1/helpers.py:envelope`, keyset `next_cursor` support).
- Pagination primitive: `app/application/pagination.py` (opaque cursor), already reused by
  notifications and projects.

---

## 8. Scheduler support

**No in-process scheduler / cron / task loop exists.**

- The FastAPI `lifespan` (`backend/app/main.py:69-86`) only pings the DB on startup and
  disposes the engine on shutdown. It starts **no** background tasks.
- All background work is **poll-once** ingress, invoked externally (tests / CI / scripts):
  `RenderWorker.run_once`, `ExportWorker.run_once`, `MediaEnrichmentWorker.run_once`,
  `PublishWorker.run_once`, and `RelayService.relay_once`. Each has a config-tunable batch
  size (`backend/app/core/config.py`).
- `PublishWorker` docstring: *"No trigger, endpoint, or cron is added … the worker is invoked
  externally, exactly as `ExportWorker` is (PUB-7)."*
- No `APScheduler` / Celery / `crontab` / `asyncio.create_task` loop anywhere in `app/`.

**Fact:** time-based scheduling of a publish relies on `scheduled_at` + whatever external
cadence already drives `PublishWorker.run_once` (the same cadence that already drives every
retry backoff today). No new scheduler is *required* to honour a future `scheduled_at`; adding
one would be a new, non-additive runtime concern (a design question for the pre-flight, not a
grounding fact).

---

## 9. `publish_jobs` scheduling model

From migration `0014_publish_jobs.py` and `db/models/publishing.py`:

- Columns relevant to scheduling/retry: `status publish_status` (enum:
  `queued|running|succeeded|failed|canceled`), `scheduled_at timestamptz` (nullable),
  `attempt int default 0`, `max_attempts int default 5`, `published_at`, `finished_at`,
  `version` (OCC), plus `content_package jsonb` (holds `publish_at`).
- Ownership: `tenant_id` + `requested_by_user_id` (direct), explicit `project_id`.
- Idempotency backstop: partial-unique `uq_publish_jobs_source_media_asset_social_account`
  over `status IN ('queued','running','succeeded')`.
- Owner-listing index: `ix_publish_jobs_requested_by_user_id_created_at`.
- OCC + audit triggers mirror `export_jobs` (`touch_updated_at`, guarded `bump_version`).

Repository contract (`app/application/interfaces/repositories.py`, publish-jobs section):
`add(..., scheduled_at, ...)`, `resolve_source`, `get_active`, `get_owned`, `list_for_owner`,
`list_claimable(now=, limit=)` (due filter), `claim`, `mark_*`, `reschedule(scheduled_at=,
error=)`.

**Fact:** the model already carries everything needed to *store* a creator-chosen schedule
(`scheduled_at`) and a platform-side schedule (`content_package.publish_at`). Nothing in the
model blocks scheduling; the block is purely that the create path never sets them.

---

## 10. Outbox events

- Transactional outbox: `event_outbox` table; `OutboxEvent`
  (`app/application/interfaces/publisher.py`), written via `uow.outbox.add(...)`.
- Relay: `RelayService.relay_once()` (`app/application/use_cases/relay/relay_service.py`)
  reads unpublished rows and dispatches to the `PublisherPort`.
- Publisher: `InProcessPublisher` (`app/infrastructure/publisher/in_process_publisher.py`),
  constructed in `container.init` with a **handler list** (fan-out) —
  `GeneratedMediaIngestionSubscriber` + `NotificationProjection`
  (`backend/app/core/container.py:343-348`).
- Event-type catalogue (all `_events.py`):
  - workflow: `WorkflowRunCreated/Started`, `WorkflowStepCompleted`,
    `WorkflowRunPaused/Resumed/Succeeded/Failed/Canceled`
  - render: `RenderJobCreated/Canceled/Succeeded/Failed`
  - export: `ExportJobCreated/Succeeded/Failed`
  - publishing: `PublishJobCreated/Succeeded/Failed`

**Fact:** a new downstream projection attaches by adding one handler to the
`InProcessPublisher([...])` list — the documented fan-out seam. Adding a consumer does not
touch producers or the relay (ADR-0042 property; ADR-0041 event-projection pattern).

---

## 11. Current contracts

Relevant published contracts under `docs/engineering/`:

- `PUBLISHING_RUNTIME_CONTRACT.md` — PUB-1..PUB-11 (incl. PUB-2 explicit-user-intent /
  no auto-publish; PUB-7 external worker invocation; PUB-8 events are fan-out; PUB-9
  deterministic `ContentPackage`; PUB-11 ambiguous-upload rule).
- `EXECUTION_RUNTIME_CONTRACT.md`, `RESOLVER_RUNTIME_CONTRACT.md`,
  `PROVIDER_RUNTIME_DATA_MODEL.md`, `CINEMATIC_STORYBOARD_CONTRACT.md`,
  `AI_RUNTIME_PLANES.md` — the frozen runtime planes this slice must not touch.
- `NEXT_VERTICAL_SLICES_DISCOVERY.md` — the read-only discovery report that already analysed
  *Scheduling*, *Publish Notifications*, *Creator dashboard*, and *Media Library* as
  candidates (reference only; all facts above are re-verified from source).

**Fact:** notifications/dashboard/scheduling have **no** dedicated contract; the closest is
`PUBLISHING_RUNTIME_CONTRACT.md` (for the publish-side scheduling/notification hooks) and the
event-projection pattern documented in ADR-0041 + `PLATFORM_STATUS.md`.

---

## 12. Current ADRs

`docs/decisions/` ADR-0030 … ADR-0047. Directly relevant to this slice:

- **ADR-0034** — authenticated-endpoint pattern (any new read/create endpoint follows it).
- **ADR-0041** — provider runtime contract **and** the *Event projection pattern* (the
  documented precedent for adding independent downstream projections: read-only, own
  idempotency, attach behind `PublisherPort`, never chain projections).
- **ADR-0042** — orchestration platform freeze (Gate 1): new capabilities are downstream/
  additive on stable seams; producers/relay untouched.
- **ADR-0043** — render composition boundary (Gate 2) — untouched by this slice.
- **ADR-0046** — execution-runtime boundaries; **ADR-0047** — publishing credential ownership
  (C8 credential-blindness; events carry no secrets).

**Fact:** a publish-notification projection and a schedule-at-create wiring are *instances of
already-decided patterns* (ADR-0041 projections; ADR-0034 endpoints; existing `scheduled_at`/
`publish_at` model), not new architectural decisions.

---

## 13. SYSTEM_MAP & PLATFORM_STATUS

- `docs/engineering/SYSTEM_MAP.md` — current component map (reflects `v0.4.39-phase3-alpha8.8`;
  Asset Promotion Bridge + CI Stage 15 recorded).
- `docs/architecture/PLATFORM_STATUS.md`:
  - Version/tag baseline `0.4.39-phase3-alpha8.8`.
  - *Completed capability lifecycles* records: *Notifications (in-app projection)* (α8.5b.3)
    and *Notification read API* (α8.5b.3r) as ✅.
  - *Event projection pattern (established α8.5b.3)* — the platform's first general projection
    and precedent for the rest (`PLATFORM_STATUS.md:286-296`).
  - *Remaining roadmap* (`PLATFORM_STATUS.md:333-337`) explicitly lists the two capabilities
    this slice touches:
    - **α8.5b.4** — Notification channels: email (`INotifier` + provider/templates/retries),
      later push/websocket. *(Not built.)*
    - **α8.6d** — Publishing: publish notifications (deferred DQ7) — a fan-out projection
      consuming `PublishJobSucceeded`/`PublishJobFailed` into a creator-facing notification;
      and/or a second destination. *(Not built.)*
  - *Deferred architecture guards* table (`:314-317`) does not contain a guard that this slice
    would trigger.

**Fact:** the roadmap already anticipates the *Notifications* portion of this slice as α8.6d
(publish notifications) and α8.5b.4 (email channel). *Dashboard* and *creator-set scheduling*
are **not** yet named roadmap slices.

---

## 14. Assessment — what exists / reuse / minimal additions / migration / ADR

> Factual assessment only. Concrete scope, decomposition, naming, and rulings are for the
> pre-flight.

### What already exists (reusable as-is)
- **Notifications end-to-end for in-app**: table, entity, repository (write + read),
  `CreateNotification`, four read endpoints, keyset feed, unread count, free-form `kind`.
- **Publish terminal events** carrying `requested_by_user_id` + post identity, fan-out ready.
- **Fan-out seam**: `InProcessPublisher([...])` handler list + `RelayService`.
- **Publish-job persistence** with `scheduled_at`, `attempt`/`max_attempts`, due-claim scan,
  retry reschedule, owner-list index; and `ContentPackage.publish_at` + YouTube `publishAt`
  mapping.
- **Owner-scoped read primitives** for publish-jobs, render-jobs, workflow-runs, media,
  social-accounts, plus `unread-count` as an aggregate precedent.
- **Established patterns**: ADR-0034 endpoints, ADR-0041 projections, α5a keyset pagination.

### Minimal additions implied (facts, not design)
- **Publish notifications**: a new projection handler (mirror of `NotificationProjection`)
  for `PublishJobSucceeded`/`PublishJobFailed`, reusing `CreateNotification` with new
  `publish.*` `kind`s; register it in the `InProcessPublisher([...])` list. Purely additive.
- **Scheduling (creator-set)**: extend `PublishJobCreateRequest` + `CreatePublishJob` to accept
  an optional schedule and thread it into `repo.add(scheduled_at=)` and/or
  `build_content_package(publish_at=)`. No new column; no runtime-execution change (the worker
  already defers due jobs).
- **Dashboard**: a new **read-only** aggregation/surface composed over existing owner-scoped
  reads; would additionally require a new owner-scoped **export-jobs list** (currently absent)
  if exports are to be listed. No new write path.

### Is a migration necessary?
- **On the evidence, no schema migration is required** for publish notifications (reuse
  `notifications`, free-form `kind`) or for creator-set scheduling (reuse `publish_jobs.
  scheduled_at` and `content_package.publish_at`). A dashboard read surface implies **no**
  migration. (The only scenario that would require DDL — activating `analytics_events`, adding
  new indexes for a heavy aggregate query, or adding notification `archive`/`email` columns —
  is not entailed by the selected scope and is a pre-flight decision.)

### Is an ADR genuinely required?
- **On the evidence, no new ADR is required.** Publish notifications are an instance of the
  ADR-0041 event-projection pattern; scheduling-at-create reuses the existing `scheduled_at`/
  `publish_at` model and ADR-0034 endpoint pattern; a dashboard is an additive read model.
  Per the workflow, if the pre-flight surfaces a genuine architectural decision (e.g.
  introducing an in-process scheduler/cron, a new bounded context, activating analytics as a
  projection target, or a notification-delivery `INotifier` port), it must **stop and propose
  an ADR** at that point rather than proceeding.

### Frozen boundaries this slice must not cross (restated from source)
- Orchestration/generation/render/export/publishing **runtime execution models**, the
  Asset Promotion Bridge, AI providers, Planner, verification pipeline — untouched (ADR-0042
  Gate 1, ADR-0043 Gate 2, ADR-0045/0046).
- Credential-blindness (ADR-0047 C8): notifications/dashboard/scheduling must consume only the
  secret-free event payloads and owned read models — never credentials.
- Producers/relay must not learn about new consumers (fan-out only; never chain projections).

---

## 15. Open questions for the pre-flight (not decisions)

1. **Slice decomposition** — is this one slice or a sequence (e.g. publish-notifications first,
   then creator-set scheduling, then dashboard), and how does it map to the roadmap labels
   α8.6d / α8.5b.4 / a new α8.9?
2. **Scheduling semantics** — creator-set `scheduled_at` (worker-deferred pickup) vs
   `publish_at` (platform-side visibility) vs both; and whether honouring `scheduled_at`
   implies any assumption about the external `PublishWorker` cadence (no in-process scheduler
   exists today).
3. **Dashboard shape** — a single aggregate endpoint vs. reusing/adding per-resource lists;
   whether an owner-scoped **export-jobs list** is added; pagination strategy; whether any
   aggregate counts are needed beyond the existing `unread-count` precedent.
4. **Notification breadth** — publish events only, or also render/workflow terminal events;
   and the `kind` taxonomy.
5. **Delivery channels** — whether email/push (α8.5b.4, `INotifier`) is in scope (it is
   currently absent and would be a larger, port-introducing addition) or explicitly deferred.

---

**End of grounding. Awaiting review before producing the pre-flight.**
