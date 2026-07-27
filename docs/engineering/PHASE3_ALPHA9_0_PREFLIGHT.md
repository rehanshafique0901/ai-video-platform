# α9.0 — Creator Analytics Foundation · Pre-flight (design)

**Baseline:** `v0.4.42-phase3-alpha8.9c` · **Milestone:** α9.0 · **Target version:**
`0.4.43-phase3-alpha9.0-dev` (held until release review).
**Depends on:** [`ADR-0048`](../decisions/ADR-0048-analytics-consumer-idempotency.md) (Accepted —
DB-enforced exactly-once analytics writing) · Grounded by
[`PHASE3_ALPHA9_0_GROUNDING.md`](./PHASE3_ALPHA9_0_GROUNDING.md).

Design-only; no implementation code. Resolves every open question the ADR deferred and freezes the
α9.0 scope. Implementation direction is fixed by the review: **DB-enforced exactly-once; consumer
idempotent on `event.id`; no application-level deduplication; the existing outbox-consumer contract
is preserved.**

---

## AN0 — Scope

**In scope (strictly additive):**

- Activate analytics **writing** for already-completed actions via a new **downstream outbox
  consumer** (no producer/runtime change).
- A single new **analytics repository** (write append + owner-scoped read/aggregate) on the Unit of
  Work.
- A **read model** (`event_name` counts over a time window) and an **owner-scoped analytics API**.
- One **additive migration** (`0015`) implementing ADR-0048's dedup key + one read-serving index.
- Deterministic event schema, full unit + integration coverage, one new CI stage.

**Out of scope (deferred, per the brief):** charts, frontend, BI/reporting, scheduled aggregation,
data warehouse, email, push, AI-generated insights, recommendation engine, additional destinations,
runtime redesign. Also deferred within this slice: analytics for render/workflow/generation events
(§AN2 rationale), time-bucketed series, and retention/rollups.

**Frozen boundaries — untouched:** orchestration/execution/generation/render/export/publish runtime,
the Asset Promotion Bridge, AI providers, Planner, verification pipeline. No producer is edited; the
outbox/relay contract (ADR-0041 D9) and the immutability of `analytics_events` (append-only) are
preserved.

---

## AN1 — Write path is a downstream outbox consumer (not inline)

**Decision.** Add one `EventHandler`, `AnalyticsProjection`, to the existing `InProcessPublisher`
fan-out (`core/container.py`), alongside `NotificationProjection` and `PublishNotificationProjection`.
It is registered via a **factory** so each delivery runs in a fresh use case + Unit of Work
(`container.py:348-356` pattern).

**Why.** Every completed action already emits a terminal outbox event; consuming them is the only
boundary-respecting way to write analytics without editing frozen runtimes (ADR-0042 "additive
consumers"). Writing inline in `process_publish_job` / `process_export_job` is rejected — it edits
frozen code.

**Shape (mirrors `PublishNotificationProjection` exactly):**
`async def __call__(self, event: OutboxEvent) -> None`, with `_HANDLED_EVENT_TYPES`; non-applicable
type → clean return; malformed payload (missing/invalid recipient) → log + clean return (never park
the relay); genuine DB failure → propagate (relay retries). It never orchestrates and never invokes
another projection (fan-out rule).

---

## AN2 — First event set: the owner-attributable publish + export lifecycle

**Decision.** α9.0 projects the **publish** and **export** lifecycle events, which already carry an
explicit end-user id (`requested_by_user_id`) in their payload:

| Outbox `event_type` | Source payload identity | Projected? |
|---|---|---|
| `PublishJobCreated` / `PublishJobSucceeded` / `PublishJobFailed` | `requested_by_user_id`, `project_id`, `social_account_id`, `platform`, … | ✅ |
| `ExportJobCreated` / `ExportJobSucceeded` / `ExportJobFailed` | `requested_by_user_id`, `render_job_id`, `format`, `quality`, … | ✅ |

**Deferred (with rationale, not a gap):** `render_job.*`, `workflow_run.*`, and `generation.*` events
carry `project_id` and/or `actor_user_id` **in metadata**, but **no owner user-id in the payload**
(`PHASE3_ALPHA9_0_GROUNDING.md §2`). Owner-scoping them would require resolving owner from
`project_id` at consume time — additional coupling beyond a foundation. They are explicitly deferred;
the consumer's `_HANDLED_EVENT_TYPES` design makes adding them later a pure extension.

**Why publish+export first.** They are the two genuinely *creator-completed* actions in the product
funnel, are already owner-attributable without an extra lookup for the user id, and are already
credential-/secret-free (PUB-8 / ADR-0047 C8) so their payloads are safe to project.

---

## AN3 — Deterministic event schema

**Decision.** A fixed, stable `event_name` vocabulary maps 1:1 from the outbox `event_type`:

| Outbox `event_type` | analytics `event_name` |
|---|---|
| `PublishJobCreated` | `publish.created` |
| `PublishJobSucceeded` | `publish.succeeded` |
| `PublishJobFailed` | `publish.failed` |
| `ExportJobCreated` | `export.created` |
| `ExportJobSucceeded` | `export.succeeded` |
| `ExportJobFailed` | `export.failed` |

`properties` (JSONB) stores a **neutral identity subset** copied from the event payload — e.g.
`{aggregate_id, project_id?, platform?, format?, quality?}` — plus `source_event_type` for
traceability. **No secrets, tokens, URLs, or bytes** (the source events already carry none; the
projection copies only whitelisted keys). The vocabulary and property keys are frozen constants in
one module so tests assert them verbatim (deterministic schema).

---

## AN4 — Idempotency (ADR-0048, verified)

**Decision.** Exactly-once is **DB-enforced**, never by an app-level pre-check:

- The consumer writes `source_event_id = event.id` and **`occurred_at = event.occurred_at`**
  (deterministic — load-bearing per ADR-0048 §empirical verification Test 2c).
- The write use case `RecordAnalyticsEvent` (mirrors `CreateNotification`) appends inside its own
  UoW and treats the unique-violation `ConflictError` as an **idempotent no-op** (`status="duplicate"`).
- The repository `add(...)` maps `IntegrityError` (23505 on `uq_analytics_events_source_event_id`) →
  `ConflictError`, exactly like `NotificationRepository.add` (`notification_repository.py:66-77`).

A relay redelivery of the same event therefore collides on `(source_event_id, occurred_at)` and is a
clean no-op — preserving the "handlers must be idempotent on `event.id`" contract.

---

## AN5 — Tenant + owner attribution

**Decision.** Populate **both** `analytics_events.user_id` (from `requested_by_user_id`) and
`analytics_events.tenant_id`, resolving `tenant_id` from the user row via `uow.users.get_by_id(user_id)`
inside the write use case's UoW (one indexed PK read per event).

**Why.** Publish/export event payloads carry `requested_by_user_id` but **not** `tenant_id`
(`PHASE3_ALPHA9_0_GROUNDING.md §2`). Populating `tenant_id` (a) keeps analytics multi-tenant-correct,
(b) lets owner-scoped reads filter `tenant_id = ? AND user_id = ?` defensively, and (c) lets the
existing `ix_analytics_events_tenant_id_occurred_at` index serve tenant/time scans. The extra read is
a single PK lookup; if the user is missing (deleted mid-flight), the projection logs + clean-returns
(the event is not owner-attributable) rather than parking the relay.

---

## AN6 — Migration `0015` (additive; ADR-0048)

**Decision.** One hand-written migration `0015_analytics_events_source_event_id` (`down_revision =
"0014_publish_jobs"`), mirroring `0009`:

```sql
-- ADR-0048: DB-enforced exactly-once dedup key (partition-key-inclusive, partial)
ALTER TABLE analytics_events ADD COLUMN source_event_id uuid NULL;

CREATE UNIQUE INDEX uq_analytics_events_source_event_id
  ON analytics_events (source_event_id, occurred_at)
  WHERE source_event_id IS NOT NULL;

-- owner-scoped read-serving index (this slice's read model filters by user_id + time)
CREATE INDEX ix_analytics_events_user_id_occurred_at
  ON analytics_events (user_id, occurred_at)
  WHERE user_id IS NOT NULL;
```

- Both are **parent-level** partitioned indexes that PostgreSQL 17 auto-propagates to all current +
  future child partitions (ADR-0048 §empirical verification Test 5). Verified accepted.
- Additive and safe: nullable column + indexes on a table that is **empty in every environment**
  (no backfill, plain transactional DDL). `downgrade()` drops both indexes + the column, so the CI
  gate's upgrade→downgrade→upgrade round-trip (stages 5–7) runs on a clean slate.
- The `AnalyticsEvent` ORM model (`db/models/analytics.py`) is updated to declare the new column +
  both indexes in `__table_args__` (so `validate_schema.py` and the ORM stay in lock-step, per the
  ADR-0030 precedent). `validate_schema.py` `EXPECTED_*` sets and `schema.md §26` are updated to list
  the new column + indexes.

**Note.** `add_column` / `create index` are DDL and are **not** blocked by the append-only
`reject_mutation` trigger (which fires only on row `UPDATE`/`DELETE`).

---

## AN7 — Read model

**Decision.** A single owner-scoped aggregate read `summary_for_owner(tenant_id, user_id, since,
until)` returning `event_name → count` plus a total, implemented as a raw-SQL `COUNT(*) … GROUP BY
event_name` (the `catalogue_reader.py` `AsyncSession` + `sqlalchemy.text()` + `.mappings()` style)
over the `ix_analytics_events_user_id_occurred_at` index. The `GetCreatorAnalytics` use case (mirrors
`GetCreatorDashboard`) opens one UoW, calls the read, and assembles a frozen result DTO.

**Response is deterministic:** every projected `event_name` is present with `0` when absent (stable
shape, like the dashboard's publish-status counts), plus `total` and the resolved `window`.

**Performance.** Owner-scoped analytics volumes are small per creator; the query is index-served over
`(user_id, occurred_at)`. Time-bucketed series / rollups are deferred (out of scope — no charts).

---

## AN8 — API surface

**Decision.** One new authenticated, owner-scoped endpoint mirroring the α8.9c dashboard:

```
GET /api/v1/analytics/summary?since=<ISO8601>&until=<ISO8601>
  → 200 { data: { window:{since,until}, counts:{ "publish.succeeded":N, ... }, total:N }, meta }
  → 401 { error: { code: UNAUTHENTICATED } }        (via CurrentUserDep)
  → 422 { error: ... }                              (bad/naive/inverted window)
```

- **Auth/scope:** `CurrentUserDep` → all scope (`tenant_id`, `user_id`) from the caller, never from
  the query (ADR-0034); router projects the DTO into `envelope(...)`; registered in `main.py` via
  `app.include_router(analytics.router, prefix="/api/v1")`.
- **Window params:** optional `since`/`until`, validated at the Pydantic boundary to be
  **timezone-aware** and `since < until`, normalised to UTC (reusing the α8.9b `publish_at`
  `field_validator` idiom). **Default window:** trailing **30 days** when omitted. No pagination (a
  summary returns scalar counts).
- **DI:** `container.get_creator_analytics_use_case()` + `deps.py` `CreatorAnalyticsDep`.

---

## AN9 — Ports / Unit of Work

**Decision.** Exactly **one new additive port**, `IAnalyticsRepository`, registered on the Unit of
Work as `uow.analytics` — carrying **both** the write append and the owner-scoped read (the
`NotificationRepository` precedent: one adapter, write `add` + owner-scoped reads). Bound in
`SqlAlchemyUnitOfWork.__aenter__` alongside the other repositories. No existing port is changed or
removed. New additive ports are consistent with prior slices (α8.8 `IGenerationReader`).

- Write: `add(*, tenant_id, user_id, event_name, properties, source_event_id, occurred_at) → …`
  (idempotent; `IntegrityError`→`ConflictError`).
- Read: `summary_for_owner(*, tenant_id, user_id, since, until) → list[(event_name, count)]`.

---

## AN10 — CI gate

**Decision.** Add **Stage 18 — "creator analytics integration"** (`requires_db=True`) to
`ci_gate.py`, following the "each new bounded context / read surface earns its own stage" discipline
(Stages 15/16/17). Update the module docstring. Static stages (mypy/import-linter/unit) already cover
the consumer + use cases.

---

## AN11 — Testing

**Unit:**
- `AnalyticsProjection`: each handled `event_type` → correct `event_name` + neutral property subset;
  owner target = `requested_by_user_id`; **deterministic `occurred_at = event.occurred_at`**;
  non-applicable event → clean return; malformed payload (missing/invalid `requested_by_user_id`) →
  log + clean return; missing user (tenant unresolved) → clean return.
- `RecordAnalyticsEvent`: `created` vs `duplicate` (ConflictError → no-op).
- `GetCreatorAnalytics`: aggregation shape (all event_names present, zero-fill, total, window default).
- Window validation: naive/inverted windows rejected.

**Integration (DB-backed, Stage 18):**
- Success + failure events → rows written with correct `event_name`/`user_id`/`tenant_id`.
- **Exactly-once:** redelivering the same `OutboxEvent` writes **one** row (ADR-0048 constraint).
- Owner isolation: a fresh user's summary is all-zero.
- API visibility: seed committed events → `GET /analytics/summary` returns the expected counts (auth
  gate + envelope), reusing the committed-seed/cleanup pattern from the notifications/dashboard suites.

---

## AN12 — Documentation & wiring (implementation-time)

`CHANGELOG.md` (α9.0 entry), `schema.md §26` + `validate_schema.py` (new column + indexes),
`INDEX_STRATEGY.md` (two new indexes), and `DECISIONS.md` (add the **ADR-0048** cross-link — note the
index is currently stale at ADR-0034; α9.0 adds only the 0048 entry, it does not backfill 0035–0047).
`PLATFORM_STATUS.md` / `SYSTEM_MAP.md` are updated at the post-release docs-sync (new baseline +
analytics component + Stage 18), consistent with prior slices.

---

## AN13 — Architecture confirmation

- **New port?** Yes — one additive `IAnalyticsRepository` (write+read). No existing port changed.
- **New migration?** Yes — `0015`, additive (ADR-0048): dedup column + unique index + one read index.
  Empirically verified valid on the partitioning scheme.
- **New ADR?** No — **ADR-0048 already governs** the single architectural decision (idempotency
  boundary). Everything else here is additive application/read wiring within established patterns
  (ADR-0034 auth, ADR-0041/0042 outbox consumers). If implementation surfaces a contradiction with
  ADR-0048, stop and amend the ADR rather than proceeding.
- **Frozen boundary crossed?** No.

---

## AN14 — Implementation order

domain/schema constants → `IAnalyticsRepository` port → migration `0015` + ORM model update →
repository impl → `RecordAnalyticsEvent` write use case → `AnalyticsProjection` consumer + register in
`InProcessPublisher` → `GetCreatorAnalytics` read use case → API router/DTO/deps/container →
`validate_schema.py`/docs → unit tests → integration tests + CI Stage 18 → full ephemeral-Postgres
gate → bump `0.4.43-phase3-alpha9.0-dev`, single feature commit, push, open release-review PR → **stop.**
