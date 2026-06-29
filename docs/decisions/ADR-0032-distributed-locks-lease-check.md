# ADR-0032 — Promote `distributed_locks` Lease Sanity to a Database CHECK Constraint

**Status:** Accepted (Phase 3, Wave 1.3, 2026-06-29). Validated end-to-end against Supabase Postgres 17.6 + pgvector 0.8.0; full 10-stage CI gate green (10/10 in 107.6s); alembic round-trip clean across both the W1.3-isolated path (`0004 → 0005 → 0004 → 0005`) and the full-baseline path (`0005 → empty → 0005` via CI gate stages 6 + 7); schema validator 9/9 (87 indexes total — W1.3 adds 0 as predicted); ERD comparator 0 drift (51 entities and 60 shared design edges, both unchanged); pre-upgrade safety check returned `distributed_locks_lease_violations = 0`; `pg_constraint` shows `chk_distributed_locks_lease_until_after_acquired_at: CHECK ((lease_until > acquired_at))` present after upgrade and absent after downgrade.
**Supersedes / refines:** `docs/database/schema.md` §32 reconciliation note (which deferred the CHECK to a Phase-3 decision); `docs/database/schema.md` §37 Q10 (prior default: "rely on application logic"). Builds on ADR-0022 (which introduced `distributed_locks` as CR-DB-2).
**Wave:** Phase 3 W1.3 (Schema integrity — promote use-case invariants into the DB).

---

## Context

`distributed_locks` (CR-DB-2, introduced by ADR-0022) is a lightweight Postgres-backed lock primitive used in addition to Postgres advisory locks and Redis locks for safety-critical operations: render-job ownership (`render_job:<uuid>`), workflow-tick serialisation (`workflow_run:<uuid>`), project-publish coordination (`project_publish:<uuid>`), and server-side timeline mutations (`timeline_edit:<uuid>`). The row shape is `(lock_key text PK, owner text, lease_until timestamptz, heartbeat_at timestamptz, acquired_at timestamptz DEFAULT now(), metadata jsonb)`.

The earliest Phase 2A design carried a `lease_until > created_at` CHECK constraint on this table. During Phase 2D reconciliation (`docs/database/schema.md` §32 reconciliation note, lines 1208-1217), three coupled changes were made:

1. The surrogate `id uuid PK` was replaced with `lock_key` as the natural PK (one row per lock by definition; the surrogate was unused).
2. `created_at` was renamed to `acquired_at` so its semantic is obvious to the worker reading it.
3. **The `lease_until > created_at` CHECK was dropped**, with the explicit reasoning *"every acquisition path computes `now() + $lease`, so a runtime CHECK at the DB just makes failure harder to diagnose without preventing any real bug; whether to add it back is a Phase-3 decision (§37)."*

`docs/database/schema.md` §37 Q10 (the open-questions catalogue) recorded this as *"Should `distributed_locks` reintroduce a `lease_until > acquired_at` CHECK?"* with the interim verdict *"rely on application logic."*

In the seven months since Phase 2D, two facts have come into focus that revise the verdict:

1. **The ROADMAP planning step.** `ROADMAP.md` line 173 (Wave 1 plan) explicitly enumerates `distributed_locks lease CHECK` as W1.3, alongside the W1.1 (`export_jobs`) and W1.2 (`idempotency_keys`) DB-promotions that have since landed. Phase 3 Wave 1 is the project's "promote use-case-layer invariants into the DB before they accumulate workarounds" wave, and Q10 has always belonged in it.

2. **The dataset is empty, the implementation is unwritten, the cost is zero.** `distributed_locks` has zero application consumers today — the only Python references are the ORM model itself and the baseline migration. Promoting the CHECK now — before any acquisition code is written — costs one `ALTER TABLE` statement and forecloses an entire class of "lease = 0" / "negative-lease passed as `$lease` argument" / "`lease_until = acquired_at` from a clock-skew bug" defects that would otherwise be silent corruption of the lock table. The defence-in-depth value is real even though the application code "shouldn't" produce such rows: every release contains code that "shouldn't" produce defects, and a DB-level CHECK turns silent corruption into a single, debuggable `IntegrityError` at the violating call site.

The §32 reconciliation note's original argument — that the CHECK "makes failure harder to diagnose without preventing any real bug" — is technically true if you accept that application code is bug-free. The Phase 3 stance is that application code is bug-free *until it isn't*, and a CHECK constraint with a constraint name that mentions the violated invariant is *easier* to diagnose than discovering a corrupted lock row in production weeks later.

---

## Decision

Promote one invariant on `distributed_locks` from application logic to a database CHECK constraint:

```sql
ALTER TABLE distributed_locks
  ADD CONSTRAINT chk_distributed_locks_lease_until_after_acquired_at
  CHECK (lease_until > acquired_at);
```

- **Predicate.** Strict greater-than (`>`, not `>=`) — `lease_until == acquired_at` represents a degenerate zero-second lease that the acquisition path should never compute. Postgres evaluates this CHECK on every `INSERT` and `UPDATE`; any code path that violates the predicate produces an `IntegrityError` naming the constraint, surfacing the bug at the call site rather than at the next lock-acquisition attempt by a different worker.
- **Naming.** `chk_distributed_locks_lease_until_after_acquired_at` — verbatim from `schema.md` §37 Q10, following `NAMING_CONVENTIONS.md` §3 (`chk_<table>_<predicate_essence>`) and the precedent set by W1.2's `chk_idempotency_keys_response_hash_matches_status`.
- **Shape.** Declared on the ORM as `CheckConstraint("lease_until > acquired_at", name="chk_distributed_locks_lease_until_after_acquired_at")` inside `DistributedLock.__table_args__` so the validator's ORM-vs-DB diff continues to report 0 drift. `CheckConstraint` is already in the SQLAlchemy import list (W1.2 added it for `IdempotencyKey`); no import changes are needed.
- **Single-predicate by deliberate choice.** Bundling additional invariants (`lease_until >= heartbeat_at`, `heartbeat_at >= acquired_at`, etc.) was considered and rejected (see Alternatives §2 and §3) in favour of the most surgical W1.x migration so far — one CHECK, matching §37 Q10 verbatim. If future profiling or incident review surfaces a missed-invariant class, a successor ADR can extend the constraint or add a sibling one without re-opening this decision.
- **No `CONCURRENTLY` / no `NOT VALID` + `VALIDATE`.** The table is empty in every environment that currently exists (the live Supabase target as of 2026-06-29 has zero `distributed_locks` rows; the Migration Plan §3 step below makes that an explicit pre-upgrade gate). Adding a CHECK to a populated production table in the future would use the `ADD CONSTRAINT ... NOT VALID` + `VALIDATE CONSTRAINT` two-step under a separate follow-up migration, but that is out of scope here.

---

## Alternatives Considered

1. **Keep the status-quo verdict ("rely on application logic").** The §37 Q10 verdict prior to this ADR. *Rejected* because Wave 1 of Phase 3 is explicitly the "promote use-case-layer invariants into the DB before they accumulate workarounds" wave; leaving this CHECK in application logic indefinitely violates Wave 1's stated purpose. The original deferral argument ("makes failure harder to diagnose") inverts in practice once the CHECK has a descriptive name — `IntegrityError: violates check constraint "chk_distributed_locks_lease_until_after_acquired_at"` is *more* diagnostic than a silently-corrupted lock row that produces incorrect lock-ownership behaviour downstream. ROADMAP W1.3 also explicitly lists this item; doing nothing leaves Wave 1 incomplete.

2. **Bundle `lease_until >= heartbeat_at` as a second CHECK predicate.** The broader temporal invariant that also catches *dynamic* bugs (e.g. a future heartbeat code path that updates `heartbeat_at` without re-extending `lease_until`). Tempting because it would be ~zero additional migration cost. *Rejected* because §37 Q10 specifies *one* CHECK (`lease_until > acquired_at`); bundling extras would inflate W1.3 beyond its documented mandate. The `adrs-and-architecture.mdc` §5 "one decision per ADR" rule cautions against bundling, even when the additions are cheap. The heartbeat path doesn't exist yet (zero consumers); adding a CHECK to protect against a code path that hasn't been written is speculative. A successor ADR can add `lease_until >= heartbeat_at` (or a sibling CHECK with its own constraint name) once the heartbeat code is being written and its invariants are concrete.

3. **Bundle `heartbeat_at >= acquired_at` as a third CHECK predicate.** The "heartbeat can't precede acquisition" invariant. *Rejected* — even more speculative than (2). Given current upsert paths set `heartbeat_at = now()` at acquire and at every subsequent heartbeat, and `acquired_at` defaults to `now()` at INSERT, this is structurally true today and would only be violated by a future code path that *explicitly* writes a `heartbeat_at` older than `acquired_at` — a level of malice or carelessness that the CHECK wouldn't be the right defence against.

4. **Use a `BEFORE INSERT` / `BEFORE UPDATE` trigger instead of a CHECK constraint.** A PL/pgSQL trigger could enforce richer invariants (e.g. "the lease can only be extended forward, never backward"), beyond what a CHECK constraint can express (CHECKs only see one row at a time, not the OLD/NEW pair). *Rejected* because the Q10 invariant *is* single-row (`lease_until > acquired_at` only looks at one row's columns), so a CHECK is the right shape. Triggers add a function dependency, are harder to read in `pg_constraint` introspection, and the validator (`scripts/validate_schema.py`) has narrower coverage for arbitrary CHECK-via-trigger predicates than for first-class CHECK constraints. Triggers are appropriate for cross-row or cross-table invariants and for side-effecting maintenance (the `touch_updated_at()` family) — single-row predicates belong in CHECK constraints (same shape-vs-tool guidance that motivated W1.2's CHECK + trigger split).

5. **Implement the assertion in a SQLAlchemy `@validates` hook or a Pydantic validator at the application layer.** Adds a Python-side guardrail before the database write. *Rejected* because the "promote to DB" pattern (ADR-0030, ADR-0031, ADR-0032) is explicitly about *not* relying on a single application path to enforce data invariants. Any future second writer (a maintenance script, a `psql` admin session, a replica failover with stale code) would bypass an application-layer validator and silently corrupt the row. The CHECK is the *additional* defence layer; nothing prevents the application from also adding a `@validates` hook on top (defence-in-depth), but the CHECK is the durable guarantee.

6. **Add a partial-unique index on the lease window instead of a CHECK.** An index of the form `CREATE UNIQUE INDEX … ON distributed_locks (lock_key) WHERE lease_until > acquired_at` would simultaneously enforce the predicate and the PK. *Rejected* because the PK already enforces `lock_key` uniqueness unconditionally; adding a partial unique that only covers the "valid lease" subset would weaken the guarantee (rows with invalid leases would be allowed, just not indexed). This also conflates two different responsibilities (uniqueness vs validity) into one object, hurting readability of `pg_indexes` and `pg_constraint` introspection.

7. **Postgres `EXCLUSION` constraint with `gist` operators.** Overkill for a single-row predicate. EXCLUSION constraints are for "no two rows can simultaneously hold overlapping ranges" semantics. *Rejected* as the wrong tool.

---

## Migration Plan

1. **Branch** `phase3/wave1.3-distributed-locks-lease-check` off `main` (HEAD `fd0dfa1`; baseline tag `v0.3.1-phase3-w1.2` at the same commit is the W1.2 rollback floor).
2. **Single PR, first commit** containing exactly these seven files:
   - `backend/alembic/versions/0005_distributed_locks_lease.py` — hand-written (Alembic autogenerate does not reliably preserve the exact text of CHECK expressions). `revision = "0005_distributed_locks_lease"`, `down_revision = "0004_idempotency_keys_invariants"`. Upgrade body:
     ```python
     op.execute(
         f"ALTER TABLE {_TABLE_NAME} "
         f"ADD CONSTRAINT {_CHECK_NAME} "
         f"CHECK ({_CHECK_PREDICATE})"
     )
     ```
     Downgrade body:
     ```python
     op.execute(
         f"ALTER TABLE {_TABLE_NAME} DROP CONSTRAINT {_CHECK_NAME}"
     )
     ```
   - `backend/app/infrastructure/db/models/operations.py` — add the matching `CheckConstraint(...)` to `DistributedLock.__table_args__` immediately before the existing `Index` declaration.
   - `docs/decisions/ADR-0032-distributed-locks-lease-check.md` — this ADR (new file).
   - `DECISIONS.md` — append one-line cross-link entry pointing at the file (sorted by ADR number, after the ADR-0031 entry).
   - `docs/database/schema.md` §32 — add the CHECK to the column block; insert a new **Lease validity invariant (DB-enforced, Phase 3 W1.3)** paragraph mirroring §31's W1.2 paragraph; revise the §32 reconciliation note (paragraph 1208-1217) from *"whether to add it back is a Phase-3 decision (§37)"* to *"…is **reversed by Phase 3 W1.3** (ADR-0032, migration `0005_distributed_locks_lease`)"*; §37 Q10 row marked **Resolved**; §37 Wave 1 bullet for §32 q10 marked ✅ Done.
   - `ROADMAP.md` line 173 (Phase 3 wave table) — annotate W1.3 as **✅ Complete (ADR-0032, migration `0005_distributed_locks_lease`)**.
   - `CHANGELOG.md [Unreleased]` — new sub-section "Phase 3 Wave 1.3 — `distributed_locks` lease CHECK (ADR-0032)" with **Added** / **Changed** / **Validated** / **Not modified** entries, mirroring the W1.1 and W1.2 entries.
   - **No changes** to `docs/database/INDEX_STRATEGY.md` (CHECK is not an index; 87-index count stays at 87) or `docs/database/ERD.md` (CHECK is not an FK; ERD doesn't track them).
3. **Live validation against Supabase** (run from `backend/`, credentials via `.env.validation`):
   - **Pre-upgrade safety check.** Confirm `SELECT COUNT(*) FROM distributed_locks WHERE NOT (lease_until > acquired_at)` returns `0` against the live target before `alembic upgrade head`. If non-zero, abort — adding a CHECK to a populated table with violators fails immediately, and the production-rollback variant (`ADD CONSTRAINT … NOT VALID` + later `VALIDATE CONSTRAINT`) would be required instead.
   - `alembic upgrade head` → assert `pg_constraint` row for `chk_distributed_locks_lease_until_after_acquired_at` exists with the expected `consrc` predicate (`lease_until > acquired_at`).
   - `alembic downgrade -1` → assert the constraint is gone; nothing else in `pg_constraint` for `distributed_locks` changed (diff = exactly one row removed).
   - `alembic upgrade head` (re-apply) → idempotency proven.
   - `python scripts/validate_schema.py` → must remain 9/9 (the new CHECK does not affect any of the 9 structural checks — CHECK constraints are not validated by name, but the table-parity check passes by construction because the ORM and DB agree on column shape).
   - `python scripts/regenerate_erd.py` + `python scripts/compare_erd.py` → must remain 0 drift (ERD reflects entities + FKs, not CHECK constraints).
4. **Full CI gate locally** — `python scripts/ci_gate.py` returns 10/10 (`.env.validation` is present, so stages 5–9 execute live). Push, open PR against `main` → GitHub Actions runs the identical gate against the pgvector service container.
5. **Status transition on merge.** The ADR header and the `DECISIONS.md` cross-link entry both initially say `Proposed` during PR review. The final pre-merge commit on this branch flips both to `Accepted` in a single atomic commit titled `docs(adr): mark ADR-0032 Accepted`. This keeps the merge timeline traceable via `git log --grep "ADR-0032 Accepted"` and prevents an "Accepted" ADR from existing in an unmerged branch.
6. **Tag on merge.** Annotated tag `v0.3.2-phase3-w1.3` is pushed pointing at the merge commit, mirroring `v0.3.0-phase3-w1.1` (W1.1) and `v0.3.1-phase3-w1.2` (W1.2).
7. **Wave constraint** — W1.4 (`usage_records` per-partition `request_id` uniqueness) is **not touched** by this PR; it gets its own branch + ADR.

---

## Rollback

- **Pre-merge (in-development).** `alembic downgrade -1` from rev `0005_distributed_locks_lease` to `0004_idempotency_keys_invariants` drops the constraint cleanly. No data to preserve (table is empty in every current environment). Alternatively, `git restore .` (plus `git checkout main && git branch -D phase3/wave1.3-distributed-locks-lease-check`) discards all uncommitted changes.
- **Post-merge (in-development).** `git revert <merge-sha>` plus a new migration `0006_revert_distributed_locks_lease.py` whose upgrade body is `op.execute("ALTER TABLE distributed_locks DROP CONSTRAINT IF EXISTS chk_distributed_locks_lease_until_after_acquired_at")` and whose downgrade re-adds it. The repo convention everywhere else is *never* to edit a merged migration in place; a successor migration is always how schema reversals are recorded.
- **Production rollback after data is present.** Same as post-merge above: a new migration with `op.execute("ALTER TABLE distributed_locks DROP CONSTRAINT IF EXISTS chk_distributed_locks_lease_until_after_acquired_at")`. `DROP CONSTRAINT` is a near-instantaneous metadata-only operation in Postgres for non-FK constraints, so no `CONCURRENTLY` variant is needed. The drop is fully transactional and can run inside Alembic's default transaction wrapper. The only consequence is that the application layer must re-introduce the use-case-layer guard it removed when the constraint was promoted.

The only way the rollback could fail is if there are rows violating the constraint *while the constraint is still active*, which Postgres prevents at write time. There is no "rollback caught by stuck data" scenario for CHECK constraints (unlike, say, unique constraints where pre-existing dupes can block adoption).

---

## Consequences

- **Positive — defence-in-depth lease sanity.** The `lease_until > acquired_at` invariant is now a database guarantee, not a wishful application convention. Any future writer (current code, future feature branches, maintenance scripts, replica failovers with stale code) is held to it. One less Python invariant for the (yet-unwritten) lock-manager use-case to enforce; one less class of cross-environment behavioural drift.
- **Wave 1 progress.** W1.3 closes; Wave 1 of Phase 3 advances by one. After W1.4 (`usage_records` per-partition `request_id` uniqueness) lands, Wave 1 will be complete and Wave 2 (data model evolution) can begin.
- **`schema.md` §37 Q10 resolved verbatim** — not bundled, not deferred, not redefined.
- **Self-describing failure mode.** The CHECK constraint name (`chk_distributed_locks_lease_until_after_acquired_at`) is self-describing — any future `IntegrityError` carrying this name lets a debugger jump straight to the violated invariant. This was the strongest counter to the original 2D deferral reasoning.
- **Exception translation at the application boundary.** Lease-validity violations will surface as `psycopg.errors.IntegrityError` rather than a domain-level "invalid lease" error. The (yet-to-be-written) lock-manager use-case will need to catch and translate at the application boundary — standard pattern; will be documented in the Phase 4 lock-manager ADR if/when one lands.
- **Operational.** One additional CHECK constraint to evaluate on every `INSERT`/`UPDATE` to `distributed_locks`. Negligible cost — Postgres CHECK evaluation on a two-column comparison is sub-microsecond, dwarfed by the disk I/O of the write itself.
- **Schema validator / ERD.** Both remain green by construction — the CHECK is declared in the ORM via `CheckConstraint(...)`, mirrored exactly in the migration, and CHECK constraints do not affect the 9 structural checks (none of which inspect CHECKs by name) or the ERD (which ignores CHECKs by design). 87-index count stays at 87; 51-entity ERD count stays at 51.
- **Wave constraint enforced.** No changes to `idempotency_keys` (W1.2 territory), `usage_records` (W1.4), or any application code, repository, service, router, or API surface. The discipline established by W1.1 and W1.2 is maintained.
- **Application-layer contract change.** None. This ADR is purely defensive — the CHECK enforces what the (yet-unwritten) application code is *already required to* compute. Any future application code path that produces `lease_until <= acquired_at` was a bug at the moment of writing; the CHECK turns that latent bug into a surfaced `IntegrityError` with a constraint-name breadcrumb.

---

## Acceptance Criteria

The PR is mergeable when **all** of the following hold:

1. Branch `phase3/wave1.3-distributed-locks-lease-check` exists locally and on `origin`, cut from `main` at `fd0dfa1`.
2. `docs/decisions/ADR-0032-distributed-locks-lease-check.md` exists with this content and (post-status-flip) `Status: Accepted (Phase 3, Wave 1.3, 2026-06-29)`.
3. `backend/alembic/versions/0005_distributed_locks_lease.py` exists with `revision = "0005_distributed_locks_lease"`, `down_revision = "0004_idempotency_keys_invariants"`, both `upgrade()` and `downgrade()` bodies, and the constraint name `chk_distributed_locks_lease_until_after_acquired_at` with predicate `lease_until > acquired_at`.
4. `backend/app/infrastructure/db/models/operations.py` `DistributedLock.__table_args__` contains a `CheckConstraint("lease_until > acquired_at", name="chk_distributed_locks_lease_until_after_acquired_at")` declaration that lexically matches the migration.
5. `docs/database/schema.md` §32 column block lists the CHECK constraint inside the table block; a new **Lease validity invariant (DB-enforced, Phase 3 W1.3)** paragraph has been inserted mirroring §31's W1.2 paragraph; the §32 reconciliation note (paragraph 1208-1217) has been revised from *"whether to add it back is a Phase-3 decision (§37)"* to a "*reversed by Phase 3 W1.3 (ADR-0032, migration `0005_distributed_locks_lease`, 2026-06-29)*" wording.
6. `docs/database/schema.md` §37 Q10 row is marked **Resolved (Phase 3 W1.3, 2026-06-29)** with full constraint details (constraint name, predicate, migration ID, ADR ID), mirroring Q8 (W1.1) and Q9 (W1.2).
7. `docs/database/schema.md` §37 epilogue Wave 1 bullet for §32 q10 reads `✅ Done — Phase 3 W1.3 (ADR-0032, migration 0005_distributed_locks_lease, 2026-06-29)`.
8. `ROADMAP.md` line 173 Wave 1 table annotates W1.3 as `✅ Complete (ADR-0032, migration 0005_distributed_locks_lease, 2026-06-29)`.
9. `CHANGELOG.md` `[Unreleased]` section has a W1.3 sub-section mirroring the W1.1/W1.2 entries (Added / Changed / Validated / Not modified / Scope discipline).
10. `DECISIONS.md` has a one-line cross-link entry for ADR-0032 immediately after the ADR-0031 entry, sorted by ADR number, with the same Status convention.
11. **Pre-upgrade safety SELECT** against live Supabase returns `distributed_locks_lease_violations = 0`. A non-zero result aborts the upgrade and forces the `NOT VALID` + `VALIDATE` two-step variant in a separate follow-up migration.
12. `alembic upgrade head` applies migration `0005`; `pg_constraint` shows `chk_distributed_locks_lease_until_after_acquired_at` present after upgrade with the expected predicate.
13. `alembic downgrade -1` reverts cleanly; `pg_constraint` shows the constraint absent after downgrade.
14. `alembic upgrade head` re-applies cleanly (idempotency); `pg_constraint` shows the constraint present again.
15. `python scripts/ci_gate.py` reports 10/10 PASSED locally against Supabase.
16. GitHub Actions on the PR reports 10/10 green.
17. After merge, annotated tag `v0.3.2-phase3-w1.3` exists locally and on `origin`, pointing at the merge commit; local + remote `phase3/wave1.3-distributed-locks-lease-check` branches are deleted; `git fetch --prune` is clean.
18. `git diff main...HEAD` touches **only** files enumerated in §Migration Plan step 2 (no edits to `export_jobs`, `idempotency_keys`, `usage_records`, or anything else outside W1.3 scope).
19. Status-flip commit (`docs(adr): mark ADR-0032 Accepted`) lands as the final commit on the branch before merge, flipping the ADR header and the `DECISIONS.md` cross-link line from `Proposed` to `Accepted` atomically.
