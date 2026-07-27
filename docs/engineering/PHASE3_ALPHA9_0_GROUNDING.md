# α9.0 — Creator Analytics Foundation · Grounding (read-only)

**Baseline:** `v0.4.42-phase3-alpha8.9c` · **Status:** facts only, no design, no code.

Objective of this grounding: verify, against the source at the baseline, exactly what already
exists to build a **foundation for creator analytics** on the dormant `analytics_events`
infrastructure — and to determine, strictly from the repository, whether activation stays
additive or **genuinely requires a migration and/or an ADR**.

Scope reminder (from the slice brief): *activate analytics event writing for existing completed
actions; an event repository/service; a read model for basic analytics; an owner-scoped analytics
API; deterministic event schema; full unit + integration coverage.* Out of scope: charts,
frontend, BI/reporting, scheduled aggregation, data warehouse, email, push, AI insights,
recommendation engine, additional destinations, runtime redesign.

---

## 1. The `analytics_events` table — what already exists

| Aspect | Fact (verified) | Source |
|---|---|---|
| ORM model | `class AnalyticsEvent(Base)` — columns `id`, `tenant_id?`, `user_id?`, `session_id?`, `event_name`, `properties` (JSONB, default `{}`), `ip?`, `user_agent?`, `occurred_at` (tz, default `now()`) | `backend/app/infrastructure/db/models/analytics.py:26-64` |
| Primary key | Composite `PRIMARY KEY (id, occurred_at)` — the partition key must be part of the PK | `analytics.py:31`, `alembic/versions/0001_baseline.py:1224` |
| Partitioning | `PARTITION BY RANGE (occurred_at)`, **monthly**; baseline pre-creates `-1 … +24` months + a default catch-all partition | `analytics.py:39`, `0001_baseline.py:156-171`, `159-201` |
| Indexes | `ix_analytics_events_tenant_id_occurred_at`, `ix_analytics_events_event_name_occurred_at`, GIN `ix_analytics_events_properties_gin` | `0001_baseline.py:1228-1239` |
| Immutability | In `_IMMUTABLE_TABLES` → `BEFORE UPDATE OR DELETE` trigger `reject_mutation()` raises `"Table % is immutable"`. **INSERT is allowed**; append-only | `0001_baseline.py:1579-1594`, `113-117` |
| Schema-validator expectations | Listed in `EXPECTED_PARTITIONED` **and** `EXPECTED_IMMUTABLE`; GIN index asserted | `backend/scripts/validate_schema.py:60,62-71,87` |
| Application usage | **Dormant.** `AnalyticsEvent` / `analytics_events` appears in CODE only in the ORM model, the model registry (`models/__init__.py:11`), the baseline migration, `validate_schema.py`, `regenerate_erd.py`, `alembic/env.py`, and `tests/test_metadata.py`. **No repository, use case, API route, service, or subscriber reads or writes it.** | grep across `backend/app/` |

**Key facts for design:**

- There is **no `user_id` index** — only `(tenant_id, occurred_at)` and `(event_name, occurred_at)`.
  An owner-scoped read filtering by `user_id` would be a residual filter on top of the
  tenant/time index (acceptable for small per-creator volumes; a dedicated index would be a
  migration).
- There is **no `source_event_id` column and no unique/dedup constraint** of any kind. The table
  today cannot DB-enforce "one analytics row per source event."

---

## 2. The event-production surface already exists (no producer change needed)

Completed actions already emit **terminal outbox events** through the transactional outbox. A new
analytics consumer can derive analytics **without touching any producer** (frozen runtimes stay
untouched). Verified event types with useful identity/scope in-payload:

| Aggregate | Terminal events (already emitted) | Identity fields in payload | Emitter |
|---|---|---|---|
| `publish_job` | `PublishJobCreated`, `PublishJobSucceeded`, `PublishJobFailed` | `project_id`, **`requested_by_user_id`**, `social_account_id`, `platform`, … (Succeeded adds `platform_post_id/url`, `published_at`; Failed adds neutral `error`) | `use_cases/publishing/_events.py`; `create_publish_job.py:190`, `process_publish_job.py:207,269` |
| `export_job` | `ExportJobCreated`, `ExportJobSucceeded`, `ExportJobFailed` | **`requested_by_user_id`**, `render_job_id`, `format`, `quality`… (Succeeded adds `output_media_asset_id`, `file_size_bytes`) — **no `tenant_id`/`project_id`** | `use_cases/export/_events.py`; `process_export_job.py:257,377` |
| `render_job` | `RenderJobCreated/Canceled/Succeeded/Failed` | `project_id`, `timeline_id`, … — **no `owner_user_id`** (metadata carries `actor_user_id`) | `use_cases/render/_events.py`; `process_render_job.py:272,436` |
| `workflow_run` | `WorkflowRunCreated/Started/…/Succeeded/Failed/Canceled` | `project_id`, `workflow_key`, … — **no `owner_user_id`** (metadata `actor_user_id`) | `use_cases/workflow/_events.py` |
| `generation` | `generation.started/shot_generated/verification_failed/repair_succeeded/video_rendered/export_completed` | `title`, `shot_count`, `asset_id`, `duration_seconds`, … — **no `tenant_id`/`owner_user_id`/`project_id`** | `use_cases/generation/events.py`; `infrastructure/generation/execution_runtime_store.py:181-193` |

**Fact — payload identity is uneven.** Only **publish** and **export** events carry an explicit
end-user id (`requested_by_user_id`). Render/workflow/generation events carry `project_id` and/or
`actor_user_id` in **metadata**, not an owner id in the payload. For **owner-scoped** analytics,
the cleanly self-describing events today are the **publish** and **export** terminal events (plus
`PublishJobCreated`). Anything owner-scoped from render/workflow/generation would require resolving
owner from `project_id`/metadata at consume time (an extra read), or would be tenant-scoped only.

**Fact — no analytics-relevant hook is missing a producer.** Every "completed action" in the brief
(publish succeeded/failed, export succeeded/failed, publish created) already has an outbox event.
The α8.8 media-promotion and `CreateProject` are the only adjacent actions with **no** outbox event
(structlog only: `promote_generation_assets.py:202-208`, `create_project.py`).

---

## 3. The consumer seam already exists (this is the additive fit)

The outbox → relay → in-process publisher fan-out is the established, **additive** way to react to
completed actions without editing producers — exactly what ADR-0042 anticipates ("new capability
plugs in as additive outbox consumers").

- `PublisherPort` / `EventHandler` / `OutboxEvent` — `application/interfaces/publisher.py:26-71`.
  The handler contract is explicit: **"handlers must be idempotent on `event.id`" (at-least-once)**
  — `in_process_publisher.py:8`, `publisher.py:52-56`.
- `InProcessPublisher` fans one event to every handler **in order**; if any handler raises, the
  **whole** publish is marked failed and the relay **redelivers the event to all handlers again** —
  `in_process_publisher.py:34-47`. Delivery is at-least-once (`RelayService`,
  `use_cases/relay/relay_service.py`).
- Current registration (the list a new consumer would join) — `core/container.py:350-356`:

  ```python
  _publisher = InProcessPublisher([
      GeneratedMediaIngestionSubscriber(get_ingest_generated_media_use_case),
      NotificationProjection(get_create_notification_use_case),
      PublishNotificationProjection(get_create_notification_use_case),
  ])
  ```

  Each consumer holds a **factory** (not an instance) so every delivery runs in its own fresh use
  case + Unit of Work (`container.py:348-349`).

- **Projection template to mirror** — `notifications/publish_notification_projection.py` and its
  export twin: a plain class implementing `async def __call__(self, event: OutboxEvent) -> None`,
  with a `_HANDLED_EVENT_TYPES` frozenset, "not-applicable → clean return", "malformed payload →
  log + clean return (never park the relay)", and "genuine DB failure → propagate".

**Fact:** an analytics writer implemented as one more `EventHandler` in this list is **strictly
additive** and touches **no** frozen runtime, no producer, and no existing consumer.

---

## 4. How existing consumers satisfy the idempotency contract (the decisive precedent)

Every existing outbox consumer is **idempotent on redelivery, DB-enforced** — never by an
application-level pre-check:

| Consumer | Idempotency mechanism | Source |
|---|---|---|
| `NotificationProjection` / `PublishNotificationProjection` → `CreateNotification` | Partial-unique index `uq_notifications_user_id_source_event_id (user_id, source_event_id)`; second write raises `ConflictError` → treated as a **no-op** | `create_notification.py:8-16,73-83`; migration `0009_notifications_source_event_id.py:10-14`; `notification_repository.py:68-76` |
| `GeneratedMediaIngestionSubscriber` → `IngestGeneratedMedia` | Deterministic storage key + `media_assets` uniqueness make a redelivery a natural no-op | `generated_media_subscriber.py:9-11` |

The house rule is explicit and load-bearing: **"correctness never depends on an application-level
pre-check"** (`create_notification.py:15-16`, invariant W8.5b.7).

**This is the crux for analytics.** `analytics_events` has **no** dedup key (see §1). To make an
analytics consumer idempotent on `event.id` the same way every other consumer is, the table needs
a DB-enforced uniqueness invariant (a `source_event_id` and a unique index). That is:

- a **new column + new constraint** on an existing, immutable, **partitioned** table → a
  **migration**; and
- because the table is `PARTITION BY RANGE (occurred_at)`, Postgres requires any unique index to
  **include the partition key** — i.e. the dedup key would have to be `UNIQUE (source_event_id,
  occurred_at)`, which only dedups correctly if `occurred_at` is set **deterministically** from the
  source event (e.g. `event.occurred_at`) so redeliveries collide.

---

## 5. The read side already has conventions to reuse

An owner-scoped read API mirrors the α8.9c dashboard pattern exactly:

- **Router:** thin router projects a DTO into `envelope(...)` (`api/v1/helpers.py:80-98`), registered
  in `main.py` via `app.include_router(<r>.router, prefix="/api/v1")`.
- **Auth/scope:** `CurrentUserDep` (`api/v1/deps.py:472`) yields the authenticated `User`; all scope
  (`tenant_id`, `owner_user_id`) comes from the caller, never the request body (ADR-0034).
- **Use case:** takes `IUnitOfWork`, `async with self._uow:`, reads, assembles a frozen result DTO
  (`get_creator_dashboard.py:72-111`).
- **DI:** `container.get_<x>_use_case()` factory + `deps.py` `XDep = Annotated[...]`.

**Fact — a read repository is missing.** There is no analytics read model, no `count … group by
event_name`/time query, and no analytics repository on the Unit of Work (§6). Existing indexes
support `WHERE tenant_id = ? AND occurred_at ∈ range` and `event_name` grouping; `user_id` is a
residual filter (no index).

---

## 6. Ports / Unit of Work — what is missing

- The UoW exposes 21 repositories (`application/interfaces/unit_of_work.py:66-86`): `users`,
  `tenants`, …, `notifications`, `social_accounts`, `publish_jobs`, `outbox`, `workflow_runs`,
  `usage`, `model_pricing`, etc. **There is no `analytics` repository** and **no
  `IAnalyticsEventRepository` / `IAnalyticsReadModel` interface anywhere** (grep: zero matches).
- Two established persistence styles exist to model a new analytics repository on:
  1. **UoW ORM repositories** (e.g. `notification_repository.py`) — transactional, registered on the
     UoW; the write consumer would use this (mirrors `CreateNotification`).
  2. **Direct-session raw-SQL readers** (e.g. `catalogue_reader.py:122-144`: `AsyncSession` +
     `sqlalchemy.text()` + `.mappings()`) — the read model / aggregation query could use this.

**Fact:** activation needs **new ports** (one write append repo for the consumer; one read/aggregate
repo for the API). New *additive* ports are consistent with prior slices (α8.8 added
`IGenerationReader`) and do not, by themselves, cross a frozen boundary.

---

## 7. CI gate

`backend/scripts/ci_gate.py` — highest stage is **17** (creator dashboard, `requires_db=True`).
Adding a DB-backed analytics integration stage follows the established "each new bounded context
earns its own stage" discipline: append `Stage(number=18, title="…", cmd=[py,"-m","pytest","-m",
"integration", "…"], requires_db=True)` and update the module docstring.

---

## 8. ADRs in play

- **ADR-0042 (orchestration freeze):** new capability must plug in as **additive outbox consumers**
  — analytics-as-consumer is squarely inside this. Not violated.
- **ADR-0041 / ADR-0039 / ADR-0040 (outbox/relay contract, D9 at-least-once):** the delivery
  contract analytics would consume under. Not changed.
- **ADR-0035 (immutable append-only tables, `reject_mutation`):** `analytics_events` is of this
  class. Appends are fine; the table stays append-only.
- **ADR-0030 (`export_jobs` partial-unique) and ADR-0033 (`usage_records.request_id` unique):**
  **direct precedent.** In both cases, promoting an **idempotency/uniqueness invariant into a DB
  constraint got its own ADR.** A `source_event_id` uniqueness invariant on `analytics_events` is
  the *same class of decision*.
- **ADR-0034 (authenticated owner-scoped endpoint):** the pattern the read API would follow. Not
  changed.

---

## 9. Conclusion — one genuine architectural decision surfaces (ADR gate)

Almost everything is reusable and additive:

- **Write:** activate analytics by adding **one more `EventHandler`** to the existing
  `InProcessPublisher` list, mapping already-emitted terminal events (cleanly, the **publish** and
  **export** families, which carry `requested_by_user_id`) into `analytics_events` appends. No
  producer change, no frozen-runtime change (ADR-0042-compliant).
- **Read:** a new owner-scoped `GET /api/v1/analytics/…` use case + DTO + router mirroring the
  α8.9c dashboard, backed by a new read/aggregate repository over the already-indexed
  `(tenant_id, occurred_at)` / `(event_name, occurred_at)` columns.
- **Ports:** two new **additive** ports (write append + read/aggregate). Consistent with prior
  slices; no port removed, no interface reshaped.

**The one thing that is *not* free is idempotency.** The existing, documented outbox-consumer
contract requires handlers to be **idempotent on `event.id`, DB-enforced, never by app-level
pre-check** (§4). `analytics_events` today has **no dedup key** (§1). Making the analytics consumer
honour that contract the house way requires a **new `source_event_id` column + a partition-aware
unique index** on the **immutable, partitioned** `analytics_events` table — i.e. a **migration that
introduces a new data-model uniqueness invariant**. By the **direct precedent of ADR-0030 and
ADR-0033**, promoting an idempotency/uniqueness invariant into a DB constraint is exactly the class
of change that has **earned its own ADR** in this codebase.

This is a **genuine architectural decision** (it changes the `analytics_events` data-model contract
enforced by `validate_schema.py` and documented in `schema.md §26`), and the slice brief's own rule
is *"introduce an ADR only if grounding proves activation of the analytics subsystem changes an
existing architectural boundary."* Grounding proves it does — **for the exactly-once path**.

The decision (for the ADR / pre-flight) is between:

- **Option A — exactly-once, DB-enforced (house-consistent):** add `analytics_events.source_event_id`
  + `UNIQUE (source_event_id, occurred_at)` (deterministic `occurred_at = event.occurred_at`); the
  consumer appends with `ON CONFLICT DO NOTHING` / `ConflictError`-as-no-op. **Requires a migration
  and, by ADR-0030/0033 precedent, an ADR.**
- **Option B — at-least-once, no schema change (strictly additive, no migration, no ADR):** the
  consumer appends and stores the source `event.id` inside `properties` for traceability; duplicates
  are possible on the rare relay redelivery, and the read model would dedup-on-read (or tolerate
  double counts). This **reinterprets** the documented "handlers must be idempotent on `event.id`"
  consumer contract for the analytics consumer — itself an architectural stance worth an explicit
  decision.

**Both credible resolutions turn on an architectural invariant (the outbox-consumer idempotency
contract and the `analytics_events` data-model contract). Per the workflow's mandatory stop
condition — "Stop only if an ADR is required" — this grounding stops here and recommends opening an
ADR to rule between Option A and Option B before any implementation.** No branch, commit, PR, code,
or migration has been created; the repository remains at the frozen baseline
`v0.4.42-phase3-alpha8.9c` and this document is an uncommitted working-tree file.
