# Phase 3 Slice α7.3 — Outbox Relay + Distributed Lock Manager — Pre-flight

> Status: **DRAFT — AWAITING SIGN-OFF.** The provider-runtime architecture and
> its runtime decisions (D1–D13) were signed off in
> [`ADR-0041`](../decisions/ADR-0041-provider-runtime-contract.md) and
> [`docs/architecture/CONTENT_GENERATION_PIPELINE.md`](../architecture/CONTENT_GENERATION_PIPELINE.md)
> (2026-07-17, α8.0, docs-only). This doc resolves the **α7.3-specific** open
> questions (§4). Nothing is implemented yet.
>
> Mirrors the α5/α6/α7.1/α7.2 discipline: ground in the physical schema → lock
> decisions → sign-off → branch → implement → CI → merge → tag. Read-only
> planning artefact.
>
> **Predecessor.** α7.2 (`v0.4.16`, tag `v0.4.16-phase3-alpha7.2`; `main` also
> carries docs commit `6d1cbb8` adding ADR-0041) — the `WorkflowRun` runner; the
> second orchestration aggregate. Together α7.1 (`RenderJob`) and α7.2
> (`WorkflowRun`) are the **only D9 outbox producers** today: they write
> `event_outbox` rows inside their UnitOfWork and leave `published_at` NULL. **No
> relay publishes them, and `distributed_locks` has zero application consumers.**
>
> **This is the first pure-infrastructure slice — the two seams the runtime
> stands on.** α7.3 does **not** add an aggregate. It bridges two *already-built,
> currently-inert* baseline tables to working application infrastructure:
>
> 1. **Outbox relay** — reads unpublished `event_outbox` rows
>    (`FOR UPDATE SKIP LOCKED`), publishes them via a publisher port, stamps
>    `published_at`, and records `attempts` / `last_error` on failure (ADR-0041 D9).
> 2. **Distributed lock manager** — the first consumer of `distributed_locks`:
>    acquire / renew (heartbeat) / release / steal-after-expiry / janitor over the
>    baseline lease table + its `lease_until > acquired_at` CHECK (ADR-0032, ADR-0041 D8).
>
> Per the runner-before-worker discipline (blueprint §13, α8.0 sign-off), α7.3
> ships these as **library seams driven by tests** — **no broker, no Celery, no
> Redis, no daemon loop, no provider, no pipeline, no workflow-execution change.**
> The background worker that *drives* the relay on a loop and the workers that
> *hold* locks are α8.1 (where Celery + Redis first appear).
>
> **Baseline versioning.** `main` is at `0.4.16` (tag `v0.4.16-phase3-alpha7.2`).
> First α7.3 commit bumps `backend/app/main.py` → `"0.4.17-phase3-alpha7.3-dev"`.
> **Zero migrations** (blueprint D7 / ADR-0041) — `event_outbox` and
> `distributed_locks` (+ their indexes and the `chk_distributed_locks_lease_until_after_acquired_at`
> CHECK) already exist in baseline `0001` (`schema.md` §27, §32; ADR-0032).

---

## Section 1 — Scope

### 1.1 One-line thesis

α7.3 turns two inert baseline tables into working infrastructure: an **outbox
relay** that reliably publishes the events α7.1/α7.2 already produce and stamps
them `published_at`, and a **distributed lock manager** that correctly acquires,
renews, releases, and reclaims expired leases on `distributed_locks`. Both are
**pure infrastructure ports + SQLAlchemy implementations exercised by tests** —
they introduce **no aggregate, no HTTP surface (unless §4 Q1 says otherwise), no
broker, and no background loop.** They are the seams α7.4→α8.x consume; nothing
above them changes.

### 1.2 What's in

1. **Distributed lock manager** (ADR-0041 D8, over ADR-0032's table):
   - `acquire(key, owner, lease)` — insert-or-steal: take a free key, or steal one
     whose `lease_until < now()`, in a single atomic round-trip; returns a `Lease`
     (or `None` if held by a live owner).
   - `renew(lease)` / heartbeat — extend `lease_until` + bump `heartbeat_at`,
     **owner-fenced** (`WHERE lock_key=? AND owner=?`).
   - `release(lease)` — free the key, **owner-fenced** (idempotent).
   - **steal-after-expiry** — folded into `acquire` (an expired lease is stolen,
     not left for a separate call), so correctness never depends on the janitor.
   - `reclaim_expired(now)` **janitor** — cleanup of abandoned expired rows;
     returns a count. A **method**, not a daemon (§4 Q5).
2. **Outbox relay** (ADR-0041 D9, over `event_outbox`):
   - `fetch_unpublished(limit)` — `SELECT … WHERE published_at IS NULL [AND
     attempts < cap] ORDER BY occurred_at … FOR UPDATE SKIP LOCKED` batching
     (uses `ix_event_outbox_unpublished_occurred_at`).
   - `mark_published(id, now)` — stamp `published_at`.
   - `mark_failed(id, error)` — bump `attempts`, set `last_error`, leave
     `published_at` NULL (at-least-once retry surface).
   - a `RelayService.relay_once(batch)` use case that pulls a batch, publishes
     each via the **publisher port**, and marks published/failed — **one explicit
     call, no loop** (§4 Q1).
3. **Publisher port** — a `Publisher` protocol the relay calls to emit an event,
   with a default **in-process** implementation (structured-log / no-op sink) so
   the relay is fully testable with **no broker** (§4 Q2). Real bus/broker
   fan-out is α7.6 / α8.1.
4. **Ports + impls**: `IDistributedLockManager` (new port) + SQLAlchemy impl;
   `event_outbox` relay read/mark methods (extend `IEventOutboxRepository` or a
   sibling relay repo — §4 Q6) + impl; DI wiring; **unit + integration tests**;
   docs (`CHANGELOG`, `ROADMAP`, `API_CONTRACT` if any surface, architecture notes,
   ADR only if a decision lands outside ADR-0041 — §4).

### 1.3 What's out (deferred)

- **Any provider, capability port, registry, dispatcher, or mock provider** — α7.4.
- **Celery, Redis, a broker, a background daemon/loop that drives the relay, and
  any worker that *holds* a lock during real work** — α8.1 (runner-before-worker).
- **Real event bus / fan-out to consumers** — the α7.3 publisher is an in-process
  sink; wiring events to actual handlers is α7.6+.
- **`event_log` projection** — whether the relay also appends published events into
  the immutable, partitioned `event_log` canonical store is deferred unless §4 Q2
  says otherwise (α7.3 targets `event_outbox` only).
- **Dead-letter table / DLQ** — zero-migration; poison rows stay in `event_outbox`
  with high `attempts` + `last_error`, surfaced by metrics, not moved (§4 Q3).
- **Full `idempotency_keys` ledger, usage recording, pipeline execution,
  rendering, export** — α7.5 / α7.6 / α8.x.
- **Workflow/render lock *acquisition* by the runner** — α7.2 Q6 kept locks
  "convention only"; α7.3 builds the *manager* but does not wire it into the
  synchronous runner (no concurrent workers exist yet).
- **Zero migrations.**

---

## Section 2 — Grounded facts (the two physical tables + their state today)

From `backend/app/infrastructure/db/models/events.py`,
`…/models/operations.py`, baseline `0001` (`schema.md` §27, §32), ADR-0022,
ADR-0032, and ADR-0041.

### 2.1 `event_outbox` — `EventOutbox(CreatedAtOnlyMixin, Base)` (CR-4)

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | `gen_random_uuid()` |
| `aggregate_type` | Text NOT NULL | e.g. `render_job`, `workflow_run` |
| `aggregate_id` | UUID NOT NULL | source aggregate id |
| `event_type` | Text NOT NULL | e.g. `RenderJobCreated`, `WorkflowStepCompleted` |
| `event_version` | Text NOT NULL | server default `'1.0'` |
| `payload` | JSONB NOT NULL | event body (orchestration fields only, D3.10) |
| `metadata` (`metadata_json`) | JSONB NOT NULL | server default `'{}'` (e.g. `actor_user_id`) |
| `occurred_at` | timestamptz NOT NULL | domain-event instant — the **relay ordering key** |
| `published_at` | timestamptz **nullable** | **NULL = unpublished**; the relay stamps this |
| `attempts` | Integer NOT NULL | server default `0`; the relay bumps this on failure |
| `last_error` | Text nullable | the relay records the last publish error here |
| `created_at` | timestamptz NOT NULL | `CreatedAtOnlyMixin` — **no `updated_at`** |

Indexes: `ix_event_outbox_unpublished_occurred_at` = partial
`(occurred_at) WHERE published_at IS NULL` (the relay's fetch index);
`ix_event_outbox_aggregate_type_aggregate_id`.

**Mutable by design.** `event_outbox` is **not** in the baseline
`_IMMUTABLE_TABLES` / `reject_mutation` trigger set (that set is
`project_versions, ai_model_pricing, workflow_checkpoints, usage_records,
credit_ledger, analytics_events, event_log, audit_log`). The relay may freely
`UPDATE published_at / attempts / last_error` — those three columns exist
*specifically* for the relay. (Contrast `event_log` §2.3, which **is** immutable.)

### 2.2 `distributed_locks` — `DistributedLock(Base)` (CR-DB-2)

| Column | Type | Notes |
|---|---|---|
| `lock_key` | Text **PK** | natural key, one row per lock (e.g. `render_job:<uuid>`) |
| `owner` | Text NOT NULL | who holds it — the **fencing identity** (§4 Q4) |
| `lease_until` | timestamptz NOT NULL | lease expiry; a lease is live iff `lease_until > now()` |
| `heartbeat_at` | timestamptz NOT NULL | last renew instant |
| `acquired_at` | timestamptz NOT NULL | server default `now()`; when this holder took it |
| `metadata` (`metadata_json`) | JSONB NOT NULL | server default `'{}'` |

Constraints / indexes:
`chk_distributed_locks_lease_until_after_acquired_at` = `CHECK (lease_until >
acquired_at)` (ADR-0032 — a zero-second/negative lease is an `IntegrityError` at
the call site, **not** silent corruption); `ix_distributed_locks_lease_until`
(the janitor's scan index). **Also mutable** (not in the immutable set) — acquire
upserts, renew/steal update, release/janitor delete.

**Canonical lock keys** (ADR-0022 / ADR-0032 §Context): `render_job:<uuid>`,
`workflow_run:<uuid>`, `project_publish:<uuid>`, `timeline_edit:<uuid>`. α7.3
builds the *manager*; wiring these keys to callers is a later slice.

### 2.3 `event_log` — immutable, partitioned (NOT an α7.3 target)

`EventLog` is append-only (in `_IMMUTABLE_TABLES`, `tg_event_log_bud_reject_mutation`)
and `RANGE (occurred_at)` monthly-partitioned. It is the *canonical* event store
in the blueprint (§28) but has **no application writer today**. Whether the relay
projects published events into it (INSERT-only; needs partition presence) is a
**genuine open question** — see §4 Q2. Default recommendation: **defer** (α7.3
stamps `event_outbox.published_at` and hands off to the publisher; it does not
write `event_log`).

### 2.4 Existing producers / consumers / missing infrastructure

- **Producers (exist):** `EventOutboxRepository.add(...)` (α7.1), called by the
  `render` use cases (`RenderJobCreated` / `RenderJobCanceled`) and the `workflow`
  use cases (`_events.py`: `WorkflowRunCreated/Started`, `WorkflowStepCompleted`,
  `WorkflowRunSucceeded/Failed/Canceled`). Both write inside the caller's UoW and
  leave `published_at` NULL. This port is **add-only** today (docstring: "no
  reads, no updates").
- **Consumers (missing):** none. No relay, no dispatcher, no publisher. No code
  reads `published_at IS NULL`. `distributed_locks` has **zero** references
  outside the ORM model + baseline migration (ADR-0032 §Context confirms this).
- **Missing infrastructure α7.3 supplies:** (1) relay read/mark surface + service +
  publisher port; (2) lock-manager port + impl + janitor. Nothing else.

### 2.5 Repository / testing conventions (to mirror)

- **Ports** live in `app/application/interfaces/` as ABCs
  (`repositories.py`, `unit_of_work.py`); **impls** in
  `app/infrastructure/repositories/`; wired onto the concrete UoW's `__aenter__`
  and surfaced as typed attributes on `IUnitOfWork` (`outbox`, `render_jobs`,
  `workflow_runs`, …). Use cases depend only on the ports.
- **DI**: container factories + `deps` aliases (the α7.1/α7.2 wiring pattern).
- **Tests**: `pytest -m unit` (fakes, no DB) + `pytest -m integration` (real
  Supabase session, SAVEPOINT-per-test rollback). Unit fakes implement the port
  ABCs. Full gate = `scripts/ci_gate.py` (ruff, black, mypy, import-linter, unit,
  integration, schema validate, ERD, migration round-trip, coverage).
- **Transaction boundary caveat (load-bearing for α7.3):** the outbox producer
  runs *inside a request's UoW*. The **relay runs outside any request** — it owns
  its **own** transaction per batch, and `FOR UPDATE SKIP LOCKED` requires an open
  transaction that spans fetch→publish→mark. §4 Q6 fixes where that transaction
  comes from (UoW reuse vs a dedicated session), keeping the future worker's
  session-factory need in view without building the worker.

---

## Section 3 — Decisions (recommended)

- **D3.1 — α7.3 is infrastructure, not an aggregate.** No domain entity, no
  `projects.version` interaction, no cross-aggregate coordination. Two ports +
  two SQLAlchemy impls + one relay service + one publisher port. Extends the
  blueprint's "reuse existing tables" ledger (ADR-0041 §reuse ledger: relay and
  lock manager are the two ⛔→**α7.3** rows).
- **D3.2 — At-least-once, idempotent-consumer relay (ADR-0041 D9).** The relay
  guarantees **at-least-once** delivery: publish-then-mark means a crash between
  publish and `mark_published` re-delivers on the next pass. Exactly-once is a
  consumer concern (consumers dedupe on event `id`) — the relay never promises it.
- **D3.3 — Fetch via the partial index, `SKIP LOCKED`, `occurred_at` order.**
  `WHERE published_at IS NULL … ORDER BY occurred_at … FOR UPDATE SKIP LOCKED
  LIMIT :batch` — matches `ix_event_outbox_unpublished_occurred_at`, lets N relay
  passes (future workers) run without blocking each other, and gives **best-effort
  chronological** delivery (ADR-0041 D9: never a *total* order guarantee).
- **D3.4 — Publish is a port; the α7.3 default is in-process (ADR-0041 D11).**
  The relay depends on a `Publisher` protocol, not a broker. The α7.3
  implementation is a structured-log / no-op sink that always succeeds; it exists
  to make `published_at` stamping and failure accounting testable. Broker fan-out
  is introduced with the first real provider (α8.1).
- **D3.5 — Lock correctness lives in `acquire` (ADR-0032 + D8).** A single atomic
  statement (upsert / conditional update predicated on `lease_until < now()`)
  both takes a free key and steals an expired one. The `lease_until > acquired_at`
  CHECK backstops any bad lease arithmetic. The janitor is **not** on the
  correctness path — it is cleanup only (D3.6).
- **D3.6 — Owner-fenced renew/release; janitor is cleanup.** `renew` and `release`
  are `WHERE lock_key=? AND owner=?` (a stale owner cannot renew/free a lease it
  no longer holds); both are idempotent (0 rows affected = already lost/freed).
  `reclaim_expired(now)` deletes rows with `lease_until < now()` and returns a
  count — an operational cleanup method, invoked explicitly, **never a loop** in α7.3.
- **D3.7 — No new schema, no new immutability.** `event_outbox` /
  `distributed_locks` are used exactly as baseline defines them. The relay's
  `UPDATE`s are legal precisely because neither table is in `_IMMUTABLE_TABLES`.
- **D3.8 — Runner-before-worker preserved.** α7.3 adds **no** loop, scheduler,
  broker, or process. Everything is a synchronous method call an eventual worker
  (α8.1) will invoke; α7.3 tests call `relay_once(...)` / `acquire(...)` directly.
- **D3.9 — Boundary invariant.** The relay owns *delivery of already-produced
  events*; it never mutates `payload` and never invents events. The lock manager
  owns *lease bookkeeping*; it never knows what the lock protects. Neither reaches
  into any aggregate.

---

## Section 4 — Open questions for sign-off

Only decisions **not** already pinned by ADR-0032 / ADR-0041 / the blueprint are
raised. (Already decided, not re-asked: at-least-once semantics, `SKIP LOCKED`
batching, `occurred_at` ordering, `attempts`/`last_error` accounting, lease CHECK,
steal-after-expiry existing, zero migrations, in-process publisher.)

**Q1 — Relay execution model + surface.**
α7.3 is runner-before-worker, so there is no daemon. How is the relay *invoked*?
Options: (a) **library service only** — `RelayService.relay_once(batch)` +
`reclaim_expired(now)`, driven exclusively by tests; no HTTP, no CLI, no loop;
(b) also add a **one-shot CLI/management entrypoint** (`python -m app.relay`)
that runs a single pass; (c) an internal **admin endpoint**.
**Recommend (a)** — the user's implementation order defers "API/CLI unless
architecture already requires"; nothing consumes a relay endpoint yet, and the
loop/CLI naturally lands with the α8.1 worker. Observability in α7.3 = **structured-log
counters** (`published`, `failed`, `batch_size`, latency) only — **no** Prometheus/statsd.
*(If you want an operator to trigger a pass manually before α8.1, (b).)*

**Q2 — What does "publish" do, and does the relay touch `event_log`?**
Two coupled sub-questions. (i) **Publisher target:** confirm the α7.3 publisher is
an in-process `Publisher` port with a default **logging/no-op sink** (D3.4) — the
relay marks `published_at` once the publisher returns OK. (ii) **`event_log`
projection:** the immutable, partitioned canonical store (§2.3) has no writer.
Does α7.3's relay *also* INSERT published events into `event_log`?
**Recommend:** (i) confirm logging sink; (ii) **defer `event_log`** — it needs
partition-presence handling and no consumer reads it yet; a later slice can add an
`EventLogProjector` publisher implementation behind the same port. *(If you want
the canonical log populated from day one, we add the INSERT-only projector now —
but it widens α7.3 and touches partition management.)*

**Q3 — Poison / head-of-line handling without a DLQ (zero-migration).**
A row that always fails to publish would, under strict `occurred_at` order, block
the queue forever. With no migration we cannot add a `dead_letter` table/column.
**Recommend:** on failure, `mark_failed` bumps `attempts` + records `last_error`
and leaves the row unpublished; the fetch query excludes **parked** rows via
`AND attempts < :max_attempts` so one poison row can't head-of-line-block the
batch. Parked rows stay in `event_outbox` (high `attempts`, non-null `last_error`),
surfaced by the log metrics and a simple "parked count" query — **no DLQ table,
no migration.** Proposed default `max_attempts = 10`, `batch = 100`, both from
`Settings` with constant fallbacks. **Confirm the cap value, batch size, and the
"park in place, no DLQ" approach.**

**Q4 — Lock ownership token + fencing semantics.**
`distributed_locks.owner` is free-form text. Who mints it and how is it used?
**Recommend:** the **caller supplies `owner`** (a stable per-holder identity —
e.g. `worker:<uuid4>` / `<process-id>`); `acquire` returns a `Lease` value object
`(lock_key, owner, lease_until)`; `renew` and `release` are **owner-fenced**
(`WHERE lock_key=? AND owner=?`), so a holder that lost the lease (stolen after
expiry) cannot later renew or free the new holder's lease. `acquire` **steals**
iff `lease_until < now()`. **Confirm caller-supplied owner + owner-fenced
renew/release + steal-on-acquire.** *(Alternative: the manager mints an opaque
fence token per acquire and fences on that instead of `owner` — richer, but
`owner` already suffices for single-holder-per-key and needs no extra column.)*

**Q5 — Janitor lifecycle.**
Given steal-on-acquire (D3.5) already guarantees correctness, what is the janitor
*for*, and how does it run? **Recommend:** the janitor is a **cleanup-only**
`reclaim_expired(now) -> int` method that deletes rows with `lease_until < now()`
(reclaiming abandoned keys that no one re-acquires), invoked **explicitly** and
tested directly — **no background loop / no scheduler in α7.3** (that is the α8.1
worker's job). **Confirm janitor = explicit cleanup method, not a daemon, and that
correctness does not depend on it.**

**Q6 — Port placement + transaction/session strategy.**
(i) **Relay read/mark methods:** extend the existing `IEventOutboxRepository`
(same table) with `fetch_unpublished` / `mark_published` / `mark_failed`, or add a
sibling `IOutboxRelayRepository` (keeping the α7.1 producer port "add-only")?
**Recommend extend** `IEventOutboxRepository` — it is the one port for the one
table; the α7.1 docstring's "append-only from this port's view" is updated to
note the relay read/mark surface. (ii) **Lock manager placement:** a new
`IDistributedLockManager` port (`app/application/interfaces/locks.py`) + SQLAlchemy
impl. (iii) **Transaction ownership:** the relay's fetch→publish→mark must share
one transaction (for `FOR UPDATE SKIP LOCKED`). **Recommend** exposing both on the
`IUnitOfWork` (`uow.outbox` extended; new `uow.locks`) and having `RelayService`
own a UoW per `relay_once` call; the standalone worker's own session-factory is
α8.1. **Confirm: extend the outbox port, new lock-manager port on the UoW, one
transaction per relay pass / per lock op.**

**Version (not a question — confirm cadence).** Continue the `0.4.x` slice
cadence → `0.4.17-phase3-alpha7.3-dev`, tag `v0.4.17-phase3-alpha7.3` on merge
(still Phase-3 infrastructure, not a product milestone).

---

## Section 5 — Planned surface (pending §4)

**No HTTP surface** under the recommended §4 Q1 (a). The α7.3 surface is ports +
impls + one service, consumed by tests:

```
# Lock manager (app/application/interfaces/locks.py) — new port
class IDistributedLockManager(ABC):
    async def acquire(self, *, key: str, owner: str, lease: timedelta) -> Lease | None
    async def renew(self, lease: Lease, *, lease: timedelta) -> Lease | None   # owner-fenced
    async def release(self, lease: Lease) -> bool                              # owner-fenced, idempotent
    async def reclaim_expired(self, *, now: datetime) -> int                   # janitor, cleanup-only

# Outbox relay (extend IEventOutboxRepository)
    async def fetch_unpublished(self, *, limit: int, max_attempts: int) -> list[OutboxRow]  # FOR UPDATE SKIP LOCKED
    async def mark_published(self, *, event_id: UUID, now: datetime) -> None
    async def mark_failed(self, *, event_id: UUID, error: str) -> None

# Publisher port (app/application/interfaces/publisher.py) — new port
class Publisher(Protocol):
    async def publish(self, event: OutboxEvent) -> None      # α7.3 default: LoggingPublisher (in-process)

# Relay service (app/application/use_cases/relay/…)
class RelayService:
    async def relay_once(self, *, batch: int) -> RelayResult   # fetch → publish → mark; returns published/failed counts
```

Signed-off **implementation order** (matches the user's α7.3 order; layer-by-layer):

1. **Distributed lock repository/manager** — `IDistributedLockManager` port +
   SQLAlchemy impl: acquire (insert-or-steal), renew (owner-fenced), release
   (owner-fenced), steal-after-expiry (in acquire), `reclaim_expired` janitor.
   Unit fakes + integration tests (real lease races, expiry, CHECK enforcement).
2. **Outbox repository (relay surface)** — extend `IEventOutboxRepository` +
   impl: `fetch_unpublished` (`FOR UPDATE SKIP LOCKED`, batching, `attempts < cap`),
   `mark_published`, `mark_failed`. Integration tests (skip-locked concurrency,
   ordering, parked rows).
3. **Application services** — `RelayService.relay_once`, `Publisher` port +
   `LoggingPublisher` default, lock-service helpers (if any). Unit tests.
4. **Infrastructure** — DI wiring (container factories, `deps`, UoW attributes:
   `uow.locks`, extended `uow.outbox`). In-process publisher only; **no broker /
   Celery / Redis.**
5. **API / CLI** — **deferred** (§4 Q1 (a)); revisit only if sign-off picks (b)/(c).
6. **Docs** — `CHANGELOG`, `ROADMAP`, `API_CONTRACT` (only if a surface lands),
   architecture notes; **ADR only if a decision falls outside ADR-0041** (none
   expected — the contract already covers D8/D9). Then CI gate → merge → tag
   `v0.4.17-phase3-alpha7.3`.

---

## Section 6 — Reviewer sign-off

**SIGNED OFF (2026-07-17).** All six §4 questions accepted, with two amendments
and one added requirement:

- **Q1 — Relay execution model:** ✅ Accept as recommended. Library/service only,
  public API `relay_once(batch_size: int | None = None) -> RelayResult` and
  `reclaim_expired(now: datetime | None = None) -> int`. **No daemon loop, no CLI,
  no HTTP endpoint.** Structured logging only. The worker loop belongs in α8.1.
- **Q2 — Publisher / `event_log`:** ✅ Accept with amendment. Use a **`PublisherPort`**
  abstraction; the default implementation is a **synchronous in-process publisher
  that invokes registered handlers** and marks `published_at` **only after
  successful completion**. **Do not write `event_log` in α7.3** (immutable +
  partitioned + no consumer; avoid two competing event histories). When needed,
  `event_log` is added later as an **explicit projection**, not implicitly inside
  the relay.
- **Q3 — Poison handling:** ✅ Accept with refinement. Park rows in-place
  (`attempts += 1`, `last_error = …`, `published_at = NULL`); the fetch query
  ignores `attempts >= max_attempts`. Defaults `max_attempts = 10`,
  `batch_size = 100`. **Every parked event emits an `ERROR` structured log** with:
  event id, aggregate id, event type, attempts, exception type, exception message.
  No DLQ table, no retry scheduler.
- **Q4 — Lock ownership:** ✅ Accept. Caller supplies `owner`; **owner-fenced**
  `renew` and `release`; `acquire` steals **expired** leases and **never** steals
  an active lease (matches ADR-0032).
- **Q5 — Janitor:** ✅ Accept exactly as proposed. No daemon, no timer;
  `reclaim_expired()` is an explicit maintenance operation. Correctness comes from
  steal-after-expiry, not cleanup.
- **Q6 — Ports / transactions:** ✅ Accept. Extend `IEventOutboxRepository` with
  `fetch_unpublished(...)` / `mark_published(...)` / `mark_failed(...)`; introduce
  `IDistributedLockManager`. **One transaction per relay batch; one transaction
  per lock operation.** The worker-owned `SessionFactory` belongs in α8.1.
- **Added requirement — `RelayResult`.** `relay_once()` returns a
  `RelayResult(fetched, published, failed, parked)` summary (not `None`), so
  testing, logging, and later metrics/Prometheus are trivial without coupling to
  any monitoring framework.
- **Version:** ✅ `0.4.17-phase3-alpha7.3-dev` → tag `v0.4.17-phase3-alpha7.3`.

**Final constraints (verbatim):** library-only relay · `PublisherPort` · no
`event_log` projection · no broker · no Celery · no Redis · poison events parked
in-place after a configurable retry limit · structured logging · owner-fenced
distributed locks · explicit `reclaim_expired()` · repository extensions only ·
one transaction per relay pass · return a `RelayResult` summary from `relay_once()`.

Proceed: branch `phase3/alpha7.3-relay-lock-manager`, bump `app/main.py` →
`0.4.17-phase3-alpha7.3-dev`, implement in the §5 order, full quality gate,
fast-forward `main`, drop `-dev`, tag `v0.4.17-phase3-alpha7.3`, push, delete the
local feature branch (linear history preserved).
