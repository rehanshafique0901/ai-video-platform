# ADR-0048 — Analytics Event Writing Is a DB-Enforced Exactly-Once Outbox Consumer

**Status:** Accepted (Phase 3, α9.0 — Creator Analytics Foundation, 2026-07-27). Governance that
**precedes** implementation — like ADR-0044/0045/0047 — because it fixes an idempotency/data-model
boundary **before any analytics code exists**. The proposed partition-compatible unique index has
been **empirically verified against PostgreSQL 17.10** (matching the project's Supabase PG 17 target)
before acceptance — see §PostgreSQL partitioned-table uniqueness — empirical verification. The
implementation (the `source_event_id` column + partition-compatible unique index migration, the
analytics write repository/consumer, the read model and owner-scoped API) lands in α9.0 and cites
this ADR.

**Builds on:** **ADR-0041** (D9 — the outbox/relay is **at-least-once**; consumers must be
idempotent), **ADR-0042** (the orchestration freeze — new capability plugs in as **additive outbox
consumers**), **ADR-0035** (immutable append-only tables + `reject_mutation` — `analytics_events` is
of this class), and the **direct precedent of ADR-0030** (`export_jobs` partial-unique) and
**ADR-0033** (`usage_records` per-partition `request_id` uniqueness), both of which promoted an
idempotency/uniqueness invariant into a database constraint rather than trusting application control
flow. Grounded by [`PHASE3_ALPHA9_0_GROUNDING.md`](../engineering/PHASE3_ALPHA9_0_GROUNDING.md).

---

## Context

α9.0 activates the dormant `analytics_events` table (baseline; append-only, partitioned monthly by
`occurred_at`; no application reader or writer today — `PHASE3_ALPHA9_0_GROUNDING.md §1`). The slice
brief is a **foundation**: activate analytics writing for already-completed actions, a read model,
and an owner-scoped API — strictly additive, reusing existing architecture.

Grounding established the shape almost entirely from existing parts:

- **Writing must be a downstream outbox consumer, not inline.** Every "completed action" the slice
  cares about already emits a terminal outbox event (`PublishJobSucceeded/Failed`,
  `ExportJobSucceeded/Failed`, `PublishJobCreated`, …). Writing analytics as one more
  `EventHandler` on the existing `InProcessPublisher` fan-out (alongside `NotificationProjection`
  and `PublishNotificationProjection`) touches **no** producer and **no** frozen runtime — exactly
  what ADR-0042 anticipates. Writing analytics *inline* inside the runtime use cases would edit
  frozen `process_publish_job` / `process_export_job` / generation code and is out of the question.

- **The outbox is at-least-once (ADR-0041 D9).** The `InProcessPublisher` fans an event to every
  handler in order; if any handler raises, the **whole** event is marked failed and the relay
  **redelivers it to all handlers again** (`in_process_publisher.py:34-47`). The handler contract is
  explicit and load-bearing: *"handlers must be idempotent on `event.id`"* (`publisher.py:52-56`).

- **Every existing consumer honours that contract with a DB constraint, never an app-level
  pre-check.** Notifications dedupe on the partial-unique index `uq_notifications_user_id_source_
  event_id (user_id, source_event_id) WHERE source_event_id IS NOT NULL` (migration `0009`); a
  redelivery raises `ConflictError` and is treated as a successful no-op (`create_notification.py`
  invariant W8.5b.7: *"correctness never depends on an application-level pre-check"*). Media
  ingestion dedupes on the deterministic storage key + `media_assets` uniqueness. There is **no**
  precedent in this codebase for a consumer that tolerates duplicates.

**The one thing not free.** `analytics_events` has **no dedup key** — no `source_event_id`, no unique
constraint (`PHASE3_ALPHA9_0_GROUNDING.md §1`). So the analytics consumer cannot, today, be
idempotent on `event.id` the way every other consumer is. Resolving that is a genuine architectural
decision (it changes the `analytics_events` data-model contract enforced by `validate_schema.py` and
documented in `schema.md §26`), so per the α9.0 rule — *"introduce an ADR only if grounding proves
activation changes an existing architectural boundary"* — it earns this ADR.

### The decision point

How does the analytics outbox consumer satisfy the at-least-once delivery contract when appending to
the immutable, partitioned `analytics_events` table?

- **Option A — DB-enforced exactly-once** via a `source_event_id` and a **partition-compatible**
  uniqueness strategy. A redelivered event collides on the constraint and is a no-op.
- **Option B — At-least-once append with duplicate tolerance** in the analytics layer. No schema
  change; the source `event.id` is stored in `properties` for traceability; the read model
  dedupes-on-read or accepts occasional double counts.

---

## Options

### Option A — DB-enforced exactly-once (partition-compatible)

Add a nullable `source_event_id uuid` column to `analytics_events` and a **partition-key-inclusive,
partial** unique index. Because the table is `PARTITION BY RANGE (occurred_at)`, PostgreSQL requires
any unique index to include the partition key `occurred_at`; unlike ADR-0033's `usage_records`
(whose key `request_id` *omitted* the partition key and thus needed per-child indexes), including
`occurred_at` here makes a **single parent-level** unique index legal and **auto-propagating** to
every child partition:

```sql
ALTER TABLE analytics_events ADD COLUMN source_event_id uuid NULL;

CREATE UNIQUE INDEX uq_analytics_events_source_event_id
  ON analytics_events (source_event_id, occurred_at)
  WHERE source_event_id IS NOT NULL;
```

Correctness hinges on **deterministic `occurred_at`**: the consumer sets
`occurred_at = event.occurred_at` (the immutable outbox event's own timestamp) and
`source_event_id = event.id`, then appends with `INSERT … ON CONFLICT DO NOTHING` (or catches the
repository `ConflictError` as a no-op). A redelivery carries the identical `(source_event_id,
occurred_at)` pair → same partition, same key → the constraint refuses the second row. The partial
predicate (`WHERE source_event_id IS NOT NULL`) mirrors the notifications precedent so future
non-outbox analytics (e.g. product page views) can be appended with a NULL `source_event_id` without
being forced through this dedup key.

*Mechanically identical to migration `0009` (notifications), adapted for a partitioned table by
adding the partition key to the index.* Appending is compatible with the append-only immutability
trigger (it rejects UPDATE/DELETE, not INSERT).

### Option B — At-least-once append with duplicate tolerance

Leave `analytics_events` unchanged. The consumer appends a row per delivery, recording the source
`event.id` inside `properties` (e.g. `properties->>'source_event_id'`) for traceability. Redelivery
after a partial failure produces a second physical row. The read model must then either dedupe on
`properties->>'source_event_id'` at query time (e.g. `COUNT(DISTINCT …)`) or accept that counts can
over-report on the (rare) redelivery.

---

## Evaluation

| Criterion | Option A — DB-enforced exactly-once | Option B — at-least-once + tolerance |
|---|---|---|
| **Existing outbox-consumer contract** (`publisher.py:52-56` "idempotent on `event.id`") | **Honours it** exactly as notifications/ingestion do — DB-enforced, no app pre-check. | **Reinterprets it.** Would be the first consumer that is *not* idempotent on `event.id`; weakens a documented, uniformly-held boundary. |
| **Consistency with ADR-0030 / ADR-0033** | **Consistent.** Same move: promote an idempotency/uniqueness invariant into a DB constraint before consumers accumulate workarounds. Reuses the `0009` shape. | **Contradicts the established pattern.** Both ADRs explicitly rejected "enforce in the app layer / tolerate races" for exactly this class of invariant. |
| **Replay / redelivery behaviour** | Redelivery is a **clean no-op** (constraint refusal → `ConflictError` → success). Deterministic regardless of how many times the relay retries. | Redelivery **inserts a duplicate** row. Correctness of every downstream count depends on remembering to dedupe-on-read forever. |
| **Partitioning implications** | Key includes the partition column `occurred_at`, so a **single parent-level** partial-unique index is legal and **auto-propagates** to all child partitions (current + future) — *simpler* than ADR-0033's per-child approach. Requires deterministic `occurred_at = event.occurred_at`. | None (no schema change) — but the read model inherits partition-aware `COUNT(DISTINCT …)` scans over `properties`, which cannot use the existing btree indexes on `(tenant_id, occurred_at)` / `(event_name, occurred_at)` as cleanly. |
| **Migration cost** | One additive migration: nullable column + one partial unique index on a **currently-empty** table (no backfill, plain transactional DDL, clean up/down round-trip) — a near-verbatim copy of `0009`. Plus `validate_schema.py` / `schema.md §26` updates. | Zero migration. |
| **Long-term maintenance** | The invariant is **self-enforcing** for every future writer (new event types, backfills, replica failovers with stale code). New partitions inherit the index automatically. One-time cost, then invisible. | Every future reader/aggregation must *remember* to dedupe on `source_event_id`; a forgotten `DISTINCT` silently inflates a metric. The dedup burden is unbounded in time and spread across every query author. |
| **Correctness guarantees** | **Exactly-once per source event, DB-guaranteed.** Analytics counts are exact and race-free. | **Best-effort.** Counts are correct only if every query dedupes and the JSONB key is always populated; a single omission corrupts a number silently. |

**Note on `occurred_at` determinism (Option A's only real obligation).** If the consumer set
`occurred_at = now()` instead of `event.occurred_at`, two redeliveries would land in (potentially)
different rows/partitions and the constraint would not fire. This ADR therefore fixes that the
analytics write timestamp for outbox-derived rows is the **event's** `occurred_at`, not wall-clock at
consume time. This is also semantically correct: an analytics event describes *when the action
happened*, which is the source event's time, not when the projection ran.

---

## Decision

**Adopt Option A.** Analytics event writing is a **DB-enforced exactly-once** outbox consumer.

- Add `analytics_events.source_event_id uuid NULL` and the partial, partition-key-inclusive unique
  index `uq_analytics_events_source_event_id (source_event_id, occurred_at) WHERE source_event_id IS
  NOT NULL` (a hand-written migration — next revision after the current head `0014_publish_jobs`,
  i.e. `0015_analytics_events_source_event_id` — mirroring `0009`).
- The analytics consumer sets `source_event_id = event.id` and `occurred_at = event.occurred_at`
  (deterministic), and appends idempotently (`ON CONFLICT DO NOTHING` / `ConflictError`-as-no-op).
- The partial predicate keeps the door open for future non-outbox analytics (NULL `source_event_id`)
  without coupling them to this dedup key.

Rationale: the platform has **consistently enforced consumer idempotency at the database layer**
(notifications `0009`, ingestion storage-key uniqueness) and **consistently promoted
idempotency/uniqueness invariants into DB constraints** (ADR-0030, ADR-0033), explicitly rejecting
"tolerate duplicates / enforce in the app layer" for this exact class of decision. Option B would
make analytics the **sole** exception to a uniform, load-bearing boundary, trading a one-time,
near-verbatim migration for an unbounded, silent, per-query correctness burden. The investigation
uncovered **no contradiction** that would override this precedent; the recommendation stands at
Option A.

---

## PostgreSQL partitioned-table uniqueness — empirical verification

Per the α9.0 review directive, the proposed index shape was **not accepted on assumption**. It was
tested against a throwaway **PostgreSQL 17.10** instance (Debian build; matches the project's Supabase
PG 17 target) using a faithful replica of the live `analytics_events` scheme — `PARTITION BY RANGE
(occurred_at)`, composite `PRIMARY KEY (id, occurred_at)`, monthly child partitions. Results:

| # | What was tested | Statement | Result |
|---|---|---|---|
| 1 | **The proposed Option A index** — partial unique, key includes the partition column, declared at the **parent** level | `CREATE UNIQUE INDEX … ON analytics_events (source_event_id, occurred_at) WHERE source_event_id IS NOT NULL` | **`CREATE INDEX` — accepted.** A partitioned unique index (`pg_class.relkind = 'p'`), unique + partial, was created on the parent. |
| 2 | Redelivery dedup with **deterministic** `occurred_at = event.occurred_at` | two INSERTs, identical `(source_event_id, occurred_at)`, 2nd with `ON CONFLICT (source_event_id, occurred_at) WHERE source_event_id IS NOT NULL DO NOTHING` | **`INSERT 0 0` on the 2nd; final count = 1.** Redelivery is a clean no-op. |
| 2b | Partial predicate lets **non-outbox** rows (`NULL` `source_event_id`) coexist | two INSERTs with `source_event_id = NULL` | **Both inserted; count = 2.** NULLs are exempt from the dedup key. |
| 2c | Same `source_event_id`, **different** `occurred_at` (→ different partition) | INSERT with a different month | **Inserted; count = 2.** Proves uniqueness is on the *pair*, so a non-deterministic `occurred_at` (e.g. `now()`) would **defeat** dedup — the deterministic-`occurred_at` rule is load-bearing, not cosmetic. |
| 3 | **Control** — `source_event_id`-only unique index (omits the partition key) | `CREATE UNIQUE INDEX … ON analytics_events (source_event_id) WHERE …` | **Rejected:** `ERROR: unique constraint on partitioned table must include all partitioning columns` / `DETAIL: … lacks column "occurred_at" which is part of the partition key`. |
| 4 | Control — non-partial variant incl. partition key | `CREATE UNIQUE INDEX … (source_event_id, occurred_at)` | Accepted (sanity; not the chosen shape). |
| 5 | **Auto-propagation to a FUTURE partition** | create a new monthly child **after** the index exists | The new child automatically received `…_source_event_id_occurred_at_idx` (unique + partial). Verified across all children: one parent partitioned index (`relkind='p'`) + one local index per child (existing **and** newly-added). |

**Why the proposed index satisfies PostgreSQL's rules.** PostgreSQL's only structural requirement for
a unique index on a partitioned table is that *the index columns include every partition-key column*
(Test 3 shows the exact error when they do not; the `CREATE INDEX` / `CREATE TABLE … PARTITION` docs
state this rule). The partition key here is `occurred_at`, and the proposed key is `(source_event_id,
occurred_at)` — it **includes** `occurred_at`, so the rule is satisfied and the index is legal at the
parent level (Test 1). PostgreSQL **does** permit a **partial** predicate (`WHERE source_event_id IS
NOT NULL`) on a partitioned unique index (Test 1, Test 2b), and it **auto-propagates** the index to
every current and future child partition as a matching local index (Test 5) — so, unlike ADR-0033's
`usage_records` case (whose key omitted the partition key and therefore required per-child index
management), **a single parent-level declaration suffices** and future partitions are covered
automatically.

**Adjustment to the proposal:** **none required.** The shape in §Decision is valid exactly as
written. The verification does, however, **harden one design obligation into a correctness
invariant**: because uniqueness is enforced on the *pair* `(source_event_id, occurred_at)` and not on
`source_event_id` alone (Test 2c), the consumer **must** write `occurred_at = event.occurred_at` (the
immutable outbox timestamp), never `now()`. This is restated in §Decision and will be a covered test
case in α9.0.

---

## What this ADR does *not* decide (deferred to the α9.0 pre-flight)

This ADR fixes **only** the idempotency/data-model boundary for analytics writes. All of the
following remain open for the pre-flight and are **not** prejudged here:

- The concrete **event taxonomy / `event_name` vocabulary** and which terminal outbox events are
  projected first (grounding notes the cleanly owner-scoped ones are the **publish** and **export**
  families, which carry `requested_by_user_id`).
- The **write port / repository** shape (`IAnalyticsEventRepository` on the UoW vs. a direct-session
  writer) and the **read model / aggregate** shape (`IAnalyticsReadModel`).
- The **read API** surface (endpoints, DTOs, aggregation windows), which will mirror the α8.9c
  dashboard owner-scoped read pattern (ADR-0034).
- Whether an additional **`user_id` index** is warranted for owner-scoped reads (a separate, purely
  additive index decision — not required for this ADR).
- CI stage additions, test strategy, and documentation wiring
  (`DECISIONS.md` cross-link, `schema.md §26`, `validate_schema.py`, `INDEX_STRATEGY.md`).

---

## Alternatives Considered (beyond A/B)

1. **Application-level pre-check (`SELECT` before `INSERT`).** *Rejected.* Racy under redelivery and
   explicitly forbidden by the house rule W8.5b.7 (`create_notification.py:15-16`). This is the anti-
   pattern ADR-0030 and ADR-0033 exist to prevent.
2. **Per-child unique indexes (ADR-0033 style) on `source_event_id` alone.** *Rejected as
   unnecessary.* ADR-0033 needed per-child indexes because its key (`request_id`) omitted the
   partition key. Here the natural dedup key already pairs with `occurred_at`, so a single
   auto-propagating parent-level index is legal and simpler. (A `source_event_id`-only global unique
   index is *not* permitted by PostgreSQL on this partitioned table.)
3. **A separate `analytics_event_dedup(source_event_id)` side table** written in the same
   transaction as the append. *Rejected.* Adds a second table and a join/txn coupling to solve what a
   single partial index on the existing table solves; more surface, no benefit.
4. **Hash-derived deterministic `id` (`id = f(event.id)`) reusing the existing PK
   `(id, occurred_at)`.** *Rejected.* Overloads the surrogate PK with dedup semantics, is opaque, and
   still requires deterministic `occurred_at`; a named `source_event_id` + partial unique index is
   explicit, self-documenting, and matches the notifications precedent exactly.
5. **Broker-level exactly-once.** *Not applicable.* The publisher is deliberately in-process with
   at-least-once semantics (ADR-0041 D9); exactly-once is owned at the consumer/DB layer by design.

---

## Consequences

- **Positive.** Analytics counts are **exact and race-free** from day one; the analytics consumer is
  idempotent on `event.id` exactly like every other consumer (no new correctness idiom to learn);
  the invariant is self-enforcing for all future writers and auto-inherited by future partitions;
  the read model can use the existing `(tenant_id, occurred_at)` / `(event_name, occurred_at)`
  indexes without `DISTINCT` gymnastics; the migration is a near-verbatim, low-risk copy of `0009`
  against an empty table.
- **Cost.** One additive migration and the associated `validate_schema.py` / `schema.md §26` /
  `INDEX_STRATEGY.md` updates (implementation-time); the consumer must set `occurred_at` from the
  **event** timestamp, not wall-clock (a one-line discipline, verified by test); a tiny per-insert
  index write-amplification (negligible; partial index skips NULL `source_event_id`).
- **Boundary added (a future slice may not cross without its own ADR).** *Analytics rows derived from
  an outbox event are unique on `(source_event_id, occurred_at)` where `source_event_id` is the
  producing `event.id` and `occurred_at` is that event's timestamp; analytics writing is exactly-once,
  DB-enforced — never duplicate-tolerant.*

---

## Change log

| Date | Change |
| --- | --- |
| 2026-07-27 | Proposed (governance, ahead of α9.0). Fixes the analytics-consumer idempotency boundary: analytics event writing is a DB-enforced exactly-once outbox consumer (Option A) via `analytics_events.source_event_id` + partial, partition-key-inclusive unique index `uq_analytics_events_source_event_id (source_event_id, occurred_at) WHERE source_event_id IS NOT NULL`, with deterministic `occurred_at = event.occurred_at`. Compares Option A vs Option B across the outbox-consumer contract, ADR-0030/0033 consistency, replay/redelivery, partitioning, migration cost, long-term maintenance, and correctness; recommends and adopts Option A. Implementation and full doc wiring land in α9.0 and cite this ADR. |
| 2026-07-27 | **Accepted.** Amended per α9.0 review with a §PostgreSQL partitioned-table uniqueness — empirical verification: the proposed index was tested against PostgreSQL 17.10 (Supabase PG 17 target) on a faithful replica of the `analytics_events` partitioning scheme. Confirmed the partial, partition-key-inclusive unique index is accepted at the parent level, dedups redeliveries with deterministic `occurred_at`, exempts `NULL` `source_event_id`, auto-propagates to current + future child partitions, and that a partition-key-omitting key is rejected. **No adjustment to the proposal required**; the deterministic-`occurred_at` obligation is hardened into a correctness invariant. |
