# CHANGELOG

> Keep-a-Changelog style. Each completed phase gets one entry. Pre-release work tracked under **[Unreleased]**.

---

## [Unreleased]

### α9.5 — Notification Delivery: Email (`0.4.48-phase3-alpha9.5`, 2026-07-29)

**Deliver in-app notifications over email — the platform's first outbound external-communication
channel that is not a publish destination.** A dedicated poll worker drains undelivered notifications
and sends each via an application-owned notifier; delivery is best-effort and never blocks the in-app
projection. Governed by
[`ADR-0051`](docs/decisions/ADR-0051-notification-delivery-email-idempotency-and-boundary.md) and
[`PHASE3_ALPHA9_5_PREFLIGHT.md`](docs/engineering/PHASE3_ALPHA9_5_PREFLIGHT.md).
**Strictly additive: no migration (delivery state reuses the existing `delivered_email_at` column +
the reserved `payload["_email"]` namespace), no relay change, no frozen-runtime change.** Correctness
never depends on provider-side deduplication (ADR-0051 Appendix A) — the lease + send-then-stamp model
gives at-least-once delivery with a bounded, explicitly-accepted rare-duplicate window.

#### Added
- **Port** — application-owned `INotifier` + neutral DTOs (`application/interfaces/notifier.py`):
  `EmailMessage` (recipient already resolved by the caller) + `NotifierDeliveryError(permanent, code)`.
  One-way dependency (ADR-0051 D5): the application owns the abstraction; infrastructure supplies the
  adapter.
- **Adapters** (`infrastructure/notifications/`) — `LoggingNotifier` (mock-first default + fail-soft
  fallback; deterministic, no I/O, emits only masked telemetry) and `SmtpNotifier` (config-gated
  `aiosmtplib`; its one policy job is failure classification — permanent vs. transient). Recipient-blind
  leaves (import-linter contract): no persistence / use-case / domain imports; `aiosmtplib` is confined
  to this package.
- **Worker** — `NotificationEmailWorker` (dedicated poll ingress, off the relay fan-out) +
  `ProcessNotificationEmail` (per-notification lease → resolve recipient owner-scoped → send **outside**
  any transaction → **send-then-stamp** `delivered_email_at`). Transient failure → capped exponential
  backoff retry; permanent failure / attempt-ceiling → terminal. One bad row never aborts the batch.
- **Persistence (no migration)** — additive `INotificationRepository` methods
  (`list_email_deliverable` / `mark_email_delivered` / `record_email_delivery_failure`); retry/terminal
  bookkeeping lives in the reserved `payload["_email"]` JSONB namespace.
- **Read-model sanitisation** — reserved `_`-prefixed payload keys (the `_email` bookkeeping) are
  stripped **centrally** at the single repository row→entity boundary, so no endpoint can ever expose
  internal delivery state. It is an implementation detail, never part of the public notification contract.
- **Config** — additive fail-soft `email_*` settings (SMTP host/port/credentials/from, batch size,
  timeout, max attempts, backoff, lease). When SMTP is unconfigured the composition root wires the
  `LoggingNotifier`; email is never a boot gate.
- **Tests** — unit (delivery send-then-stamp, backed-off retry, permanent/ceiling terminal, unresolved
  recipient, lease skip; worker drain/batch/error-isolation; SMTP failure classification; payload
  sanitisation) and CI **Stage 23** (`notification email delivery integration`: worker delivers +
  stamps + never re-scans; transient/permanent bookkeeping in the reserved namespace; the `_email`
  namespace is present in the stored row but stripped from the read API).

### α9.4 — Multi-Destination Publishing (`0.4.47-phase3-alpha9.4`, 2026-07-28)

**Publish one finished export to many connected accounts in a single action.** A creator fans out
a publish to N of their own connected destination accounts at once — the natural capstone of the
publishing workflow (captions α9.1 + thumbnails α9.3 + scheduling α8.9b all compose once and apply
to every channel). Governed by
[`PHASE3_ALPHA9_4_PREFLIGHT.md`](docs/engineering/PHASE3_ALPHA9_4_PREFLIGHT.md).
**Strictly additive orchestration: no migration, no ADR, no new port, no frozen-runtime change; the
single-create `POST /publish-jobs` endpoint is unchanged.** The batch endpoint composes the existing
`CreatePublishJob` once per account — it duplicates no validation, idempotency, or persistence logic.

#### Added
- **API** — new additive `POST /api/v1/publish-jobs/batch` (`api/v1/schemas/publish_jobs.py`,
  `routers/publish_jobs.py`): body `{ export_job_id, social_account_ids[1..20], <shared metadata
  overrides>, publish_at?, thumbnail_media_asset_id? }`; the list is bounded + duplicate-free (`422`).
- **`CreatePublishJobs`** (`application/use_cases/publishing/create_publish_jobs.py`) — orchestration
  fan-out over `CreatePublishJob`. A **shared** prerequisite failure (the export, or the optional
  thumbnail) aborts the whole request (fail-fast `404`/`422`); a **per-account** failure (account not
  owned / not connected / unsupported platform) is recorded as that item's outcome so one bad account
  never blocks the rest. Failures are classified from the existing error `details` keys — the single
  validation source is untouched.
- **Response** — `data` is an ordered per-account outcome array (`PublishJobBatchItemPublic`): each
  item is `created` (freshly queued), an idempotent replay (`created=false` + `publish_job`), or a
  neutral per-account `error{code,message}` — so callers never infer outcomes. Overall `201`.
- **Automatic downstream reuse** — every created job is an ordinary `PublishJob`, so scheduling,
  captions, thumbnails, notifications (α8.9a), and analytics (α9.0) all apply with **no**
  batch-specific logic. Idempotency (PUB-7) is unchanged — each account replays its own existing job.
- **Tests** — unit (fan-out orchestration: all-created, mixed replay, isolated per-account errors,
  shared fail-fast; batch schema cardinality/dedupe) and CI **Stage 22** (`multi-destination
  publishing integration`: two accounts → two jobs both drain; best-effort isolation; unknown-export
  fail-fast; HTTP auth + shape validators).

### α9.3 — Publish Thumbnail Support (`0.4.46-phase3-alpha9.3`, 2026-07-28)

**Optional, best-effort custom thumbnail for a publish.** A creator may nominate one of their own
`image` media assets as the destination video's thumbnail. Governed by
[`ADR-0050`](docs/decisions/ADR-0050-publish-thumbnail-source-and-delivery-boundary.md) (Option A —
creator-supplied) and [`PHASE3_ALPHA9_3_PREFLIGHT.md`](docs/engineering/PHASE3_ALPHA9_3_PREFLIGHT.md).
**Strictly additive: no migration (`content_package.thumbnail_media_asset_id` already existed), no AI,
no thumbnail generation, no lineage resolution, no new providers; the frozen `IDestinationPublisher.publish`
signature is unchanged — only the `UploadMedia` DTO gains an optional `thumbnail` handle.**

#### Added
- **API** — optional `thumbnail_media_asset_id` on `POST /api/v1/publish-jobs`
  (`api/v1/schemas/publish_jobs.py`, `routers/publish_jobs.py`).
- **`CreatePublishJob`** — owner-scoped validation (a non-owned id → `404`; a non-image → `422`),
  captured immutably into the existing `ContentPackage` before the job is queued.
- **Boundary** — `UploadThumbnail` DTO + optional `UploadMedia.thumbnail`
  (`application/interfaces/destination_publisher.py`); additive and backward-compatible.
- **`ProcessPublishJob`** — resolves + materialises the owned image **in the worker** (owner-scoped),
  attaches it to `UploadMedia`; a missing/soft-deleted image or a materialisation failure is
  best-effort (the video still publishes with no thumbnail).
- **Adapters** — `YouTubeDestination` performs `thumbnails.set` **after** a durable `videos.insert`
  (any failure is swallowed + logged, never retried — preserves PUB-11); `MockDestination`
  deterministically acknowledges a present thumbnail. Adapters never resolve or generate thumbnails.
- **Tests** — unit (schema, create validation, worker materialisation + best-effort, YouTube
  `thumbnails.set`, mock) and CI **Stage 21** (`publish thumbnail integration`).

#### Invariants (ADR-0050)
- Thumbnail is advisory and optional; it never blocks or retries the primary publish.
- Ownership + `kind='image'` verified once at create; the reference is immutable thereafter.
- Deterministic, unchanged publish behaviour when no thumbnail is supplied.

### α9.2 — Media Library Foundation (`0.4.45-phase3-alpha9.2`, 2026-07-28)

**Deterministic, owner-scoped Media Library over registered `media_assets`.** Realises the Asset
Library reserved by [`ADR-0037`](docs/decisions/ADR-0037-media-generation-outputs.md) **CR-8**:
folders + curated library entries (name / description / tags / reuse counters) built as a *sibling*
over the Media aggregate — the library never mutates `media_assets`. Governed by
[`PHASE3_ALPHA9_2_GROUNDING.md`](docs/engineering/PHASE3_ALPHA9_2_GROUNDING.md) and
[`PHASE3_ALPHA9_2_PREFLIGHT.md`](docs/engineering/PHASE3_ALPHA9_2_PREFLIGHT.md). **Strictly additive:
no migration (schema pre-built in `0001_baseline`), no AI, no embeddings, no vector/semantic search,
no frozen-boundary change.**

#### Added
- **Domain** (`domain/library/`) — frozen `LibraryFolder` and `LibraryAsset` value entities
  (`LibraryAsset` carries the `VersionMixin` OCC handle; the dormant `embedding` column is
  deliberately unmodelled — vector search is a later increment + ADR).
- **`ILibraryRepository`** (`application/interfaces/repositories.py`) + `LibraryRepository`
  (`infrastructure/repositories/library_repository.py`) — owner-scoped, live-only persistence:
  folder CRUD, asset CRUD, keyset browse, tag ANY-of via the existing GIN index (`tags && :tags`),
  version-fenced asset CAS, folder-delete asset detachment, and idempotent reuse recording
  (`library_asset_projects` upsert). Wired into the Unit of Work (`library`).
- **11 use cases** (`application/use_cases/library/`) — create/list/get/update/delete folders;
  add/list/get/update/delete assets; record reuse. 404-before-412 OCC on asset update (mirrors
  `UpdateProject`); folder-move cycle guard (`422`); non-empty-parent delete refusal (`409`);
  reads hide entries whose underlying media is soft-deleted.
- **`/api/v1/library/*`** (`api/v1/routers/library.py`, `schemas/library.py`) — folder + asset
  endpoints with `extra="forbid"` DTOs, tri-state PATCH, keyset pagination, three-state folder
  filters, comma-separated tag filter, and version-fenced updates. Wired via container factories +
  `deps.py` aliases + `main.py` include.
- **App-level folder-name uniqueness pre-check** (`folder_name_conflicts`) — closes the root-folder
  (`parent_folder_id IS NULL`) gap left by the frozen partial-unique index's default NULL-distinct
  semantics, so duplicate names are a uniform `409` at every level; the DB index remains the
  race-safe backstop for the non-null-parent case.

#### Known limitation (accepted)
- **Root-folder name uniqueness is application-enforced, not database-enforced.** The frozen
  `0001_baseline` index `uq_library_folders_parent_folder_id_name` is keyed on `(parent_folder_id,
  name)` and, by PostgreSQL's default NULL-distinct semantics, does **not** constrain root folders
  (`parent_folder_id IS NULL`); it also omits `owner_user_id`, so a `NULLS NOT DISTINCT` variant on
  the existing columns would incorrectly make root names globally unique across tenants. Under
  α9.2's **no-migration** constraint the app-level pre-check is the only viable enforcement, which
  leaves a **narrow TOCTOU race for root folders only** (READ COMMITTED): two concurrent creates of
  the same root name for one owner may both succeed. This is **non-corrupting** — assets reference
  folders by id, and no FK/OCC/referential invariant is affected — and **child-folder uniqueness
  remains database-enforced**. The permanent fix is a **future migration** adding a per-owner
  partial unique index (`(owner_user_id, name) WHERE parent_folder_id IS NULL AND deleted_at IS
  NULL`) with matching ORM metadata; no ADR is required.

#### Enforcement
- **Tests** — 38 use-case + DTO units (folder/asset CRUD, cycle guard, OCC, tag normalisation,
  soft-deleted-media hiding, reuse idempotency, DTO validation) and a DB-backed integration suite
  (**Stage 20**): uniqueness + owner isolation, move-cycle rejection, folder-delete detachment,
  media-asset uniqueness, OCC update/stale-version, tag GIN browse, hidden soft-deleted media,
  keyset pagination, idempotent reuse, and the real `/api/v1/library/*` endpoints end-to-end.

**No migration, no ADR, no AI/embeddings/vector search; additive over the pre-built `0001_baseline`
library schema (ADR-0037 CR-8).**

### α9.1 — AI Caption & Hashtag Generation (`0.4.44-phase3-alpha9.1`, 2026-07-28)

**Opt-in, advisory AI suggestions for publish metadata (title / description / hashtags).** The first
real consumer of the `Capability.LLM` seam: a creator may *suggest* metadata for a finished, owned
export through one authenticated `POST /api/v1/publish-metadata/suggestions`, then accept, edit, or
discard it before creating a publish job through the **existing** overrides. Governed by
[`PHASE3_ALPHA9_1_PREFLIGHT.md`](docs/engineering/PHASE3_ALPHA9_1_PREFLIGHT.md) and
[`ADR-0049`](docs/decisions/ADR-0049-ai-publish-metadata-boundary.md) (the publishing→AI boundary +
determinism invariants). **Strictly additive: no migration, no frozen-runtime change; the AI
capability is advisory only and always degrades to the existing deterministic template (PUB-9).**

#### Added
- **`IPublishMetadataGenerator`** (`application/interfaces/publish_metadata_generator.py`) — a
  **Publishing-owned** port with **neutral, `ContentPackage`-free** DTOs (`PublishMetadataRequest`,
  `GeneratedPublishMetadata`, ephemeral `MetadataProvenance`, `PublishMetadataGenerationError`). The
  AI subsystem supplies the adapter; the dependency is strictly one-way (ADR-0049).
- **`LlmPublishMetadataGenerator`** (`infrastructure/ai/metadata/`) — the sole publishing→AI bridge:
  resolves `Capability.LLM` from the registry (deterministic `MockLLMProvider` by default), isolates
  all prompt text (`prompt_template_version = "cap-hashtag/v1"`), deterministically parses a
  completion into title/description/tags **within the strictest destination caps** (title ≤100, tags
  ≤500 total chars), and maps **every** provider error / timeout / non-terminal-or-empty / empty-title
  to `PublishMetadataGenerationError`. Cancellation-safe (`asyncio.wait_for`; `CancelledError`
  propagates, no leaked task); logs a coarse `reason` only (never prompts/text/tokens).
- **`GeneratePublishMetadata`** (`application/use_cases/publishing/`) — owner-scoped, advisory use
  case: cheap validation first (ownership `404` → export readiness `422`, the same gate as
  `CreatePublishJob`), then the AI call **after** the read UoW closes, with a **mandatory
  deterministic template fallback** (`build_content_package`, `provenance.is_fallback=True`) on any
  AI failure. Reads only — persists nothing.
- **`POST /api/v1/publish-metadata/suggestions`** (`api/v1/routers/publish_metadata.py`,
  `schemas/publish_metadata.py`) — thin authenticated endpoint returning
  `PublishMetadataSuggestionPublic` (`extra="forbid"` request; explicit `provenance` contract). Wired
  through `get_generate_publish_metadata_use_case` + `GeneratePublishMetadataDep`.
- **`llm_metadata_timeout_seconds`** (config only, default 15.0) — per-suggestion timeout.

#### Enforcement
- **Import-linter contract** "AI plane never imports the Publishing bounded context (ADR-0049)" —
  mechanically pins the one-way boundary (`app.infrastructure.ai` ✗→ publishing domain/use-cases/
  infrastructure).
- **Tests** — use-case units (LLM happy path within caps, project-description context, `404`/`422`
  raised **before** any AI work, deterministic-template fallback, default title); adapter units
  (determinism over the mock, cap enforcement, every failure mode → domain error, timeout +
  cancellation safety); DTO validation; and a DB-backed integration suite (**Stage 19**):
  deterministic suggestion over live PostgreSQL, owner isolation (`404`), not-ready export (`422`),
  generator-failure fallback, and the real endpoint end-to-end (`200` / `401` / `404` / `422`).

**No migration, no frozen-runtime change; one new port + one import-linter contract; ADR-0049.**

**Activates the dormant, partitioned `analytics_events` table as a downstream outbox consumer.** The
foundation for creator analytics: publish/export lifecycle events are projected into owner-scoped
analytics rows, exposed through one authenticated read-only `GET /api/v1/analytics/summary`. Governed
by [`PHASE3_ALPHA9_0_PREFLIGHT.md`](docs/engineering/PHASE3_ALPHA9_0_PREFLIGHT.md) (AN0–AN14) and
[`ADR-0048`](docs/decisions/ADR-0048-analytics-consumer-idempotency.md) (idempotency boundary).
**Strictly additive: no producer change, no frozen-runtime change; the single architectural decision
(DB-enforced exactly-once) is governed by ADR-0048.**

#### Added
- **`analytics_events.source_event_id uuid NULL`** + two indexes (migration `0015`): the partial-unique
  **`uq_analytics_events_source_event_id`** over `(source_event_id, occurred_at) WHERE source_event_id
  IS NOT NULL` (DB-enforced exactly-once — ADR-0048; includes the partition key so it is valid on and
  auto-propagates across the partitioned table, empirically verified against PostgreSQL 17.10) and the
  owner-read **`ix_analytics_events_user_id_occurred_at`** over `(user_id, occurred_at) WHERE user_id IS
  NOT NULL`.
- **`IAnalyticsRepository`** (`add` + `summary_for_owner`) on the UoW, with a raw-SQL
  `AnalyticsRepository` — the write maps the unique-index violation to `ConflictError`; the read is an
  owner-scoped half-open-window `COUNT(*) … GROUP BY event_name`.
- **`AnalyticsProjection`** — the fourth independent in-process outbox consumer, mapping the publish +
  export lifecycle events to a stable `event_name` vocabulary + neutral property subset
  (`event_schema.py`), with `source_event_id = event.id` and **`occurred_at = event.occurred_at`
  (deterministic, never `now()`)**. Idempotent on `event.id` (relay redelivery → no-op).
- **`RecordAnalyticsEvent`** — the idempotent write use case (resolves the owner's `tenant_id` in the
  same UoW; `ConflictError` → already-recorded no-op; vanished user → clean skip).
- **`GetCreatorAnalytics`** + **`GET /api/v1/analytics/summary?since=&until=`** — a read-only
  owner-scoped summary (per-`event_name` counts + total, zero-filled over the full vocabulary; window
  defaults to a trailing 30 days; naive/inverted windows → `422`). Wired through
  `get_creator_analytics_use_case` + `CreatorAnalyticsDep`.

#### Enforcement
- **Tests** — unit tests of the projection (event→name/properties mapping, owner targeting,
  deterministic `occurred_at`, non-applicable/malformed/unknown-user no-ops), `RecordAnalyticsEvent`
  (recorded vs duplicate vs skipped), `GetCreatorAnalytics` (zero-fill/total), and window validation;
  a DB-backed integration suite (**Stage 18**): success/failure rows written with correct
  `event_name`/`user_id`/`tenant_id`, **exactly-once** under redelivery, owner isolation, and API
  visibility through `GET /analytics/summary`.

**Migration `0015` (additive), one new port, ADR-0048.** Full ephemeral-DB gate (stages 0–18) **PASS**.

### α8.9c — Creator Dashboard (`0.4.42-phase3-alpha8.9c`, 2026-07-27)

**A read-only owner-scoped creator dashboard.** Final increment of the **α8.9 Creator Experience**
milestone. Adds one authenticated `GET /api/v1/dashboard/summary` that surfaces the caller's product
state as scalar counts, composed entirely from **existing** owner-scoped repository reads. Governed by
[`PHASE3_ALPHA8_9c_PREFLIGHT.md`](docs/engineering/PHASE3_ALPHA8_9c_PREFLIGHT.md) (CD1–CD6).
**Strictly additive: no analytics subsystem, no charts/reporting, no new repository method, no new
SQL, no migration, no new port, no ADR, no runtime change.**

#### Added
- **`GetCreatorDashboard`** (`application/use_cases/dashboard/`) — a read-only use case that, inside a
  single `IUnitOfWork`, reuses `publish_jobs.list_for_owner` (grouped by `PublishStatus`),
  `social_accounts.list_for_owner` (connected + total), `notifications.count_unread`, and
  `media.list_owned` (total), returning a `CreatorDashboardSummary` (CD3). Every publish status is
  always present (`0` when absent — a stable shape).
- **`GET /api/v1/dashboard/summary`** (`api/v1/routers/dashboard.py`, `schemas/dashboard.py`) — thin
  router projecting `DashboardSummaryPublic` via `envelope`; all scope from `CurrentUserDep` (CD4), so
  a fresh caller sees all-zero. Wired through `get_creator_dashboard_use_case` + `CreatorDashboardDep`.

#### Explicitly out of scope (CD6)
Analytics subsystem / `analytics_events` (dormant table untouched), charts, reporting, email, push,
scheduler, AI, Planner, generation/render/export/publish runtime changes, second destination,
per-kind media breakdown, and pagination.

#### Enforcement
- **Tests** — unit test of the aggregation (mixed statuses grouped exactly; absent statuses → `0`;
  connected vs. total; unread; media total) + a DB-backed API integration test (**Stage 17**): seed
  committed owner-scoped rows across publish jobs / social accounts / notifications / media, assert the
  summary; a fresh user sees all-zero (owner isolation); `401` unauthenticated.

**No migration, no new port, no ADR, no analytics.** Full ephemeral-DB gate (stages 0–17) **PASS**.

### α8.9b — Creator Scheduling (`0.4.41-phase3-alpha8.9b`, 2026-07-27)

**Lets a creator schedule a YouTube go-live using the existing pipeline.** Second increment of the
**α8.9 Creator Experience** milestone. Adds the one missing creator-facing ingress — an optional,
validated `publish_at` on the publish-create request — and threads it into the already-wired
`ContentPackage.publish_at` → YouTube `status.publishAt` path. The job still uploads immediately; the
destination keeps the video private and flips it live at `publish_at` (platform-native scheduling).
Governed by [`PHASE3_ALPHA8_9b_PREFLIGHT.md`](docs/engineering/PHASE3_ALPHA8_9b_PREFLIGHT.md) (SC1–SC7).
**Strictly additive: no scheduler/cron/timer/worker-loop, no migration, no new port, no ADR, no
runtime change.**

#### Added
- **`PublishJobCreateRequest.publish_at`** (`api/v1/schemas/publish_jobs.py`) — optional
  `datetime | None`, validated by a `field_validator`: **timezone-aware only** and **strictly
  future**, **normalised to UTC** (SC3). A bad value is a 422 at the ingress. `extra="forbid"` was
  **not** introduced (would be a behaviour change) — the addition is purely additive.
- **`CreatePublishJob.execute(..., publish_at=…)`** threads the schedule into the existing
  `build_content_package(publish_at=…)`; the router forwards `body.publish_at`. `publish_jobs.scheduled_at`
  (worker-side deferral) is **left `None`** — the runtime is unchanged (SC1).

#### Reused unchanged
- `ContentPackage.publish_at` + its JSONB (de)serialisation; the **YouTube adapter** mapping
  (`publish_at` ⇒ `privacyStatus=private` + `status.publishAt`, already shipped in α8.6c); the publish
  runtime, `(source_media_asset, social_account)` idempotency (a replay does **not** reschedule — SC5),
  ownership, retry model, and the `/api/v1/publish-jobs` API.

#### Enforcement
- **Tests** — DB-free schema-validator unit tests (future tz-aware accepted + normalised to UTC;
  naive → 422; past/now → 422; `None` passes) + use-case unit tests (publish_at threaded into the
  persisted `ContentPackage`; `scheduled_at` stays `None`; idempotent replay ignores a new
  `publish_at`); **Stage 14** integration extended: API create validation (naive/past → 422; future
  parses → account gate) and a DB round-trip where a scheduled publish persists `publish_at` and still
  runs to `succeeded`.

**No migration, no new port, no ADR, no scheduler.** Full ephemeral-DB gate (stages 0–16) **PASS**.

### α8.9a — Publish Notifications (`0.4.40-phase3-alpha8.9a`, 2026-07-27)

**Exposes the publishing pipeline's outcome to the creator.** First increment of the **α8.9 Creator
Experience** milestone: the deferred **DQ7** fan-out. A new event projection turns the publish runtime's
terminal outbox events into in-app notifications, so a creator sees when their video was published or
failed — reusing the notification subsystem (write projection α8.5b.3 + read API α8.5b.3r) **exactly**.
Governed by `docs/engineering/PHASE3_CREATOR_EXPERIENCE_PREFLIGHT.md` (§3). **Strictly additive: no
migration, no new port, no ADR; only a new downstream consumer on the existing fan-out seam.**

#### Added
- **`PublishNotificationProjection`** (`application/use_cases/notifications/publish_notification_projection.py`)
  — a faithful twin of the export `NotificationProjection`. Consumes only `PublishJobSucceeded` /
  `PublishJobFailed`; produces only the `publish.succeeded` / `publish.failed` notification kinds
  (free-form `kind`, so no schema change); addresses the recipient by the event's `requested_by_user_id`;
  copies only the already-neutral event fields (no credential/bearer/URL/bytes — PUB-8 / ADR-0047 C8).
  Reuses `CreateNotification` (built fresh per event via the injected factory → own UoW per event) and the
  DB-owned `(user_id, source_event_id)` partial-unique index for exactly-once under relay redelivery.
- **Composition** — registered as the **third** independent consumer on the existing
  `InProcessPublisher([...])` fan-out alongside the generated-media subscriber and the export notification
  projection; producers and the relay are untouched (ADR-0042 fan-out property).

#### Enforcement
- **Tests** — DB-free use-case unit tests (both events, owner-targeting, `PublishJobCreated`/other-type
  ignore, malformed/invalid-recipient no-op, redelivery-duplicate swallow, DB-error propagation) + a live-DB
  **Stage 16** integration test proving success + failure notifications, exactly-once under redelivery, and
  visibility through the real `/api/v1/notifications` read API (commits + cleans up on teardown).

**No migration, no port changes, no new ADR — additive fan-out consumer only.** Full ephemeral-DB gate
(stages 0–16) **PASS** on a throwaway `pgvector/pgvector:pg16`.

### α8.8 — Asset Promotion Bridge (`0.4.39-phase3-alpha8.8`, 2026-07-27)

**Connects the two parallel pipelines.** Bridges the Stage-13 AI generation runtime output
(execution-owned `generation_assets`, Path B) into the platform media library
(`media_assets(source='generated')`, Path A) that already feeds Render → Export → Publish. Implements the
**ADR-0046 X8** (`PublishGenerationAssets`) seam as an explicit, user-initiated use case —
`PromoteGenerationAssets` — **library-only** (it ends at `media_assets`; Render/Export/Publish/Timeline/
orchestration are untouched). Governed by `docs/engineering/PHASE3_ASSET_PROMOTION_BRIDGE_PREFLIGHT.md`
(AP1–AP9). **Strictly additive: no schema migration, no changes to any existing port.**

#### Added
- **`IGenerationReader` port + `PromotableGenerationVideo` DTO** (`application/interfaces/generation_reader.py`)
  — the single new, **read-only** seam. `IExecutionRuntimeStore` is write-only and the UoW carries no
  generation repository, so promotion reads the generation head + its final video artefact through this
  port rather than widening any frozen write seam (AP4).
- **`GenerationReader`** (`infrastructure/generation/generation_reader.py`) — raw-SQL, ORM-less
  (`generations LEFT JOIN generation_assets`), one short read session; never writes, emits events, or
  advances the state machine (W8.6.7).
- **`PromoteGenerationAssets`** use case (`application/use_cases/media/`) — authorizes a required, owner-scoped
  `project_id` (request-time ownership, mirroring `IngestGeneratedMedia` — AP2), **copies** the finished
  bytes into the active media store under a deterministic key (copy, never a shared reference — AP5),
  recomputes the checksum, and registers an owned `media_assets(source='generated')` row with generation
  provenance in `source_metadata`. Re-promotion collides on the existing `media_assets` storage-coordinate
  uniqueness → idempotent **`noop`** (AP3, no new constraint/migration).
- **API** — `POST /api/v1/media/promotions` (`{generation_id, project_id}`) → **201** on first promotion,
  **200** on idempotent replay; **404** unknown generation, **422** foreign project / no promotable final
  video. Composition-root factory + dependency wired.

#### Enforcement
- **Import-linter** — new contract *"Execution Runtime never writes the media library (ADR-0046 X8)"*: the
  execution plane (its use cases + infrastructure, incl. the new reader) may not directly import the media
  bounded context, so the only path from a generation to `media_assets` is the promotion bridge.
- **Tests** — DB-free use-case unit tests (promote / idempotent noop / 404 / 422 / foreign-project) + an
  AST-based X8 isolation unit test; a live-DB **Stage 15** integration test proves promote + idempotent
  replay end-to-end against real PostgreSQL (commits + cleans up on teardown).

**No migration, no port changes, library-only (ends at `media_assets`).** Full ephemeral-DB gate
(stages 0–15) **PASS** on a throwaway `pgvector/pgvector:pg16`: static (mypy + 11 import-linter contracts)
green, new unit tests green, migration up→down→up roundtrip clean (no new migration), and the new Stage 15
asset-promotion-bridge integration green.

### α8.6c — Destination Adapters: YouTube (`0.4.38-phase3-alpha8.6c`, 2026-07-27)

**Third slice of the α8.6 Publishing / Creator Workflow bounded context — adapter-only, no migration.**
Replaces the Mock destination with the **first production-quality destination (YouTube)** while leaving
the runtime, ports, domain, and schema exactly as α8.6b left them. Two new credential-blind infrastructure
leaves behind unchanged ports — `YouTubeOAuthClient` (`ISocialOAuthClient`: connect / exchange / refresh /
revoke) and `YouTubeDestination` (`IDestinationPublisher`: Data API v3 `videos.insert` resumable upload) —
plus a thin injected `httpx` transport, configuration-blind `Settings`, and composition-root wiring.
Governed by `docs/engineering/PUBLISHING_RUNTIME_CONTRACT.md` (PUB-1…PUB-11) and
`docs/engineering/PHASE3_ALPHA8_6c_PREFLIGHT.md` (EQ1–EQ5).

#### Added
- **`YouTubeOAuthClient`** (`infrastructure/publishing/oauth/`) — Google OAuth 2.0 mechanics against
  injected endpoints: consent-URL construction (`access_type=offline`, `prompt=consent`), code exchange
  (+ `channels?mine=true` identity lookup so a `SocialAccount` is keyed by the real channel), refresh, and
  best-effort revoke. Configuration-blind (client id/secret/scopes/endpoints injected).
- **`YouTubeDestination`** (`infrastructure/publishing/destinations/`) — resumable `videos.insert`
  (initiate session → stream bytes → parse video id), deterministic `ContentPackage → snippet/status`
  mapping with adapter-side limit validation, and Google-error → `DestinationError(retryable)`
  classification. Credential-blind leaf: consumes only the `AuthorizedContext` bearer.
- **`PUB-11`** (new invariant) — ambiguous post-transmission upload outcomes are **permanent
  manual-review** failures, never retried (no double-post); only pre-upload / unambiguously-transient
  failures are retryable. Recorded in `PUBLISHING_RUNTIME_CONTRACT.md` §11 (EQ3).
- **Config** — configuration-blind YouTube `Settings` (client id/secret, scopes, authorize/token/revoke/
  API base URLs, timeout); **fail-soft**: if the client id/secret are unset, YouTube is simply not
  registered (parallels the α8.6a master-key fail-soft).
- **Composition wiring** — `_get_oauth_clients()` + `_get_destination_registry()` register `"youtube"`
  when configured, sharing one memoised `httpx.AsyncClient` (closed on shutdown). `supported_platforms()`
  then admits YouTube at create-time with no create-path change.

#### Enforcement
- **Import-linter** — no new contract; the existing *destination adapters are credential-blind leaves* +
  *encryption primitives confined* contracts already box in the new `destinations`/`oauth` files. A unit
  test asserts `YouTubeDestination.publish` consumes only the `AuthorizedContext`.
- **Tests (network-free, Stage 4 unit)** — request/response mapping, error-classification (incl. PUB-11
  ambiguous-outcome), and resumable-protocol tests for `YouTubeDestination`; consent-URL / exchange /
  refresh / revoke tests for `YouTubeOAuthClient` — all via an injected `httpx.MockTransport`.
- **Opt-in live smoke** — an env-gated (`YOUTUBE_LIVE_SMOKE=1`) real-upload test, **excluded from CI**
  (Stage 14 stays deterministic + offline; the Mock destination remains the CI default).

**No migration, no port change, no runtime/domain/schema expansion.** Full ephemeral-DB gate
(stages 0–14) **PASS** on a throwaway `pgvector/pgvector:pg16`: static (mypy + 10 import-linter
contracts) green, 33 new network-free YouTube unit tests green, migration up→down→up roundtrip clean
(no new migration), Stage 14 publishing integration green (38 tests) with the runtime still
credential-blind and the Mock destination still the CI default.

### α8.6b — Publish Runtime (`0.4.37-phase3-alpha8.6b`, 2026-07-27)

**Second slice of the α8.6 Publishing / Creator Workflow bounded context — implementation, additive
migration.** Adds *publish execution*: a user queues a `PublishJob` to upload a finished
export-delivery `MediaAsset` (PUB-1) to one connected `social_accounts` destination (PUB-2). A faithful
adaptation of the proven `ExportJob` execution model (DQ8) — poll-ingress worker, lease-guarded claim,
version-fenced CAS, transactional outbox — with a second serialisation lock and bounded retries. **No
notification projection** (deferred, DQ7) and **no real destination adapter yet** (YouTube is α8.6c;
this slice ships the Mock destination). Governed by `docs/engineering/PUBLISHING_RUNTIME_CONTRACT.md`
(PUB-1…PUB-10) and `docs/engineering/PHASE3_ALPHA8_6b_PREFLIGHT.md` (SIGNED OFF, DQ1–DQ8).

#### Added
- **Publishing domain** — `PublishStatus` (`queued|running|succeeded|failed|canceled`), the `PublishJob`
  aggregate (+ `PublishSource`, `PublishJobClaim`), and a deterministic, platform-agnostic
  `ContentPackage` (default visibility **private**) with a pure builder (PUB-9).
- **Ports** — `IPublishJobRepository` (source resolution, owner-scoped CRUD, claim scan, version-fenced
  CAS transitions) on the UoW; `IDestinationPublisher` + `IDestinationRegistry` (credential-blind upload
  contract, consuming only the α8.6a `AuthorizedContext`).
- **Infrastructure** — `PublishJobRepository` (raw CAS/OCC, `resolve_source` join, idempotency-conflict
  mapping), `MockDestination` + `DestinationRegistry`, ORM model + UoW/conftest wiring.
- **Use cases** — `CreatePublishJob` (ownership + readiness + idempotency), `ProcessPublishJob`
  (dual-lock claim → authorize → materialize → upload → settle, with capped-exponential-backoff retries,
  DQ5/DQ6), `PublishWorker` (poll ingress), `GetPublishJob`, `ListPublishJobs`, and PascalCase outbox
  emitters (`PublishJobCreated`/`PublishJobSucceeded`/`PublishJobFailed`, DQ4).
- **API** — top-level `POST/GET /api/v1/publish-jobs` (`201` create / `200` idempotent replay), DTOs,
  deps, container factories, and a `publish_batch_size` config field.
- **Migration `0014_publish_jobs`** (additive) — `publish_status` enum + `publish_jobs` (direct
  ownership, explicit `project_id` (DQ1), `scheduled_at` scheduling, `attempt`/`max_attempts`,
  `content_package` JSONB, neutral `error` JSONB), the `(source_media_asset_id, social_account_id)`
  partial-unique idempotency index over active/fulfilled rows (DQ2), claim/owner/account indexes, and
  the `touch_updated_at` + guarded `bump_version` triggers (OCC mirrors `export_jobs`, DQ8).

#### Enforcement
- **Import-linter** — new contract *destination adapters are credential-blind leaves* (10 contracts
  total, all kept): destination adapters cannot import the credential store, repositories, UoW, use
  cases, or the generation/workflow domains.
- **CI Stage 14** — extended with the α8.6b publishing integration suites: `PublishJob` repository,
  the publish runtime end-to-end (create → worker → succeeded/failed with real `distributed_locks` +
  outbox chain, credential-blind), and the `/publish-jobs` router.
- **Enum guard** — `EXPECTED_ENUM_COUNT` 27 → 28 for `publish_status` (created by 0014).
- **ERD** — new *Cluster 14 — Publishing / Publish Runtime* (`publish_jobs`) + cross-cluster FK rows.

Full ephemeral-DB gate (stages 0–14) **PASS** on a throwaway `pgvector/pgvector:pg16`: migration 0014
upgrade→downgrade→upgrade roundtrip clean, schema validator + ERD green, 38 publishing integration
tests green. The runtime remains credential-blind (consumes only `AuthorizedContext`); the slice is
strictly additive within the Publishing bounded context.

### α8.6a — Publishing account connections (`0.4.36-phase3-alpha8.6a`, 2026-07-26)

> **Release ordering.** α8.6 (Publishing) was **intentionally completed after the α8.7 baseline**
> (`v0.4.35-phase3-alpha8.7`, Planner V2): the roadmap deferred Publishing until *after* the AI/AR
> runtime, so this release's numeric prefix moves forward (`0.4.36`) while the roadmap-milestone
> suffix steps back to `alpha8.6a`. Publishing is its own bounded context, **not** an α8.7 increment.

**First slice of the α8.6 Publishing / Creator Workflow bounded context — implementation, additive
migration.** Establishes *credential and connection ownership* only: how a user connects
an external destination account (OAuth) and how we hold that account's tokens safely. **No `PublishJob`,
no upload execution, no scheduling, no publishing worker** — those are α8.6b/α8.6c. Governed by
`docs/engineering/PUBLISHING_RUNTIME_CONTRACT.md` (PUB-1…PUB-10, APPROVED),
`docs/decisions/ADR-0047-publishing-credential-ownership.md` (C1–C8 / R1–R4), and
`docs/engineering/PHASE3_ALPHA8_6a_PREFLIGHT.md` (SIGNED OFF, OQ1–OQ5).

#### Added
- **Publishing domain** (`app/domain/publishing/`) — `SocialAccount` aggregate + `AccountStatus`
  (`connected|expired|revoked`). A distinct bounded context; login identity (`oauth_identities`) is
  **not** reused (grounding conclusion).
- **Ports** — `ISocialCredentialStore` (`store`/`authorize`/`revoke`, returns an immutable
  `AuthorizedContext`, never raw stored tokens), `ISocialOAuthClient` (+ Mock this slice; real YouTube
  is α8.6c — OQ1), `IOAuthStateSigner` (signed, stateless CSRF state — OQ3), `IMasterKeyProvider`
  (env-injected now, Cloud KMS is a future swap behind the same seam — OQ5), and
  `ISocialAccountRepository` on the UoW.
- **Envelope encryption** (`app/infrastructure/publishing/credentials/`) — AES-256-GCM per-record DEK
  wrapped by the master key (`cryptography`, now a direct dependency). The database stores **only**
  ciphertext + nonce + wrapped DEK + `key_version` — never a plaintext or usable OAuth token
  (ADR-0047 C1/C2). `key_version` gives a clean rotation path.
- **Credential service, mock OAuth client, JWT state signer, `SocialAccount` repository** + container
  wiring, config fields (`publishing_credential_master_key`, `publishing_credential_key_version`,
  `publishing_oauth_redirect_base_url`, `publishing_oauth_state_ttl_seconds`), and **fail-closed**
  startup: production refuses to boot without a master key; dev/tests use an explicitly-injected
  deterministic key only.
- **Use cases** — `StartSocialConnection`, `CompleteSocialConnection`, `RevokeSocialAccount`,
  `ListSocialAccounts`; **API** — `POST /api/v1/social-accounts/connect`, `GET …/callback`,
  `GET …`, `POST …/{id}/revoke` (owner-scoped, anti-enumeration).
- **Migration `0013_social_accounts`** (additive) — `social_accounts` (profile; `platform` free-text
  per OQ2, `status` = new `social_account_status` enum) + `social_credentials` (1:1 encrypted secret).
  Multiple accounts per `(user, platform)` (R4). ORM-backed.

#### Enforcement
- **Import-linter** — two new contracts: encryption primitives confined to the publishing credential
  adapter, and the publishing domain kept an isolated bounded context (9 contracts total, all kept).
- **CI Stage 14 — publishing integration verification** — `SocialAccount` repository, credential
  service (no-plaintext-token proof), and `/social-accounts` router, run against a DB at head. Kept out
  of Stage 12 (scope freeze) and Stage 13 (generation slice) — publishing is its own context
  (`CI_QUALITY_GATE.md` §2.10; contract §13).
- **Enum guard** — `EXPECTED_ENUM_COUNT` 26 → 27 for `social_account_status` (created by 0013).
- **ERD** — new *Cluster 13 — Publishing / Creator Workflow* + cross-cluster FK rows.

#### Fixed (CI tooling)
- **`scripts/_load_env.py`** — environment variables now take precedence over `.env.validation`
  (`setdefault`, standard 12-factor semantics). This aligns the schema validator (stage 8) and every
  other `_load_env` consumer with the ci_gate's documented precedence
  (`--ephemeral-db` > `VALIDATION_DATABASE_URL` > `DATABASE_URL`/`.env.validation`), so
  `--ephemeral-db` correctly retargets *all* live-DB stages at the isolated container instead of
  silently validating the shared database.

Full ephemeral-DB gate (stages 0–14) **PASS** on a throwaway `pgvector/pgvector:pg16`: migration 0013
upgrade→downgrade→upgrade roundtrip clean, schema validator + ERD green, 20 publishing integration
tests green.

### Design lock + tooling — α8.5d Capability-registry seed pre-flight & manifest enrichment (2026-07-25)

**Governance + design-time tooling only — no runtime change, no migration, no version bump yet** (the
runtime seed + additive migration land with the `0.4.35-phase3-alpha8.5d-dev` bump once implemented).
Signs off the α8.5d pre-flight (`docs/engineering/PHASE3_ALPHA8_5d_PREFLIGHT.md`) and — since `main`
is held unpushed — **amends the α8.5c manifests/schema/validator in place** (cleaner than a follow-up
governance commit) so the catalogue is rich enough for the α8.5e resolver and the MRC planner without
future schema churn.

#### Added (design-time, all additive to α8.5c)
- **Capability dependencies** (`capabilities.yaml`) — a `dependencies` block (`requires`/`optional`
  *capabilities*, distinct from param-level requires/optional; requires-graph validated acyclic) so
  the planner can build capability graphs (e.g. `video_generation` requires `image_generation`).
- **Feature matrix** (adapter, `providers.yaml`) — controlled `features` vocabulary
  (`txt2img/img2img/negative_prompt/seed_control/reference_image/consistent_character/lora/
  inpainting/outpainting/motion_control/face_reference/depth_control/pose_control`) so provider
  capabilities aren't fragmented into dozens of tiny capabilities.
- **Resource estimation** (adapter `runtime.estimated`) — `cold_start_seconds`/`warm_start_seconds`/
  `image_seconds`/`video_seconds`/`audio_seconds`/`peak_ram_gb`/`peak_vram_gb`/`disk_gb` for
  scheduling, local execution, batching, progress estimates, device selection.
- **Output characteristics** (adapter `outputs`) — concrete formats per io type (validated against a
  vocabulary and against the capability's declared outputs) so downstream planning needs no
  adapter-specific logic.
- **Runtime requirements** (adapter `runtime.execution` + `runtime.hardware`) + **cost hints** (adapter
  `cost`, estimation-only — never a billing source) + **device profiles**
  (`backend/providers/devices.yaml`, optional manifest) — the execution/hardware/mode metadata folded
  into the provider/runtime schema per ADR-0044 X-G.
- **Local providers** — Ollama (`ollama.text`) and ComfyUI (`comfyui.flux_schnell`) seeded as ordinary
  free/local adapters (`execution.local=true`), proving local models are just providers.
- **Validator rules** — capability-dependency integrity + cycle check, feature vocabulary/applicability,
  output-format vocabulary + capability-output subset check, resource-estimation sanity, free-provider
  cost sanity, device-profile uniqueness; the offline validator now also loads the optional
  `devices.yaml`. Unit tests grew to 66 (all green); full offline gate (stages 0–4) PASS.

### Tooling / CI — α8.5c Capability Catalogue & Provider Registry (design-time spec, 2026-07-25)

**Tooling + design-time spec only — no application or runtime change, no version bump, no new
migration.** Lays the *capability-first* ground truth for the future AI orchestrator: a curated,
human-reviewable YAML spec of the capability→provider graph plus an offline CI validator. The
**runtime never reads the YAML** (W8.5c.2) — the database stays the runtime source of truth; a later
slice (α8.5d) seeds the DB from this validated spec (`YAML → validator → seeder → DB → runtime`).

Structured as **three focused manifests** (post-ship design review, R2): `capabilities.yaml`
(vocabulary), `providers.yaml` (providers/adapters/families), `routing.yaml` (policy).

#### Added
- **Capability catalogue** (`backend/providers/capabilities.yaml`) — 27 fine-grained capabilities,
  each grouped under a coarse `kind` (`llm|image|video|voice`) that mirrors `plugin_kind_enum` / the
  code `Capability` enum, and **enriched** with typed `inputs`/`outputs`
  (`text|image|video|audio|subtitle|embedding`) + `requires`/`optional` request params — the data a
  future planner needs to compose capability graphs and a UI needs to auto-generate request forms.
  `publishing` is deliberately excluded (separate bounded context, α8.6).
- **AI providers** (`backend/providers/providers.yaml`) — a free-first seed set (Pollinations,
  Hugging Face, fal, Kokoro) modelled *capability → providers*. Selection is **score +
  routing-strategy** driven (`quality/cost/speed/reliability`, 0–100) — **no integer priority
  anywhere**. Richer free-tier model: `pricing` (`free|freemium|paid`) + `quota` (daily/monthly) +
  `authentication` + `requires_login`. Adapters are first-class (`pollinations.image`) and carry
  capability-specific `supports` constraints (`commercial/nsfw/watermark/max_duration_seconds/
  max_resolution/queue/async/polling/webhook`) the future resolver can match on. Static-only:
  operational state (health, latency, quota-remaining, success rate, 429 frequency) lives in the DB,
  never in Git (W8.5c.3). Families carry variants with acyclic inheritance.
- **Routing policy** (`backend/providers/routing.yaml`) — per-capability strategy overrides over a
  default; static routing only (dynamic health/latency scoring is explicitly deferred to the future
  resolver).
- **Pydantic v2 schema/loaders** (`backend/scripts/provider_manifest.py`) — strict (`extra="forbid"`,
  so a stray `priority:` fails loudly); lives under `scripts/` (not `app/`) so the runtime cannot
  import it.
- **Offline validator** (`backend/scripts/validate_providers.py`) — deterministic, **no network / no
  DB** (W8.5c.5); 14 rule families (uniqueness, capability-metadata integrity, catalogue integrity,
  unique provider+capability, adapter shape/interface, adapter-constraint applicability, fallback
  graph acyclicity, family inheritance acyclicity, pricing/quota sanity, routing enums,
  config-keys-are-names-not-values, anti-drift vs the code vocabulary, free-provider sanity). Writes
  `.validation/provider_validation_report.json`; fails closed.
- **CI gate Stage 0.** A fast, no-DB pre-flight that runs before every other stage (so a manifest
  regression fails cheaply before the DB round-trip). Numbered `0` on purpose — the 1–10 map stays
  stable so the restoration guard's "stage 6 = downgrade" / live-DB range 5–9 keying is untouched.
  Skips cleanly (exit 0) when the manifest is absent; fails closed on an incomplete (partial) trio.

#### Verified
- New offline stage green; the three committed manifests validate clean (27 capabilities, 4
  providers). 43 unit tests (red + green fixture per validator rule, plus schema strictness) pass.
  Fast gate (Stage 0–4) + freeze guard green. No migration, no runtime version bump.

#### Docs
- New pre-flight `docs/engineering/PHASE3_ALPHA8_5c_PREFLIGHT.md` (SIGNED OFF); ADR-0041 addendum
  (capability-first registry direction); `CI_QUALITY_GATE.md` + `docs/CI_QUALITY_GATE.md` Stage 0.

### Tooling / CI — validation-DB isolation & self-healing restoration guard (2026-07-24)

**Infrastructure/tooling only — no application or runtime change, no version bump, no new
migration.** Hardens `backend/scripts/ci_gate.py` so the destructive migration stages can never
leave a *persistent* validation database at `base`. This was prompted by a live incident during the
α8.5b.3r release: a transient DNS failure struck between stage 6 (`alembic downgrade base`) and the
stage-7 re-upgrade against the shared Supabase validation DB, momentarily emptying it until an
upgrade was retried by hand.

#### Added
- **DB isolation for the live stages (5–9).** New precedence: `--ephemeral-db` /
  `CI_GATE_EPHEMERAL_DB=1` (runner starts a throwaway `pgvector/pgvector:pg16` container on a random
  loopback port and always tears it down in a `finally`, even on Ctrl-C) → `VALIDATION_DATABASE_URL`
  (a dedicated validation DB; the primary `DATABASE_URL` is left untouched) → `DATABASE_URL` /
  `backend/.env.validation` (legacy path, now with a printed warning when a destructive stage runs
  against the primary DB).
- **Self-healing restoration guard.** Whenever a downgrade may have run against a persistent DB, the
  runner performs a bounded-retry `alembic upgrade head` (8 attempts, 6 s backoff — tolerant of
  transient connection failures) on **every** exit path, then verifies `alembic current == heads`.
  The gate refuses to print `PASSED` until the DB is confirmed at head
  (`[ OK ] DB restored & verified at head: <rev>` vs `[FAIL] DB NOT restored to head …`) — it never
  claims a success it cannot verify.

#### Verified
- Stages 1–4 green (ruff / black / mypy+import-linter / pytest). The self-healing guard was
  exercised against the live DB and, during a naturally-occurring transient failure, correctly
  restored and re-verified the DB to head after a mid-run stage failure; a clean pass reported
  `DB restored & verified at head: 0009_notifications_source_event_id`. The `--ephemeral-db` path
  fails closed with a clear message and leaves no container behind when no Docker daemon is
  reachable.

#### Docs
- `CI_QUALITY_GATE.md` + `docs/CI_QUALITY_GATE.md` — new "Validation-DB isolation & restoration
  guard" section (§2.6) and local-run examples.

### Phase 3 Slice α8.5b.3r — Notification Read API — read & manage the projection (2026-07-24)

The **read/query completion of the notifications bounded context** — the follow-up deferred from
α8.5b.3. α8.5b.3 established `Immutable Event → Exactly-once Projection → Notification row`; this
slice adds the owner-facing surface on top of that stable foundation, and **nothing more**: list
(keyset-paginated), unread badge count, mark-one-read, mark-all-read. It is a pure **read-model**
slice — query-only + metadata-only mutations — so it touches **no** frozen orchestration surface
(**ADR-0042** Gate 1 PASS) and does not change how notifications are *projected* or *created*
(projection contract intact, Gate 2 PASS). Every repository method is additive; the write path
(`add`) and `NotificationProjection` are byte-for-byte unchanged. **Zero migration** — the feed
index, the unread partial index, and the `read_at` / `archived` columns all pre-date this slice.
Freeze guard green, **zero override markers**. Runtime capability → version bump to
`0.4.34-phase3-alpha8.5b3r`. **Archive / delete / search / filters / folders / labels / pinning /
snooze stay out** (this is a read-model completion, not an inbox product); **email deferred to
α8.5b.4**.

#### Added
- **`GET /api/v1/notifications` (Fork A/B — list, keyset-paginated).** The caller's notifications,
  newest first (`created_at DESC, id DESC`), owner-scoped by `user_id`. **Reuses the existing
  `app/application/pagination.py` keyset primitive verbatim** (the α5a `GET /projects` shape):
  `?limit=` (1..100, default 20) + opaque `?cursor=` → `meta.next_cursor`; a malformed cursor is a
  clean 422. No offset, no notification-specific cursor format.
- **`GET /api/v1/notifications/unread-count` → `{ count }`.** The badge count — matches the
  `ix_notifications_user_id_unread` partial-index predicate (`read_at IS NULL AND archived = false`),
  an index-only scan.
- **`POST /api/v1/notifications/{id}/read` (Fork C — action verb).** Marks one notification read;
  idempotent (a repeat is a 200 no-op); a missing / foreign id is a uniform 404 (anti-enumeration).
  Matches the established `POST …/render-jobs/{id}/cancel` convention rather than `PATCH`.
- **`POST /api/v1/notifications/read-all` → `{ updated }`.** Bulk mark-read of the caller's unread
  set; returns the affected count; a second call returns `0` (idempotent).
- **`INotificationRepository` read methods (Fork D — pure additive).** `list_for_user` (keyset,
  owner-scoped, archived excluded), `count_unread`, `mark_read` (owner-scoped CAS on
  `read_at IS NULL`; already-read → unchanged row so mark-read is idempotent; foreign → `None`),
  `mark_all_read` (bulk CAS, returns count). All write **only** `read_at` — never identity /
  `source_event_id` / delivery provenance.
- **Read use cases** — `ListNotifications`, `CountUnreadNotifications`, `MarkNotificationRead`
  (404 on missing/foreign), `MarkAllNotificationsRead`; wired via 4 container factories + `deps.py`
  aliases; router registered in the composition root.
- **`NotificationPublic` DTO** (id, kind, title, body, payload, source_event_id, read_at,
  created_at, updated_at) + `{ count }` / `{ updated }` response models. `archived` /
  `delivered_email_at` intentionally kept off the wire (archive deferred; email is α8.5b.4).
- **Domain `Notification` entity extended** with `read_at` + `archived` — now legitimate domain
  state, not repository-only implementation details (implementation-note ruling).
- **Tests** — read use-case unit suite (keyset ordering + `next_cursor`, owner scoping, bad cursor
  → 422, count scoping, mark-read idempotency + 404, mark-all count + idempotency, order-stability
  under read-state); `NotificationRepository` read integration (N5–N8: keyset + owner isolation,
  unread count scoping, scoped/idempotent mark-read with metadata-only guarantee, mark-all count);
  `/api/v1/notifications` API integration (auth 401s, empty list + envelope, unread-count 0,
  read-all 0, mark-read 404/422, bad cursor 422). Seeded-data feed behaviour is proven by the
  repository + unit suites (layered proof, the α8.5b.3 cadence).

#### Index (Fork E — existing index; no migration)
- **Composite keyset index `(user_id, created_at, id)` intentionally deferred pending observed
  production need.** The existing `ix_notifications_user_id_created_at` supports the dominant feed
  access pattern and the `id` tie-break resolves equal-timestamp rows; a composite index is an
  optimization, not a capability. Consistent with the platform cadence: prove correctness first,
  optimize when justified — not speculative indexing.

#### Invariants
- **W8.5b.8 (new) — Notification queries never expose notifications belonging to another
  principal.** Every read/read-state method is scoped by `user_id` at the repository layer; a
  foreign / missing id is indistinguishable (uniform 404). The read-side ownership invariant.
- **W8.5b.9 (new) — Read-state mutations modify only notification metadata and never alter
  projection identity, source-event linkage, or delivery provenance.** `mark_read` / `mark_all_read`
  write only `read_at`; they never touch `id`, `user_id`, `kind`, `title`, `body`, `payload`,
  `source_event_id`, or `delivered_in_app_at`. Mirrors W8.5b.6/7 — the read side cannot re-project
  or re-key.
- **W8.5b.10 (new) — Notification ordering is observational only; read-state mutations must not
  affect feed ordering.** The feed is ordered purely by `(created_at, id)`, independent of
  `read_at`; marking one read, or marking all read, never moves a notification or reshuffles the
  feed.

#### Unchanged (freeze holds)
- **Read-model + additive only** — no frozen contract modified: `NotificationProjection`,
  `CreateNotification`, `INotificationRepository.add`, the outbox, the relay, the export event
  emitters, and the notification write path are byte-for-byte unchanged (ADR-0042 Gate 1;
  projection contract intact, Gate 2). Nothing near composition/render (ADR-0043). **Zero
  migration** — `read_at` / `archived` / both feed indexes pre-date this slice. Freeze guard green,
  **zero override markers**. **Deferred:** archive/delete + inbox features → out of scope;
  email/push/websocket → α8.5b.4+; publishing → α8.6.

### Phase 3 Slice α8.5b.3 — Notifications — project export terminal events into in-app notifications (2026-07-24)

The **third and final distribution-stage slice** — it closes the loop opened by α8.5a/α8.5b.1–2:
the export artifact now *exists* (α8.5a), is *obtainable* (α8.5b.1), from *any backend* (α8.5b.2),
and — with this slice — the requester is *told* when it's ready or failed. The entire runtime
addition is **one downstream projection**: a new `EventHandler` on the existing in-process
`PublisherPort` reacts to `ExportJobSucceeded` / `ExportJobFailed` and writes exactly one in-app
`Notification` per recipient. This is the platform's **fan-out seam** in action — a second, fully
independent consumer attaches to the same event stream the runner emits, and the runner knows
nothing about it (kin to α8.4a ingestion). **Exactly-once = at-least-once relay + a DB uniqueness
invariant**: the relay may redeliver; the database refuses the second write; the projection treats
the refusal as an already-notified no-op. Additive **consumer**, so entirely outside the
**ADR-0042** frozen surface (Gate 1) and far below the **ADR-0043** render boundary (Gate 2).
**One small additive migration** (a nullable column + a partial unique index that *encodes* the
exactly-once invariant, not a new capability). Freeze guard green, **zero override markers**.
Runtime capability → version bump to `0.4.33-phase3-alpha8.5b3`. **Read side (list / unread /
mark-read / archive) is deferred to α8.5b.3r**; **email deferred to α8.5b.4**.

#### Added
- **`NotificationProjection` (Ruling C — relay subscriber; named a *projection*, not a service).**
  A new `EventHandler` registered on the existing `InProcessPublisher`, reacting to
  `ExportJobSucceeded` + `ExportJobFailed` **only** (Ruling B); every other event is a clean no-op.
  It maps the event → notification content and delegates the write to a fresh `CreateNotification`
  (own UoW per event). The name reflects the architectural intent: it *derives read state from
  immutable events* and never orchestrates. Recipient is read straight from the event's
  `requested_by_user_id` (no ownership lookup). Error posture mirrors ingestion: malformed payload
  → log + clean return (never parks the relay); genuine DB failure → raise (relay retries).
- **`CreateNotification` use case (idempotent write half).** Persists one `notifications` row in
  its own UoW, stamping `delivered_in_app_at = now()` (in-app "delivery" = the committed row);
  catches the repository's `ConflictError` and returns a `duplicate` no-op. Exactly-once is owned
  by the database, never an application-level pre-check.
- **`INotificationRepository` port + `NotificationRepository` adapter.** `add(...)` inserts and
  maps the partial-unique `uq_notifications_user_id_source_event_id` violation → `ConflictError`
  (same shape as `MediaRepository.add`). Wired onto the UoW alongside the other repositories.
  Write path only — the read/query surface is deferred to α8.5b.3r.
- **`Notification` domain entity.** A slim frozen projection of the `notifications` row (the repo
  return type), keeping the ORM out of the application/domain layers.
- **Migration `0009` (Ruling D — DB-enforced idempotency).** `ADD COLUMN notifications.source_event_id
  uuid NULL` (the outbox `event.id`; **nullable**, **no FK** to `event_outbox` — transport state vs
  product state) + partial `CREATE UNIQUE INDEX … (user_id, source_event_id) WHERE source_event_id
  IS NOT NULL`. `(user_id, source_event_id)` (not `source_event_id` alone) future-proofs fan-out
  while keeping today's single-recipient semantics unchanged. Additive + safe (empty table);
  `downgrade` drops both.
- **Tests** — `NotificationProjection` (success/failure mapping incl. `source_event_id = event.id`,
  neutral failure body, ignore-other-types, malformed-payload no-op, redelivery `duplicate`
  swallowed, genuine-DB-error propagation); `CreateNotification` (create + commit, duplicate
  no-op with a single persisted row, same-event/different-recipient both persist, null
  `source_event_id` un-deduped); `NotificationRepository` integration (insert + `delivered_in_app_at`,
  duplicate → `ConflictError`, per-recipient uniqueness, multiple NULL-source rows coexist).

#### Invariants
- **W8.5b.6 (new) — Notification creation is a pure projection of immutable events.** The
  projection + use case only **read** a terminal, already-committed event and **write** notification
  state. They never mutate export/render/orchestration state, never re-drive the export, never
  dispatch provider/render work, never call back into the frozen pipeline (kin to W8.4.2 / W8.5b.1).
- **W8.5b.7 (new) — A notification is projected exactly once per recipient per source event. This
  invariant is enforced by the persistence layer, not by subscriber control flow.** The relay may
  deliver more than once; the projection may execute more than once; the database guarantees the
  projection exists **at most once** (partial `UNIQUE (user_id, source_event_id)`), and the use
  case treats the refused duplicate as a successful no-op. Correctness never depends on
  application-level control flow — resilient even if the projection implementation changes later.

#### Unchanged (freeze holds)
- **Additive consumer only** — no frozen contract modified: `PublisherPort`, `InProcessPublisher`,
  the relay, the export event emitters, and the outbox schema/semantics are untouched (ADR-0042
  Gate 1; registering a second handler in the composition root is a growth-surface change). Nothing
  near composition/render (ADR-0043 Gate 2). `DownloadExport`, the download endpoint, and all
  export/render/orchestration contracts are byte-for-byte unchanged. Freeze guard green, **zero
  override markers**. **Deferred:** read/query API → α8.5b.3r; email/push/websocket → α8.5b.4+;
  publishing → α8.6.

### Phase 3 Slice α8.5b.2 — Storage Backends & Signed-URL Delivery — where artifacts live and how they're delivered (2026-07-24)

The **second distribution-stage slice** — it completes the α8.5b.1 delivery seam by making
storage **multi-backend**. α8.5b.1 shipped local streaming behind `IDownloadDelivery`; α8.5b.2
adds **AWS S3 / Cloudflare R2** object storage plus **fixed-TTL presigned-URL redirect**
delivery, selected per artifact — with **no change** to `DownloadExport` or the
`GET …/exports/{id}/download` endpoint. Selection is centralised in two registries so no use
case is backend-aware. Write-side is **E2**: a single `storage_active_backend` config value
selects where *new* `MediaAsset`s are persisted; reads/deletes/deliveries **always** resolve by
the artifact's *persisted* backend — so a backend change is operational, never migratory
(W8.5b.5). Entirely outside the **ADR-0042** frozen surface (Gate 1) and below the **ADR-0043**
render boundary (Gate 2): storage is transport/persistence, never re-encoding (RC5 / W8.5.3).
The `media_assets.storage_backend` / `storage_bucket` / `storage_key` columns already existed
(ADR-0030), so **zero migration**. Freeze guard green, **zero override markers**. Runtime
capability → version bump to `0.4.32-phase3-alpha8.5b2`.

#### Added
- **`IStorageResolver` port + `StorageResolver` registry (Ruling A — centralised selection).**
  `active() → IObjectStorage` (the single configured write backend) and
  `resolve(backend) → IObjectStorage` (an existing artifact's persisted backend). No use case is
  backend-aware — they receive the resolver and call `active()` to write, `resolve(...)` to read.
  `StorageResolver.single()` mirrors the pre-α8.5b.2 single-instance behaviour.
- **`S3ObjectStorage` adapter (S3 + R2 — Ruling C).** One S3-compatible adapter serves both AWS
  S3 and Cloudflare R2 (R2 differs only by the injected endpoint + credentials); the persisted
  `backend` label (`s3`/`r2`) is injected. Synchronous boto3 calls run in a worker thread (same
  discipline as the local adapter); SDK/transport errors map to the neutral `ObjectStorageError`.
- **`S3RedirectDelivery` adapter (Ruling B — signing lives in delivery, not storage).** Returns a
  `RedirectDelivery` to a **fixed-TTL** presigned GET URL (Fork F: `download_signed_url_ttl_seconds`,
  default 900 s; **no** per-request TTL, **no** CDN/edge signing). The presign is **offline**
  (`botocore.generate_presigned_url` — no request-path network call); `expires_at` is populated.
- **`DeliveryResolver` registry (backend-dispatching `IDownloadDelivery` facade).** Dispatches
  `deliver()` on `request.storage_backend` (local → `LocalStreamDelivery`, s3/r2 →
  `S3RedirectDelivery`). Because it *is* an `IDownloadDelivery`, `DownloadExport` and the download
  endpoint are unchanged (Ruling A + E). Observable difference only: `200` stream (local) vs
  `302` redirect (cloud).
- **Config (E2, Ruling C, Fork F).** `storage_active_backend ∈ {local, s3, r2}` (default `local`);
  `s3_bucket` / `s3_region` / `s3_endpoint_url` / `s3_access_key_id` / `s3_secret_access_key`
  (injected, W8.1.1); `download_signed_url_ttl_seconds`. A cloud active backend with missing
  bucket/credentials is a hard fail-fast config error. **Exactly one** active write backend — no
  `preferred`/`fallback`/`mirror`/`replication` semantics.
- **Cloud-SDK isolation contract (Ruling D).** New import-linter `forbidden` contract keeps
  `boto3`/`botocore` out of `app.domain` / `app.application` / `app.api` / `app.core` (direct
  imports); the SDK is confined to `app.infrastructure.{storage,delivery}`. `boto3>=1.35.0` added
  as a runtime dependency with a mypy `ignore_missing_imports` override.
- **Tests** — `StorageResolver` (active/resolve/single, unknown-backend `ObjectStorageError`,
  active-must-be-registered); `S3ObjectStorage` (put/get/exists/delete against a stub client,
  404→`False`, transport error→`ObjectStorageError`); `DeliveryResolver` (dispatch-by-backend,
  unknown→`DownloadDeliveryError`); `S3RedirectDelivery` (presigned URL + `expires_at`, fixed TTL
  passed to signer, backend/bucket mismatch + signer error → `DownloadDeliveryError`); end-to-end
  `DownloadExport` parity through the real `DeliveryResolver` (local→stream 200, s3→redirect 302).

#### Threaded (write-active, read-by-persisted-backend)
- **`ProcessExportJob`, `ProcessRenderJob`, `EnrichGeneratedMedia`, `IngestGeneratedMedia`** now
  take an `IStorageResolver` instead of a single `IObjectStorage`: **writes** go to
  `active()` (new artifacts honour `storage_active_backend`); **byte reads** resolve by the
  source artifact's *persisted* backend (`resolve(asset.storage_backend)`), so existing artifacts
  stay readable wherever they live (W8.5b.5). No behavioural change when `storage_active_backend`
  is `local` (the default).

#### Invariants
- **W8.5b.4 (new) — Delivery selection is derived solely from the artifact's persisted storage
  backend.** The delivery mechanism is a pure function of `MediaAsset.storage_backend` via the
  resolver — never request headers, endpoint params, feature flags, client preference, or the
  active write backend. The same artifact always delivers the same way.
- **W8.5b.5 (new) — The active write backend affects only future writes.** Changing
  `storage_active_backend` changes where *new* `MediaAsset`s are persisted and **never** changes
  the location or interpretation of existing ones — each remains readable/deliverable from its
  own `(storage_backend, storage_bucket, storage_key)`. Backend changes are operational, not
  migratory.

#### Unchanged (freeze holds)
- **Zero migrations** (`storage_backend` / `storage_bucket` / `storage_key` already modelled,
  ADR-0030). No frozen orchestration path changed (ADR-0042 Gate 1). Storage/delivery are below
  the render boundary and uphold RC5/W8.5.3 (ADR-0043 Gate 2). `DownloadExport` + the download
  endpoint are byte-for-byte unchanged (Ruling E). Freeze guard green, **zero override markers**.
  No notifications / publishing / share links / CDN (deferred to α8.5b.3 / α8.6).

### Phase 3 Slice α8.5b.1 — Download Serving — deliver an export artifact to the user (2026-07-24)

The **first distribution-stage slice** — downstream of the α8.5a export engine. α8.5a made the
delivery artifact *exist*; α8.5b.1 makes it *obtainable*: an authenticated, owner-scoped
endpoint that streams a completed export's bytes. Grounding established that α8.5b is **four**
downstream capabilities with different risk profiles, so this slice ships only the smallest,
zero-migration, highest-value one — **download serving** — and explicitly defers cloud storage
backends (α8.5b.2), notifications (α8.5b.3), and publishing (α8.6). Entirely outside the
**ADR-0042** frozen surface (Gate 1) and *below* the **ADR-0043** render boundary (Gate 2):
download is a pure read + transfer that never re-encodes or mutates the artifact (RC5 / W8.5.3).
The `export_jobs.download_count` / `last_downloaded_at` columns already existed (ADR-0030), so
**zero migration**. Freeze guard green, **zero override markers**. Runtime capability → version
bump to `0.4.31-phase3-alpha8.5b1`.

#### Added
- **`IDownloadDelivery` port + `LocalStreamDelivery` adapter (Fork A — the seam, local only).**
  `deliver(DownloadRequest) → DeliveryDecision` where the decision is a `StreamDelivery` (bytes
  streamed through the API) or a `RedirectDelivery` (signed-URL redirect). α8.5b.1 implements
  **`LocalStreamDelivery` only** (reads the object from `IObjectStorage`, yields it in 64 KiB
  chunks; refuses non-local backends); **no** `signed_url()` / S3 / R2 / CDN code — those are
  α8.5b.2, and arrive with no endpoint change. Pure transfer (W8.5b.2): the bytes are never
  re-encoded, resized, or transformed.
- **`DownloadExport` use case + HTTP ingress.** `GET /projects/{id}/render-jobs/{id}/exports/{id}/download`
  resolves + authorizes (owner-only via the existing project → render-job gate; foreign/missing
  → `404`, anti-enumeration), requires the export `succeeded` with a live
  `output_media_asset_id` (`409` otherwise — Fork C), resolves the delivery `MediaAsset`, and
  renders the `DeliveryDecision` (`200` streamed attachment, or `302` redirect for the future
  cloud shape). Missing/foreign artifact or unavailable bytes → `404`.
- **Best-effort download accounting (Fork B / W8.5b.3).** New additive
  `IExportJobRepository.record_download` — `download_count += 1`, `last_downloaded_at = now()`
  guarded on `status='succeeded'`, **no `version` bump** (telemetry, not an OCC transition).
  Called in its own short transaction after delivery is prepared and **swallowed on failure**:
  a counter-store outage is telemetry loss, never a failed download; no retry.
- **Tests** — use case (stream + accounting; foreign-user / wrong-render-job / missing-export
  404s; not-succeeded / succeeded-without-artifact 409s; vanished artifact + unavailable bytes
  404 *not counted*; delivery-error → 404; **accounting failure does not fail the download**;
  redirect pass-through); `LocalStreamDelivery` (chunked byte-identical stream, foreign
  backend/bucket rejected, missing object → `ObjectStorageError`); router (streamed attachment
  headers, `302` redirect, `404`/`409` envelope mapping).

#### Invariants
- **W8.5b.1 (new) — Download serving is observational and read-only.** It reads a finished
  delivery `MediaAsset` and transfers its bytes; its only write is the `export_jobs` accounting
  fields. It never mutates the artifact, the master, orchestration/render/export lifecycle, or
  any upstream entity.
- **W8.5b.2 (new) — Delivery is a pure transfer.** No encoding, transcoding, re-composition,
  re-timing, or resize on the download path (reinforces RC5 + W8.5.3 — deliveries are
  replaceable byte artifacts of the canonical master).
- **W8.5b.3 (new) — Accounting never blocks or corrupts delivery.** Download-count updates are
  best-effort, non-transactional with the byte transfer, and non-retrying; a failure is
  telemetry loss, not a user-visible error.

#### Unchanged (freeze holds)
- **Zero migrations** (`download_count` / `last_downloaded_at` already modelled, ADR-0030). No
  frozen orchestration path changed (ADR-0042 Gate 1). Download is below the render boundary and
  upholds RC5/W8.5.3 (ADR-0043 Gate 2). Freeze guard green, **zero override markers**. No cloud
  storage adapters / signed URLs / CDN / notifications / publishing / share links (deferred to
  α8.5b.2 / α8.5b.3 / α8.6).

### Phase 3 Slice α8.5a — Export Engine — render output → delivery encoding (2026-07-24)

The **first delivery-stage slice** — downstream of render + enrichment. It adds an **export
engine** that transcodes a completed render's master `MediaAsset` into requested
`(format, quality)` **delivery encodings**, following the platform's established
claim → lease → transform → idempotent-settle → event worker model. Entirely outside the
**ADR-0042** frozen orchestration surface (Gate 1) and within **ADR-0043** — export is a
delivery transform operating on a *finished, immutable* render (RC5) and is deterministic
(RC6 / RP1–RP9). The `export_jobs` table + `export_*` enums were already modelled (ADR-0030),
so **zero migration**. Freeze guard green, **zero override markers**. Runtime capability →
version bump to `0.4.30-phase3-alpha8.5a`.

Export is **delivery-only and same-orientation** (Fork F, tightened at sign-off): it changes
container / codec / bitrate / **resolution** within the master's own orientation
(`horizontal→horizontal`, `vertical→vertical`, `square→square`), scaling with preserved
aspect and **no** pad / crop. Cross-orientation exports (which change *presentation*, not
delivery) and any letterbox / pillarbox / smart-reframe are **deferred** to a future policy
slice — a request whose orientation differs from the master's is a `422`. Publishing,
notifications, download-serving endpoints, storage-provider backends, and CDN are deferred to
**α8.5b** (Fork A).

#### Added
- **`IExporter` port + `FfmpegExporter` adapter (Fork C1 — a discrete domain, never the
  renderer)** — `ExportSpec` (`source_path`, `output_path`, `format`, `quality`,
  `orientation`) → `ExportResult` (stored-object facts). The adapter maps `quality` → a fixed
  resolution box, `orientation` → its box orientation, `format` → container/codec
  (`mp4`=h264/aac, `mov`=h264/aac, `webm`=vp9/opus, `gif`=palettegen/paletteuse, no audio);
  scaling preserves aspect (`force_original_aspect_ratio=decrease` + even rounding, no pad).
  Deterministic encode knobs (fixed CRF / GIF fps). Configuration-blind (W8.1.1 — reuses the
  render binary config). Shared `EXPORT_FORMAT_MIME` / `EXPORT_FORMAT_KIND` on the port.
- **`CreateExportJob` use case + HTTP ingress** — `POST /projects/{id}/render-jobs/{id}/exports`
  (201, or 200 idempotent replay) + `GET …/exports/{id}` (status). Gates project ownership
  (404), requires the render `succeeded` with a master output (422 otherwise), enforces the
  **same-orientation** guard against the master's dimensions (422), and is idempotent per
  `(render_job, format, quality, orientation)` (Fork E, backed by the partial-unique index).
- **`ProcessExportJob` + `ExportWorker` (Fork B1 — a poll worker, not the relay)** — claims a
  `queued` job under an `export_job:<id>` lease (`queued`→`running` CAS), resolves the master
  (the **only** legal source, Fork D), materializes it from `IObjectStorage`, transcodes via
  `IExporter`, stores under a **deterministic key**, registers a delivery `MediaAsset`
  (`source='generated'`, `source_metadata.origin='export'` + master lineage), and settles
  `succeeded` (with `output_media_asset_id` + `file_size_bytes`) or `failed` — emitting
  `ExportJobCreated` / `ExportJobSucceeded` / `ExportJobFailed` on the transactional outbox.
- **Additive persistence** — `IExportJobRepository` (+ SQLAlchemy adapter + in-memory fake) —
  `add` (partial-unique → `ConflictError`), `get_active`, render-derived `get_owned`,
  worker-facing `list_claimable` (FIFO, resolving each job's owning `project_id` via
  `render_jobs`), and self-versioned `mark_running` / `mark_succeeded` / `mark_failed` CAS.
  Domain `ExportJob` / `ExportStatus` / `ExportJobClaim`; `export_jobs` wired into the UoW.
- **Tests** — create (queue + event, idempotent replay, distinct-encoding, ownership 404s,
  master-not-ready 422, cross-orientation 422, same-orientation vertical, unknown-dims 422,
  invalid enums); process (master → delivery asset with export lineage, GIF → image kind,
  deterministic-key idempotency recovers the existing asset, failure path, missing master,
  non-queued no-op, lock skip); worker (FIFO drain, empty no-op); get (ownership 404s); plus
  `FfmpegExporter` unit validation + `_target_box` math and **opt-in** real-FFmpeg mp4
  (orientation-preserving) + gif roundtrips (skipped without the binary).

#### Invariants
- **W8.5.1 (new) — Export is downstream-only.** It never recomposes, mutates, or re-renders
  the master; it only reads a finished `MediaAsset` and produces a new delivery `MediaAsset`
  (upholds RC5).
- **W8.5.2 (new) — Export consumes only a `MediaAsset` + request params.** Never a Timeline,
  provider output/URL, checkpoint, request/job id, or webhook (mirror of W8.4b.2 / W8.4c.2).
- **W8.5.3 (new) — The rendered `MediaAsset` is the canonical master; exports are replaceable
  delivery artifacts.** Same master + same `(format, quality, orientation)` ⇒ a functionally
  equivalent delivery (RC6), regenerable at any time; deleting/regenerating a delivery never
  affects the master. One master → N replaceable encodings (MP4 / MOV / WEBM / GIF).

#### Unchanged (freeze holds)
- **Zero migrations** (`export_jobs` + `export_*` enums already modelled, ADR-0030). No frozen
  orchestration path changed (ADR-0042 Gate 1). Export satisfies ADR-0043 RC5/RC6 + RP1–RP9
  (Gate 2). Freeze guard green, **zero override markers**. Publishing / notifications /
  download service / storage-provider backends / CDN and cross-orientation reframe deferred.

### Phase 3 Slice α8.4e — Render Composition — audio mixing (2026-07-24)

The **first render-composition slice** and the first feature implemented entirely under
**ADR-0043** (render composition boundary, RC1–RC6) while staying completely outside the
**ADR-0042** frozen orchestration surface. It extends rendering from *video-only
composition* to **video + deterministic audio composition** using **already-authorable**
Timeline state — audio-kind tracks, `clip.volume`, `track.muted` — so **zero migration**.
The α8.4b renderer discarded all audio (`concat …:a=0`); α8.4e teaches the FFmpeg adapter
to mix: each video clip's own audio travels with its segment (at `clip.volume`, silenced
if its track is `muted`), and dedicated **audio-track** clips (music / voiceover) are
trimmed, gained, delayed to their `start_seconds` (`adelay`), and combined with the video
bed via a **pure** `amix` (`normalize=0` — no implicit gain staging). A timeline with no
authored audio renders a silent video, exactly as α8.4b (Fork F). Transitions / crossfades
/ color grading / effects / subtitle burn-in are deferred to **α8.4f** — they require the
α6.4 Timeline **authoring** write paths (`transition_in_id` / `transition_out_id` /
`effects` / subtitles), which remain intentionally deferred. Freeze guard green, **zero
override markers**. Runtime capability change → version bump to `0.4.29-phase3-alpha8.4e`.

#### Added
- **Neutral render-contract extension (Fork C1 — extend the contract, never reach back
  into the Timeline)** — `RenderInput` gains `volume` + `muted` (a video clip's own audio);
  new `AudioInput` DTO (`path`, `source_start_seconds`, `source_end_seconds`,
  `start_seconds`, `volume`) for dedicated audio-track clips; `RenderSpec` gains
  `audio_inputs: tuple[AudioInput, ...]`. The renderer receives everything it needs as
  immutable composition inputs (RC1/RC2/RC3).
- **`FfmpegRenderer` audio graph** — per-clip audio *bed* concatenated in video order
  (real audio where present at `volume`, silence-filled otherwise so the bed stays synced),
  audio-track overlays via `atrim`/`volume`/`adelay`, combined with
  `amix=…:duration=first:normalize=0`; audio-bearing streams normalized to a fixed format
  (stereo / 44.1 kHz / fltp) for deterministic mixing. `ffprobe`-based audio-stream
  detection decides whether a source contributes audio; **no** audio → `a=0` (α8.4b path).
  Configuration-blind (W8.1.1 — reuses the α8.4b binary config).
- **`ProcessRenderJob` composition resolve** — `_resolve_clips` → `_resolve_composition`,
  now reading `list_tracks` for each track's `kind` + `muted`: video clips carry
  `clip.volume` + owning-track `muted`; **audio-kind, non-muted** tracks contribute
  `AudioInput`s (ordered by `(start_seconds, media_asset_id)`). Shared `_materialize`
  helper fetches both from storage (W8.4b.2 — only `MediaAsset` coordinates).
- **Tests** — video-only timeline → no audio inputs (silent, Fork F); `clip.volume` +
  muted video track → `RenderInput.volume`/`muted`; audio-track clip → `AudioInput` at its
  offset (materialized from storage); muted audio track skipped; deterministic audio
  ordering; renderer audio-trim / negative-start validation (no binary); plus **opt-in**
  real-FFmpeg **audio-mix** roundtrip (output has an audio stream) and **silent-timeline**
  roundtrip (output has none), skipped without the binary.

#### Invariants
- **W8.4e.1 (new) — Audio composition is a pure function of Timeline audio state.** The
  rendered audio is determined solely by the Timeline's audio state (tracks, clips, `muted`
  flags, `volume` values) and the `RenderSpec`. The renderer introduces **no** implicit
  gain staging, normalization, dynamic processing (ducking / side-chain / compression),
  fades, or hidden audio sources — a deterministic weighted sum of the authored inputs.
  (Reinforces RC3 + RC6; enforced concretely by `amix …:normalize=0`.)
- **W8.4b.1 / W8.4b.2** — carry over unchanged: a pure Timeline → Media transform
  consuming only `MediaAsset` ids + Timeline data; never provider outputs/URLs,
  checkpoints, or orchestration state.

#### Unchanged (freeze holds)
- No migrations (audio tracks / `clip.volume` / `track.muted` already exist and are
  authorable; the change is an FFmpeg filter-graph extension + additive Timeline reads +
  additive neutral-DTO fields). No frozen orchestration path changed (ADR-0042 Gate 1).
  Every change satisfies ADR-0043 RC1–RC6 (Gate 2). Freeze guard green, **zero override
  markers**. Absolute-time placement (Fork D deferred) and transitions/effects/color
  grading/subtitles deferred to α8.4f.

### Phase 3 Slice α8.4d — Derived-Preview Enrichment — preview clip + GIF + waveform (2026-07-24)

Extends the α8.4c enrichment seam with the remaining **derived-preview** artifacts. The
gating question — *"can this be expressed as a pure downstream transformation of an
existing `MediaAsset`?"* — split the α8.4c-deferred list: **preview clip / GIF /
waveform** are transforms of a finished asset (α8.4d), while **audio mixing /
transitions / render quality tuning** change *what the render is* (composition) and are
deferred to a new **α8.4e** render slice. The **same** `MediaEnrichmentWorker` now runs
a **pipeline of independent enrichers** — thumbnail (α8.4c) + preview + GIF + waveform —
each a pure `parent (+ bytes) → one derived artifact` transform behind its own neutral
FFmpeg port. The enrichment marker is now **versioned**: bumping
`CURRENT_ENRICHMENT_VERSION` (1 → 2) re-claims already-enriched assets so α8.4c-era
videos backfill previews; a **recursion guard** (new invariant **W8.4d.1**) ensures
derived assets are never themselves enriched. Freeze guard green, **zero override
markers**; **zero migration**. Runtime capability change → version bump to
`0.4.28-phase3-alpha8.4d`.

#### Added
- **Neutral ports (α8.4d Fork C — discrete, never a "God" `IMediaEnricher`)** —
  `IPreviewClipper` (`preview_clipper.py`), `IGifPreviewer` (`gif_previewer.py`),
  `IWaveformRenderer` (`waveform_renderer.py`), each with its own DTO + neutral error.
  `IWaveformRenderer.waveform` returns `None` for a **silent source** (not applicable,
  not a failure).
- **FFmpeg adapters** — `FfmpegPreviewClipper` (trim + downscale → MP4),
  `FfmpegGifPreviewer` (`fps` + lanczos scale → GIF), `FfmpegWaveformRenderer`
  (`showwavespic`, audio-probed) in `app/infrastructure/render/`; **configuration-blind**
  (W8.1.1 — reuse the α8.4b binary config); every failure maps to the port's neutral
  error via a shared `_ffmpeg_exec` helper.
- **Internal enricher pipeline** (`app/application/use_cases/media/enrichers/`) — an
  `Enricher` ABC + `DerivedArtifact` DTO and `ThumbnailEnricher` / `PreviewEnricher` /
  `GifEnricher` / `WaveformEnricher`. The worker orchestrates; each enricher owns
  applicability, its deterministic key, and its derived-asset + metadata contribution.
  **Implementation detail only** — no new platform abstraction, worker, or ADR.
- **`EnrichGeneratedMedia` refactor** — from one thumbnail to a pipeline over a single
  materialization: run each applicable enricher, register each derived `MediaAsset`
  (idempotent, `ConflictError` recovery), merge ids + scalars into
  `source_metadata.enrichment`, and set `version = CURRENT_ENRICHMENT_VERSION` **iff
  every applicable enricher succeeded**. Per-artifact failure isolation: a transient
  failure leaves the version un-bumped so a later pass retries (recovering the already-
  registered artifacts).
- **Versioned + recursion-guarded claim scan** — `IMediaRepository`.`list_unenriched_generated_videos`
  → `list_enrichable_generated_videos(*, target_version, limit)`:
  `kind='video' AND source='generated' AND deleted_at IS NULL AND NOT (source_metadata ?
  'parent_media_asset_id') AND COALESCE((source_metadata #>> '{enrichment,version}')::int,
  0) < target_version`. Additive, non-frozen.
- **Config** — `enrichment_preview_max_seconds` / `enrichment_preview_max_width` /
  `enrichment_gif_max_seconds` / `enrichment_gif_fps` / `enrichment_gif_max_width` /
  `enrichment_waveform_width` / `enrichment_waveform_height`; FFmpeg binary config reused.
- **Container wiring** — lazy `FfmpegPreviewClipper` / `FfmpegGifPreviewer` /
  `FfmpegWaveformRenderer` (cleared on `shutdown`/`reset`); `_build_enrichers()` assembles
  the pipeline into the existing `get_enrich_generated_media_use_case()` factory.
- **Tests** — the full derived set from one materialization; **backfill** (α8.4c-era
  marker, version 0 → re-claimed → gains previews); **idempotent re-run** (no dupes);
  **recursion guard / W8.4d.1** (a derived video is never claimed or enriched); **per-
  artifact failure isolation** (partial status, version un-bumped, re-claimable);
  **waveform-not-applicable** (clean, terminal); guards (non-video / unsupported storage
  / locked); worker drain / batch / empty; plus **opt-in** real-FFmpeg preview/gif/
  waveform roundtrips (incl. the silent-source `None` path) skipped without the binary.

#### Invariants
- **W8.4c.1 / W8.4c.2 / W8.4c.3** — carry over unchanged (observational, parent-only,
  pure-function enrichment).
- **W8.4d.1 (new) — Derived media is terminal.** A derived `MediaAsset` SHALL NOT
  participate as the source of further enrichment processing. Enrichment operates
  exclusively on **primary** generated or rendered `MediaAsset`s. Derived artifacts are
  observational outputs only. (Enforced by the recursion guard — the derivation graph is
  a shallow tree, never a cycle.)

#### Unchanged (freeze holds)
- No migrations (derived artifacts are `media_assets` rows on the existing `media_kind`
  enum; provenance + the versioned marker are JSONB `source_metadata`; the scan change is
  additive). No frozen orchestration path changed. No `_paused` / checkpoint contract
  change. Freeze guard green, **zero override markers** (α8.4d gating criterion). Audio
  mixing / transitions / render quality tuning deferred to α8.4e (they change composition,
  not a transform of an existing asset).

### Phase 3 Slice α8.4c — Media Enrichment — generated video → thumbnail + probed metadata (2026-07-24)

The platform's **first *derived-media* capability**. A new **poll worker** (mirroring the α8.3
`CompletionEngine.poll_once` and the α8.4b `RenderWorker.run_once`) scans the **media table** for
generated video `MediaAsset`s that have not yet been enriched, and for each one: claims it under a
`media_enrichment:<id>` lease, **materializes** the source bytes from `IObjectStorage`, extracts one
thumbnail frame + probes the source bitrate via a new neutral `IThumbnailer` port (FFmpeg adapter),
stores the thumbnail under a **deterministic key**, registers a derived `MediaAsset(kind="image",
source="generated")` cross-linked to the parent, and augments the parent's `source_metadata` with an
`enrichment` marker (`{thumbnail_media_asset_id, bitrate, enriched_at}`) — which also removes it from
the claim scan. Enrichment is **observational + downstream** and a **pure function of the parent
`MediaAsset`** (new invariants **W8.4c.1** + **W8.4c.2** + **W8.4c.3**): it never reads or mutates
orchestration state, checkpoints, provider state, workflow/render lifecycle, Timeline definitions, or
render-job history. Per the sign-off it runs behind a **dedicated worker (Fork B → B2)**, never a relay
subscriber — *PublisherPort subscribers orchestrate work; they do not perform media processing* — so
FFmpeg never runs on the relay fan-out path. The freeze guard stayed green with **zero override
markers**. Runtime capability change → version bump to `0.4.27-phase3-alpha8.4c`. Previews / GIF
previews / waveform / audio mixing / transition improvements / FFmpeg quality tuning are deferred to
α8.4d (Fork A scope split).

#### Added
- **`IThumbnailer` port + `Thumbnail` / `ThumbnailError`** (`app/application/interfaces/thumbnailer.py`)
  — a backend-neutral "extract one still frame + probe scalars from a video" seam, kept **separate from
  `IRenderer`** (Fork D): rendering is `Timeline → Video`, thumbnailing is `Video → Image`.
- **`FfmpegThumbnailer`** (`app/infrastructure/render/ffmpeg_thumbnailer.py`) — shells out to `ffmpeg`
  (`-ss … -frames:v 1`) + `ffprobe` (dimensions + `format.bit_rate`); **configuration-blind** (W8.1.1 —
  reuses the α8.4b binary paths + timeout); any non-zero exit / timeout / missing output / launch failure
  maps to a neutral `ThumbnailError`.
- **`EnrichGeneratedMedia` use case** (`app/application/use_cases/media/enrich_generated_media.py`) — the
  worker body for a single asset: lease → re-read the parent (must be a live, un-enriched generated
  video) → materialize + thumbnail + probe **outside any DB transaction** → register the derived
  thumbnail `MediaAsset` → augment the parent's `source_metadata`. Idempotent via a **deterministic**
  thumbnail key in `(tenant, parent_media_asset_id)`: a re-run hits the `media_assets` storage-key
  uniqueness → `ConflictError` → the existing thumbnail is recovered via `get_by_storage_coords`, never
  duplicated. Transient FFmpeg/storage failures leave the parent un-enriched so a later scan retries.
- **`MediaEnrichmentWorker.run_once()`** (`app/application/use_cases/media/media_enrichment_worker.py`) —
  the enrichment poll ingress (Fork B → B2): one scan claims the oldest un-enriched generated videos
  (FIFO, capped by `enrichment_batch_size`) and settles each independently under its own lease.
- **`IMediaRepository.list_unenriched_generated_videos(*, limit)`** — additive, **non-frozen**,
  owner-agnostic claim scan: `kind='video' AND source='generated' AND deleted_at IS NULL AND NOT
  (source_metadata ? 'enrichment')`, oldest first, capped. The set **shrinks** as assets are marked
  enriched — no new column, no new table.
- **Config** — `enrichment_thumbnail_at_seconds` (default `1.0`), `enrichment_batch_size` (default `10`);
  FFmpeg paths/timeout reused from α8.4b.
- **Container wiring** — `FfmpegThumbnailer` built **lazily** on first use (cleared on `shutdown`/`reset`);
  `get_enrich_generated_media_use_case()` + `get_media_enrichment_worker()` factories (fresh UoW per call);
  object storage reused from α8.4a.
- **Tests** — unit coverage for `EnrichGeneratedMedia` (happy path: derived thumbnail + parent metadata
  augmented; enriched asset drops out of the claim scan; idempotent re-run recovers the thumbnail;
  already-enriched / non-video / unsupported-storage no-ops; locked skip; thumbnail failure leaves the
  asset un-enriched), `MediaEnrichmentWorker.run_once` (drain / batch cap / empty scan), and the
  `FfmpegThumbnailer` (validation + launch-failure mapping, plus an **opt-in** real-ffmpeg frame/probe
  roundtrip skipped when the binary is absent).

#### Invariants
- **W8.4c.1 (strengthened)** — media enrichment is **observational and downstream**. It may derive
  additional media artifacts and augment the owning `MediaAsset`'s `source_metadata`, but it **never**
  mutates orchestration state, checkpoints, provider state, workflow/render lifecycle, Timeline
  definitions, or renderer inputs. (Prevents enrichment from becoming "smart rendering.")
- **W8.4c.2** — the enricher consumes **only** `MediaAsset` bytes + identifiers. Never provider outputs,
  URLs, checkpoints, request IDs, provider job IDs, webhook payloads, or Timeline internals. (Mirror of
  W8.4b.2.)
- **W8.4c.3** — derived media is **reproducible from its parent `MediaAsset` alone**. Enrichment never
  depends on provider payloads, workflow checkpoints, Timeline state, or render-job history once the
  parent exists — `MediaAsset → Thumbnail` is a **pure function** of the parent, so thumbnails can be
  regenerated years later (e.g. after an FFmpeg upgrade) without the workflow that produced the video.

#### Unchanged (freeze holds)
- No migrations (thumbnails are ordinary `media_assets` rows; enrichment scalars + the marker are JSONB
  `source_metadata`; the new repository method is additive). No frozen orchestration path changed
  (`AdvanceWorkflowRun`, `ResumeWorkflowRun`, `CompletionEngine`, dispatcher, provider ports, usage
  recorder, lock manager, workflow registry). No `_paused` / checkpoint contract change. Freeze guard
  green, **zero override markers** (α8.4c gating criterion).

### Phase 3 Slice α8.4b — Render Engine — Timeline → FFmpeg → output MediaAsset (2026-07-23)

The platform's **first media-*transforming* capability**. A new **poll worker** (mirroring the α8.3
`CompletionEngine`) drains `queued` `RenderJob`s: for each it claims the job (`queued → running` CAS
under a `render_job:<id>` lease), resolves the project's Timeline into an ordered list of video
`MediaAsset`s, **materializes** those source bytes from `IObjectStorage`, composes them via a new
neutral `IRenderer` port (FFmpeg adapter), stores the output under a **deterministic key**, registers
a `MediaAsset(kind="video", source="generated")`, and settles the job `succeeded`
(`output_media_asset_id`) — or `failed`. This completes the dependency graph
`Provider → Completion → GeneratedMediaIngestion → MediaAsset → Timeline → Renderer → output MediaAsset`.
The render path is a **pure Timeline → Media transform** (new invariants **W8.4b.1** + **W8.4b.2**);
the freeze guard stayed green with **zero override markers**. Runtime capability change → version bump
to `0.4.26-phase3-alpha8.4b`. Thumbnails / waveforms / audio mixing / richer metadata / previews are
deferred to α8.4c (Fork E scope split).

#### Added
- **`IRenderer` port + `RenderSpec` / `RenderInput` / `RenderResult` / `RenderError`**
  (`app/application/interfaces/renderer.py`) — a backend-neutral "compose ordered, trimmed source
  segments into one output video" seam, kept **separate from the frozen provider ports** (Fork B). A
  different engine could replace FFmpeg with no use-case change.
- **`FfmpegRenderer`** (`app/infrastructure/render/ffmpeg_renderer.py`) — shells out to `ffmpeg`
  (`filter_complex` trim + concat) then probes with `ffprobe`; **configuration-blind** (W8.1.1 — binary
  paths + timeout injected); any non-zero exit / timeout / missing output / launch failure maps to a
  neutral `RenderError`. Video-concat baseline (audio mixing / transitions → α8.4c).
- **`ProcessRenderJob` use case** (`app/application/use_cases/render/process_render_job.py`) — the
  worker body for a single job: lease → `queued → running` CAS → resolve Timeline video clips → download
  sources + render + store output **outside any DB transaction** → register output `MediaAsset` →
  `mark_succeeded`. Idempotent via a **deterministic** output key in `(tenant, project, render_job)`:
  a re-render hits the `media_assets` storage-key uniqueness → `ConflictError` → the existing asset is
  recovered via `get_by_storage_coords`, never duplicated.
- **`RenderWorker.run_once()`** (`app/application/use_cases/render/render_worker.py`) — the render poll
  ingress (Fork A): one scan claims the oldest `queued` jobs (FIFO, capped by `render_batch_size`) and
  settles each independently under its own lease, so one slow render never blocks the others. Rendering
  runs behind a poller, **not** the relay fan-out (the relay stays fast for lightweight subscribers).
- **`IRenderJobRepository` worker transitions** — `list_claimable`, `mark_running`, `mark_succeeded`,
  `mark_failed`: additive, **non-frozen**, keyed by `render_job_id`, each a race-safe status-predicated
  CAS with hand-set `version = version + 1` (mirroring the α7.1 `cancel` CAS).
- **`IMediaRepository.get_by_storage_coords(...)`** — additive owner-agnostic idempotent-recovery lookup
  (immutable, unique physical-object columns) so the worker recovers an already-registered output.
- **`RenderJobSucceeded` / `RenderJobFailed` outbox events** (`_events.py`) — emitted by the worker
  inside the settle UoW; carry orchestration identity + `output_media_asset_id` (or a neutral `error`)
  only — no rendered bytes, provider, or timeline-edit state (W8.4b.2).
- **Config** — `render_ffmpeg_path`, `render_ffprobe_path`, `render_timeout_seconds`,
  `render_workspace_dir`, `render_batch_size`.
- **Container wiring** — `FfmpegRenderer` built **lazily** on first render (cleared on `shutdown`/`reset`);
  `get_process_render_job_use_case()` + `get_render_worker()` factories (fresh UoW per call); object
  storage reused from α8.4a.
- **Tests** — unit coverage for `ProcessRenderJob` (happy path + probed metadata + deterministic key,
  clip ordering, idempotent re-render, render failure → `failed` + event, empty timeline → `failed`,
  non-queued no-op, locked skip), `RenderWorker.run_once` (drain / batch cap / empty scan), and the
  `FfmpegRenderer` (validation + launch-failure mapping, plus an **opt-in** real-ffmpeg concat/probe
  roundtrip skipped when the binary is absent).

#### Invariants
- **W8.4b.1** — the render worker is a **pure Timeline → Media transform**. It neither reads nor mutates
  orchestration state, checkpoints, provider state, workflow status, or the completion lifecycle. It
  touches only `render_jobs` lifecycle fields, the Timeline (read), `MediaAsset`s (read sources / create
  output), storage, and render events.
- **W8.4b.2** — the renderer consumes **only** `MediaAsset` identifiers + Timeline data. It never
  consumes provider-specific outputs, URLs, checkpoints, request IDs, provider job IDs, or webhook
  payloads — making it completely provider-agnostic.

#### Unchanged (freeze holds)
- No migrations (all additive repository methods on the existing `render_jobs` / `media_assets` tables).
  No frozen orchestration path changed (`AdvanceWorkflowRun`, `ResumeWorkflowRun`, `CompletionEngine`,
  dispatcher, provider ports, usage recorder, lock manager, workflow registry). No `_paused` / checkpoint
  contract change. Freeze guard green, **zero override markers** (α8.4b gating criterion).

### Phase 3 Slice α8.4a — Generated Media Ingestion — download / store / register (2026-07-22)

The platform's **first *producing* capability** and its **first real outbox consumer**. When a run
settles `running → succeeded`, a downstream subscriber on the existing `WorkflowRunSucceeded` event
downloads the provider's produced artifact (`image_ref` / `video_ref`), stores the bytes via a new
`IObjectStorage` port (local filesystem in α8.4a), and registers a `MediaAsset(source="generated")`.
This turns the orchestration layer into a **platform**: independent consumers (analytics, billing,
export, …) can now attach to the same event stream without the runner ever knowing. Ingestion is
strictly downstream of — and never mutates — the frozen pipeline (new invariants **W8.4.1** +
**W8.4.2**); the freeze guard stayed green with **zero override markers**. Runtime capability change →
version bump to `0.4.25-phase3-alpha8.4a`. FFmpeg / thumbnails / render jobs are deferred to α8.4b.

#### Added
- **`IObjectStorage` port + `StoredObject` / `ObjectStorageError`**
  (`app/application/interfaces/object_storage.py`) — the platform's first backend-neutral blob store
  (`put` / `get` / `exists` / `delete` by opaque `/`-delimited key). Lets S3 / R2 / GCS / Azure / MinIO
  adapters replace the local one later with **no use-case change**.
- **`LocalObjectStorage`** (`app/infrastructure/storage/local_object_storage.py`) — filesystem adapter
  (`<root>/<bucket>/<key>`, `storage_backend="local"`), file I/O off the event loop via
  `asyncio.to_thread`, confines keys to the bucket root (rejects `..` traversal).
- **`IMediaDownloader` port + `DownloadedMedia` / `MediaDownloadError`**
  (`app/application/interfaces/media_downloader.py`) — neutral "fetch an artifact by URL" seam, kept
  **separate from the frozen provider ports** (α8.4a Fork B): the provider already resolved the job;
  downloading the result is generic infrastructure.
- **`HttpMediaDownloader`** (`app/infrastructure/media/http_media_downloader.py`) — single-GET fetch
  over an injected `httpx` client with a hard byte cap; any non-2xx / transport error / timeout / cap
  breach maps to a neutral `MediaDownloadError` (the subscriber's retry is the relay's, not the
  downloader's).
- **`IngestGeneratedMedia` use case** (`app/application/use_cases/media/ingest_generated_media.py`) —
  reads a succeeded run's steps, extracts each `image_ref` / `video_ref` from the opaque provider
  output envelopes, downloads **outside any DB transaction**, stores, and registers a
  `MediaAsset(source="generated")`. Idempotent via a **deterministic** storage key in
  `(run, step, request_id)`: a redelivery re-writes identical bytes and the `media_assets`
  storage-key uniqueness raises `ConflictError` → caught as an already-ingested no-op. Minimal
  metadata only (checksum, mime, size, coordinates, provider); duration / dimensions / codec deferred
  to α8.4b.
- **`GeneratedMediaIngestionSubscriber`** (`app/application/use_cases/media/generated_media_subscriber.py`)
  — the first `EventHandler` registered on the in-process `PublisherPort`; filters
  `WorkflowRunSucceeded`, builds a **fresh** use case per event (own UoW), idempotent under the relay's
  at-least-once redelivery.
- **`IProjectRepository.get_ownership(project_id) -> (tenant_id, owner_user_id) | None`** — a
  **system-only, non-frozen** lookup (mirrors α8.3b's `find_paused_by_provider_job_id`) so the
  server-side subscriber can resolve the owning `(tenant, user)` for a run's project. Never wired to an
  HTTP endpoint; deliberately sidesteps the owner-scoped anti-enumeration posture of `get_owned`.
- **Config** — `media_storage_root`, `media_storage_bucket`, `media_download_timeout_seconds`,
  `media_download_max_bytes`.
- **Container wiring** — object storage + downloader (with a dedicated `httpx` client) built **lazily**
  on first ingestion (disposed in `shutdown()`), and the subscriber registered on the publisher at
  `init()`; the α7.3 relay is untouched.
- **Tests** — unit coverage for `LocalObjectStorage`, `HttpMediaDownloader`, `IngestGeneratedMedia`
  (happy path, idempotent redelivery, multi-ref, no-media / non-succeeded / missing-ownership no-ops),
  and the subscriber (trigger / ignore other events / malformed payload).

#### Invariants
- **W8.4.1** — generated-media ingestion is strictly downstream of the frozen completion pipeline; the
  runner / completion / dispatcher never download, store, or register media.
- **W8.4.2** — ingestion is **observational**: it may create downstream artifacts (storage objects,
  `MediaAsset` rows, logs, metrics) but must never mutate `WorkflowRun`, `WorkflowCheckpoint`, steps,
  `UsageRecord`, or any orchestration decision.

#### Unchanged (freeze holds)
- No migrations. No frozen orchestration path changed (`AdvanceWorkflowRun`, `ResumeWorkflowRun`,
  `CompletionEngine`, dispatcher, provider ports, usage recorder, relay, lock manager, `_paused`
  checkpoint contract). Full gate green: ruff, black, mypy, import-linter (6 contracts), 555 unit
  tests, `check_frozen_platform.py --base main` OK with zero overrides.

### Governance — Platform validation (pre-α8.4): two accepted risks recorded (2026-07-22)

A grounded validation pass after `v0.4.24-phase3-alpha8.3b` (freeze-guard coverage, crash recovery,
completion correctness, DI coverage) confirmed the orchestration platform is sound and neither finding
blocks α8.4. Two **known limitations** are recorded as *accepted risks* in ADR-0042 (docs-only, no code
change) so they surface as intentional decisions rather than drift:
- **AR-1** — `dispatcher.py` (single-dispatch seam, G1) has no structured logging; observability, not
  correctness. Fixable under ADR-0042 §D2 without a new ADR.
- **AR-2** — the `_paused` checkpoint handoff has no top-level `schema_version` (one producer, read
  defensively). *Future* evolution concern; per §D2 any incompatible change requires a dedicated ADR
  first. The α8.4 pre-flight's first design question is whether α8.4 must touch the checkpoint contract.

### Phase 3 Slice α8.3b — Webhook Completion Ingress (Fal) — a thin second ingress (2026-07-22)

The **first integration slice built entirely on top of the frozen platform** (ADR-0042). It adds a
*second* way to learn a provider job finished — an inbound Fal webhook — alongside α8.3's polling,
and both converge on the **same** frozen `CompletionEngine.complete()`. The webhook is a **signal,
not a source of truth** (new invariant **W8.3b.1**): after signature verification it is used *only*
to locate the paused run; `complete()` then re-resolves the job authoritatively. No frozen
orchestration path changed — the freeze guard stayed green with **zero override markers** for the
whole branch. Runtime capability change → version bump to `0.4.24-phase3-alpha8.3b`.

#### Added
- **`IWebhookVerifier` port + neutral DTOs** (`app/application/interfaces/webhook_verifier.py`) —
  provider-agnostic `verify(body, headers) -> VerifiedWebhook(provider_job_id)`, with
  `WebhookVerificationError` (→ 401) and `WebhookMalformedError` (→ 400). Returns only the resume
  coordinate (the provider job id) — never the payload's claimed result.
- **`FalWebhookVerifier`** (`app/infrastructure/ai/providers/fal/webhook.py`) — ED25519 verification
  against Fal's **public** JWKS keys (fetched via an injected `httpx` client + `cryptography`,
  cached with a TTL). Requires the `X-Fal-Webhook-{Request-Id,User-Id,Timestamp,Signature}` headers,
  enforces a timestamp-tolerance replay guard, and verifies the canonical
  `"\n".join([request_id, user_id, timestamp, sha256(body)])` message. Strict provider leaf (httpx +
  cryptography + the neutral port only).
- **`IWorkflowRunRepository.find_paused_by_provider_job_id`** — an **additive, non-frozen** lookup
  (impl + fakes) that resolves the webhook's only trusted datum (the job id) to its paused run by
  matching the `_paused.provider_job_id` in the latest checkpoint JSONB. Documented as an
  implementation detail, not a new architectural contract. **Zero migration.**
- **`ReceiveProviderWebhook` use case** (`app/application/use_cases/workflow/`) — verify → find paused
  run → trigger `CompletionEngine.complete()`. Performs **no writes** (W8.3b.1); all state changes
  stay inside the frozen completion pipeline. Duplicate deliveries are inherently safe (retry after
  resume → no paused run → ack; retry mid-processing → held lease → ack).
- **`POST /api/v1/webhooks/providers/{provider}`** router — thin, unauthenticated (the *signature* is
  the auth), reads the **raw** body, and maps outcomes: `401` bad/stale/missing signature, `400`
  malformed, `404` unknown provider, `200` accepted (resumed/in-progress/duplicate/unknown job id).
- **Config** (`FAL_WEBHOOK_JWKS_URL`, `FAL_WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS`,
  `FAL_WEBHOOK_JWKS_CACHE_SECONDS`) + lazy container wiring (verifier built on first use so the common
  test path opens no HTTP client).
- **Unit tests** — verifier (real ED25519 keypair over an in-memory JWKS: valid / tampered / wrong
  key / missing header / non-hex / stale / rotation / caching), ingress (resume / unknown / duplicate
  / unsupported / bad-signature-no-op / poll-vs-webhook race), and router status mapping + raw-body
  passthrough.

#### Notes
- **W8.1.1 clarification:** the "configuration-blind adapter" invariant governs *credentials /
  authentication material*. Fal's JWKS holds **public verification keys** — configuration-independent
  trust anchors — so fetching + caching them is permitted and injects no secret.
- **Deferred (signed off):** inbound webhook **receipt persistence**. The `idempotency_keys` table
  exists but has no application consumer; wiring one would mean a new `IIdempotencyRepository` +
  `IUnitOfWork.idempotency` + every fake UoW — a cross-cutting subsystem, not a thin ingress.
  Exactly-once is already owned by `complete()`'s lease + `paused → running` CAS, and 200-on-duplicate
  holds without a receipt. A first-class idempotency subsystem arrives when ≥2 inbound endpoints need
  shared receipt/audit semantics (Fal + Stripe + publishing/OAuth callbacks).

### Governance — Orchestration Platform Freeze (ADR-0042) (2026-07-22)

With `v0.4.23-phase3-alpha8.3` the async orchestration loop closed end-to-end and the core became
**feature-complete**: α7.x built the substrate, and α8.1–α8.3 proved it drives both synchronous and
asynchronous *real* providers without duplicating orchestration or exposing runner internals. The
remaining Phase 3 work (α8.3b webhook ingress, α8.4 media + FFmpeg, α8.5 export/publishing) is
**integration on top of stable seams**, not orchestration design — exactly when a freeze pays off.
This is a **governance** change: no application code, no schema migration, no runtime behaviour
change, **no version bump** (mirroring ADR-0041's docs-only precedent). It makes the boundary
explicit *and mechanically enforced*, so a feature slice can't quietly reshape the core for a
shortcut; if a slice appears to need a core change, that surfaces as an ADR-worthy architectural gap
rather than slipping into a feature branch.

#### Added
- **ADR-0042** (`docs/decisions/ADR-0042-orchestration-platform-freeze.md`) — defines the **frozen
  public orchestration surface** (§D1: runner, `ResumeWorkflowRun`, `CompletionEngine`, workflow
  events, dispatcher, provider ports + registry + neutral DTOs, usage recorder + pricing + port,
  relay, distributed lock manager + port, workflow registry/aggregate/status enums), the **change
  policy** (§D2: bug/security/perf/observability/docs allowed; signature/DTO/checkpoint/lifecycle/
  retry/provider-protocol/usage-semantics changes require a new ADR), the **platform guarantees**
  (§D3, G1–G10: single dispatch, deterministic request IDs, exactly-once completion under locks,
  provider-agnostic orchestration, exactly-once usage, versioned checkpoints, resume never
  re-dispatches, configuration-blind providers, runner/provider boundary, two public resume seams),
  and **enforcement** (§D4).
- **`backend/scripts/check_frozen_platform.py`** — a dependency-free guard that diffs the change set
  against a base ref and **fails** if any frozen path is touched without an override marker
  (`Freeze-Override: ADR-XXXX <reason>` commit trailer, or `ALLOW_FROZEN_CHANGES=1` for local
  iteration). `FROZEN_PATHS` is the single machine-readable mirror of ADR-0042 §D1. Run locally
  before fast-forwarding: `python backend/scripts/check_frozen_platform.py --base main`.
- **`freeze-guard` CI job** (`.github/workflows/ci.yml`) — a fast, DB-free job (separate from the
  ADR-0028 ten-stage quality gate) that runs the guard on every pull request (diffing the PR base)
  and every push to `main` (diffing the pushed range).
- **`.github/CODEOWNERS`** — requires the platform owner's review for the frozen paths: a second,
  GitHub-native layer so a contract-affecting change is *seen*, not only flagged.
- **`tests/unit/scripts/test_check_frozen_platform.py`** — unit coverage for the guard's matching /
  override / decision logic, plus a lock-step assertion that every `FROZEN_PATHS` entry still exists
  on disk (a rename without updating the list — or the ADR — fails the suite).

#### Notes
- Explicitly **not** frozen: concrete provider *adapters* (`providers/openai|fal|mocks/`) and all
  new-capability surfaces (new ingress/downstream use cases, repositories, routers, DI wiring, tests)
  — these are the growth surfaces new work plugs into. `v0.4.23-phase3-alpha8.3` is hereby the point
  at which the orchestration platform is feature-complete.

### Phase 3 Slice α8.3 — Completion Engine (poll-first) — the resume slice (2026-07-22)

The **first slice to move orchestration state since α7.6**: it closes the async loop α8.2 opened.
α8.2 proved a real async provider can drive the pause seam (`submit → IN_PROGRESS → provider_job_id
→ paused`); α8.3 adds the **single, idempotent completion engine** ADR-0041 **D5** mandates — the
sole writer that turns an in-flight provider job's terminal outcome into aggregate state. The
decisive architectural fact, verified in grounding, is that **the runner already supports resume**:
`AdvanceWorkflowRun` re-advances a `running` run and *skips already-`succeeded` steps*. So the
completion engine does **not** re-implement step execution and **never re-dispatches** the async
command (**W8.3.3**). It resolves the job, records the deferred **terminal usage** row under the
**checkpointed** `request_id`, marks the paused step `succeeded`, flips `paused → running`, and hands
continuation back to the **unchanged** runner. Completion → resume is deliberately split across **two
public seams** so no service reaches into runner internals: `CompletionEngine` (resolve the provider
job under the per-run lease) → **`ResumeWorkflowRun`** (own the atomic *resume + terminal usage +
step-succeeded + delegate continuation* transaction) → `AdvanceWorkflowRun` (step-execution semantics
untouched, entered via a new **public** continuation entrypoint). Every future completion mechanism
(the α8.3b webhook, manual resume, admin replay) converges on the single public `ResumeWorkflowRun`
seam — a stable orchestration API, not a private helper.

**Ingress = polling only** for α8.3 (D6 "poll-first, built first"); the webhook receiver is a *thin
second ingress to the identical `complete()`* deferred to **α8.3b**. The async capability is now
modelled as a **lifecycle** (Q3): `VideoProvider.submit()` (renamed from `generate_video` — already
submit-only by construction) + a new `resolve()` (terminal `SUCCEEDED`/`FAILED`, or `IN_PROGRESS` if
still running). This generalises to every future async provider (Runway, Kling, Pika, Luma, …) and
keeps orchestration provider-agnostic (**W8.3.4** — the engine reads only
`ProviderResponse.status`/`usage`/`output`; the adapter owns all payload parsing). **Exactly-once
resume** (**W8.3.2**) is guaranteed by the per-run `workflow_run:<id>` lease (D8) *and* the
`paused → running` CAS backstop: concurrent ingresses contend on the lease, and the loser's CAS
writes nothing. Terminal usage is idempotent on `request_id` (the recorder dedupes replays). The
engine is **library-only** (D11 runner-before-worker): `complete()` + `poll_once()` driven
synchronously by a test loop or trigger — **no Celery/Redis/daemon**. **Zero migration** — every
table needed already exists; the only new persistence surface is two repo methods on existing tables
(`resume_run` CAS `paused → running`, `list_paused` global scan). **Explicitly forbidden and absent:**
webhook receiver + signature verification (→ α8.3b) · media registration / `video_ref` (→ α8.4) ·
FFmpeg · storage/download of provider output · export · new providers · provider selection / fallback
· rate limiter · circuit breaker · any change to the pure step handlers or the `StepCommand` dispatch
`kind` contract · schema migrations. Four invariants govern the slice: **W8.3.1** (single idempotent
entrypoint; a replay for an already-resumed/terminal run is a no-op), **W8.3.2** (exactly-once
resume), **W8.3.3** (completion delegates, never re-dispatches), **W8.3.4** (orchestration stays
provider-agnostic). See `docs/engineering/PHASE3_ALPHA8_3_PREFLIGHT.md` and **ADR-0041**
(D5/D6/D8/D11).

#### Added
- **`CompletionEngine`** (`app/application/use_cases/workflow/completion_engine.py`, new) — the
  completion engine: one public idempotent `complete(project_id, run_id)` (acquire the per-run lease
  → read the `_paused` handoff → `dispatcher.resolve_job(...)` → leave paused if still `IN_PROGRESS`,
  else delegate a terminal result to `ResumeWorkflowRun`) plus a `poll_once()` polling ingress that
  scans all `paused` runs (oldest-first) and completes each under its own lease. Provider I/O happens
  outside any DB transaction; the lease + CAS (not a long-held row lock) provide isolation.
- **`ResumeWorkflowRun`** (`app/application/use_cases/workflow/resume_workflow_run.py`, new) — the
  **public atomic-resume use case** every completion mechanism converges on. In one transaction:
  idempotent no-op if not `paused` → `resume_run` CAS (`paused → running`, the exactly-once gate) →
  `WorkflowRunResumed` → terminal usage under the checkpointed `request_id` → `mark_step_succeeded`
  (+ opaque terminal envelope) → delegate continuation to `AdvanceWorkflowRun.continue_paused_run_in_uow`
  on the *same* open UoW → commit. On `FAILED` it settles the run failed itself (a failed step must
  not be driven), emitting a `WorkflowRunFailed` error **shape-identical** to the α7.6 inline path.
- **`VideoProvider.resolve`** (`app/infrastructure/ai/providers/ports.py`; impls in `fal/video.py`
  and `mocks/mock_video.py`) — the completion half of the async lifecycle. Fal `resolve` GETs the
  checkpointed `status_url`/`response_url` from the α8.2 opaque envelope and maps status → the
  existing typed `ProviderError` buckets; the mock resolves deterministically (tests need no network).
- **`ProviderDispatcherPort.resolve_job`** (`app/application/interfaces/provider_dispatcher.py`;
  impl in `dispatcher.py`) — additive: routes a paused VIDEO job to `provider.resolve(...)`; a
  synchronous capability has no resolvable job → terminal `ProviderValidationError`.
- **`WorkflowRunResumed` event** (`app/application/use_cases/workflow/_events.py`) — emitted on
  `paused → running`, carrying `step_index` + `provider_job_id` (Q9; `event_version` `"1.0"`).
- **Repo methods (no migration)** — `IWorkflowRunRepository.resume_run` (CAS `paused → running`) and
  `list_paused` (global paused scan for the poller), with SQL + fake implementations.
- **Completion config** (`app/core/config.py`) — `completion_lock_owner` (default
  `"completion-engine"`) and `completion_lease_seconds` (default `60.0`).
- **Unit tests** — `tests/unit/application/use_cases/workflow/test_completion_engine.py` (idempotent
  `complete`, exactly-once resume under a held foreign lease, `IN_PROGRESS`-leaves-paused,
  resume→drive-to-succeeded, terminal-usage under the checkpointed `request_id`, FAILED settlement,
  no-re-dispatch, `poll_once` sweep); extended dispatcher (`resolve_job` routes VIDEO / rejects sync),
  Fal `resolve` (status→result GETs, `IN_PROGRESS`/`ERROR`/transport-fault mapping), and mock
  `resolve` cases.

#### Changed
- **Runner (`AdvanceWorkflowRun`)** — behaviour-preserving refactor: `execute()`'s step-loop + settle
  core is extracted into a private `_drive_and_settle`; a new **public** `continue_paused_run_in_uow`
  drives an already-`running` run's remaining steps + settles on the **caller's open UoW** (no
  transaction, no commit), letting `ResumeWorkflowRun` commit resume + continuation atomically. Exactly
  one copy of step-execution remains; `execute()`'s observable behaviour is unchanged. The `_paused`
  checkpoint handoff is additively enriched (Fork 1A) with `command_index` / `capability` / `model_id`
  / `tenant_id` and the opaque submit `envelope`, so completion records terminal usage deterministically
  without re-running a handler.
- **Dispatcher (`StepCommandDispatcher`)** — the one VIDEO call-site now calls `provider.submit(...)`
  (renamed from `generate_video`); the closed `StepCommand.kind` table and dispatch contract are
  unchanged (`"generate_video"` is a *workflow* verb, not a provider method).
- **DI container** (`app/core/container.py`) — composes `ResumeWorkflowRun` (shared UoW + runner) and
  `CompletionEngine` (UoW + resume + dispatcher + lease config); the runner/dispatcher/recorder/relay
  and lock-manager implementations are untouched.

#### Version
- App version bumped to **`0.4.23-phase3-alpha8.3`** (staying on `0.4.x`; first orchestration-state
  move since α7.6 — the async completion/resume loop behind the unchanged Phase-3 runner).

### Phase 3 Slice α8.2 — First Real Async Provider (Fal.ai Video, submit-only) — the pause-proving slice (2026-07-21)

The **first real *async* provider** slice: it replaces the *one* remaining async-shaped
mock — the video provider — with a **real** Fal.ai adapter that **submits** a queue-based
video job and returns `IN_PROGRESS` + a `provider_job_id`, driving the **pause seam built
in α7.6** with a real external system. It is the first slice to exercise *new orchestration
behaviour* (the async/pause branch) rather than swap a synchronous mock — yet, as with
α8.1, the orchestration itself does **not** move: the runner, `StepCommandDispatcher`,
`UsageRecorderService`, relay, lock manager, `ProviderRegistry` class, neutral DTOs, the
`ports.py` protocols, and the `generate-video` pipeline are all **byte-for-byte unchanged**;
the entire behavioural diff lives inside the new provider leaf
(`app/infrastructure/ai/providers/fal/`) plus minimal DI wiring in the container.
`FalVideoProvider` implements the existing `VideoProvider` protocol, makes **exactly one**
HTTP request per call (**W7.6.2** — it *submits* the job and immediately returns
`IN_PROGRESS`; it never polls, waits, or resolves the result — completion is α8.3), and maps
HTTP status → the existing typed `ProviderError` buckets (401/403 → auth·terminal; other
4xx → validation·terminal; 429 → rate-limited·transient; 5xx/connection →
unavailable·transient; timeout → timeout·transient) so **nothing HTTP leaks upward** (Q8).
`provider_job_id` is set to the Fal `request_id` (the runner's resume coordinate — already
checkpointed + emitted in `WorkflowRunPaused`); the completion URLs (`status_url`,
`response_url`) ride a **versioned opaque `output` envelope** (`schema_version: 1`, Q4) the
runner checkpoints verbatim without inspecting (W7.6.1), giving α8.3 a stable payload
contract. **No usage is recorded on submit** (Q5 — the runner already discards usage on
pause; α8.3 records the priced terminal row under the same `request_id`). The container
composes VIDEO by config, **independently of IMAGE**: with `FAL_API_KEY` set, VIDEO resolves
to the real provider; without it, VIDEO stays on `MockVideoProvider` — **exactly one provider
per capability, no selection engine, no fallback**. LLM/VOICE remain mock. **Zero migration.**
Four signed-off invariants govern the slice: **W8.1.1 — adapters are completely
configuration-blind** (the provider receives a pre-authenticated `httpx.AsyncClient` with the
`Authorization: Key …` header baked in; it performs no env/DB/filesystem/vault lookup and
never sees the raw key); **W8.2.1 — observational equivalence**: the real adapter returns the
*same* `GenerateVideoResponse` shape, `IN_PROGRESS` status, set `provider_job_id`, and opaque
`output` envelope as the mock, so the runner cannot tell which produced it and **pauses
identically** — only the values differ; **W8.2.2 — the run stops at the pause boundary** (the
adapter only ever returns `IN_PROGRESS`); and **W8.2.3 — the adapter never mutates
orchestration state** (no resume/complete/checkpoint/event/usage — a pure request→response
leaf with no reference to the UoW, event bus, checkpoint store, or usage recorder).
**Explicitly forbidden and absent:** polling · webhook receiver · completion service ·
resume/advance-after-pause logic · Celery · Redis · broker · usage recording for the async
job · storage · media registration · `video_ref` population · export · image-path changes ·
multi-provider fallback · provider selection · rate limiter · circuit breaker. See
`docs/engineering/PHASE3_ALPHA8_2_PREFLIGHT.md` and **ADR-0041** (D1/D4/D5/D10).

#### Added
- **`FalVideoProvider`** (`app/infrastructure/ai/providers/fal/video.py`, new `fal/`
  subpackage in the strict provider leaf) — an asynchronous submit-only adapter over the
  Fal.ai queue endpoint implementing `VideoProvider`. Imports only `httpx` + the neutral
  provider DTOs/errors (no runner/dispatcher/recorder/workflow import — import-linter leaf
  contract still KEPT). Validates the requested route against a supported set **before** any
  network call (unsupported → terminal `ProviderValidationError`, zero HTTP), performs one
  submit request, maps status → typed error, and returns `IN_PROGRESS` with `provider_job_id`
  + a versioned opaque `output` envelope + `usage=None`. Metadata advertises
  `supports_polling=True`/`supports_webhooks=True` (Q9 — α8.3's completion service branches
  on these). Static `health()`.
- **Fal settings** (`app/core/config.py`) — `fal_api_key: SecretStr | None` (default `None`
  → VIDEO stays mock), `fal_base_url` (default `https://queue.fal.run`), and
  `fal_timeout_seconds` (default `60.0`, must be `> 0`). Mirrored in `backend/.env.example`.
- **Unit tests** — `tests/unit/infrastructure/ai/providers/test_fal_video.py` (submit shape
  incl. the versioned envelope, one-request-per-call, the full status→error map,
  timeout/connection faults, `request_id`-less 2xx bodies, metadata, static health, and the
  **W8.2.1** observational-equivalence check against `MockVideoProvider`, all through an
  in-memory `httpx.MockTransport` — CI never touches the network); extended
  `tests/unit/core/test_container_provider_registry.py` (independent IMAGE/VIDEO composition:
  Fal-key-present → real VIDEO, absent → mock; both-keys → both real; the injected key baked
  into the shared client's `Authorization: Key …` header); plus new `Settings` cases in
  `tests/unit/core/test_config.py`.

#### Changed
- **DI container** (`app/core/container.py`) — the registry composition is extended with a
  VIDEO branch, symmetric to α8.1's IMAGE branch: a new `_build_fal_client(settings)` (a
  single shared, pre-authenticated `httpx.AsyncClient`, or `None` when no key is configured)
  and `_build_provider_registry(openai_client, fal_client)` now register the real-or-mock
  provider for **both** IMAGE and VIDEO, composed independently. The Fal client joins the
  `init`/`shutdown`/`reset` lifecycle (`shutdown()` `aclose()`s it). `StepCommandDispatcher`
  and the runner factory are unchanged — they still receive a `ProviderRegistry` and never
  learn which concrete provider serves a capability (W8.1.3/W8.2.1).

#### Version
- App version bumped to **`0.4.22-phase3-alpha8.2`** (staying on `0.4.x`; first real
  *async* external provider behind the unchanged Phase-3 orchestration + pause seam).

### Phase 3 Slice α8.1 — First Real Provider (OpenAI Images, synchronous) — the adapter slice (2026-07-21)

The **adapter** slice: it replaces the *one* mocked box at the bottom of the α7.6
pipeline — the image provider — with a **real** synchronous OpenAI Images adapter,
and proves the α7.4 abstraction / α7.6 orchestration can drive an external system
**without any orchestration-layer change**. The runner, `StepCommandDispatcher`,
`UsageRecorderService`, relay, lock manager, `ProviderRegistry` class, neutral DTOs,
and `ports.py` are all **byte-for-byte unchanged**; the entire behavioural diff lives
inside the provider leaf (`app/infrastructure/ai/providers/openai/`) plus minimal DI
wiring in the container. `OpenAIImageProvider` implements the existing `ImageProvider`
protocol over `POST /images/generations` (`dall-e-3`, `response_format="url"` — a
compact URL ref so **no storage layer** is needed; `gpt-image-1`/base64 waits for
α8.4), makes **exactly one** HTTP request per call (**W7.6.2** — all retry belongs to
the runner), and maps HTTP status → the existing typed `ProviderError` buckets
(401/403 → auth·terminal; other 4xx/policy → validation·terminal; 429 →
rate-limited·transient; 5xx/connection → unavailable·transient; timeout →
timeout·transient) so **nothing HTTP leaks upward** (Q7). The container composes the
registry by config: with `OPENAI_API_KEY` set, IMAGE resolves to the real provider;
without it, IMAGE stays on `MockImageProvider` — **exactly one provider per
capability, no selection engine, no fallback** (Q5). LLM/VIDEO/VOICE remain mock.
**Zero migration.** Three signed-off invariants govern the slice: **W8.1.1 —
adapters are completely configuration-blind** (the provider receives a
pre-authenticated `httpx.AsyncClient`; it performs no env/DB/filesystem/vault lookup
and never sees the raw key — *constructors receive secrets, they never retrieve
them*, Q4); **W8.1.2 — exactly one real capability** (IMAGE only); and **W8.1.3 —
observational equivalence**: the real adapter returns the *same* `GenerateImageResponse`
shape, populated field-set, and `SUCCEEDED` semantics as the mock, so the runner
cannot tell which produced a response — only the values (image URL, provider id)
differ. **Explicitly forbidden and absent:** Celery · Redis · webhooks · polling ·
storage · media registration · export · video/LLM/voice real providers ·
multi-provider fallback · provider selection · rate limiter · circuit breaker. See
`docs/engineering/PHASE3_ALPHA8_1_PREFLIGHT.md` and **ADR-0041** (D1/D4/D10).

#### Added
- **`OpenAIImageProvider`** (`app/infrastructure/ai/providers/openai/image.py`, new
  `openai/` subpackage in the strict provider leaf) — a synchronous adapter over the
  OpenAI image-generations endpoint implementing `ImageProvider`. Imports only
  `httpx` + the neutral provider DTOs/errors (no runner/dispatcher/recorder/workflow
  import — import-linter leaf contract still KEPT). Validates the requested model
  against a supported set (`dall-e-3`/`dall-e-2`) **before** any network call
  (unsupported → terminal `ProviderValidationError`, zero HTTP), performs one
  request, maps status → typed error, and returns `SUCCEEDED` with `image_ref` +
  `usage(unit="images")`. Static `health()` (Q10 — the registry does not consult
  health yet).
- **OpenAI settings** (`app/core/config.py`) — `openai_api_key: SecretStr | None`
  (default `None` → provider stays mock), `openai_base_url` (default
  `https://api.openai.com/v1`), and `openai_timeout_seconds` (default `60.0`, must be
  `> 0`). Mirrored in `backend/.env.example`.
- **Unit tests** — `tests/unit/infrastructure/ai/providers/test_openai_image.py`
  (success shape, request payload, one-request-per-call, the full status→error map,
  timeout/connection faults, empty/`url`-less 200 bodies, metadata, static health,
  and the **W8.1.3** observational-equivalence check against `MockImageProvider`, all
  through an in-memory `httpx.MockTransport` — CI never touches the network);
  `tests/unit/core/test_container_provider_registry.py` (Q5/W8.1.2 composition:
  key-present → real IMAGE provider, key-absent → mock, LLM/VIDEO/VOICE always mock,
  and the injected key baked into the shared client's `Authorization` header); plus
  new `Settings` cases in `tests/unit/core/test_config.py`.

#### Changed
- **DI container** (`app/core/container.py`) — the provider registry is now built by
  `init(settings)` (it joins the `init`/`shutdown`/`reset` lifecycle) via two new
  private helpers: `_build_openai_client(settings)` (a single shared,
  pre-authenticated `httpx.AsyncClient`, or `None` when no key is configured) and
  `_build_provider_registry(client)` (registers the real or mock IMAGE provider and
  the three mocks). `get_provider_registry()` returns that init-built singleton;
  `shutdown()` now `aclose()`s the shared client. `StepCommandDispatcher` and the
  runner factory are unchanged — they still receive a `ProviderRegistry` and never
  learn which concrete provider serves a capability (W8.1.3).
- **Dependencies** (`backend/pyproject.toml`) — `httpx>=0.27.0` promoted from the
  `dev` extra to a **core runtime** dependency (a real provider now calls it).

#### Version
- App version bumped to **`0.4.21-phase3-alpha8.1`** (staying on `0.4.x`; first
  real external provider behind the unchanged Phase-3 orchestration).

### Phase 3 Slice α7.6 — First Pipeline (mock) — runner ⇄ dispatcher ⇄ recorder ⇄ outbox, end-to-end (2026-07-19)

The **composition** slice: it introduces **almost no new infrastructure** and
instead wires the five seams already built (α7.2 runner · α7.4 dispatcher · α7.5
recorder · α7.3 outbox · checkpoints) into **one complete, deterministic,
in-process orchestration loop** — proving the entire stack end-to-end with **no
external provider dependency** (mocks stand in behind the dispatcher). The α7.2
`AdvanceWorkflowRun` is **extended, not forked** (Q6): after a pure step handler
succeeds, the runner now interprets its `StepResult.commands` — minting a
**deterministic** `request_id` (`run_id:step_index:command_index`, D5/Q3),
dispatching each command **exactly once** (W7.6.2 — retries are the runner's alone,
never the dispatcher's) via the injected `ProviderDispatcherPort`, recording
**terminal** usage in the **same** transaction (Q5), and either **pausing** on
`IN_PROGRESS` (Q2) or checkpointing the **opaque** provider envelope (W7.6.1). Two
pipelines ship: **`generate-image@1.0.0`** — fully executable (prepare-prompt →
mock image `SUCCEEDED` → priced `usage_records` row → checkpoint → `succeeded`) —
and **`generate-video@1.0.0`** — minimal pause seam (mock `IN_PROGRESS` +
`provider_job_id` → `running → paused`; **nothing** beyond pause: no completion, no
polling, no webhook — Q1). **Explicitly forbidden and absent:** real providers,
HTTP/SDKs, Redis/Celery/broker, polling loops, webhooks, and **`Media` rows** (Q7 —
generated media stays checkpointed; α8.4 owns registration). **Zero migration** —
reuses every existing table/enum. Two invariants govern the seam: **W7.6.1 — the
runner never interprets provider payloads** (it knows only `StepCommand` /
`ProviderResponse` / `ProviderStatus`; `image_ref` / prompt text / JSON payloads /
video metadata belong to the dispatcher + provider adapter) and **W7.6.2 — exactly
one dispatcher invocation per `StepCommand`.** See
`docs/engineering/PHASE3_ALPHA7_6_PREFLIGHT.md` and **ADR-0041** (D4/D11/D13).

#### Added
- **Provider-backed workflow pipelines** (`app/domain/workflow/registry.py`) — the
  `generate-image@1.0.0` (steps `prepare-prompt` → `generate-image`) and
  `generate-video@1.0.0` (step `generate-video`) definitions, registered in
  `default_registry()`, plus their **pure** handlers (`_prepare_image_prompt` /
  `_generate_image_step` / `_generate_video_step` and the `_generation_args`
  helper). Handlers only *emit* a `StepCommand` and thread `model` / `model_id` from
  the run input into its args — they never mint the `request_id`, never see a
  `ProviderResponse`, and do no I/O (provider-agnostic by construction).
- **Runner command execution** (`app/application/use_cases/workflow/advance_workflow_run.py`)
  — `AdvanceWorkflowRun` gains an optional `dispatcher: ProviderDispatcherPort` (+
  `default_currency`). After a step succeeds it runs `_execute_commands`: mints the
  deterministic `request_id`, injects it into a fresh `StepCommand`, dispatches
  **once** (W7.6.2), maps `ProviderError` → transient (runner-retry up to the step
  bound) / terminal (fail), handles `IN_PROGRESS` → pause and `FAILED` → record
  failed usage **then** fail (Q9), and records `SUCCEEDED` usage — all in the run's
  single transaction. The provider `output` is stored as an **opaque** checkpoint
  envelope via `_response_view` (W7.6.1). Fail-fast `MODEL_ID_MISSING` before
  dispatch (Q4). On pause the run settles `running → paused`, checkpoints the resume
  coordinates (`provider_job_id`, `pending_step_index`), and emits `WorkflowRunPaused`.
- **`record_usage_in_uow(...)`** (`app/application/use_cases/usage/usage_recorder_service.py`)
  — a **transaction-participating** helper that runs the account → price →
  idempotent-insert body on an **already-open** UoW **without committing**, so the
  runner records usage inside its own transaction (Q5). `UsageRecorderService.record`
  is refactored to wrap it (open → helper → commit) — the α7.5 public API is
  **unchanged**.
- **`WorkflowRunPaused` event** (`app/application/use_cases/workflow/_events.py`) —
  the single new event (Q8), carrying `step_index` + `provider_job_id` so the α8.3
  completion service can resume under the same `request_id`. No usage for a pause
  (Q6 — terminal-only).
- **`mark_run_paused` CAS** — `IWorkflowRunRepository.mark_run_paused` +
  its SQLAlchemy impl: a status-guarded `running → paused` that leaves `finished_at`
  **unset** (`paused` is not terminal). Mirrored in the test fakes.
- **DI wiring** (`app/core/container.py`) — `get_advance_workflow_run_use_case()`
  injects `dispatcher=get_step_command_dispatcher()`; the integration test UoW
  (`tests/integration/conftest.py`) wires `usage` + `model_pricing` for parity.
- **Docs** — this CHANGELOG, the α7.6 pre-flight sign-off, the content-generation
  pipeline note (§13 row + change log), and an ADR-0041 change-log line.
- **Tests** — unit (`test_advance_workflow_run_pipeline.py`, a `_ScriptedDispatcher`
  fake: image success + opaque checkpoint + priced usage; deterministic `request_id`
  + replay idempotency; video pause on `IN_PROGRESS` — `PAUSED` + event + no usage;
  provider `FAILED` — records usage then fails; transient retry with stable
  `request_id`; `model_id` fail-fast before dispatch; α7.2 backward-compat with a
  wired dispatcher) and integration (`test_first_pipeline_e2e.py`, the real runner +
  registry + `StepCommandDispatcher` + mocks + recorder on live SQL: image pipeline
  to `succeeded` with a priced `usage_records` row + verbatim opaque checkpoint +
  the started→completed×2→succeeded outbox chain; video pipeline to `paused` with
  the resume checkpoint, `WorkflowRunPaused`, and **no** usage).

#### Version
- App version bumped to **`0.4.20-phase3-alpha7.6`** (staying on `0.4.x`; still
  Phase-3 orchestration infrastructure).

### Phase 3 Slice α7.5 — Usage Recorder (priced, idempotent `usage_records` seam) (2026-07-18)

Activates the persistence that already exists (`usage_records`, `ai_model_pricing`)
by adding the **producer** ADR-0033 assumed but Phase 2 never built: a
`UsageRecorderService` that turns **one terminal provider call** into **exactly one**
immutable, priced `usage_records` row (ADR-0019, partitioned monthly by
`occurred_at`), **idempotent on `request_id`** (ADR-0033), priced against
`ai_model_pricing` (CR-11). This is **ADR-0041 D13**'s usage half. **Zero
migration** — every table, enum, and the per-partition `uq_<child>_request_id`
index (migration `0007`) already exist. Shipped as an explicit **seam** (Q2):
nothing is wired into the runner/dispatcher — the α7.6 pipeline calls
`record(...)` around the dispatch. **Explicitly forbidden and absent:** HTTP,
provider SDKs, Redis, Celery, polling, webhooks, event publishing (Q8 — no
`UsageRecorded` event, no consumer exists), and the `credit_ledger` debit (Q1 —
`credits_consumed` stays `0`; the append-only financial ledger is its own later
slice). **W7.5.1 — the recorder is purely observational:** its only write is
`usage_records`; it never mutates `WorkflowRun` / `WorkflowStep` / `RenderJob` /
`Media` / `Timeline` / `Project` / `ProviderSetting` (it holds only the `usage` +
`model_pricing` repos). See `docs/engineering/PHASE3_ALPHA7_5_PREFLIGHT.md`,
**ADR-0033**, and **ADR-0041**.

#### Added
- **Usage Recorder port + DTOs** (`app/application/interfaces/usage_recorder.py`) —
  the `UsageRecorderPort` (`record(RecordUsageCommand) -> UsageRecordView`), the
  `PricingUnit` / `UsageStatus` vocabularies (mirroring the DB enums without
  importing them), the **`RecordUsageCommand`** application contract (Q3 — richer
  than α7.4's `ProviderResponse`: `tenant_id` / `model_id` / `capability` / usage +
  workflow/render/project linkage, incl. `render_job_id` which has **no** column
  and rides in `extra`), the neutral `NewUsageRecord` (insert payload) /
  `UsageRecordRow` (read-model) / `EffectivePrice` (resolved price) DTOs, and the
  `DuplicateRequestIdError` replay signal. **No SQLAlchemy import** — neutral, like
  `OutboxEvent`.
- **Pure accounting/pricing policy** (`app/application/use_cases/usage/accounting.py`)
  — side-effect-free `account(command)` (maps `ProviderUsage` onto the typed
  `usage_records` axes **by capability** — D3.4 — and derives the **primary billing
  axis**: LLM→`completion_token`, image→`image`, video→`video_second`,
  voice→`audio_second`) + `price(accounting, prices)` (Q4 — `estimated_cost =
  Σ(unit_price × quantity)` over line items; a unit with no price contributes 0 and
  is reported). Tolerant of both the minimal α7.4 mock usage and a richer `detail`
  breakdown, so real α8.x providers need no recorder change.
- **`UsageRecorderService`** (`app/application/use_cases/usage/usage_recorder_service.py`)
  — account → price → assemble → **idempotent insert** in one transaction over one
  row. Terminal-only (Q6 — `IN_PROGRESS` is rejected with a `ValueError`; the α8.3
  completion service records the terminal outcome later under the same
  `request_id`). Missing pricing **never blocks** (Q5 — prices affected units at 0,
  leaves `pricing_id` NULL, emits a `WARN`). A colliding `request_id` (Q7) is
  recovered by returning the pre-existing row (`idempotent_replay=True`, `INFO`
  log). `credits_consumed` stays `0` (Q1).
- **Repository ports** (`IUsageRecordRepository` — `insert` (raises
  `DuplicateRequestIdError`) + `get_by_request_id`; `IModelPricingRepository` —
  read-only `get_effective(model_id, unit, at)`) and their SQLAlchemy impls
  (`app/infrastructure/repositories/usage_record_repository.py`,
  `model_pricing_repository.py`). The insert runs inside a **SAVEPOINT**
  (`begin_nested`) so an ADR-0033 unique-violation rolls back only the failed insert
  — the caller's transaction survives for the recovery SELECT. A `NULL` `request_id`
  never collides (the ADR-0033 index is partial: `WHERE request_id IS NOT NULL`).
  Pricing resolution is effective-at-time (`effective_from <= at < effective_to`,
  newest window wins).
- **DI wiring** — `IUnitOfWork` gains **`usage`** + **`model_pricing`**; the
  SQLAlchemy UoW instantiates both in `__aenter__`; `get_usage_recorder_service()`
  factory added to the container. The test UoW + fakes mirror it
  (`FakeUsageRecordRepository` — in-memory idempotent insert/replay;
  `FakeModelPricingRepository`).
- **Docs** — this CHANGELOG, the α7.5 pre-flight, the content-generation pipeline
  note, and an ADR-0041 change-log line recording that D13's usage half now has a
  concrete producer.
- **Tests** — unit (accounting per capability: LLM explicit-split / single-quantity
  fallback / image / video / voice / no-usage; pricing Σ + missing-price 0; service
  terminal success, `IN_PROGRESS` reject, failed-no-usage, missing-pricing WARN,
  idempotent replay, observational — no aggregate repo touched) and integration
  (partitioned insert + read-back, effective-at-time pricing resolution incl.
  unpriced→None, duplicate `request_id` → `DuplicateRequestIdError` with the original
  surviving, `NULL` `request_id` coexistence, and the full service pricing +
  idempotent-replay path on real rows).

#### Version
- App version bumped to **`0.4.19-phase3-alpha7.5-dev`** (staying on `0.4.x`; still
  Phase-3 orchestration infrastructure).

### Phase 3 Slice α7.4 — Provider Skeleton (capability ports · registry · dispatcher · mock providers) (2026-07-17)

Establishes the **provider abstraction layer** every real provider (α8.x) plugs
into — and **nothing else**: four async capability ports (LLM / Image / Video /
Voice), a framework-free **registry** with explicit registration + capability
discovery, a **`StepCommandDispatcher`** that turns α7.2's inert `StepCommand`s
into capability calls through a closed mapping table (ADR-0041 D4), a typed
provider-error hierarchy, immutable per-provider metadata, and **one deterministic
mock per capability**. It is **pure ports + an imperative shell over mocks** — the
slice is *almost entirely architecture*. **Explicitly forbidden and absent:** HTTP
clients (aiohttp/requests), API keys, external calls, Redis, Celery, retries,
provider fallback/weighting/priority/health-ordering, usage accounting, event
publishing, polling loops, webhook handlers. **Zero migration.** The α7.2 runner is
**untouched** (it still ignores `StepResult.commands`); wiring the dispatcher into
the runner is α7.6. See `docs/engineering/PHASE3_ALPHA7_4_PREFLIGHT.md` and
**ADR-0041**.

#### Added
- **Neutral provider contract** (`app/application/interfaces/providers.py`) — the
  `Capability` / `ProviderStatus` enums, the `ProviderUsage` (α7.5 seam) /
  `ProviderHealth` / **`ProviderMetadata`** DTOs, the shared `ProviderResponse`
  envelope + per-capability immutable request/response pairs
  (`GenerateText*` / `GenerateImage*` / `GenerateVideo*` / `GenerateSpeech*`), and
  the typed error hierarchy — `ProviderError` (base, `transient` classification) →
  `ProviderUnavailable` / `ProviderRateLimited` / `ProviderTimeout` (transient),
  `ProviderAuthenticationError` / `ProviderValidationError` (terminal), and
  `NoProviderAvailable` (registry exhaustion — plays ADR-0041's `NoHealthyProvider`
  role; fallback/health-ordering deferred). **No HTTP mapping.**
- **`ProviderDispatcherPort`** (`app/application/interfaces/provider_dispatcher.py`)
  — the runner-facing port (`dispatch(StepCommand) -> ProviderResponse` plus
  `supports` / `list_capabilities` discovery). Split from the DTO module so it can
  reference `StepCommand` without the provider leaf transitively importing the
  workflow domain.
- **Capability ports** (`app/infrastructure/ai/providers/ports.py`) — the
  `Provider` base + `LLMProvider` / `ImageProvider` / `VideoProvider` /
  `VoiceProvider` `Protocol`s (each `metadata` + async generate + `health`).
- **Deterministic mocks** (`app/infrastructure/ai/providers/mocks/`) — one per
  capability, byte-reproducible, no I/O. LLM/image/voice return `SUCCEEDED` inline;
  **video models the async path** (`IN_PROGRESS` + a deterministic `provider_job_id`)
  so the completion shape (α8.3) is exercised before any real async provider.
- **`ProviderRegistry`** (`app/infrastructure/ai/providers/registry.py`) — explicit
  `register(provider=…, capabilities=[…])` (no decorators), `resolve` →
  `NoProviderAvailable`, and capability discovery (`supports` / `has_provider` /
  `list_capabilities` / `list_providers`). `default_registry()` wires the four
  mocks; `PROVIDER_REGISTRY` is the process singleton.
- **`StepCommandDispatcher`** (`app/infrastructure/ai/dispatcher.py`) — the closed
  `kind` → capability table for the **four** provider capabilities only
  (`generate_text` / `generate_image` / `generate_video` / `synthesize_voice`);
  `start_render` and render/export/storage are excluded (`ProviderValidationError`).
  A missing `request_id` is a terminal `ProviderValidationError`. Discovery delegates
  to the registry. Lives **above** the leaf so the leaf stays orchestration-free.
- **`IProviderSettingsRepository`** (read-only) +
  **`ProviderSettingsRepository`** — minimal `get_value(provider, key, tenant_id)`
  with **tenant-shadows-global** precedence over `provider_settings` (the config
  read seam; no fallback/priority/weighting — Q4). Wired onto the UoW.
- **`import-linter` contract** — `app.infrastructure.ai.providers` is a **strict
  leaf**: forbidden from importing `app.application.use_cases`, `app.api`, or the
  workflow domain (it depends only on the neutral contract in
  `app.application.interfaces`, the same direction every repository uses).
- **DI wiring** — `get_provider_registry()` (the singleton) and
  `get_step_command_dispatcher()` factories; `IUnitOfWork` gains
  **`provider_settings`**; the test UoW + fakes mirror it
  (`FakeProviderSettingsRepository`).
- **Docs** — this CHANGELOG, the α7.4 pre-flight, ROADMAP, architecture notes, and
  an ADR-0041 change-log line recording the port-placement refinement.
- **Tests** — unit (neutral contract immutability + error taxonomy; each mock incl.
  the video async path + reproducibility; registry resolution/discovery/idempotence;
  dispatcher routing of all four kinds, excluded-kind + missing-`request_id` errors,
  `NoProviderAvailable` propagation, discovery delegation) and integration
  (`provider_settings` global read, tenant shadow, tenant→global fallback, per-key
  isolation).

#### Version
- App version bumped to **`0.4.18-phase3-alpha7.4-dev`** (staying on `0.4.x`; still
  Phase-3 orchestration infrastructure).

### Phase 3 Slice α7.3 — Outbox Relay + Distributed Lock Manager (pure infrastructure) (2026-07-17)

Adds the two pieces of **execution-substrate plumbing** every later slice depends
on, and nothing else: a **library-only outbox relay** that drains the
`event_outbox` (written transactionally by α7.1/α7.2) through a **`PublisherPort`**,
and a **distributed lock manager** over the baseline `distributed_locks` table
(**no migration** — the table, its `lock_key` PK, and the `lease_until > acquired_at`
CHECK all already exist). **No worker, no daemon, no CLI, no HTTP, no broker, no
Celery, no Redis** — the relay and the janitor are plain `async` methods a caller
invokes; the worker loop that calls them on a timer is α8.1. **No `event_log`
projection** (the default publisher is a synchronous in-process sink that fans out
to registered handlers; the immutable/partitioned `event_log` becomes an explicit
projection only when a consumer needs it). Poison events are **parked in-place**
(`attempts += 1`, `last_error`, `published_at` stays `NULL`) and the fetch query
ignores rows at/over `max_attempts` — **no DLQ table, no retry scheduler**.
Distributed locks are **owner-fenced** (`renew`/`release` require the owning
lease), `acquire` **steals expired leases and never active ones**, and correctness
comes from **steal-after-expiry**, not the explicit `reclaim_expired()`
maintenance sweep (ADR-0032). See `docs/engineering/PHASE3_ALPHA7_3_PREFLIGHT.md`
and **ADR-0032** (locks) / **ADR-0041** (the provider-runtime blueprint that will
consume both).

#### Added
- **`PublisherPort`** (`app/application/interfaces/publisher.py`) — the publish
  abstraction plus the immutable **`OutboxEvent`** DTO (id, aggregate, event type,
  version, payload, metadata, `occurred_at`, `attempts`) and the async
  **`EventHandler`** protocol. Default impl **`InProcessPublisher`**
  (`app/infrastructure/publisher/in_process_publisher.py`) — a synchronous
  in-process sink that awaits each registered handler in order; any handler raising
  fails the publish (the relay then parks the row).
- **`RelayService`** (`app/application/use_cases/relay/relay_service.py`) —
  `relay_once(batch_size=None) -> RelayResult` and `reclaim_expired(now=None) -> int`.
  One transaction per pass: `fetch_unpublished` → publish each → `mark_published`
  on success / `mark_failed` (park) on error → commit. Returns a **`RelayResult`**
  (`fetched`, `published`, `failed`, `parked`) for trivial testing/logging/metrics.
  Defaults `batch_size=100`, `max_attempts=10`. Every parked event emits an
  **`ERROR`** structured log (`outbox.publish_failed`) carrying event id, aggregate
  id/type, event type, `attempts`, `max_attempts`, `parked`, exception type +
  message; each pass emits an `INFO` `outbox.relay_pass` summary.
- **`IEventOutboxRepository`** extended with the relay read/mark surface —
  `fetch_unpublished(limit, max_attempts)` (`published_at IS NULL AND attempts <
  max_attempts`, ordered `occurred_at, id`, `FOR UPDATE SKIP LOCKED`),
  `mark_published(event_id, published_at)`, `mark_failed(event_id, error)`
  (`attempts += 1`, `last_error`). Implemented in `EventOutboxRepository` (the α7.1
  `add` producer path is unchanged).
- **`IDistributedLockManager`** + **`Lease`** VO
  (`app/application/interfaces/locks.py`) and **`SqlAlchemyDistributedLockManager`**
  (`app/infrastructure/repositories/distributed_lock_manager.py`) — `acquire`
  (atomic `INSERT … ON CONFLICT DO UPDATE … WHERE lease_until < now()` — free or
  expired only), owner-fenced `renew`/`release`, and `reclaim_expired(now=None)`
  (`DELETE … WHERE lease_until < now()`, returns the count). All clock arithmetic
  uses the DB `now()` so leases are wall-clock-agnostic.
- **DI wiring** — `IUnitOfWork` gains **`locks`** (and the extended `outbox`
  surface); `SqlAlchemyUnitOfWork` exposes `SqlAlchemyDistributedLockManager`; the
  container adds an `InProcessPublisher` singleton + a `RelayService` factory. The
  test UoW and fakes mirror both (`FakeDistributedLockManager`, the relay methods
  on `FakeEventOutboxRepository`).
- **Docs** — this CHANGELOG, the α7.3 pre-flight, ROADMAP, and architecture notes.
- **Tests** — unit (`InProcessPublisher` order/propagation; `RelayService` happy
  path, empty batch, transient failure, park-at-cap + log assertion, parked-row
  exclusion, `batch_size` override, chronological order) and integration
  (lock acquire/steal-expired/never-steal-active, owner-fenced renew/release,
  `reclaim_expired`, the `lease_until > acquired_at` CHECK; relay
  `fetch_unpublished`/`mark_published`/`mark_failed`, ordering, `max_attempts`
  exclusion, and `FOR UPDATE SKIP LOCKED` disjoint claims across two connections).

#### Version
- App version bumped to **`0.4.17-phase3-alpha7.3-dev`** (staying on `0.4.x`; still
  Phase-3 orchestration infrastructure — `0.5.0` reserved for a product-level
  milestone).

### Phase 3 Slice α7.2 — WorkflowRun Aggregate + Deterministic Runner (the first sequencing orchestration slice) (2026-07-16)

Introduces the **WorkflowRun aggregate** and a **synchronous, deterministic
runner** — the record of one workflow execution and the orchestration graph
beneath it. It is the project's **second** orchestration aggregate (after α7.1's
`RenderJob`) and the first that **sequences** work: it owns an ordered graph of
`WorkflowStep` children and **append-only** `WorkflowCheckpoint` children. Backed
by the existing baseline `workflow_runs` / `workflow_steps` / `workflow_checkpoints`
tables (**no migration** — the tables, the `workflow_status` / `step_status` ENUMs,
the per-project `idempotency_key` unique, the per-run `step_index` unique, and the
append-only checkpoint trigger all already exist). WorkflowRun uses **status-guarded
CAS** — `workflow_runs` / `workflow_steps` carry **no `version` column** (not in
`_VERSION_BUMP_TABLES`), a **deliberate divergence** from `RenderJob`'s self-versioned
OCC forced by the baseline schema (ADR-0040 D2). It owns **only orchestration/graph
state** and never mutates `projects.version` / `RenderJob` / `MediaAsset` / `Timeline`
(pre-flight D3.10); it coordinates **only** through domain events on the `event_outbox`
(D9). Step handlers are **pure, deterministic, side-effect-free** — a step returns a
command/result *describing* what should happen, and the runner (the imperative shell)
interprets it (D3.11), keeping the eventual move to an async worker an execution
concern, not a domain rewrite. **No worker, no providers, no scheduler in this
slice** — pause/resume (`paused`), `StepCommand` dispatch, render-producing steps
(`render_jobs.workflow_run_id`), backoff, and the `workflow_run:{id}` lock are α8.x.
See `docs/engineering/PHASE3_ALPHA7_2_PREFLIGHT.md`,
`docs/domain/WORKFLOW_RUN_AGGREGATE.md`, and **ADR-0040**.

#### Added
- **`POST /api/v1/projects/{project_id}/workflow-runs`** — queue a run. Body
  `{ workflow_key, workflow_version, input_snapshot?, idempotency_key? }`
  (`extra="forbid"`; `input_snapshot` defaults `{}`). `workflow_key@workflow_version`
  is resolved against the **in-code registry before any DB work** — an unknown pair →
  **`422`** (the project IS visible, so not `404`). Seeds ordered `pending` steps from
  the definition. Returns `201` + `WorkflowRunPublic` (`status='queued'`) and emits
  **`WorkflowRunCreated`** to the `event_outbox`. **Idempotent (Q7):** a repeat with
  the same `idempotency_key` for the project returns the **existing** run with **`200`**
  (no duplicate, no second event). Missing/foreign project → `404`; unauthenticated →
  `401`.
- **`GET  …/workflow-runs`** — the project's runs **newest-first** (`created_at` DESC,
  `id` DESC tiebreak) as summaries; optional **`?status=`** filters by one
  `workflow_status` (bad enum → `422`). Missing/foreign project → `404`.
- **`GET  …/workflow-runs/{workflow_run_id}`** — one run with its ordered `steps` and
  `latest_checkpoint`. Two-level gate (project → run); unknown, or under another
  owner's project → `404` (anti-enumeration).
- **`POST …/workflow-runs/{workflow_run_id}/advance`** — **no body**. Runs the
  deterministic runner to a terminal state (resume-safe: already-`succeeded`/`skipped`
  steps skipped, threading their checkpoint forward). `404` (project/run not visible);
  **`409`** if already terminal; otherwise **`200`** + `WorkflowRunPublic` (`succeeded`
  or `failed`). Emits **`WorkflowRunStarted`** (first `queued → running`), one
  **`WorkflowStepCompleted`** per step, and a terminal **`WorkflowRunSucceeded`** /
  **`WorkflowRunFailed`**.
- **`POST …/workflow-runs/{workflow_run_id}/cancel`** — **no body**. Status-guarded CAS
  (`status IN ('queued','running','paused')` in the WHERE), decided **404 → classify**:
  already `canceled` → **`200`** no-op (no event); `succeeded`/`failed` → **`409`**;
  cancelable → **`200`** + `WorkflowRunPublic` (`status='canceled'`) and emits
  **`WorkflowRunCanceled`**. **No `?version=`, no `412`, no `DELETE`** (no OCC token;
  runs are audit records — no `deleted_at`).
- **Domain** `app/domain/workflow/` — frozen `WorkflowRun` / `WorkflowStep` /
  `WorkflowCheckpoint`; the `WorkflowRunStatus` (`queued, running, paused, succeeded,
  failed, canceled`; `is_terminal` / `is_cancelable` / `is_advanceable`) and
  `WorkflowStepStatus` (`pending, running, succeeded, failed, skipped, retrying`;
  `is_terminal` / `is_done` / `is_runnable`) `StrEnum`s; and the **in-code registry**
  (`registry.py`) — the pure `StepHandler` protocol, `StepContext` / `StepResult` /
  `StepCommand` / `StepOutcome` contracts (D3.11), and four provider-free workflows
  (`noop-chain`, `retry-succeed`, `terminal-fail`, `retry-exhaust`).
- **`IWorkflowRunRepository`** + `WorkflowRunRepository` — `add` (idempotency
  pre-check + unique-violation backstop), `seed_steps`, `get_by_project_and_key`,
  `list_by_project` (status filter), `get_owned`, `list_steps`, `latest_checkpoint`,
  the status-guarded run transitions (`mark_run_running` / `mark_run_succeeded` /
  `mark_run_failed` / `cancel`), the step transitions (`mark_step_running` /
  `mark_step_succeeded` / `mark_step_retrying` / `mark_step_failed`, with a DB-side
  `retries` increment), and append-only `append_checkpoint`. Wired into `IUnitOfWork` /
  `SqlAlchemyUnitOfWork` and mirrored on the test UoW + fakes
  (`FakeWorkflowRunRepository`).
- **Use cases** `app/application/use_cases/workflow/` — `CreateWorkflowRun`,
  `ListWorkflowRuns`, `GetWorkflowRun`, `CancelWorkflowRun`, and the runner
  `AdvanceWorkflowRun`; `_view.py` (the shared `WorkflowRunView` read-model) and
  `_events.py` (emits the six `WorkflowRun*` events — orchestration-only payloads,
  `event_version="1.0"`). None call `IProjectRepository.touch_version`.
- **DTOs** `app/api/v1/schemas/workflow.py` — `WorkflowRunCreateRequest`
  (`extra="forbid"`), `WorkflowStepPublic`, `WorkflowCheckpointPublic`,
  `WorkflowRunSummary` (list), `WorkflowRunPublic` (detail; **no `version` field**).
- **Router** `app/api/v1/routers/workflow_runs.py` (registered in `main.py`); DI
  factories in `core/container.py` (wired with the `WORKFLOW_REGISTRY`) and `deps.py`
  aliases.
- **Docs** — `docs/domain/WORKFLOW_RUN_AGGREGATE.md`, **ADR-0040**, this CHANGELOG, the
  α7.2 pre-flight, `API_CONTRACT.md` §2 (Resource Map) + §3.2.6, and ROADMAP.
- **Tests** — unit (`create`/`list`/`get`/`cancel`/`advance`: happy paths, field +
  input-snapshot persistence, idempotent replay + distinct keys, status filter +
  cross-project isolation, cancel terminal/re-cancel, the runner's `noop-chain`
  success / `retry-succeed` retry accounting / `terminal-fail` / `retry-exhaust` /
  already-terminal `409` / resume of a partial run, event shapes) and integration
  (API happy/`401`/`404`/`422`/`409` + cross-owner isolation across all five verbs;
  repository `add`/dupe/`get_by_project_and_key`/`seed_steps`/`list_by_project`/
  `get_owned`/run + step CAS chains/retry accounting/append + latest checkpoint).

#### Version
- App version bumped to **`0.4.16-phase3-alpha7.2-dev`** (staying on `0.4.x`; still
  Phase-3 orchestration infrastructure — `0.5.0` reserved for a product-level
  milestone, per pre-flight Q9).

### Phase 3 Slice α7.1 — RenderJob Aggregate (the first orchestration slice) (2026-07-16)

Introduces the **RenderJob aggregate** — the request to render a project's
timeline and the record of that request's lifecycle. It is the project's first
**orchestration** aggregate (contrast the α5–α6 domain-model aggregates: Project,
Scene, Prompt, Media, Timeline). Backed by the existing baseline `render_jobs`
table (**no migration** — the table, its `version`/OCC trigger, `render_status`
ENUM, `queue`/`priority`/`progress` columns, and the per-project
`idempotency_key` unique all already exist). RenderJob is **self-versioned**
(`render_jobs.version` is its **own** OCC token — like `projects` / `media_assets`,
**not** the timeline's borrowed token; ADR-0039, adopts ADR-0037) and owns **only
orchestration metadata** — it does **not** own rendered/exported files, workflow
state, or timeline edits (pre-flight D3.10). It coordinates **only** through
domain events on the `event_outbox` (D9). **No render worker in this slice** —
`queued → running → {succeeded, failed}`, the distributed lock (`render_job:{id}`,
ADR-0032), and the worker-owned fields (`output_media_asset_id`, `started_at`,
`finished_at`, `error`, `progress` beyond `'0.00'`) are α8.x. Release/Draft
binding is the **worker's** decision (α7.1 persists no `mode`/`project_version_id`
— pre-flight Q1). See `docs/engineering/PHASE3_ALPHA7_1_PREFLIGHT.md`,
`docs/domain/RENDER_JOB_AGGREGATE.md`, and **ADR-0039**.

#### Added
- **`POST /api/v1/projects/{project_id}/render-jobs`** — enqueue a render. Body
  `{ pipeline?, pipeline_version?, queue?, priority?, idempotency_key? }`; defaults
  `pipeline='ffmpeg'`, `pipeline_version='0.0.0'` (Q2), `queue='normal'`,
  `priority=0` (clamped `0–1000`). The **timeline is resolved server-side** (1:1
  with the project) — a project with **no timeline → `422`** (visible but not
  fulfillable). Returns `201` + `RenderJobPublic` (`version=1`, `status='queued'`,
  `progress='0.00'`) and emits **`RenderJobCreated`** to the `event_outbox`.
  **Idempotent (Q4):** a repeat with the same `idempotency_key` for the project
  returns the **existing** job with **`200`** (no duplicate, no second event).
  Missing/foreign project → `404`; unauthenticated → `401`.
- **`GET  …/render-jobs`** — the project's jobs **newest-first** (`created_at`
  DESC, `id` DESC tiebreak); optional **`?status=`** filters by one `render_status`
  (bad enum → `422`). Missing/foreign project → `404`.
- **`GET  …/render-jobs/{render_job_id}`** — one job. Two-level gate (project →
  render-job); unknown, or under another owner's project → `404` (anti-enumeration).
- **`POST …/render-jobs/{render_job_id}/cancel`** — required `{ version }` (the
  job's own token). Version-fenced CAS with a **race-safe terminal guard**
  (`status IN ('queued','running')` in the WHERE), decided **404 → classify →
  412**: already `canceled` → **`200`** no-op (no event); `succeeded`/`failed` →
  **`409`**; cancelable but stale → **`412`**; success → **`200`** +
  `RenderJobPublic` (`status='canceled'`, `version` +1) and emits
  **`RenderJobCanceled`**. **No `DELETE` verb** (jobs are audit records — no
  `deleted_at`).
- **Domain** `app/domain/render/` — frozen `RenderJob` and the `RenderStatus`
  `StrEnum` (`queued, running, succeeded, failed, canceled`; `is_terminal` /
  `is_cancelable`).
- **`IRenderJobRepository`** + `RenderJobRepository` — `add` (idempotency
  pre-check + unique-violation backstop), `get_by_project_and_key`,
  `list_by_project` (status filter), `get_owned`, `cancel` (version-fenced CAS).
  **`IEventOutboxRepository`** + `EventOutboxRepository` — `add` (append to the
  outbox in the same UoW txn). Both wired into `IUnitOfWork` /
  `SqlAlchemyUnitOfWork` and mirrored on the test UoW + fakes
  (`FakeRenderJobRepository`, `FakeEventOutboxRepository`).
- **Use cases** `app/application/use_cases/render/` — `CreateRenderJob`,
  `ListRenderJobs`, `GetRenderJob`, `CancelRenderJob`; `_events.py` emits
  `RenderJobCreated` / `RenderJobCanceled` (orchestration-only payloads,
  `event_version="1.0"`). None call `IProjectRepository.touch_version`.
- **DTOs** `app/api/v1/schemas/render.py` — `RenderJobCreateRequest`,
  `RenderJobCancelRequest` (`extra="forbid"`, `priority` clamp), `RenderJobPublic`.
- **Router** `app/api/v1/routers/render_jobs.py` (registered in `main.py`); DI
  factories in `core/container.py` and `deps.py` aliases.
- **Docs** — `docs/domain/RENDER_JOB_AGGREGATE.md`, **ADR-0039**, this CHANGELOG,
  the α7.1 pre-flight, `API_CONTRACT.md` §2 (Resource Map) + §3.2.5, and ROADMAP.
- **Tests** — unit (`create`/`list`/`get`/`cancel`: happy paths, field
  persistence, idempotent replay + distinct keys, status filter + isolation,
  cancel OCC/terminal/re-cancel/stale, event shapes) and integration
  (API happy/`401`/`404`/`422`/`409`/`412` + cross-owner isolation; repository
  `add`/dupe/`get_by_project_and_key`/`list_by_project`/`get_owned`/`cancel` +
  outbox persistence).

#### Version
- App version bumped to **`0.4.15-phase3-alpha7.1-dev`** (staying on `0.4.x`;
  `0.5.0` reserved for a product-level milestone — end-to-end render/export — per
  pre-flight Q7).

### Phase 3 Slice α6.3b — Timeline Aggregate (clips) (2026-07-14)

Completes the **Timeline aggregate** (Timeline → Tracks → **Clips**) by placing
registered media (α6.2) onto tracks (α6.3a) as time-bounded **clips**, backed by
the existing baseline `clips` table (no migration — the table, its FKs, and the
`start_seconds` / `end_seconds` / `volume` CHECKs already exist). Clips are pure
**children of the Timeline aggregate** (α6.3 pre-flight Q13, **ADR-0038**): they
carry **no `version`** column, so **`timelines.version` remains the single OCC
token** for the whole tree. A clip write fences on / bumps `timelines.version`
and never touches `projects.version` (adopts ADR-0035). `track_id` is
**immutable** in this slice (α6.3b pre-flight Q4 — a cross-track move is a
delete + recreate); `effects` is **read-only** (write path deferred to α6.4,
Q1); clip **overlaps are allowed** (α6.3 Q6); timeline `duration_seconds` stays
**client-controlled** (no auto-growth from clips, Q5). See
`docs/engineering/PHASE3_ALPHA6_3B_PREFLIGHT.md`.

#### Added
- **`POST /api/v1/projects/{project_id}/timeline/tracks/{track_id}/clips`** —
  append a clip. Body `{ media_asset_id?, start_seconds, end_seconds,
  source_start_seconds?, source_end_seconds?, volume?, locked?, version? }`;
  `end_seconds > start_seconds` and `source_end_seconds ≥ source_start_seconds`
  (else `422`); `volume` `0–4`. `media_asset_id`, when set, must reference a
  **live media asset you own** (else `422`, Q… link validation). `version` is
  **optional** (a child create cannot be harmfully stale): omitted → bumps
  `timelines.version` unconditionally; supplied → fence (stale → `412`). Returns
  `201` + `ClipPublic` (**no `version`**) with the token in `meta.timeline_version`.
  Unknown project / timeline / track → `404`.
- **`GET  …/tracks/{track_id}/clips`** — the track's live clips ordered by
  `start_seconds` ASC (`id` ASC tiebreak); token in `meta.timeline_version`.
- **`GET  …/tracks/{track_id}/clips/{clip_id}`** — one live clip. Four-level gate
  (project → timeline → track → clip); any miss → `404`; cross-track → `404`.
- **`PATCH …/tracks/{track_id}/clips/{clip_id}`** — required `version` (the
  **timeline's**); body any subset of `{ media_asset_id, start_seconds,
  end_seconds, source_start_seconds, source_end_seconds, volume, locked }`.
  `media_asset_id` re-validated when present (explicit `null` unlinks); the
  **merged** time range is validated against stored values (else `422`) so an
  invalid state never reaches the DB CHECK. Bumps the token; `412` on stale;
  `200` no-op on same-value; empty patch → `422`. 404-before-412.
- **`DELETE …/tracks/{track_id}/clips/{clip_id}?version=<n>`** — required
  `?version=`; soft-deletes, bumps the token; `204`. **Idempotent-by-404**
  (repeat delete → `404`, not `412`, Q3).
- **Domain** `app/domain/timeline/clip.py` (frozen `Clip`, **no** `version`;
  `effects: list[Any]` read-only).
- **`ITimelineRepository`** + `TimelineRepository` — `add_clip`, `list_clips`,
  `list_clips_for_timeline` (grouped by `track_id` for composition reads),
  `get_clip`, `update_clip`, `soft_delete_clip`. Mirrored on `FakeTimelineRepository`.
- **Use cases** `app/application/use_cases/timeline/` — `CreateClip`, `ListClips`,
  `GetClip`, `UpdateClip`, `DeleteClip` (+ `ClipResult` / `ClipListResult`, and
  `TimelineResult.clips_by_track`). `_links.validate_clip_media_link` re-uses the
  media aggregate's `get_owned`. None call `IProjectRepository.touch_version`.
- **DTOs** `app/api/v1/schemas/timeline.py` — `ClipCreateRequest`,
  `ClipUpdateRequest`, `ClipPublic` (no `version`; `extra="forbid"`; cross-field
  range checks); `TrackPublic.clips[]` now embeds `ClipPublic` (ordered) in
  composition reads (`GET …/timeline`, `GET …/tracks`); container factories +
  `deps` aliases + 5 nested routes on `routers/timeline.py`.
- **Tests** — unit matrix for the 5 clip use cases (create with optional-fence /
  valid+unknown media / stale→412 / 404s; list ordering + isolation; get 4-level
  gate + cross-track→404; update incl. relink / unlink / merged-range→422 /
  stale→412; delete idempotent-by-404 + 404-before-412); repository integration
  (R11–R15: ordered listing excl. soft-deleted, `id` tiebreak, track isolation,
  real-change update, idempotent soft delete, grouped `list_clips_for_timeline`);
  HTTP integration `test_timeline.py` (A17–A26: 201/200/204/404/412/422,
  media validation, stale fence, composition-tree embedding).

#### Documentation
- `docs/engineering/PHASE3_ALPHA6_3B_PREFLIGHT.md` (new) — the five resolved
  open questions and the approved slice scope.
- `docs/domain/TIMELINE_AGGREGATE.md` — clips documented as the third tier of the
  aggregate (children, no `version`, `media_asset_id` validation, immutable
  `track_id`, read-only `effects`).
- `API_CONTRACT.md` §3.2.4 — the five clip endpoints + `TrackPublic.clips[]`.

#### Version
- `0.4.14-phase3-alpha6.3b-dev`.

### Phase 3 Slice α6.3a — Timeline Aggregate (root + tracks) (2026-07-13)

Introduces the **Timeline aggregate** — the *composition layer* that places
registered media (α6.2) onto ordered **tracks** (α6.3b adds clips) — backed by the
existing baseline `timelines` / `tracks` tables (no migration — tables, the
`uq_timelines_project_id` / `uq_tracks_timeline_id_z_index` partial uniques, and
the `frame_rate` CHECK already exist). The Timeline is a **self-contained
optimistic-concurrency aggregate** (α6.3 pre-flight Q1, **ADR-0038**) — a **third**
posture, distinct from projects+scenes (aggregate OCC *in* the version ledger) and
prompts+media (last-writer-wins, no OCC): the root carries `version` (baseline
`VersionMixin` + guarded bump trigger), its children do **not**, so
**`timelines.version` is the single OCC token for the whole tree** (root + tracks +
clips, Q13). A timeline edit is a composition change — it fences on / bumps
`timelines.version` but does **NOT** bump `projects.version` and is **excluded**
from `project_versions` snapshots / restore / diff (**adopts ADR-0035**).
Endpoints are **project-nested** (Q4); the timeline is created **explicitly** (Q3,
one per project — second → `409`); `z_index` is **client-assigned** and unique per
live timeline (Q5, collision → `409`). See `docs/domain/TIMELINE_AGGREGATE.md`,
**ADR-0038**, and `docs/engineering/PHASE3_ALPHA6_3_PREFLIGHT.md`.

#### Added
- **`POST /api/v1/projects/{project_id}/timeline`** (`CurrentUserDep`) — provision
  the single timeline (explicit, non-lazy). Body `{ aspect_ratio?, frame_rate?,
  background_color? }`; `aspect_ratio` defaults from the project orientation
  (`horizontal→16:9`, `vertical→9:16`, `square→1:1`) when omitted; `frame_rate`
  `1–240`; `background_color` hex. Returns `201` + `TimelinePublic` (`version = 1`,
  `tracks = []`). Second provision → `409 CONFLICT` (`uq_timelines_project_id`
  backstop). Missing/foreign project → `404`.
- **`GET /api/v1/projects/{project_id}/timeline`** — the timeline root + its live
  tracks ordered by `z_index` ASC. Un-provisioned timeline → `404`.
- **`PATCH /api/v1/projects/{project_id}/timeline`** — version-fenced root update.
  Body `{ version, aspect_ratio?, frame_rate?, background_color?,
  duration_seconds? }`; net **+1** on a real change; `412` on stale; `200` no-op on
  same-value; empty patch → `422`. No `projects.version` bump.
- **`POST /api/v1/projects/{project_id}/timeline/tracks`** — append a track. Body
  `{ kind, z_index, name, locked?, muted?, version? }`; `kind` a `track_kind` enum
  (`video/audio/subtitle/effect`); `z_index ≥ 0`, unique per live timeline
  (collision → `409`). `version` is **optional** (a child create cannot be
  harmfully stale — Q13): omitted → bumps `timelines.version` unconditionally;
  supplied → fence (stale → `412`). Returns `201` + `TrackPublic` (**no
  `version`**) with the token in `meta.timeline_version`.
- **`GET /api/v1/projects/{project_id}/timeline/tracks`** — the live tracks
  (`z_index` ASC); token in `meta.timeline_version`.
- **`PATCH /api/v1/projects/{project_id}/timeline/tracks/{track_id}`** — required
  `version` (the **timeline's**); body any subset of `{ kind, z_index, name,
  locked, muted }`; z_index collision → `409`; bumps the token; `412` on stale;
  `200` no-op on same-value; empty patch → `422`. 404-before-412.
- **`DELETE /api/v1/projects/{project_id}/timeline/tracks/{track_id}?version=<n>`**
  — required `?version=`; soft-deletes (frees the `z_index`), bumps the token;
  `204`. **Idempotent-by-404** (repeat delete → `404`, not `412`).
- **Domain** `app/domain/timeline/timeline.py` (frozen `Timeline`, **with**
  `version`), `app/domain/timeline/track.py` (frozen `Track`, **no** `version`).
- **`ITimelineRepository`** + `TimelineRepository` (`add` [unique→`ConflictError`],
  `get_by_project`, `update_owned` [version-fenced CAS, net +1], `bump_version`
  [fenced vs unconditional aggregate roll-up], `add_track` / `list_tracks` /
  `get_track` / `update_track` [z_index→`ConflictError`] / `soft_delete_track`).
  Wired onto the real `UnitOfWork`, the integration `_TestUnitOfWork`, and
  `FakeUnitOfWork` (+ `FakeTimelineRepository`).
- **Use cases** `app/application/use_cases/timeline/` — `ProvisionTimeline`,
  `GetTimeline`, `UpdateTimeline`, `CreateTrack`, `ListTracks`, `UpdateTrack`,
  `DeleteTrack` (+ `TimelineResult` / `TrackResult`). None call
  `IProjectRepository.touch_version`.
- **DTOs** `app/api/v1/schemas/timeline.py` — `TimelineProvisionRequest`,
  `TimelineUpdateRequest`, `TrackCreateRequest`, `TrackUpdateRequest`,
  `TimelinePublic`, `TrackPublic` (no `version`; `extra="forbid"`; empty PATCH →
  `422`); container factories + `deps` aliases + `routers/timeline.py`, mounted in
  `app/main.py`.
- **Tests** — unit matrix for the 7 use cases (provision happy / aspect default /
  second→409 / 404; fenced root PATCH incl. same-value no-op / stale→412; track
  create with optional-fence / z_index→409 / stale→412; update fenced incl.
  404-before-412; delete incl. idempotent-by-404 + z_index slot freeing);
  repository integration (`add`→409, fenced CAS net +1, `bump_version` fenced vs
  unconditional, z_index uniqueness, ordered listing, soft-delete slot reuse); HTTP
  integration `test_timeline.py` (A1–A16 end-to-end: 201/200/204/404/409/412/422/401,
  cross-owner isolation, `meta.timeline_version`, `projects.version` untouched).

#### Documentation
- `API_CONTRACT.md` §2 resource map + new §3.2.4 — timeline documented as shipped
  (project-nested, self-contained OCC via `timelines.version`, `meta.timeline_version`).
- `docs/domain/TIMELINE_AGGREGATE.md` (new) + **ADR-0038** (new, adopts ADR-0035) —
  the composition-layer identity, self-contained OCC aggregate model, explicit
  provision, client-assigned `z_index`, and the exclusion from the project version
  ledger.
- `docs/domain/PROJECT_AGGREGATE.md` §6/§8 — timeline noted as **outside** the
  versioned snapshot boundary; §8 map updated with the α6.3a Timeline composition.

#### Version
- `0.4.13-phase3-alpha6.3a-dev`.

### Phase 3 Slice α6.2 — Media Asset Aggregate (generation-output CRUD) (2026-07-13)

Introduces the **Media Asset aggregate** — the first *generation-output* content
— as a **register-by-metadata** CRUD surface backed by the existing baseline
`media_assets` table (no migration — table + indexes + the `(storage_backend,
storage_bucket, storage_key)` unique constraint already exist). Unlike prompts /
scenes, `media_assets` carries its **own `tenant_id` + `owner_user_id`** (direct
ownership) and only a **nullable `project_id`**, so the endpoints are **top-level
and owner-scoped** (α6.2 pre-flight Q1), not project-nested. α6.2 **registers**
an object the client already holds (`source ∈ {uploaded, stock}`) — it makes
**no** provider call, byte upload, presigned URL, or checksum fetch; AI
generation (`source = generated`) and object storage are later slices (Q2). The
concurrency posture **adopts ADR-0036** (Q3, **ADR-0037**): no `version` column,
no per-row OCC, a `PATCH` is **last-writer-wins** (no `412`), mutations do **not**
bump `projects.version`, and media is **excluded** from `project_versions`
snapshots / restore / diff. Duplicate storage coordinates → `409`; foreign /
unknown optional links → `422`. See `docs/domain/MEDIA_AGGREGATE.md`,
**ADR-0037**, and `docs/engineering/PHASE3_ALPHA6_2_PREFLIGHT.md`.

#### Added
- **`POST /api/v1/media`** (`CurrentUserDep`) — register a media asset
  (metadata only). Body `{ kind, source, storage_backend, storage_bucket,
  storage_key, mime_type, size_bytes, checksum_sha256, project_id?, scene_id?,
  prompt_id?, model_id?, provider?, width?, height?, duration_seconds?,
  source_metadata? }`. `kind` / `storage_backend` validated enums; `source`
  restricted to `uploaded` / `stock` (`generated` → `422`); `checksum_sha256` a
  64-char hex string (→ 32 bytes); `size_bytes ≥ 0`. Each present optional link
  validated for the caller (foreign/unknown `project_id`, `scene_id`/`prompt_id`
  without or outside that project, unknown/retired `model_id` → `422
  VALIDATION_FAILED`, not `404`). Duplicate `(storage_backend, storage_bucket,
  storage_key)` → `409 CONFLICT` (unique-constraint backstop behind a
  pre-check). Identity + ownership server-owned (`extra="forbid"` → `422`).
  Returns `201` + `MediaPublic`.
- **`GET /api/v1/media`** — list the caller's live assets newest-first
  (`created_at` desc, `id` desc) with optional `?kind=<enum>`, `?source=<str>`,
  `?project_id=<uuid>`, `?scene_id=<uuid>` filters (combined = AND; bad enum /
  non-UUID → `422`). Owner-scoped; not paginated. Empty → `200 []`.
- **`GET /api/v1/media/{media_id}`** — one asset (`200`) or the uniform owner
  `404` (unknown / other owner / soft-deleted).
- **`PATCH /api/v1/media/{media_id}`** — narrow, partial, **no version fence**.
  Body = any subset of `{ project_id, scene_id, prompt_id, model_id, provider,
  source_metadata }`; tri-state via `exclude_unset` (explicit `null` clears a
  nullable link, re-validated when non-null → `422`; `source_metadata`
  non-nullable). Physical-object fields immutable (`extra="forbid"` → `422`);
  empty patch → `422`; same-value patch is a `200` no-op. No `projects.version`
  bump.
- **`DELETE /api/v1/media/{media_id}`** — owner-scoped soft delete (`204`), no
  version fence, idempotent-by-404.
- **Domain** `app/domain/media/media_asset.py` — frozen `MediaAsset` entity
  (slim view of the physical row; **no `version` field** by design;
  `checksum_sha256` as `bytes`).
- **`IMediaRepository`** + `MediaRepository` (`add` [unique→`ConflictError`],
  `list_owned` + `kind`/`source`/`project_id`/`scene_id` filters, `get_owned`,
  `update_owned` [no OCC fence], `soft_delete_owned`, `model_is_linkable`) — all
  owner-scoped (tenant + owner_user) + soft-delete excluded. Wired onto the real
  `UnitOfWork`, the integration `_TestUnitOfWork`, and `FakeUnitOfWork`
  (+ `FakeMediaRepository`).
- **Use cases** `app/application/use_cases/media/` — `RegisterMedia`,
  `ListMedia`, `GetMedia`, `UpdateMedia`, `DeleteMedia`, plus a shared
  `_links.validate_media_links` helper (project/scene/prompt/model consistency →
  `422`). Structured logs never carry `storage_key` / `checksum` /
  `source_metadata` values.
- **DTOs** `app/api/v1/schemas/media.py` — `MediaRegisterRequest`,
  `MediaUpdateRequest` (tri-state, `extra="forbid"`, empty-patch → `422`),
  `MediaPublic` (no `version`; `owner_user_id`/`tenant_id`/`deleted_at` omitted;
  `checksum_sha256` emitted as hex); container factories + `deps` aliases +
  `routers/media.py`, mounted in `app/main.py`.
- **Tests** — unit matrix for the 5 use cases (happy / each-link-foreign→422 /
  scene-without-project→422 / unknown-model→422 / duplicate→409 / same-value
  no-op / explicit-null clears / idempotent-by-404); repository integration incl.
  the load-bearing **F5** test (a media asset's `project_id`/`scene_id`/
  `prompt_id` links **survive** a parent *soft-delete* — `ON DELETE SET NULL`
  fires only on a hard delete) + the unique-conflict path; HTTP integration
  `test_media.py` (A1–A15 end-to-end: 201/200/204/404/422/409/401, owner
  isolation, filters, tri-state PATCH, immutable-field rejection,
  idempotent-by-404).

#### Documentation
- `API_CONTRACT.md` §2 resource map + new §3.2.3 — media documented as shipped
  (top-level, owner-scoped, register-by-metadata, no `version`).
- `docs/domain/MEDIA_AGGREGATE.md` (new) + **ADR-0037** (new, adopts ADR-0036) —
  the generation-output identity, direct owner-level ownership, register-by-
  metadata boundary, storage-identity uniqueness, and the no-OCC / no-snapshot
  rationale.
- `docs/domain/PROJECT_AGGREGATE.md` §6/§8 — media noted as **outside** the
  versioned snapshot boundary; §8 map updated with the α6.2 Media output.

#### Version
- `0.4.12-phase3-alpha6.2-dev`.

### Phase 3 Slice α6.1 — Prompt Aggregate (generation-input CRUD) (2026-07-12)

Introduces the **Prompt aggregate** — the first *generation-input* content — as
an owner-scoped CRUD surface nested under a project
(`/projects/{id}/prompts`), backed by the existing baseline `prompts` table
(no migration — table + all three indexes already exist). A prompt is authored
text (`kind` + `text_content`) with an **optional** live-scene link and an
**optional** validated `ai_models` link. The load-bearing decision (α6.1
pre-flight Q1/Q8, **ADR-0036**): the baseline gave `prompts` **no `version`
column** on purpose — prompts are **generation inputs, not versioned editorial
content**. So they take **no per-row OCC**, a `PATCH` is **last-writer-wins**
(no `version` on the wire, no `412`), mutations do **not** bump
`projects.version`, and prompts are **excluded** from `project_versions`
snapshots / restore / diff. The versioned aggregate stays {project root +
scenes}; generated media (α6.2) may later retain the prompt used for provenance
independently of the current prompt record. All endpoints reuse the α5c
patterns: `CurrentUserDep`, owner+tenant scoping via the project gate,
two-level `404`-anti-enumeration, soft-delete idempotent-by-404. See
`docs/domain/PROMPT_AGGREGATE.md`, **ADR-0036**, and
`docs/engineering/PHASE3_ALPHA6_1_PREFLIGHT.md`.

#### Added
- **`POST /api/v1/projects/{project_id}/prompts`** (`CurrentUserDep`) — create
  a prompt. Body `{ kind, text_content, scene_id?, model_id?, extra? }`;
  `kind` validated against the `prompt_kind` enum (8 modality kinds), 
  `text_content` `1 ≤ len ≤ 10000` (stripped). A non-null `scene_id` must be a
  **live scene in the same project** (else `422 VALIDATION_FAILED`, not `404`);
  a non-null `model_id` must be a live, non-`retired` `ai_models` row (else
  `422`). Identity + `generated_by_agent` are server-owned (`extra="forbid"` →
  `422`). `404` if the project is missing / not the caller's. Returns `201` +
  `PromptPublic`.
- **`GET /api/v1/projects/{project_id}/prompts`** — list the project's live
  prompts newest-first (`created_at` desc, `id` desc) with optional
  `?kind=<enum>` and `?scene_id=<uuid>` filters (combined = AND; bad enum /
  non-UUID → `422`). Not paginated. Empty → `200 []`. `404` on unowned project.
- **`GET /api/v1/projects/{project_id}/prompts/{prompt_id}`** — one prompt
  (`200`) or the uniform two-level `404`.
- **`PATCH /api/v1/projects/{project_id}/prompts/{prompt_id}`** — partial,
  content-only, **no version fence**. Body = any subset of `{ text_content,
  kind, model_id, extra }`; tri-state via `exclude_unset` (explicit
  `model_id: null` clears the link; a non-null `model_id` is re-validated →
  `422`; `text_content`/`kind` non-nullable). `scene_id` immutable (not
  accepted, `extra="forbid"` → `422`); empty patch → `422`; same-value patch is
  a `200` no-op. Returns `200` + `PromptPublic`. No `projects.version` bump.
- **`DELETE /api/v1/projects/{project_id}/prompts/{prompt_id}`** — owner-scoped
  soft delete (`204`), no version fence, idempotent-by-404.
- **Domain** `app/domain/prompts/prompt.py` — frozen `Prompt` entity (slim view
  of the physical row; **no `version` field** by design).
- **`IPromptRepository`** + `SqlAlchemyPromptRepository` (`add`, `list_owned`
  + `kind`/`scene_id` filters, `get_owned`, `update_owned`,
  `soft_delete_owned`, `model_is_linkable`) — all project-scoped + soft-delete
  excluded. Wired onto the real `UnitOfWork`, the integration `_TestUnitOfWork`,
  and `FakeUnitOfWork` (+ `FakePromptRepository`).
- **Use cases** `app/application/use_cases/prompts/` — `CreatePrompt`,
  `ListPrompts`, `GetPrompt`, `UpdatePrompt`, `DeletePrompt` (two-level gate,
  scene/model link validation, same-value no-op detection, structured logs
  that never carry `text_content`/`extra` values).
- **DTOs** `app/api/v1/schemas/prompts.py` — `PromptCreateRequest`,
  `PromptUpdateRequest` (tri-state, `extra="forbid"`), `PromptPublic` (no
  `version`; `generated_by_agent`/`deleted_at` omitted); container factories +
  `deps` aliases + `routers/prompts.py`, mounted in `app/main.py`.
- **Tests** — unit matrix for the 5 use cases (happy / scene-link-foreign→422 /
  model-link-unknown→422 / not-owned→404 / filters / same-value no-op /
  explicit-null clears / idempotent-by-404); repository integration incl. the
  load-bearing **F6** test (a prompt's `scene_id` link **survives** a scene
  *soft-delete* — `ON DELETE SET NULL` fires only on a hard delete); HTTP
  integration `test_prompts.py` (A1–A15 end-to-end: 201/200/204/404/422/401,
  two-level 404, filters, tri-state PATCH, idempotent-by-404).

#### Documentation
- `API_CONTRACT.md` §2 + new §3.2.2 — prompts documented as shipped; the
  `/prompts/{id}` stub reconciled to the nested `/projects/{id}/prompts/{id}`
  shape (α6.1 pre-flight Q2).
- `docs/domain/PROMPT_AGGREGATE.md` (new) + **ADR-0036** (new) — the
  generation-input identity, the no-OCC / no-snapshot rationale, and the
  governing principle recorded verbatim.
- `docs/domain/PROJECT_AGGREGATE.md` §6/§8 — prompts noted as **outside** the
  versioned snapshot boundary; §8 map updated with the α6.1 Prompt child.

#### Version
- `0.4.11-phase3-alpha6.1-dev`.

### Phase 3 Slice α5d.3 — Project Version Branch (fork to a new project) (2026-07-12)

Completes the versioning story: a historical snapshot can now be **branched** —
forked into a **new, independently-editable project** (α5d.3 pre-flight Q1
Option A). Unlike restore (which rewinds *this* project onto an old snapshot),
branch leaves the source untouched and spins up a fresh aggregate seeded from
the chosen version's content — the "fork this save into a new project"
operation. This is the only migration-free reading of "branch" that is
genuinely distinct from restore: the schema has a single `current_version_id`
per project and per-project-unique `version_number`, so true in-project
multi-head branches would need a new table (deferred). Provenance is preserved
by a structured `branched_from` block (`{ project_id, version_id,
version_number }` of the source) embedded in the new project's `v1` snapshot
and echoed in the response `meta` — a one-way historical link, not a live
coupling. No migration — `reason=branch` already exists in the enum and the fork
reuses the α5d restore scene-materialization helpers and the guarded version-bump
trigger. See `docs/domain/PROJECT_AGGREGATE.md` §6, **ADR-0035** (D12), and
`docs/engineering/PHASE3_ALPHA5D3_PREFLIGHT.md`.

#### Added
- **`POST /api/v1/projects/{project_id}/versions/{version_id}/branch`**
  (`CurrentUserDep`) — fork a snapshot into a new project. Body is `{ name }`
  (the new project's name; every other root field, including the immutable
  `aspect_ratio`, is inherited from the snapshot; `extra="forbid"` → `422`).
  Two-level `404` gate (source project owned → version belongs to it) runs
  **before** any write (anti-enumeration); a duplicate live project name for the
  caller → `409 CONFLICT`. On success, creates a new caller-owned project,
  materializes the snapshot scenes with **fresh** ids (ordered by
  `scene_number`, full fat columns), captures the new project's `reason=branch`
  `v1` (`parent_version_id` NULL) with a `branched_from` provenance block, and
  advances the new project's `current_version_id` — all in **one transaction**.
  There is **no OCC fence** and **no source `projects.version` bump** (the source
  is not mutated). Returns `201` with the **new project** as `ProjectPublic`
  (its `version` = 2, i.e. created + first capture) plus `meta.branched_from`.
- **`IProjectVersionRepository.branch`** (+ real repo and fake) — inserts the new
  project row (name-collision → `ConflictError` before any child write),
  materializes scenes via the shared restore writer (`_scene_write_values`) with
  fresh ids, captures `v1` via the canonical snapshot builder with an embedded
  `branched_from` block, and advances the new project's pointer (guarded trigger
  bumps its `version` to 2). The source aggregate is provably untouched.
- **`BranchProjectVersion` use case** — runs the source project + version gates,
  delegates to `versions.branch`, and re-raises `ConflictError` (→ `409`).
  DTO `ProjectVersionBranchRequest` (`{ name }`, 1..200 chars, `extra="forbid"`);
  reuses `ProjectPublic` for the response; provenance echoed via a new
  `envelope(..., extra_meta=...)` helper param; container factory + `deps` alias
  + router endpoint.
- **Tests** — 5 branch use-case unit tests (happy fork + provenance, fresh scene
  ids in order, source untouched, duplicate-name `409`, unowned/unknown `404`);
  4 `ProjectVersionRepository.branch` integration tests (fork fidelity incl. fat
  columns + decimal strings + fresh ids, source untouched, one-transaction
  rollback on injected failure, duplicate-name `ConflictError`); 4 HTTP
  integration tests (branch happy — asserting the new project is a first-class
  project via follow-up GET project/scenes/`v1` — plus `422` / `404` / `409`).

#### Documentation
- `API_CONTRACT.md` §3.3 — branch documented as shipped; autosave + field-level
  diff deferred to α5d.4+.
- `PROJECT_AGGREGATE.md` §6/§8 + **ADR-0035** (D12) — branch = fork-to-new-project,
  the `branched_from` lineage/provenance model, and the rejected alternatives
  (in-project multi-head, restore-alias) recorded.

#### Version
- `0.4.10-phase3-alpha5d3-dev`.

### Phase 3 Slice α5d.2 — Project Version Restore + Diff (2026-07-12)

Makes the version ledger *actionable*: a historical snapshot can be **restored**
into live state, and two versions can be **diffed**. Restore never rewrites
history (**ADR-0035** D2) — it appends a new `reason=restore` version parented on
the source and repoints `current_version_id`. The load-bearing decision is the
**Aggregate OCC Rule**: `projects.version` is promoted to the concurrency token
for the *entire* Project aggregate, so a scene mutation now also bumps
`projects.version`. This gives restore a single, honest fence: the token the
caller last observed is invalidated by *any* observable aggregate change
(project column **or** scene edit) since their read. No migration — restore
reuses the α5c project lock and the existing guarded version-bump trigger. See
`docs/domain/PROJECT_AGGREGATE.md` §6, **ADR-0035**, and
`docs/engineering/PHASE3_ALPHA5D2_PREFLIGHT.md`.

#### Added
- **`POST /api/v1/projects/{project_id}/versions/{version_id}/restore`**
  (`CurrentUserDep`) — restore a snapshot into live content. Body is the
  aggregate OCC token `{ version }` (required; `extra="forbid"` → `422`).
  Two-level `404` gate (project owned → version belongs to it) runs **before**
  the fence (anti-enumeration); a stale `projects.version` → `412
  VERSION_CONFLICT` with **zero writes**. On success, appends a
  `reason=restore` version (`parent_version_id` = source), reconciles the live
  scene set to the snapshot (upsert by `id`, soft-delete removed, insert added),
  rewrites the mutable project root, advances `current_version_id`, and bumps
  `projects.version` by **exactly one** — all in **one transaction**. Returns
  `200` with the new head `ProjectVersionDetail`.
- **`GET …/versions/{version_id}/diff?against={base_version_id}`** — a coarse,
  **on-demand** change summary between the `against` base and the `{version_id}`
  target, computed from the two stored snapshots (nothing persisted). Uniform
  `404` if either version is missing / not the caller's; `against` required
  (missing/malformed → `422`). Returns `200` with `ProjectVersionDiff`
  (`base_version_number`, `target_version_number`, `project_changed`,
  `scene_changes` = `added` / `removed` / `modified`).
- **Aggregate OCC Rule** — `IProjectRepository.touch_version` (+ real repo and
  fake) bumps `projects.version` explicitly; wired into all four scene use cases
  (`create` / `update` / `move` / `delete`), guarded so **no-op** edits (an
  update to an identical value, a move that doesn't change order) do **not** bump.
- **`IProjectVersionRepository.restore`** (+ real repo and fake) — project-row
  lock, aggregate OCC fence, source-snapshot load, `aspect_ratio` immutability
  assert, default-storyboard rehome, scene reconcile (blanket soft-delete →
  upsert-by-`id`, reviving soft-deleted rows in place), trailing capture, and a
  single project UPDATE that rewrites the root + advances the pointer + bumps the
  version in one statement.
- **`RestoreProjectVersion` / `DiffProjectVersions` use cases** — restore runs
  the project + source-version gates then delegates to `versions.restore`
  (`None` → `412`); diff is a pure function over the two snapshots (no repo
  method). DTOs `ProjectVersionRestoreRequest` (`{ version }`, `extra="forbid"`)
  and `ProjectVersionDiff` (+ `SceneChangeCounts`); container factories + `deps`
  aliases; two new router endpoints.
- **Tests** — 8 restore/diff use-case unit tests + 6 Aggregate-OCC-bump
  regression tests (`tests/unit/.../versions/`, `tests/unit/.../scenes/`);
  5 `ProjectVersionRepository.restore` integration tests (round-trip fidelity
  incl. fat columns + decimal strings, revive-soft-deleted, stale-fence
  no-write, one-transaction rollback on injected failure, history immutability);
  8 HTTP integration tests (restore happy / 412 / 404 / 422; diff happy / 404 /
  422).

#### Documentation
- `API_CONTRACT.md` §3.3 — restore + diff documented as shipped; branching /
  autosave deferred to α5d.3+.
- `PROJECT_AGGREGATE.md` §6 + **ADR-0035** — Aggregate OCC Rule invariant,
  restore algorithm, and on-demand diff recorded.

#### Version
- `0.4.9-phase3-alpha5d2-dev`.

### Phase 3 Slice α5d.1 — Project Versions (capture / list / get) (2026-07-12)

Establishes the **Project Version** ledger — immutable, append-only content
snapshots of a project plus its ordered scenes. This is the *product* "version
history" feature and the foundation for restore/branch (α5d.2). It is
deliberately distinct from the row-OCC `projects.version` concurrency counter:
the ledger is a user-facing history, the row `version` is a write guard
(**ADR-0035** D1). A capture serializes on the project row (reusing the α5c
lock), assigns a monotonic `version_number`, links a `parent_version_id`
lineage chain, stores a canonical JSONB snapshot, and advances
`projects.current_version_id`. No migration — `project_versions`, its
immutability trigger, and the current pointer all exist in the α1 baseline.
See `docs/domain/PROJECT_AGGREGATE.md` §6, **ADR-0035**, and
`docs/engineering/PHASE3_ALPHA5D_PREFLIGHT.md`.

#### Added
- **`POST /api/v1/projects/{project_id}/versions`** (`CurrentUserDep`) —
  capture an immutable snapshot. `reason` is server-set to `manual_save`
  (α5d.1 takes **no** client input; `extra="forbid"` → `422`). Assigns the
  next `version_number` (1, 2, 3 …) under the project-row lock, links
  `parent_version_id` to the previous current version, advances
  `current_version_id` (bumps the row `version` by one), returns `201` with
  the full `ProjectVersionDetail` (metadata + `snapshot`). An empty project
  is valid (`snapshot.scenes == []`). `404` if the project is missing / not
  the caller's.
- **`GET …/versions`** — the project's version history as **metadata-only**
  `ProjectVersionPublic`, newest-first by `version_number`, un-paginated
  (bounded editorial history). No snapshot bodies. `404` on an unowned
  project.
- **`GET …/versions/{version_id}`** — one version WITH its immutable
  `snapshot` (`ProjectVersionDetail`), addressed by UUID `id` (α5d Q3), or
  the uniform `404` (two-level gate: project owned → version belongs to it).
- **Snapshot boundary** (ADR-0035 D3) — project root fields + capture-time
  `version` + the default storyboard identity + all live scenes ordered by
  `scene_number`, each as its **full physical row** (restore-ready). Canonical
  serialization: leading `schema_version`, `Numeric` durations as lossless
  decimal strings, scene `id`s preserved (α5c Q8). Excludes prompts / media /
  render / timeline / tags / folder (not yet API-managed).
- **`ProjectVersion` + `ProjectVersionSummary` domain entities**
  (`app/domain/versions/`) — frozen; the summary is a metadata-only read model
  so the list never drags snapshots off the DB.
- **`IProjectVersionRepository` + `ProjectVersionRepository`** —
  `create_snapshot` (project-row-locked numbering + lineage + snapshot
  assembly + current-pointer advance), `list_by_project` (metadata columns
  only), `get_owned` (UUID-scoped). Extended on the unit-test
  `FakeProjectVersionRepository` and the integration `_TestUnitOfWork`; `.versions`
  added to the UoW.
- **Three use cases** (`CreateProjectVersion` / `ListProjectVersions` /
  `GetProjectVersion`) — all run the project ownership gate first;
  each pairs its payload with the project's `current_version_id`
  (`VersionResult` / `VersionListResult`) so the router derives the
  `is_current` DTO flag without a second query.
- **DTOs** `ProjectVersionCreateRequest` (empty, `extra="forbid"`) /
  `ProjectVersionPublic` (metadata + derived `is_current`) /
  `ProjectVersionDetail` (+ `snapshot`, `diff_summary`); router
  `app/api/v1/routers/versions.py` mounted at `/api/v1`; container factories +
  `deps` aliases.
- **Tests** — 12 use-case unit tests (`tests/unit/.../versions/`),
  `ProjectVersionRepository` integration tests (numbering + lineage, snapshot
  fidelity incl. ordering / fat fields / decimal strings, DB-enforced
  immutability — direct UPDATE rejected), and 13 HTTP integration tests
  (`tests/integration/api/test_versions.py`).

#### Documentation
- `API_CONTRACT.md` §3.3 (Project Versions) filled in with the shipped
  capture + browse surface.
- New `docs/decisions/ADR-0035-project-version-snapshots.md` (immutable
  ledger, restore-by-new-version, identity preservation, hard-delete
  constraint); `PROJECT_AGGREGATE.md` §6 updated to mark the ledger shipped.

#### Version
- `0.4.8-phase3-alpha5d-dev`.

### Phase 3 Slice α5c — Scenes (create / list / get / patch / move / soft-delete) (2026-07-11)

Establishes the **Scene** aggregate — the first child aggregate under a
project and the first real content-editing workflow. Keeps the α1 baseline
`Project → Storyboard → Scene` schema but hides the intermediary: a single
**implicit default storyboard** is auto-created on the first scene, so the
public API is a flat `…/projects/{id}/scenes` surface (storyboard never on
the wire). Introduces two patterns reused by every future nested resource:
the **two-level visibility gate** (project ownership → scene visibility,
both → uniform `404`) and **project-row-locked ordering** (sparse gap-based
`scene_number` with a transparent full rebalance). See
`docs/domain/SCENE_AGGREGATE.md` and
`docs/engineering/PHASE3_ALPHA5C_PREFLIGHT.md`.

#### Added
- **`POST /api/v1/projects/{project_id}/scenes`** (`CurrentUserDep`) —
  append a scene (`201`, `version=1`, `position`=last). Body `{ title,
  duration_seconds, narration?, subtitle? }` (`extra="forbid"`;
  `duration_seconds > 0`). Auto-creates the default storyboard on the first
  scene (emits `storyboard.default_created`). `404` if the project is
  missing / not the caller's.
- **`GET /api/v1/projects/{project_id}/scenes`** — the project's live
  scenes ordered by `position` ascending, **un-paginated** (bounded
  editorial list). Read-only: no storyboard is created for an empty project
  (`data: []`). `404` on an unowned project.
- **`GET …/scenes/{scene_id}`** — one scene (`200`) or the uniform `404`
  (two-level gate). `ScenePublic` exposes a dense 1-based `position` and
  omits `storyboard_id` + the raw `scene_number`.
- **`PATCH …/scenes/{scene_id}`** — partial, version-fenced, **content
  only** (`title` / `duration_seconds` / `narration` / `subtitle`).
  Tri-state via `SceneUpdateRequest` (`extra="forbid"`, required
  `version`). `200` (real change → `version` +1; same-value → no-op);
  `404` (404-before-412); `412 VERSION_CONFLICT`; `422` (empty patch,
  forbidden `position`, non-nullable `null`, missing version).
- **`POST …/scenes/{scene_id}/move`** — dedicated version-fenced reorder.
  Body `{ version, position }` (1-based, clamped to `[1, N]`; current-slot
  = `200` no-op). `412` on stale version / concurrent content bump.
- **`DELETE …/scenes/{scene_id}`** — owner-scoped soft delete (`204`, no
  version fence), idempotent-by-404.
- **`Scene` domain entity** (`app/domain/scenes/scene.py`) — frozen slim
  projection of the fat `scenes` table (defers cinematography columns).
- **`ISceneRepository` + `SceneRepository`** — `ensure_default_storyboard`
  (get-or-create under a `SELECT … FOR UPDATE` project-row lock),
  gap-based `add` (`max + 1000`), `list_by_project`, `get_owned_scene`,
  version-fenced `update_owned` CAS (hand-set `+1` over the guarded
  trigger → net +1), `soft_delete_owned`, and `reorder_owned` (gap
  midpoint with a two-phase full rebalance when a gap is exhausted).
  Extended on the unit-test `FakeSceneRepository` and the integration
  `_TestUnitOfWork`.
- **Six use cases** (`CreateScene` / `ListScenes` / `GetScene` /
  `UpdateScene` / `MoveScene` / `DeleteScene`) — all run the two-level gate;
  `UpdateScene` is fetch-then-fence with same-value no-op detection.
- **DTOs** `SceneCreateRequest` / `ScenePublic` / `SceneUpdateRequest` /
  `SceneMoveRequest`; router `app/api/v1/routers/scenes.py` mounted at
  `/api/v1`; container factories + `deps` aliases; `.scenes` on the UoW.
- **Tests** — 23 use-case unit tests (`tests/unit/.../scenes/`), 11
  `SceneRepository` integration tests (incl. the load-bearing +1
  anti-double-bump and the reorder rebalance path), and 22 HTTP
  integration tests (`tests/integration/api/test_scenes.py`).

#### Documentation
- `API_CONTRACT.md` §3.2.1 (Scenes) + Resource Map updated to the nested
  scene routes.
- New `docs/domain/SCENE_AGGREGATE.md` and
  `docs/engineering/PHASE3_ALPHA5C_PREFLIGHT.md`; `PROJECT_AGGREGATE.md`
  corrected to `Project owns Storyboards, Storyboard owns Scenes`.

#### Version
- `0.4.7-phase3-alpha5c-dev`.

### Phase 3 Slice α5b — Projects update + soft-delete (`PATCH`/`DELETE /projects/{id}`) (2026-07-11)

Completes the Project CRUD lifecycle (`create → read → update →
soft-delete`). Brings the α4 optimistic-concurrency CAS to a
**path-addressed** resource and establishes the **404-before-412**
pattern — a path-addressed authenticated mutation decides *visibility*
(missing / out-of-scope / soft-deleted → `404`, exactly like the read)
**before** the version fence (`412`), so a caller can never learn a
resource exists via a `412`. Ships the M3 composite pagination index
(deferred from α5a) in the same slice. See
`docs/engineering/PHASE3_ALPHA5B_PREFLIGHT.md`.

#### Added
- **`PATCH /api/v1/projects/{id}`** (`CurrentUserDep`) — partial,
  version-fenced update. Mutable surface: `name` / `description` /
  `language` / `style` / `settings`. Tri-state semantics via
  `ProjectUpdateRequest` (`extra="forbid"`, required `version`): absent =
  unchanged, explicit `null` clears a nullable field, value sets it;
  `settings` is whole-object replace. `200` on success (real change →
  `version` +1 and `updated_at` advances; same-value → `200` no-op);
  `404` (missing/not-yours/soft-deleted); `412 VERSION_CONFLICT` (stale
  version / concurrent-bump race); `409 CONFLICT` (rename collision);
  `422` (empty patch, forbidden/mis-typed field, missing version,
  `null` for a non-nullable field).
- **`DELETE /api/v1/projects/{id}`** (`CurrentUserDep`) — owner-scoped
  soft delete → `204 No Content`, no version fence. Idempotent-by-404
  (repeat delete, and GET/PATCH after delete → `404`); frees the project
  `name` for re-use (uniqueness index excludes soft-deleted rows).
- **`IProjectRepository.update_owned` / `soft_delete_owned`** + their
  `SqlAlchemy` implementations. `update_owned` is a `UPDATE ... WHERE
  version = :expected` CAS (mirrors `UserRepository.update_profile`),
  mapping a rename `IntegrityError` → `ConflictError`; `soft_delete_owned`
  sets `deleted_at` and reports whether a live owned row was marked.
  Extended on the unit-test `FakeProjectRepository` too.
- **`UpdateProject` / `DeleteProject` use cases** — `UpdateProject` is
  fetch-then-fence (404-before-412) with same-value no-op detection;
  `DeleteProject` maps a `False` soft-delete to `404`. Container
  factories + `UpdateProjectDep` / `DeleteProjectDep` aliases wire them.
- **Migration `0008`** — composite partial index
  `ix_projects_owner_created_id`
  `(tenant_id, owner_user_id, created_at DESC, id DESC) WHERE deleted_at
  IS NULL` (M3), declared on `Project.__table_args__` and created/dropped
  by the migration. Serves `list_owned`'s keyset scan; the older
  `ix_projects_tenant_id_owner_user_id` is kept.
- **Tests** — unit: `UpdateProject` (9, U1–U9), `DeleteProject` (4,
  U10–U13); integration: `ProjectRepository` (6, R8–R13 incl. the
  guarded-trigger version-bump check) and HTTP `test_projects.py` (16,
  H17–H32 covering 200/204/404/409/412/422, 404-before-412, tri-state
  PATCH, and idempotent-by-404 delete).

#### Changed
- **`API_CONTRACT.md`** — §3.2 annotated with the α5b PATCH (tri-state,
  404-before-412) and DELETE (soft, idempotent-by-404) semantics.
- App version → `0.4.6-phase3-alpha5b-dev`.

#### Fixed
- **`validation_error_handler`** (`app/core/errors.py`) now runs
  `exc.errors()` through `jsonable_encoder`. A Pydantic `model_validator`
  that raises `ValueError` (first exercised by the α5b empty-PATCH guard)
  embeds the raw exception object in `ctx["error"]`, which the plain
  `json.dumps` render path could not serialise — turning an intended
  `422 VALIDATION_FAILED` into a `500`. The encoder coerces such objects
  recursively while preserving the human-readable `msg`, hardening the
  422 path for every field- and model-level validator.

### Phase 3 Slice α5a — Projects create + read (`POST`/`GET /projects`) (2026-07-11)

The first resource beyond identity, and the first **collection** endpoint.
Establishes the owner-and-tenant scoping pattern and the cursor
(keyset) pagination primitives that every future list endpoint reuses.
A thin, additive slice: create + read only (`PATCH` / `DELETE` /
`duplicate` deferred to α5b+). **No migration** — the `projects` table
and its `version` / soft-delete / uniqueness constraints already exist
from α1. See `docs/domain/PROJECT_AGGREGATE.md` (aggregate model) and
`docs/engineering/PHASE3_ALPHA5_PREFLIGHT.md` (slice scope + decisions).

#### Added
- **`Project` domain entity** (`app/domain/projects/project.py`) — a
  frozen dataclass mirroring the `projects` row. `current_version_id` /
  `duration_seconds` are modelled but unset in α5a (managed by later
  slices).
- **Cursor pagination primitives** (`app/application/pagination.py`) —
  `Cursor` / `Page` dataclasses + opaque, versioned, URL-safe base64
  `encode_cursor` / `decode_cursor`. A malformed token is a
  `ValidationFailedError` (422), never a 500. Placed in the application
  layer so both use cases and the API import it without an import-linter
  violation.
- **`IProjectRepository`** (`add` / `get_owned` / `list_owned`) + a
  `SqlAlchemy` `ProjectRepository`. `get_owned` / `list_owned` filter on
  `(tenant_id, owner_user_id, deleted_at IS NULL)`; `list_owned`
  paginates by keyset `(created_at, id) DESC` with a deterministic
  `id` tie-break (D14); `add` maps a duplicate-name `IntegrityError` to
  `ConflictError`. `IUnitOfWork` gains a `projects` attribute wired
  through the SQLAlchemy UoW, the unit-test fakes, and the integration
  conftest.
- **`CreateProject` / `GetProject` / `ListProjects` use cases**
  (`app/application/use_cases/projects/`) — ownership + tenancy come
  from the authenticated caller; a cross-owner / cross-tenant / missing
  project collapses to `NotFoundError` (anti-enumeration).
- **DTOs** (`app/api/v1/schemas/projects.py`) — `ProjectCreateRequest`
  (`extra="forbid"`; `aspect_ratio` constrained to
  `horizontal|vertical|square`; ownership/tenancy not accepted from the
  body) and `ProjectPublic` (omits `current_version_id` /
  `duration_seconds`, exposes `version` as the α5b PATCH handle).
- **Endpoints** (`app/api/v1/routers/projects.py`, all `CurrentUserDep`):
  `POST /api/v1/projects` (201, `version=1`; 409 on duplicate name),
  `GET /api/v1/projects` (owner-scoped, newest-first, `?limit=` 1–100
  default 20 + opaque `?cursor=`; `meta.next_cursor` present iff a
  further page exists), and `GET /api/v1/projects/{project_id}` (200 or
  a uniform 404). Container factories + `*Dep` aliases wire them through
  the composition root.
- **Tests** — unit: `CreateProject` (6), `GetProject` (4),
  `ListProjects` (5, incl. a multi-page keyset walk), pagination (4);
  integration: `ProjectRepository` (7, R1–R7) and HTTP `test_projects.py`
  (16, H1–H16 covering 201/401/404/409/422, owner scoping, and cursor
  pagination).

#### Changed
- **`API_CONTRACT.md`** — §1.2 error example corrected from
  `PROJECT_NOT_FOUND` to the canonical `NOT_FOUND`; §3.2 annotated with
  the α5a-shipped subset and the deferred surface.
- App version → `0.4.5-phase3-alpha5a-dev`.

### Phase 3 Slice α4 — Authenticated profile update (`PATCH /users/me`) (2026-07-10)

The first authenticated **mutation**, and the write-path counterpart to
α3's read-path `CurrentUserDep` pattern. Establishes the canonical
authenticated mutation flow — `CurrentUserDep` → DTO validation →
optimistic-concurrency check → domain mutation → versioned repository
CAS → updated representation — that every future write endpoint copies.
**No migration** (the `users.version` column and its `bump_version` /
`touch_updated_at` triggers already exist from α1). In α4 the only
mutable field is `display_name`.

#### Added
- **`IUserRepository.update_profile(user_id, expected_version, display_name)`**
  — a targeted, version-fenced mutation. The concrete `UserRepository`
  implementation runs a SQL compare-and-swap
  (`UPDATE … WHERE id = ? AND version = ? AND deleted_at IS NULL …
  RETURNING`) so optimistic-concurrency violations are detected
  atomically at the DB layer (no TOCTOU). A same-value target
  short-circuits before any `UPDATE` — no write, no `version` bump, no
  `updated_at` bump. Returns the updated entity on change, the unchanged
  entity on a no-op, and `None` on a version mismatch or a soft-deleted
  row (the two collapse deliberately — anti-enumeration).
- **`UpdateUserProfile` use case** (`app/application/use_cases/users/`,
  a new user-management package distinct from `auth/`) — orchestrates
  the update, distinguishes real change from same-value no-op by
  comparing the returned version, raises `VersionConflictError` when the
  repository returns `None`, and emits the audit log. Field **names**
  only in logs, never submitted values.
- **`VersionConflictError`** (`app/core/errors.py`) — `ApplicationError`
  subclass, `code = "VERSION_CONFLICT"`, HTTP 412. Rendered by the
  existing centralized exception handler.
- **`UpdateUserProfileRequest` DTO** (`app/api/v1/schemas/users.py`) —
  `extra="forbid"`, required `display_name` (1–200, whitespace-stripped,
  non-null), required `version` (≥ 1). This is the 422 rejection
  surface.
- **`PATCH /api/v1/users/me`** endpoint (`app/api/v1/routers/users.py`)
  — the canonical mutation reference. 200 with the updated `UserPublic`
  on success (never 204); 412 `VERSION_CONFLICT` on a stale fence; 422
  on body validation; 401 on any α3 auth-rejection branch. Container
  factory `get_update_user_profile_use_case` + `UpdateUserProfileDep`
  wire it through the composition root; a shared `_to_public` helper
  projects the domain `User` for both `GET` and `PATCH`.
- **Structured-log events** — `user.profile.updated` (INFO:
  `changed_fields`, `previous_version`, `new_version`) and
  `user.profile.update_rejected` (WARN for `version_mismatch`, INFO for
  `same_value_noop`).
- **Tests** — 8 unit tests for `UpdateUserProfile`
  (`tests/unit/application/use_cases/users/test_update_profile.py`),
  10 HTTP integration tests (H15–H24 in `test_users_me.py`) covering the
  happy path, 401/412/422 surfaces, same-value no-op, PATCH→GET
  round-trip, and a sequential-CAS race, and 3 repository integration
  tests (R1–R3 appended to `test_user_repository.py`) for the CAS happy
  path, version mismatch, and soft-deleted-row guard.
- **`docs/api/AUTH_ENDPOINTS.md`** — new §7.1 (`PATCH /users/me`) and §9
  (Canonical Authenticated Mutation Flow); §7 `UserPublic` example
  updated to show `version` + `updated_at`.

#### Changed
- **`UserPublic`** (`app/api/v1/schemas/users.py`) gains `version: int`
  and `updated_at: datetime`. Additive — every response returning a
  `UserPublic` (register, login, refresh, `GET /me`, `PATCH /me`) now
  carries both. `version` is the optimistic-concurrency fence clients
  round-trip with `PATCH /me`; `updated_at` supports "last modified" UX.
  `routers/auth.py::_to_payload` and `routers/users.py::get_me` updated
  to populate them.
- Version bumped to `0.4.4-phase3-alpha4-dev` in `app/main.py`.
- **Version-increment invariant** established project-wide:
  `users.version` moves only when a persisted field actually changes —
  never on auth, reads, identical PATCHes, or failed mutations.

### Phase 3 Slice α3 — Authenticated-request seam + `GET /users/me` (2026-07-09)

The read-path foundation for every authenticated endpoint, and the
predecessor to α4's write path. Introduces the `get_current_user`
dependency that resolves a bearer access token into a live `User` domain
entity, and proves the seam end-to-end with the first authenticated
business endpoint, `GET /api/v1/users/me`. **No migration** —
application-layer only.

#### Added
- **`get_current_user` dependency + `CurrentUserDep` alias**
  (`app/api/v1/deps.py`) — resolves `Authorization: Bearer <access>` →
  `User`. Strict verification (`allow_expired=False`), sid-driven
  session-liveness check, soft-delete-aware user lookup. Emits an
  anti-enumeration 401 with a single generic message for every failure
  mode; the server-side structured log carries the specific reason.
- **`GET /api/v1/users/me`** (`app/api/v1/routers/users.py`, a new router
  registered under the `/api/v1` prefix) — returns `UserPublic` for the
  authenticated caller. The first endpoint to consume `CurrentUserDep`.
- **`ISessionRepository.get_by_id`** — sid → session read used by the
  dependency for the session-liveness check (the one port-surface
  addition α3 introduces).
- **`app/api/v1/schemas/users.py`** — new schema module re-exporting
  `UserPublic` so the users router does not import an auth-named module.
- **Structured-log events** — `auth.request.authenticated` (INFO, happy
  path) and `auth.request.rejected` (WARN, `reason=` field, with the
  `security_event` flag on tamper-flavoured reasons).
- **Tests** — unit coverage for `get_current_user` (every rejection
  branch) and HTTP integration for `GET /users/me` (200 happy path + the
  401 surfaces).
- **`docs/engineering/AUTH_TOKEN_LIFECYCLE.md` §3.5** — authenticated-request
  path appendix (`bearer → verify_access → session-liveness →
  user-liveness → User`).

#### Changed
- Version bumped to `0.4.3-phase3-alpha3` in `app/main.py`.
- **`docs/engineering/RUNBOOK_WAVE.md` §7.5** — "no file-sync-hosted
  repositories" codified after the OneDrive → `C:\dev\ai-video-platform`
  migration (2026-07-09).
- **`ROADMAP.md`** — Phase 3 row updated with the α3 status line.

#### Fixed
- **`render_jobs.progress` type-hint drift** (`chore(orm)`, PR #12, merge
  `d30fb3a`) — the ORM annotation was `Mapped[float]` while the column is
  `text`; corrected to `Mapped[str]` to match what SQLAlchemy actually
  loads. Carried as a debt from the α2 trilogy and closed here in its own
  dedicated PR. No migration.

### Phase 3 Slice α2b — Auth (refresh + logout) (2026-07-01)

Completes the authentication lifecycle started in α2a. Adds refresh
token rotation with family-level reuse detection, session revocation
via logout, and the `IClock` port used by both new use cases.
Delivered as two internal checkpoints (α2b.1: `ISessionRepository`
extensions + `RefreshSession`; α2b.2: `verify_access(allow_expired)` +
`LogoutSession` + router wiring). **No migration, no ADR** — a direct
application of the α2a auth foundation.

#### Added
- **`IClock` port** (`app/application/interfaces/clock.py`) and
  `SystemClock` implementation (`app/infrastructure/clock.py`). All
  auth use cases (`RegisterUser`, `LoginUser`, `RefreshSession`,
  `LogoutSession`) now take an injected clock instead of calling
  `datetime.now(UTC)` inline. `FakeClock` in the unit fakes supports
  a frozen `fixed_at` + `tick(seconds)` for deterministic time-based
  tests.
- **`ISessionRepository.get_by_hash / revoke / list_family`** —
  three new methods on the α2a port. `revoke` uses a compare-and-swap
  clause (`WHERE revoked_at IS NULL`) so the first revoker wins and
  the original `revoked_at` timestamp is preserved for audit through
  all subsequent no-op calls. `list_family` powers the family sweep on
  reuse detection. `get_by_hash` returns revoked rows too, matching
  the use case's need to inspect `revoked_at` as the reuse signal.
- **`RefreshSession` use case** (`app/application/use_cases/auth/`) —
  orchestrates the full rotation flow: JWT verify → SHA-256 hash lookup
  → sid consistency check (A12) → reuse detection with full-family
  revocation → user liveness check → CAS-revoke old row → mint fresh
  tokens preserving `family_id` → insert new row. Every failure mode
  raises the same client-facing `InvalidRefreshTokenError` for
  anti-enumeration; server-side logs carry the specific reason.
- **`LogoutSession` use case** — CAS-revokes the session identified by
  the access token's `sid` claim. **Accepts expired access tokens**
  (documented prominently in the class docstring and in
  `docs/engineering/AUTH_TOKEN_LIFECYCLE.md`): forcing a refresh before
  logout would defeat the "I am done" intent. Signature and `kind` are
  still strictly enforced. Idempotent: second logout returns 204 and
  preserves the original `revoked_at`.
- **`verify_access(allow_expired: bool = False)`** — new kwarg on
  `ITokenIssuer`, threaded through `JWTService.verify` via PyJWT's
  `options={"verify_exp": False}`. Only `LogoutSession` sets it; every
  other consumer keeps the strict default.
- **`InvalidRefreshTokenError`** (`app/application/use_cases/auth/errors.py`) —
  subclass of `UnauthorizedError` used by both `RefreshSession` and
  `LogoutSession` for uniform 401 envelopes on every non-happy path.
- **`RefreshRequest` DTO** + `BearerAccessTokenDep` (in `app/api/v1/`) —
  the FastAPI dependency parses `Authorization: Bearer <token>`,
  raising 401 for missing / malformed headers.
- **Two new endpoints** — `POST /api/v1/auth/refresh` (200 with the
  rotated pair) and `POST /api/v1/auth/logout` (204 No Content).
- **`docs/engineering/AUTH_TOKEN_LIFECYCLE.md`** — operational spec
  covering the session state machine, endpoint sequence diagrams, the
  Refresh Family Example (visualising why reuse detection nukes the
  whole family), invariants, and the structured-log event catalogue
  including which events carry `security_event=True` for SIEM alerting.
- **Extended tests** — `test_token_issuer.py` +2 (`allow_expired`
  accepts stale / still rejects tampered), `test_refresh_session.py`
  (13 unit tests), `test_logout_session.py` (8 unit tests),
  `test_clock.py` (1 unit test), `test_session_repository.py` +5
  integration tests (get_by_hash / revoke CAS / list_family),
  `test_auth.py` +9 integration tests (refresh happy path, reuse
  detection, garbage token, access-token-as-refresh, sid mismatch,
  logout happy path, logout idempotent, missing header, malformed
  header, refresh-token-as-logout).

#### Changed
- `RegisterUser` and `LoginUser` constructors now take an `IClock`
  parameter. All timestamp assignments (`created_at`, `updated_at`,
  `last_login_at`, `issued_at`, `last_used_at`) go through the clock.
  `FakeTokenIssuer` and `FakeSessionRepository` extended for the new
  port surface.
- Version bumped to `0.4.2-phase3-alpha2b-dev` in `app/main.py`.

### Phase 3 Slice α2a — Auth (register + login) (2026-07-01)

First real business capability shipped on top of the α1 architecture
scaffold. Delivers the password-auth happy path — `POST /api/v1/auth/register`
and `POST /api/v1/auth/login` — end-to-end through the layered
architecture (domain → application → infrastructure → API). Split from
the original combined α2 plan into α2a (register + login) + α2b
(refresh + logout) per the pre-flight review for reviewability. **No
migration, no ADR** (no new architectural trade-off — the plan is a
direct application of ADR-0008 + the α1 DI pattern).

#### Added
- **Domain layer** — `app/domain/identity/{user,tenant,session}.py`.
  Frozen dataclasses with `slots=True`, zero ORM inheritance, zero
  framework dependencies. Enforced by import-linter contract #1.
- **Two new application ports** (`app/application/interfaces/security.py`):
  `IPasswordHasher`, `ITokenIssuer`, plus the `IssuedTokens` and
  `TokenClaims` value objects. Existed to keep unit tests fast (Argon2id
  fake substitution) and to lock the seam for future token-scheme
  swaps (PASETO, opaque tokens).
- **Extended `IUserRepository`** — new methods `get_by_email`,
  `get_by_id`, `add`, `update_last_login`. α1 methods
  (`count`, `exists_by_id`) preserved per the pre-flight review.
- **Three new repository ports** — `ITenantRepository` (add /
  get_by_id / exists_by_slug), `ISessionRepository` (add only in α2a;
  extended in α2b), `IRoleRepository` (assign_role_by_code, idempotent
  via ON CONFLICT DO NOTHING).
- **UoW attribute-style repos** — `IUnitOfWork` now exposes
  `.users`, `.tenants`, `.sessions`, `.roles` populated by the
  concrete UoW on `__aenter__`, so use cases call
  `await uow.users.add(...)` without ever seeing SQLAlchemy classes.
- **Two application use cases** — `RegisterUser` (application-level
  global email-uniqueness pre-check → auto-creates a self-service
  tenant per signup with slug-collision retry → inserts the user →
  assigns the `owner` role → issues tokens → persists the initial
  session), `LoginUser` (get-by-email → constant-time Argon2 verify
  → issue tokens with fresh family/session ids → persist session →
  bump `last_login_at`).

  *Note on the email-uniqueness pre-check:* the auto-tenant-per-signup
  design (Decision 1A) defeats the DB per-tenant unique constraint on
  `(tenant_id, email)` for the "same email registered twice"
  scenario — each signup arrives at a different `tenant_id` so the
  constraint always sees a distinct pair. Without an application-layer
  pre-check, re-registration would silently create a second orphan
  tenant under the same email. `RegisterUser` therefore calls
  `users.get_by_email(email)` inside the same UoW before creating the
  tenant and raises `EmailAlreadyRegisteredError` on a hit. Race
  window between the pre-check and insert is acceptable for α2a; a
  later hardening pass may add an application-level lock table or
  rate-limit if this proves exploitable in practice.

  *Note on the role assignment:* the pre-flight originally called for
  `user + owner`. On implementation this proved to conflate two
  orthogonal concepts — the `roles` table (workspace permissions,
  seeded with `owner, admin, editor, viewer, billing, support`) vs
  the `auth_role` ENUM in `schema.md` §0.1 (plan tiers). `user` lives
  on the ENUM, not the table. Assigning `owner` alone captures the
  intended semantics ("creator owns the tenant they just created");
  "any authenticated user" is enforced by JWT validity, not by a
  role row. Documented in the `RegisterUser` class docstring.
- **Anti-enumeration login path** — `LoginUser` burns one Argon2
  verify against a startup-computed dummy hash when the email is
  unknown or the account is OAuth-only, so wall-time is
  indistinguishable from the wrong-password branch (OWASP ASVS L2 §2.6.3).
- **`AuthTokenIssuer`** (`app/infrastructure/security/token_issuer.py`) —
  wraps the α1 `JWTService` + SHA-256 + up-front `session_id` /
  `family_id` generation into one call. Emits `sid` (session id) and
  `fam` (family id) claims on **both** access and refresh tokens so
  α2b `LogoutSession` can revoke a precise session row from the access
  token alone (no need to accept the refresh token in the logout body).
- **DTOs** (`app/api/v1/schemas/auth.py`) — Pydantic v2 request /
  response models. Request DTOs strip whitespace and lowercase the
  email before it ever reaches the use case (canonical `CITEXT` values).
  `UserPublic` explicitly enumerates public fields — `password_hash`
  cannot leak through DTO drift because it isn't declared.
- **Router** (`app/api/v1/routers/auth.py`) — two POST endpoints,
  mounted under `/api/v1`. Envelope response per API_CONTRACT §1.1.
  Zero try/except — errors surface via the α1 exception-handler chain.
- **DI wiring** — `app.core.container` grows a
  `get_token_issuer` singleton, a pre-computed
  `get_dummy_password_hash` (Argon2 cost paid once at process start,
  not per request), and two use-case factories
  (`get_register_user_use_case`, `get_login_user_use_case`).
- **New unit tests** (~19 across three files):
  `test_register_user.py`, `test_login_user.py`,
  `test_token_issuer.py`. All auth use-case tests use in-memory fakes
  (`tests/unit/application/use_cases/auth/_fakes.py`) — total unit-suite
  runtime stays sub-second because Argon2id verify is stubbed with a
  string comparison.
- **New integration tests** — `test_tenant_repository.py`,
  `test_session_repository.py`, `test_role_repository.py`; extended
  `test_user_repository.py` with α2a method coverage; new
  `tests/integration/api/test_auth.py` (9 scenarios covering register /
  login happy paths, duplicate email → 409, short password → 422,
  email lowercasing, `sid`/`fam` claim presence in JWT, anti-enumeration
  message equality, distinct families per device).
- **Integration test client fixture rebind** —
  `tests/integration/conftest.py::client` now overrides
  `container.get_session` and `container.get_unit_of_work` so mutation
  handlers run inside the test's SAVEPOINT connection. Nothing persists
  across tests; the shared Supabase instance stays clean.
- **New runtime dependency** — `email-validator>=2.2,<3` (required by
  Pydantic `EmailStr` at DTO parse time).
- **Fifth `import-linter` contract** — "Application use_cases never
  import infrastructure or api". Locks the layered boundary the
  moment the layer is introduced.

#### Changed
- **`app/main.py`** — imports and mounts `auth.router` under
  `/api/v1`; health router stays at the root path (API_CONTRACT §2
  designates `/healthz` + `/readyz` as public, versionless). App
  version bumped `0.4.0-phase3-alpha1-dev → 0.4.1-phase3-alpha2a-dev`.
- **`app/infrastructure/security/password_hasher.py`** — now declares
  `class PasswordHasher(IPasswordHasher)` (implements the new port).
  No runtime behaviour change.
- **`app/infrastructure/uow/sqlalchemy_unit_of_work.py`** — `__aenter__`
  populates the four repository attributes from the session it owns.

#### Deferred (Slice α2b)
- `POST /api/v1/auth/refresh` — token rotation with reuse detection.
- `POST /api/v1/auth/logout` — precise per-`sid` revocation using the
  claim shipped in α2a.
- `ISessionRepository` extensions: `get_by_hash`, `revoke`,
  `list_family`. Kept out of α2a intentionally per the pre-flight
  review — repositories in α2a cover only the α2a use cases.
- `IClock` port — introduced in α2b where `RefreshSession` needs it
  for the session-row `expires_at` computation.

#### Deferred (Slice α3+)
- Email verification (`/auth/email/verify`, `/auth/email/resend`) — α3.
- Password reset (`/auth/password/forgot`, `/auth/password/reset`) — α4.
- Google OAuth (PKCE) — α5.
- RBAC enforcement at endpoint boundaries — α6.
- OCC retry on `LoginUser.update_last_login` — retained as a deferred
  optimisation; add only if concurrent-login contention becomes
  observable.

---

### Phase 3 Wave 1.4 — `usage_records` per-partition `(request_id)` uniqueness (ADR-0033) (2026-06-30)

Wave-closing item for Phase 3 Wave 1: promotes a per-partition
partial-unique `(request_id) WHERE request_id IS NOT NULL` index to
every child partition of `usage_records`, resolving `schema.md` §37 q6.
First migration-coupled ADR to reference `docs/engineering/RUNBOOK_WAVE.md`
in place of inlining operational steps (per `CONTRIBUTING.md` §6,
established at `v0.3.3-infra`). **Wave 1 of Phase 3 closes with this
release (`v0.3.4-phase3-w1.4`).**

#### Added
- **`backend/alembic/versions/0007_usage_records_request_id_unique.py`** —
  hand-written single revision (`revision = "0007_usage_records_request_id_unique"`,
  `down_revision = "0006_widen_alembic_version_num"`). Upgrade body
  iterates `pg_inherits` for all current children of `usage_records`
  (26 monthly + 1 DEFAULT today) and creates one partial-unique index
  per child named `uq_<child>_request_id` (e.g.
  `uq_usage_records_y2025m12_request_id`,
  `uq_usage_records_default_request_id`) with predicate
  `(request_id) WHERE request_id IS NOT NULL`. Idempotent via
  `IF NOT EXISTS`. Downgrade mirrors with `DROP INDEX IF EXISTS`.
  Hand-written rather than via `alembic revision --autogenerate`
  because autogenerate cannot express per-child partition-level DDL
  and would not preserve the partial predicate. The per-child
  mechanic is PostgreSQL's standard and correct pattern for
  unique-on-non-partition-key constraints (the parent-level form
  `CREATE UNIQUE INDEX ON usage_records (request_id)` is rejected
  because the unique key omits the `occurred_at` partition key) —
  not a workaround. The 35-char revision ID fits the `VARCHAR(255)`
  ceiling established by `0006_widen_alembic_version_num` (v0.3.3-infra).
- **`docs/decisions/ADR-0033-usage-records-request-id-unique.md`** — new
  ADR (fourth file-per-ADR adopter; first to reference
  `RUNBOOK_WAVE.md` in §Migration Plan rather than inlining operational
  steps). Documents the architectural-review process that preceded the
  ADR, the rejected alternatives (`(provider, request_id)` scope
  expansion deferred to a future separate decision; `(model_id,
  request_id)` invention with no repository support; documentation-only
  closure inconsistent with wave-era planning artifacts; top-level
  parent index too weak; `ON ONLY` + `ATTACH PARTITION` rejected by
  the same partition-key rule), the per-child mechanic, the deliberate
  ORM-absence, the validator-extension rationale, the future-partition
  contract, and a Future Considerations section preserving the broader
  architectural pattern for a separate later decision.
- **`backend/scripts/validate_schema.py::check_usage_records_per_partition_unique_indexes`** —
  new ~120-LoC check function and `run_all_checks` wiring. Scans
  `pg_inherits` for all `usage_records` children and asserts each
  carries `uq_<child>_request_id` with `indisunique = true` and the
  expected `WHERE (request_id IS NOT NULL)` partial predicate. This
  is a CI-visibility addition compensating for the
  `load_snapshot()` bulk-index query that deliberately excludes
  partition children (`NOT EXISTS (SELECT 1 FROM pg_inherits ...)`
  for performance — Supabase round-trip count would otherwise scale
  with partition count). Not a workaround for a PostgreSQL
  limitation; not a substitute for ORM declaration (which is
  impossible by PostgreSQL design). The check passes when
  27/27 partition children carry the expected index after
  `alembic upgrade head`, fails on missing children, missing
  indexes, or wrong predicate.

#### Changed
- **`docs/database/schema.md`** §18 reconciliation note: amended to
  record the §37 q6 resolution with the architectural-review
  conservative wording — "The Phase 3 wave-planning artifacts
  consistently anticipate a `request_id`-based W1.4 implementation.
  Earlier architectural documents describe provider-scoped idempotency
  at the application level. W1.4 implements the scope reflected in
  the Phase 3 planning artifacts without attempting to reconcile that
  broader architectural question." The Step-A `(provider, request_id)`
  design is explicitly described as neither implemented nor
  superseded by W1.4; any future move is reserved for a separate
  decision informed by CR-12 implementation evidence (ADR-0033
  §Future Considerations). The §18 schema box, the §18 indexes line,
  and the §31 CR-12 use-case table row remain unchanged — column
  shape and broader app-layer idempotency are not altered by this
  wave.
- **`docs/database/schema.md`** §37 Q6 row: flipped from `rely on
  idempotency_keys` to **Resolved (Phase 3 W1.4, 2026-06-30)** with
  full constraint details, mirroring the Q8/Q9/Q10 resolved-row
  shape established by W1.1/W1.2/W1.3.
- **`docs/database/schema.md`** §37 Wave 1 epilogue: §18 q6 bullet
  marked **✅ Done — Phase 3 W1.4**, closing the Wave 1 quartet.
- **`docs/database/INDEX_STRATEGY.md`** line 147: status flipped from
  **Deferred (Phase 3)** to **Implemented (Phase 3 W1.4)**; rationale
  expanded to document the per-child mechanic, the PostgreSQL
  partition-key rule, the future-partition contract enforced by the
  validator check, and the explicit non-supersession of broader
  `(provider, request_id)` architectural semantics.
- **`ROADMAP.md`** Wave 1 row: W1.4 annotated **✅ Complete** with
  full ADR + migration cross-reference; "**Wave 1 closes with this
  tag (`v0.3.4-phase3-w1.4`).**" sentence appended.
- **`DECISIONS.md`**: one-line cross-link entry for ADR-0033 appended
  after the ADR-0032 entry, sorted by ADR number. Status initially
  `Proposed`; flipped to `Accepted` on the pre-merge status-flip
  commit.
- **`backend/app/infrastructure/db/models/usage.py`** —
  `UsageRecord.__table_args__` gains a multi-line inline comment near
  the existing `Index("ix_usage_records_request_id", "request_id")`
  declaration documenting that the per-child unique indexes are added
  by migration `0007` and intentionally have no ORM counterpart
  (PostgreSQL's partition-key rule makes a parent-level
  `Index(unique=True, postgresql_where=...)` impossible for
  `(request_id)` because the key omits the `occurred_at` partition
  key, and the children themselves are not ORM-modelled). The
  comment points at ADR-0033 §Implementation Notes and at the
  validator check. No `Index` or `CheckConstraint` declaration is
  added to the ORM.

#### Validated
- **Pre-upgrade safety SELECT** against live Supabase: `SELECT
  request_id, count(*) FROM usage_records WHERE request_id IS NOT NULL
  GROUP BY request_id HAVING count(*) > 1` returned zero rows
  (expected — the table is empty in every current environment; run
  for audit-trail completeness and to prove the production-rollback
  variant is not required).
- `alembic upgrade head` from `0006_widen_alembic_version_num` →
  `0007_usage_records_request_id_unique` applied cleanly; `pg_indexes`
  shows 27 new unique partial indexes (one per child) named per the
  `uq_<child>_request_id` pattern with `indexdef` containing the
  expected `WHERE (request_id IS NOT NULL)` predicate.
- `alembic downgrade -1` reverted cleanly; `pg_indexes` shows the 27
  indexes removed; `ix_usage_records_request_id` (the parent's
  non-unique propagating index) unaffected.
- `alembic upgrade head` re-applied cleanly (idempotency proven via
  `IF NOT EXISTS` guards).
- `python scripts/validate_schema.py` reported **all checks PASS**
  with the new `check_usage_records_per_partition_unique_indexes`
  reporting `27/27 usage_records partition(s) carry uq_<child>_request_id`.
- `python scripts/regenerate_erd.py` + `python scripts/compare_erd.py`
  reported 0 drift (per-child unique indexes are invisible to the ERD
  by design — it tracks entities and FKs, not indexes).
- `python scripts/ci_gate.py` reported **10/10 PASSED** locally
  against Supabase from a cold shell — `RUNBOOK_WAVE.md` referenced
  per ADR-0033 §Migration Plan, success-metric satisfied: W1.4
  required fewer manual steps than W1.3 (no env-load preamble, no
  migration-ID length gymnastics, no `.cursor/` accidents, no
  inline operational-steps duplication in the ADR).
- GitHub Actions on the PR: 10/10 green.

#### Not modified
- `backend/alembic/versions/0001_baseline.py` (no in-place edits to
  merged migrations; the new indexes are owned entirely by `0007`).
- `backend/alembic/versions/0003_export_jobs_partial_unique.py` (W1.1
  territory).
- `backend/alembic/versions/0004_idempotency_keys_invariants.py` (W1.2
  territory).
- `backend/alembic/versions/0005_distributed_locks_lease.py` (W1.3
  territory).
- `backend/alembic/versions/0006_widen_alembic_version_num.py`
  (`v0.3.3-infra` territory).
- `docs/database/ERD.md` (per-child unique indexes are invisible to
  ERD by design — entities + FKs only).
- `docs/database/schema.md` §18 schema box (lines 638–668), §18
  indexes line (line 673), §31 use-case table row (line 1175) —
  column shape and broader app-layer idempotency are unchanged by
  this wave.
- `ARCHITECTURE.md` §8k.1 (CR-12 domain spec), `API_CONTRACT.md`
  line 233 (webhook handlers) — broader architectural pattern
  remains documented; W1.4 neither implements nor supersedes it.
- `backend/app/application/`, `backend/app/api/`,
  `backend/app/infrastructure/ai/` — CR-12 (Usage Recorder
  middleware named in `schema.md` §31 / `ARCHITECTURE.md` §8k.1) is
  not built; W1.4 establishes the DB-level invariant in advance of
  the producer and does not anticipate the producer's design.
- `CONTRIBUTING.md` (file-per-ADR + ADRs-vs-Runbooks conventions
  established in earlier releases; W1.4 is the first migration-coupled
  ADR to exercise the runbook reference convention).
- `pyproject.toml`, dependency manifests.

#### Scope discipline
- One PR, one branch (`phase3/wave1.4-usage-records-request-id-unique`),
  one Alembic revision (`0007`), one ADR (ADR-0033), one validator
  check function. `git diff main...HEAD` touches **only** the files
  enumerated above. No opportunistic refactors, no unrelated cleanup,
  no W2 work; the `v0.3.3-infra` discipline rule held.
- ADR-0033 deliberately reserves provider-scoped DB-level enforcement
  for a separate later decision rather than expanding W1.4 scope —
  preserves the wave-era documented implementation shape exactly,
  preserves the architectural pattern documented elsewhere unchanged,
  and preserves the historical record's accuracy by neither
  rewriting earlier documents nor inventing supersession claims.

---

### Phase 3 Engineering Checkpoint — `v0.3.3-infra` workflow cleanup (2026-06-30)

Non-feature release between W1.3 and W1.4 removing three recurring
engineering friction points discovered while shipping W1.1–W1.3, plus
the first engineering runbook. **Success metric:** W1.4 must require
fewer manual steps than W1.3.

#### Added
- **`backend/alembic/versions/0006_widen_alembic_version_num.py`** —
  Alembic migration widening `alembic_version.version_num` from the
  default `VARCHAR(32)` to `VARCHAR(255)`. The 32-char ceiling was hit
  by W1.3's natural revision ID `0005_distributed_locks_lease_check`
  (34 chars), which had to be renamed in-place to
  `0005_distributed_locks_lease` (28 chars) to fit. The widen removes
  the ceiling globally so W1.4's natural slug
  `0007_usage_records_request_id_unique` (35 chars) and every future
  Wave migration can use descriptive names without abbreviation
  gymnastics. The migration's own revision ID
  (`0006_widen_alembic_version_num`, 30 chars) fits the pre-existing
  limit, so it applies cleanly; the widen DDL and the row insert
  happen in the same `upgrade()` transaction, so no chicken-and-egg.
  Hand-written rather than via `alembic revision --autogenerate`
  because autogenerate does not emit DDL against system tables like
  `alembic_version`.
- **`docs/engineering/RUNBOOK_WAVE.md`** — first entry in a new
  `docs/engineering/` directory for repeatable engineering procedures.
  Six sections: Pre-flight, Development, Verification, Release,
  Recovery, Lessons Learned. Documents the Phase 3 Wave process that
  W1.1–W1.3 executed by hand (with minor per-Wave variation) so W1.4
  onwards can simply reference the runbook rather than have its ADR
  re-describe operational steps. Per `CONTRIBUTING.md` §6, ADRs
  answer WHY a decision was made; runbooks answer HOW it is executed.

#### Changed
- **`backend/scripts/ci_gate.py`** — stages 5–9 now load
  `DATABASE_URL` from `backend/.env.validation` automatically. The
  previous code path checked the FILE'S existence for the
  `db_available` flag but never actually loaded variables from it, so
  the `alembic`, `validate_schema`, and `regenerate_erd` subprocesses
  inherited an empty env and silently fell back to `alembic.ini`'s
  localhost URL — failing to reach Supabase. The 6-line fix imports
  the existing `_load_env.load()` function (already in
  `backend/scripts/_load_env.py` and used by every Step B validation
  script since Phase 2) and calls it when `db_available` but
  `DATABASE_URL` is not yet exported. Idempotent; safe to re-run; no
  behavioral change in GitHub Actions CI (where `DATABASE_URL` is set
  by the service container, so the conditional short-circuits).
- **`backend/scripts/run_ci_gate.ps1`** — header comment near the
  Python invocation clarifies that env loading happens inside
  `ci_gate.py` (no PowerShell-level `.env.validation` sourcing
  required). No behavioral change; the comment exists at the
  call-site so future contributors don't add redundant PowerShell
  env-load logic. Single source of truth for env loading is Python.
- **`.gitignore`** — replaced the partial Cursor ignore
  (`.cursor/state/` + `.cursor/cache/`, lines 114–116) with `.cursor/`
  (whole directory, single rule). The partial ignore left
  `.cursor/rules/`, `.cursor/automations/`, and any future
  Cursor-managed subdirectory exposed to `git add -A` sweeps, which
  caused a pre-commit incident during W1.3's amend cycle. No
  `.cursor/` content has ever been intentionally tracked in practice;
  if a specific rule ever needs sharing,
  `git add -f .cursor/rules/<file>` works for the deliberate case.
- **`CONTRIBUTING.md`** — §6 Documentation Policy extended with an
  "ADRs vs Runbooks (v0.3.3-infra)" paragraph codifying the
  convention: ADRs are for WHY (context, alternatives, consequences);
  runbooks are for HOW (step lists, commands, recovery actions).
  Cross-references `docs/engineering/RUNBOOK_WAVE.md` and notes that
  the W1.4 ADR (ADR-0033) will be the first to reference the runbook
  in place of inlining operational steps.
- **`ROADMAP.md`** — small engineering-checkpoint annotation between
  the Phase 3 wave table's W1 row and the surrounding "each wave
  produces its own ADR(s)" sentence, recording the `v0.3.3-infra`
  release and pointing at the new runbook. The Wave table itself is
  unchanged (the checkpoint is not a Wave).

#### Validated (live, 2026-06-30)
- **Pre-fix reproduction** — confirmed that the v0.3.2 `ci_gate.py`,
  when run from a shell where `DATABASE_URL` is unset, fails stage 5
  (`alembic upgrade head`) with `psycopg.OperationalError` despite
  `backend/.env.validation` being present. This was the original
  W1.2 symptom that required a manual PowerShell env-load workaround.
- **Post-fix reproduction** — same shell, no env vars set, no manual
  PowerShell loader: `scripts/ci_gate.py` reaches 10/10 stages green
  with `DATABASE_URL` loaded from `backend/.env.validation`
  automatically. The success metric — *will W1.4 require fewer manual
  steps than W1.3?* — is satisfied: zero manual steps for env
  loading.
- **Alembic round-trip** — `alembic upgrade head` (applies 0006,
  widens column); inspection of `\d alembic_version` confirms
  `version_num` is now `character varying(255)`; `alembic downgrade -1`
  returns the column to `character varying(32)`; `alembic upgrade head`
  re-applies cleanly (idempotency proven).
- **`.gitignore` enforcement** — `git status` after the new ignore is
  in place no longer lists `.cursor/` as untracked; `git add -A` no
  longer stages anything under `.cursor/`.
- **Full CI gate** — `scripts/ci_gate.py` 10/10 local; matching
  GitHub Actions run on the PR also 10/10 against the pgvector
  service container.

#### Not modified (scope discipline)
- **`backend/app/infrastructure/db/models/*.py`** — no ORM changes.
  `alembic_version` is Alembic's own bookkeeping table and is not
  modelled in the ORM (it is explicitly whitelisted out of the
  schema validator's table-parity check).
- **`docs/database/schema.md`** — no changes (`alembic_version` is
  intentionally not documented there; it is build infrastructure,
  not application data).
- **`docs/database/INDEX_STRATEGY.md`** — no changes (no new indexes
  or unique constraints; the column-type widen does not affect index
  counts).
- **`docs/database/ERD.md`** — no changes (ERD does not include
  `alembic_version`).
- **`DECISIONS.md`** — no new ADR. This is a workflow cleanup, not
  an architectural decision; the rationale lives in this CHANGELOG
  entry and in `docs/engineering/RUNBOOK_WAVE.md` §6 Lessons Learned.
- **`backend/alembic/versions/0001_baseline.py` through
  `0005_distributed_locks_lease.py`** — none amended in place; the
  column widen is added entirely by `0006` and reverted on its
  `downgrade()`.

#### Scope discipline (per the v0.3.3-infra PR scope rule)
- Every changed file either implements one of the five engineering
  improvements (env-load fix, alembic widen, gitignore, runbook,
  ADRs-vs-runbooks convention) or documents the release (this
  CHANGELOG entry, the ROADMAP annotation).
- No feature work. No schema changes other than the
  `alembic_version` column widen. No API changes. No refactors. No
  opportunistic cleanup. No "while we're here" edits.

### Phase 3 Wave 1.3 — `distributed_locks` lease CHECK (2026-06-29, ADR-0032)

#### Added
- **`backend/alembic/versions/0005_distributed_locks_lease.py`** —
  Alembic migration adding a single CHECK constraint
  `chk_distributed_locks_lease_until_after_acquired_at` enforcing
  `lease_until > acquired_at`. Strict greater-than (`>`, not `>=`)
  rejects the degenerate zero-second lease that a buggy `$lease = 0` or
  negative-`$lease` call site would produce. Hand-written rather than via
  `alembic revision --autogenerate` because autogenerate does not
  reliably preserve the exact text of CHECK expressions. Smallest W1.x
  migration to date: one `ALTER TABLE … ADD CONSTRAINT` in `upgrade()`,
  one `ALTER TABLE … DROP CONSTRAINT` in `downgrade()`. Forward + reverse
  + idempotency round-trip validated against Supabase Postgres 17.6 +
  pgvector 0.8.0 via `backend/.env.validation`.
- **`docs/decisions/ADR-0032-distributed-locks-lease-check.md`** — third
  file-per-ADR under `docs/decisions/` (ADR-0030 was the first,
  ADR-0031 the second). Records the promotion of the §37 Q10 invariant
  verbatim — no bundling with `lease_until >= heartbeat_at` or other
  temporal-anchor invariants (those remain future-ADR territory). 7
  alternatives considered, 3-tier rollback plan, 19-item acceptance
  criteria including an explicit pre-upgrade safety SELECT.
- **`DECISIONS.md`** — one-line cross-link entry for ADR-0032 pointing
  at the new file (status flips from `Proposed` → `Accepted` in the
  pre-merge `docs(adr): mark ADR-0032 Accepted` commit).

#### Changed
- **`backend/app/infrastructure/db/models/operations.py`** —
  `DistributedLock.__table_args__` extended with the matching
  `CheckConstraint("lease_until > acquired_at", name="chk_distributed_locks_lease_until_after_acquired_at")`
  declaration so the ORM mirrors the migration exactly. No import changes
  needed; `CheckConstraint` was already imported during W1.2 for
  `IdempotencyKey`. The existing `Index("ix_distributed_locks_lease_until", "lease_until")`
  is preserved unchanged; the new `CheckConstraint` is placed
  immediately before it inside the `__table_args__` tuple per the
  W1.2 ordering precedent (constraint before index).
- **`docs/database/schema.md`** — §32 column block now lists the CHECK
  constraint inline; new **Lease validity invariant (DB-enforced,
  Phase 3 W1.3)** paragraph mirrors §31's W1.2 FSM-invariant paragraph
  and explains the single-predicate scoping decision (and why
  `lease_until >= heartbeat_at` is intentionally deferred); §32
  reconciliation note revised to acknowledge that W1.3 reverses the 2D
  deferral with stated reasoning (the original "harder to diagnose"
  argument inverts in practice once the CHECK has a descriptive name);
  §37 Q10 row marked **Resolved (Phase 3 W1.3, 2026-06-29)** with full
  constraint details; §37 epilogue Wave 1 bullet for §32 q10 marked
  ✅ Done.
- **`ROADMAP.md`** — Phase 3 wave table W1 row annotated with W1.3
  ✅ Complete alongside W1.1 and W1.2; remaining W1.4 split out.

#### Validated (live, 2026-06-29)
- **Pre-upgrade safety check** — `SELECT COUNT(*) FROM
  distributed_locks WHERE NOT (lease_until > acquired_at)` against
  Supabase returned `0`, clearing the gate for `alembic upgrade head`.
  Zero existing rows in the live target, so the gate is trivially
  satisfied — but the SELECT is run for audit-trail completeness and
  to verify the production-rollback variant (`ADD CONSTRAINT … NOT
  VALID` + later `VALIDATE CONSTRAINT`) is not required.
- **Alembic round-trip** — `upgrade head → downgrade -1 → upgrade head`
  clean; `pg_constraint` diff = exactly one CHECK added on forward,
  exactly one removed on reverse; the constraint's `consrc` predicate
  reads exactly `lease_until > acquired_at`.
- **Schema validator** — `scripts/validate_schema.py` 9/9 (no validator
  code change; none of the 9 structural checks inspect CHECK
  constraints by name — the table-parity check passes by construction
  because the ORM and DB agree on the column shape, which is unchanged
  by W1.3).
- **ERD round-trip** — `regenerate_erd.py` + `compare_erd.py` 0 drift
  (ERD ignores CHECK constraints).
- **Full CI gate** — `scripts/ci_gate.py` 10/10 local; matching GitHub
  Actions run on the PR also 10/10 against the pgvector service
  container.

#### Not modified (scope discipline)
- **`docs/database/INDEX_STRATEGY.md`** — no changes (W1.3 adds zero
  indexes and zero unique constraints; only a CHECK constraint, which
  `INDEX_STRATEGY.md` does not track; the 87-index count stays at 87).
- **`docs/database/ERD.md`** — no changes (ERD tracks entities and FKs
  only; CHECK constraints are invisible to it; the 51-entity / 60-edge
  count stays unchanged).
- **`CONTRIBUTING.md`** — no changes (the file-per-ADR convention was
  already documented in W1.1; ADR-0032 is the third adopter, not the
  convention-establisher).
- **`backend/alembic/versions/0001_baseline.py`** — baseline migrations
  are historical and never amended in place; the new CHECK is added
  entirely by migration `0005` and dropped on its downgrade.

#### Scope discipline (per Wave 1 isolation constraint)
- No changes to `export_jobs` (W1.1 territory), `idempotency_keys`
  (W1.2), or `usage_records` (W1.4). W1.4 gets its own branch + ADR.

### Phase 3 Wave 1.2 — `idempotency_keys` mutability + status↔response invariant (2026-06-29, ADR-0031)

#### Added
- **`backend/alembic/versions/0004_idempotency_keys_invariants.py`** — Alembic
  migration applying three coordinated changes to `idempotency_keys` in a
  single transaction: (1) `ADD COLUMN updated_at timestamptz NOT NULL
  DEFAULT now()`, (2) `CREATE TRIGGER tg_idempotency_keys_biu_touch_updated_at`
  bound to the shared `touch_updated_at()` function (already defined in
  the baseline, already wired to 30+ other tables), and (3) `ADD
  CONSTRAINT chk_idempotency_keys_response_hash_matches_status CHECK
  ((status = 'in_flight') = (response_hash IS NULL))`. Hand-written
  rather than via `alembic revision --autogenerate` because autogenerate
  does not emit `CREATE TRIGGER` statements and would not preserve the
  exact text of the CHECK expression or the explicit sequencing of the
  three ops. Forward + reverse + idempotency round-trip validated
  against Supabase Postgres 17.6 + pgvector 0.8.0 via
  `backend/.env.validation`.
- **`docs/decisions/ADR-0031-idempotency-keys-invariants.md`** — second
  file-per-ADR under `docs/decisions/` (ADR-0030 was the first). Records
  the promotion of two long-standing application-layer assumptions to
  the DB: the mixin misclassification that left `idempotency_keys` in a
  "mutable-but-untracked" state, and the unprotected status↔response
  FSM invariant. 8 alternatives considered, 3-tier rollback plan, 17-item
  acceptance criteria including an explicit pre-upgrade safety SELECT.
- **`DECISIONS.md`** — one-line cross-link entry for ADR-0031 pointing
  at the new file (status flips from `Proposed` → `Accepted` in the
  pre-merge `docs(adr): mark ADR-0031 Accepted` commit).

#### Changed
- **`backend/app/infrastructure/db/models/operations.py`** — `IdempotencyKey`
  switched from `CreatedAtOnlyMixin` to `TimestampMixin` (the original
  mixin choice was a Phase 2 Step-A misclassification — `CreatedAtOnlyMixin`
  is documented as "for immutable / append-only tables" but the row IS
  mutated `in_flight → succeeded`/`failed`). `__table_args__` extended
  with the matching `CheckConstraint(..., name="chk_idempotency_keys_response_hash_matches_status")`
  declaration so the ORM mirrors the migration exactly. `CheckConstraint`
  added to the SQLAlchemy import line; `CreatedAtOnlyMixin` removed
  from the mixins import.
- **`docs/database/schema.md`** — §31 column block now lists
  `updated_at`; new **FSM invariant (DB-enforced, Phase 3 W1.2)**
  paragraph explains the CHECK's scope decision (`response_hash` only,
  not `response_payload` or `http_status`); §31 reconciliation note
  updated to acknowledge that W1.2 reverses the 2D `updated_at`
  omission with stated reasoning (the original "audit event covers it"
  rationale conflated audit replay with operational observability);
  §37 Q9 row marked **Resolved (Phase 3 W1.2, 2026-06-29)**; Wave 1
  bullet for §31 q9 marked ✅ Done.
- **`ROADMAP.md`** — Phase 3 wave table W1 row annotated with
  W1.2 ✅ Complete alongside W1.1; remaining W1.3 / W1.4 split out.

#### Validated (live, 2026-06-29)
- **Pre-upgrade safety check** — `SELECT COUNT(*) FROM idempotency_keys
  WHERE (status = 'in_flight') <> (response_hash IS NULL)` against
  Supabase returned `0`, clearing the gate for `alembic upgrade head`.
- **Alembic round-trip** — `upgrade head → downgrade -1 → upgrade head`
  clean; `pg_constraint` diff = exactly one CHECK added on forward,
  exactly one removed on reverse; `pg_trigger` diff = exactly one BIU
  trigger added on forward, exactly one removed on reverse;
  `information_schema.columns` confirms `updated_at` is
  `timestamp with time zone NOT NULL` after upgrade and gone after
  downgrade.
- **Schema validator** — `scripts/validate_schema.py` 9/9 (no validator
  code change; `check_table_parity` picked up the new `updated_at`
  column automatically from ORM metadata; no validator check covers
  CHECK constraints or `_UPDATED_AT_TABLES` membership, so those
  remain green by construction).
- **ERD round-trip** — `regenerate_erd.py` + `compare_erd.py` 0 drift
  (ERD ignores CHECK constraints, triggers, and per-column shape; only
  entity-level changes show up there).
- **Full CI gate** — `scripts/ci_gate.py` 10/10 local; matching GitHub
  Actions run on the PR also 10/10 against the pgvector service container.

#### Not modified (scope discipline)
- **`docs/database/INDEX_STRATEGY.md`** — no changes (W1.2 adds zero
  indexes and zero unique constraints; only a column, a trigger, and a
  CHECK constraint — none of which `INDEX_STRATEGY.md` tracks).
- **`CONTRIBUTING.md`** — no changes (the file-per-ADR convention was
  already documented in W1.1; ADR-0031 is the second adopter, not the
  convention-establisher).
- **`backend/alembic/versions/0001_baseline.py`** — baseline migrations
  are historical and never amended in place; the new trigger is added
  entirely by migration `0004` and dropped on its downgrade.

#### Scope discipline (per Wave 1 isolation constraint)
- No changes to `export_jobs` (W1.1 territory), `distributed_locks`
  (W1.3), or `usage_records` (W1.4). W1.3 / W1.4 each get their own
  branch + ADR.

### Phase 3 Wave 1.1 — `export_jobs` partial-unique constraint (2026-06-29, ADR-0030)

#### Added
- **`backend/alembic/versions/0003_export_jobs_partial_unique.py`** — Alembic
  migration creating the partial-unique index
  `uq_export_jobs_render_job_id_format_quality_orientation` on
  `export_jobs (render_job_id, format, quality, orientation)` with
  `WHERE status IN ('queued','running','succeeded')`. Hand-written rather
  than via `alembic revision --autogenerate` because autogenerate does not
  reliably emit partial-unique indexes via `postgresql_where` (it produces
  a vanilla unique constraint instead). Forward + reverse + idempotency
  round-trip validated against Supabase Postgres 17.6 + pgvector 0.8.0
  via `backend/.env.validation`.
- **`docs/decisions/ADR-0030-export-jobs-partial-unique.md`** — first
  file-per-ADR under the new `docs/decisions/` directory. Records the
  promotion of the `(render_job_id, format, quality, orientation)`
  uniqueness invariant from the use-case layer (where it had no consumer
  yet) directly to the database, with full rationale, 7 rejected
  alternatives, 3-tier rollback plan, and 15-item acceptance criteria.
  ADRs 0001–0029 remain inline in `DECISIONS.md`; all Phase-3-and-later
  ADRs use the file-per-ADR convention.
- **`DECISIONS.md`** — one-line cross-link entry for ADR-0030 pointing
  at the new file (status flips from `Proposed` → `Accepted` in the
  pre-merge `docs(adr): mark ADR-0030 Accepted` commit).

#### Changed
- **`backend/app/infrastructure/db/models/jobs.py`** — `ExportJob.__table_args__`
  extended with the matching `Index(..., unique=True, postgresql_where=text(...))`
  declaration so the ORM mirrors the migration exactly. Same shape as
  the existing partial-unique pattern used across the model layer.
- **`docs/database/schema.md`** — §17 reconciliation note for `export_jobs`
  flipped from "Phase-3 decision" to "Implemented via ADR-0030 / migration
  `0003`"; §37 Q8 row marked **Resolved (Phase 3 W1.1, 2026-06-29)**;
  Wave 1 bullet for §17 q8 marked ✅ Done.
- **`docs/database/INDEX_STRATEGY.md`** — §8 `export_jobs` row moved
  **Deferred (Phase 3)** → **Implemented** with full predicate spelled out;
  §18 reconciliation summary counts updated (indexes 81 → 82,
  unique constraints 23 → 24, Implemented rows 73 → 74,
  Deferred (Phase 3) 21 → 20).
- **`ROADMAP.md`** — Phase 3 wave table W1 row annotated with
  W1.1 ✅ Complete and the remaining W1.2 / W1.3 / W1.4 split out.
- **`CONTRIBUTING.md`** — §1 ground rule 2 and §6 documentation policy
  updated to acknowledge the new `docs/decisions/` file-per-ADR
  convention (introduced by ADR-0030) while preserving compatibility
  with the inline ADRs 0001–0029 in `DECISIONS.md`.

#### Validated (live, 2026-06-29)
- **Pre-upgrade safety check** — `SELECT COUNT(*) FROM export_jobs WHERE
  status IN ('queued','running','succeeded')` against Supabase returned
  `0`, clearing the gate for the in-development upgrade path.
- **Alembic round-trip** — `upgrade head → downgrade -1 → upgrade head`
  clean; `pg_indexes` diff = exactly one row added on forward, exactly
  one row removed on reverse; `indexdef` contains the expected
  `WHERE … status = ANY` predicate.
- **Schema validator** — `scripts/validate_schema.py` 9/9 (no validator
  code change; `check_unique_constraints` and `check_indexes` picked up
  the new `Index(unique=True, postgresql_where=…)` automatically from
  ORM metadata).
- **ERD round-trip** — `regenerate_erd.py` + `compare_erd.py` 0 drift
  (ERD ignores non-FK indexes).
- **Full CI gate** — `scripts/ci_gate.py` 10/10 local; matching GitHub
  Actions run on the PR also 10/10 against the pgvector service container.

#### Scope discipline (per Wave 1 isolation constraint)
- No changes to `idempotency_keys`, `distributed_locks`, or
  `usage_records`. W1.2 / W1.3 / W1.4 each get their own branch + ADR.

### Phase 2D — Documentation Reconciliation (2026-06-29, approved by reviewer; no code changes)

#### Verification
- **Manual spot-check** (8/8 MATCH) — `PHASE2D_SPOT_CHECK.md`. Eight
  models (`tenants`, `projects`, `project_tags`, `workflow_runs`,
  `usage_records`, `credit_ledger`, `audit_log`, `provider_settings`)
  compared by hand against ORM, baseline migration, `schema.md`,
  `ERD.md`, and `INDEX_STRATEGY.md`. Zero semantic mismatches.
- **CI quality gate** — re-run with no code changes; 10/10 stages
  green (5 non-DB + 5 DB; oracle migration round-trip clean; schema
  validator 9/9; ERD compare 0 drift; coverage 100% over Phase 2 scope).
- **Phase 3 wave sequencing** recorded in `ROADMAP.md` and
  `schema.md` §37 (Waves 1–4).
- **Baseline tag (pre-flight)** — deferred to the user. Workspace is
  not yet a git repository; exact `git init`/commit/tag command
  sequence recorded in `ROADMAP.md` Phase 3 Pre-flight section.


#### Changed (docs only)
- **`docs/database/schema.md`** — added a top-of-doc audit-of-truth rule
  ("implementation is the source of truth"); reconciled §16 (workflow
  runs/steps/checkpoints), §17 (render/export jobs), §18 (usage records),
  §19 (cost reconciliations), §20 (plans/subscriptions/invoices), §22
  (feature flags), §25 (event outbox), §26 (event log), §27 (system /
  tenant / provider settings), §31 (idempotency keys), §32 (distributed
  locks), and §33 (audit log) to match the validated ORM column shapes,
  FK shapes, and indexes. Each section carries an inline
  "Reconciled in 2D" note documenting what changed and why. Added §37
  cataloguing the 13 questions deferred to Phase 3 entry (relationship()
  pattern, deferred indexes, `cost_reconciliations` immutability,
  `auth_role` enum retention, ERD cross-cluster elision policy, …).
- **`docs/database/ERD.md`** — added a top-of-doc reconciliation note;
  rewrote the column shapes in Cluster 6 (workflows / render / export),
  Cluster 7 (usage records / cost reconciliations), Cluster 8 (billing),
  Cluster 9 (feature flags / event outbox / event log), and Cluster 10
  (config / operations / audit) to match the ORM. Cross-cluster FK
  elision policy made explicit so `compare_erd.py` continues to report
  zero design-edge drift.
- **`docs/database/INDEX_STRATEGY.md`** — full rewrite. Every row is now
  labeled `Implemented` (matches an ORM index by name), `Renamed` (the
  design name differed; row updated to the actual ORM name), or
  `Deferred (Phase N)` with a Phase-3 entry decision attached. Added
  §16 (Phase 3 index decisions) and §18 (reconciliation summary:
  81 implemented indexes + 23 unique constraints).
- **`docs/database/BACKUP_RESTORE.md`** — `_backup_sentinel` column
  shape updated from the draft `(taken_at, marker)` to the shipped
  `(inserted_at, label, notes)`.
- **`DECISIONS.md`** — renumbered the second ADR-0028 to **ADR-0029**
  ("CI Quality Gate Operational Contract — Phase 2C Ratification") to
  resolve the duplicate ADR id surfaced by the architectural audit.
  ADR-0028 retains its original content. ADR-0029's Context paragraph
  notes the renumber explicitly.

#### Not changed (deferred to Phase 3 entry by reviewer rule)
- ORM models / Alembic migrations / database schema / seed data / CI
  gate remained untouched. The validation harness (`validate_schema.py`)
  and ERD round-trip continue to pass with the same 81 indexes,
  95 FKs, 52 base tables. The architectural audit's recommendations on
  `relationship()` adoption, additional indexes, `cost_reconciliations`
  immutability, `auth_role` retention, and cross-cluster ERD edges
  were deliberately left as Phase-3-entry questions per the reviewer's
  guidance.

### Phase 2C — CI Quality Gate (implementation complete, awaiting reviewer)

#### Added
- **`backend/scripts/ci_gate.py`** — cross-platform 10-stage runner
  (ruff → black → mypy + import-linter → pytest+cov → alembic up → down
  → up → validator → ERD diff → coverage threshold). Stages 5–9 are
  skipped (not failed) when `DATABASE_URL` is absent so the
  laptop-no-Postgres path still works.
- **`backend/scripts/run_ci_gate.ps1`** — PowerShell wrapper for Windows
  developers; thin convenience layer over `ci_gate.py` with stage-range
  pass-through and credential redaction in the banner.
- **`.github/workflows/ci.yml`** — GitHub Actions wiring: triggers on
  PRs and pushes to `main`, runs against a `pgvector/pgvector:pg16`
  service container, uploads validator + ERD + coverage artefacts, and
  appends the coverage report to the job summary.
- **`backend/tests/`** — Phase 2C smoke suite (24 tests, **100 % branch
  coverage** on `app/` for Phase 2C scope):
  - `test_models_import.py` — every model module imports; metadata
    contains the expected aggregate-root subset; `Base` is declarative
    and shares the canonical metadata.
  - `test_metadata.py` — partitioned parents declare
    `postgresql_partition_by`; every FK declares an explicit
    `ON DELETE`; immutable tables have no `updated_at`/`deleted_at`;
    pgvector is scoped to the two approved columns; naming convention is
    populated; no naive `DateTime` columns.
  - `test_mixins.py` — UUID PK, timestamp, soft-delete, version, and
    created-at-only mixins all expose the documented column shapes; the
    UUID PK Python default is the `uuid.uuid4` factory (verified by
    `__module__` + `__qualname__` to survive import-system reloads).
  - `test_enums.py` — enum count pinned at 26, all `native_enum=True`,
    all values lowercase snake_case, no duplicate values, no PG type
    name collisions.
- **`backend/pyproject.toml`** — `black`, `pytest`, `pytest-cov`,
  `pytest-asyncio`, `types-PyYAML`, `import-linter` added to
  `[project.optional-dependencies.dev]`; configs added for
  `[tool.black]`, `[tool.pytest.ini_options]`, `[tool.coverage.*]`,
  `[tool.importlinter]`; existing `[tool.ruff]` extended with
  `SIM/C4/RUF` rule sets and per-file ignores for migrations / tests /
  scripts; `[tool.mypy]` narrowed to `app/` only with strict mode
  preserved.
- **`CI_QUALITY_GATE.md`** — stage map, runtime budgets, local
  invocation contract, failure runbook, and coverage threshold roadmap
  (60 % → 80 % → 85 % across phases).
- **`DECISIONS.md` ADR-0028** — "Mandatory CI Quality Gate Before
  Phase 3" (ratified at close of Phase 2 Step B).
- **Architectural fitness contracts** (`import-linter`, 4 contracts):
  domain layer has no infra / app / api deps; DB models cannot import
  app / api; api layer cannot import infra directly; application layer
  never imports api.
- **`backend/app/{domain,application,api}/__init__.py`** — empty
  package skeletons created at the close of Phase 2 so the
  architectural contracts are live the moment any Phase 3 code lands.

#### Changed
- **`backend/app/infrastructure/db/models/*.py`** — 39 `Mapped[dict]` /
  `Mapped[list]` annotations parameterised to `Mapped[dict[str, Any]]`
  / `Mapped[list[Any]]` (resolved 39 of 44 mypy `--strict` errors);
  three unused `# type: ignore[assignment]` comments removed from
  pgvector fallback branches.
- **`backend/scripts/ci_gate.py`** stage 3 — now invokes both `mypy`
  and `lint-imports` (previously only `mypy` despite the title); the
  `lint-imports` entrypoint is resolved relative to the active venv to
  avoid PATH surprises.

#### Self-tested (local, 2026-06-29)
- Stages 1–4 (lint / format / static analysis / tests + coverage):
  **green** — 24 tests pass, mypy 0 errors, lint-imports 0 violations.
- Stages 8–10 (live schema validator / ERD diff / coverage threshold):
  **green** against Supabase Postgres 17.6 + pgvector 0.8.0 — 9/9
  structural checks pass, 51/51 entities + 58/58 design edges in ERD
  round-trip, coverage 100 % over the 22 `app/` modules currently in
  scope (well above the 60 % Phase 2C threshold).
- Stages 5–7 (alembic up/down/up): deliberately not re-exercised in the
  self-test to avoid re-running migrations against the live target;
  wired identically to the proven Step B validation path and will
  execute against the pgvector service container in CI.

#### Pending (Phase 2C exit criteria)
- Reviewer sign-off on `CI_QUALITY_GATE.md` + ADR-0028 → unlocks
  Phase 3.

---

### Phase 2 — Database, Step B: SQLAlchemy + Alembic — ✅ APPROVED 2026-06-28

#### Added
- `backend/pyproject.toml`, `backend/alembic.ini`, `backend/alembic/env.py`,
  `backend/alembic/script.py.mako`.
- Declarative base + naming convention (`app/infrastructure/db/base.py`).
- Reusable mixins: `UUIDPrimaryKeyMixin`, `TimestampMixin`,
  `SoftDeleteMixin`, `VersionMixin`, `CreatedAtOnlyMixin`.
- Central ENUM registry (`app/infrastructure/db/enums.py`).
- 23 ORM model files (`app/infrastructure/db/models/*.py`) covering every
  table in `docs/database/schema.md`.
- Alembic baseline migration `0001_baseline.py` — extensions, ENUMs,
  helper PL/pgSQL functions, all tables, indexes (incl. imperative
  GIN / HNSW), triggers (`touch_updated_at`, `bump_version`,
  `reject_mutation`, `enforce_credit_ledger_balance`), partition
  bootstrap (current month + 24 forward months + default partitions),
  and the deferred `projects.current_version_id` FK.
- Alembic seed migration `0002_seed_system_data.py` — plans, feature
  flags, provider plugins, AI model catalogue, RBAC roles, and the
  initial system settings rows. Idempotent via `ON CONFLICT DO NOTHING`.
- Schema validator (`backend/scripts/validate_schema.py`) — 9 automated
  checks covering extensions, tables, partitions, FKs, unique
  constraints, indexes, immutability triggers, pgvector column scope,
  and the credit_ledger balance trigger.
- ERD regenerator (`backend/scripts/regenerate_erd.py`) — Mermaid output
  for stable diffs against `docs/database/ERD.md`.
- One-command orchestrator (`backend/scripts/run_validation.py` and
  PowerShell wrapper `run_validation.ps1`) implementing the
  upgrade → downgrade → re-upgrade → introspect → ERD-regenerate cycle.
- `backend/docker-compose.db.yml` — local pgvector Postgres 16.
- `SCHEMA_VALIDATION.md` — methodology, checks, run instructions,
  pending live-run section.
- `PROJECT_STATUS.md` — living project status with version, milestones,
  debt, risks, open questions, and step-level checklist.
- ADR-0027 — Tenant-Scoped Billing Aggregates (`DECISIONS.md`).

#### Changed (during live validation)
- Validator rewritten against `pg_catalog`: a single `load_snapshot(engine)`
  pulls every base table, FK, and index in three bulk queries; per-check
  functions consume the cached snapshot instead of issuing ~400 per-table
  `inspect()` round-trips. Validator runtime against the Supabase pooler:
  **263 s → 17 s.**
- ERD regenerator rewritten against `pg_catalog`; partition children
  excluded at the SQL level so the FK query no longer hits Supabase's
  2-minute `statement_timeout`. ERD generation: **>120 s timeout → 13 s.**
- `alembic_version` whitelisted in `validate_schema.py`'s table-parity check
  (it's Alembic's own bookkeeping; not in the ORM `metadata`).
- `validate_schema.py` redacts the password from the connection URI in
  `schema_validation_report.json`.
- `alembic/env.py` doubles `%` in URL-encoded passwords before handing the
  URI to ConfigParser (fixes `%40` → `@` round-tripping for Supabase URIs).
- Credentials now loaded via `_load_env.py` from `backend/.env.validation`
  (git-ignored); never appear on the shell command line.
- `docs/database/ERD.md` Cluster 8 (Billing) corrected: subscriptions are
  tenant-scoped (not user-scoped); invoices are subscription-scoped;
  `users → credit_ledger` is nullable (SET NULL).
- `docs/database/ERD.md` Cluster 7 (Media/Library): direction of the
  `media_assets ↔ library_assets` edge corrected (library_assets has the
  FK, not the other way around).
- `docs/database/ERD.md` Clusters 5/9: `provider_plugin_registrations →
  ai_models` and `event_outbox → event_log` converted to Mermaid comments
  (logical references — no DB FK).
- `docs/database/schema.md` §20–§21 corrected to match the implementation
  (subscriptions/invoices have no `user_id` column; credit_ledger.user_id
  is nullable with SET NULL).

#### Validated (live, 2026-06-28)
- Target: Supabase managed PostgreSQL 17.6 + pgvector 0.8.0
  (ap-northeast-2 session pooler, IPv4).
- `alembic upgrade head` ✅; `alembic downgrade base` ✅
  (only `alembic_version` retained); `alembic upgrade head` again ✅
  (idempotency proven).
- All 9 structural checks pass: 5 required extensions, 52 ORM tables,
  4 partitioned parents (27 children each), 95 FKs, all declared
  unique indexes, 86 indexes including 5 imperative GIN/HNSW,
  8 immutable-trigger-protected tables, exactly 2 pgvector columns,
  `credit_ledger` balance trigger present.
- ERD round-trip: 51/51 entities match; 58/58 design-declared edges
  present in implementation; 0 design edges missing.

#### Pending
- Reviewer sign-off on `SCHEMA_VALIDATION.md` §6.

### Phase 2 — Database, Step A: Design Documents (APPROVED 2026-06-28, revision 2)

#### Added (initial)
- `docs/database/NAMING_CONVENTIONS.md`
- `docs/database/ERD.md` (Mermaid ER diagram covering every aggregate root)
- `docs/database/schema.md` (full table-by-table schema with FKs / ON DELETE / uniqueness / checks)
- `docs/database/INDEX_STRATEGY.md`
- `docs/database/RETENTION_POLICY.md`
- `docs/database/BACKUP_RESTORE.md`

#### Added (revision 2 — final design CRs)
- **CR-DB-1** First-class Idempotency Framework — `idempotency_keys` table (ADR-0021).
- **CR-DB-2** Database-backed Distributed Locks — `distributed_locks` table with lease + heartbeat (ADR-0022).
- **CR-DB-3** Audit Log — partitioned, immutable `audit_log` table separate from `event_log`, Class C retention (ADR-0023).
- **CR-DB-4** Explicit Configuration Tables — `system_settings`, `tenant_settings`, `provider_settings`; generic `settings` table removed (ADR-0024).
- ADR-0025 — defer `user_preferences` to `users.extra` JSONB.
- ERD cluster 10 (Configuration & Operations) added.
- Index strategy §14a/§14b/§14c added.
- Retention policy updated: `audit_log` → Class C (7 years); `idempotency_keys` / `distributed_locks` → TTL classes.
- Immutability verification job now also covers `audit_log` and `cost_reconciliations`.

#### Pending
- Step A review and approval → unlocks Step B (SQLAlchemy models + Alembic baseline) following the execution order recorded in `ROADMAP.md` Phase 2 Step B.

---

## [Phase 1 — 2026-06-28] — Architecture & Folder Structure (Rev 3, APPROVED)

#### Added
- `rule.md` — governing requirements document with anti-hallucination guardrails.
- `ARCHITECTURE.md` — full system architecture, folder structure, and tech decisions (rev 3).
- `ROADMAP.md` — phased delivery plan with explicit exit criteria.
- `DECISIONS.md` — twenty ADRs (ADR-0001 … ADR-0020).
- `CONTRIBUTING.md` — coding standards and contribution workflow.
- `API_CONTRACT.md` — API surface designed before implementation.
- **CR-1** AI Provider Plugin System (`BasePlugin` + capability ABCs + `@register_plugin`).
- **CR-2** Multiple Rendering Pipelines (Pipeline A stock-footage, B AI-images-motion, C AI-video-clips).
- **CR-3** Split AI orchestration into seven subpackages: `agents`, `providers`, `prompts`, `memory`, `tools`, `chains`, `workflows`.
- **CR-4** Event Bus (Redis Streams default, NATS/Kafka pluggable) with canonical topic registry and transactional outbox.
- **CR-5** Multi-storage Provider plugins (Local / S3 / R2 / Azure Blob / GCS).
- **CR-6** Versioned Projects — immutable `ProjectVersion` snapshots, branching, restore.
- **CR-7** Resumable Workflow Engine with Postgres checkpointer.
- **CR-8** Asset Library — auto-persist every generated artefact.
- **CR-9** Feature Flags — pluggable provider, default DB-backed, optional Unleash.
- **CR-10** Explicit Domain Layer — framework-free `app/domain/` with named aggregate roots.
- **CR-11** AI Model Registry — model catalogue, deprecation lifecycle, default-selection chain.
- **CR-12** AI Cost Tracking — single recorder middleware producing immutable `UsageRecord` per call.
- **CR-13** Five-tier Priority Queues — `critical / high / normal / low / background` with tenant fairness.

#### Approved
- 2026-06-28 — User approved Phase 1 Rev 3; Phase 2 unlocked.

---

## How to Update This Changelog

When a phase is accepted:

1. Move the **Unreleased** section into a new dated entry: `## [Phase N — YYYY-MM-DD]`.
2. Group changes under: **Added**, **Changed**, **Deprecated**, **Removed**, **Fixed**, **Security**.
3. Reference ADRs and CRs by ID.
4. Start a fresh **[Unreleased]** block.

Format example:

```
## [Phase 2 — 2026-MM-DD] — Database

### Added
- Alembic baseline migration.
- ORM models for every aggregate root listed in `ARCHITECTURE.md` §6.
- pgvector extension.

### Security
- Per-row `tenant_id` enforced via DB-level row-level security policies.
```
