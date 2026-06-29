# ADR-0031 — Promote `idempotency_keys` Mutability Tracking and Status↔Response Invariant to the Database

**Status:** Proposed (Phase 3, Wave 1.2). To be flipped to Accepted in the pre-merge `docs(adr): mark ADR-0031 Accepted` commit after live validation passes.
**Supersedes / refines:** `docs/database/schema.md` §31 Reconciled-in-2D note (which justified dropping `updated_at` as "redundant — any state transition produces an audit event"); §37 Q9 (prior default: "application invariant only"); extends ADR-0021 — First-Class Idempotency Framework (CR-DB-1).
**Wave:** Phase 3 W1.2 (Schema integrity — promote use-case invariants into the DB).

---

## Context

ADR-0021 (Phase 2 Step A) introduced `idempotency_keys` as a first-class table for exactly-once semantics across payments, AI generation, workflow retries, and webhook surfaces. The intended runtime behaviour (documented in `docs/database/schema.md` §31 "Behaviour") is a two-step finite-state machine:

1. **INSERT** a row with `status='in_flight'` and only `request_hash` populated; `response_hash`, `response_payload`, `http_status` are NULL.
2. On request completion, **UPDATE** the row to `status='succeeded'` or `status='failed'`, populating `response_hash` (mandatory), `response_payload` (optional), and `http_status` (optional).

Two latent issues exist in the current implementation that Phase 3 Wave 1 ("promote use-case invariants into the DB before they accumulate workarounds") is the natural place to fix.

### Issue 1 — Mixin misclassification (`updated_at` missing, no biu trigger)

The ORM declares `IdempotencyKey(UUIDPrimaryKeyMixin, CreatedAtOnlyMixin, Base)`. `CreatedAtOnlyMixin`'s docstring (`backend/app/infrastructure/db/mixins.py:70–71`) reads:

> ```python
> class CreatedAtOnlyMixin:
>     """For immutable / append-only tables (no updated_at, no deleted_at)."""
> ```

But the table IS mutated (step 2 above), and `idempotency_keys` is **also not** in the baseline migration's `_IMMUTABLE_TABLES` tuple (`backend/alembic/versions/0001_baseline.py:1579–1597`) — so it has no `reject_mutation` trigger either. The table sits in a limbo "mutable-but-untracked" state.

Practical consequences:
- **No `updated_at` column** → the in_flight → terminal transition timestamp is unrecoverable; only `created_at` (the in-flight insertion time) is preserved. Stuck-job dashboards and SLA telemetry cannot distinguish "still in flight" from "completed at insertion-time + n".
- **No `tg_idempotency_keys_biu_touch_updated_at` trigger** → even with the column added, application bugs that update `status` to `succeeded`/`failed` without explicitly setting `updated_at` would silently lose audit fidelity. The shared `touch_updated_at()` trigger function (baseline migration lines 89–94, already wired to 30+ tables via `_UPDATED_AT_TABLES`) needs to be bound to this table.
- **Misclassification will spread.** Future code reviewers will see `CreatedAtOnlyMixin` and assume the row is immutable, building wrong mental models or correctness assumptions on top.

The schema.md §31 reconciliation note attributes the original `updated_at` omission to "any state transition produces an audit event." That reasoning is speculative — no audit-event sink exists yet — and conflates *audit replay* (an offline analytical use) with *operational observability* (the live "is this key stuck?" query). The two are different concerns. `updated_at` solves the second cheaply (8 bytes per row, one shared trigger, zero new functions) and the audit log can still grow later for the first.

### Issue 2 — Status↔response invariant unprotected

The `status idempotency_status NOT NULL` column models a finite-state machine:
- `in_flight` → `succeeded` (transitions, `response_hash` MUST be populated)
- `in_flight` → `failed` (transitions, `response_hash` MUST be populated)
- Forbidden: `in_flight` with `response_hash IS NOT NULL`, or terminal (`succeeded`/`failed`) with `response_hash IS NULL`.

Currently this invariant is enforced only by convention. A repository-layer bug that updates `status` to `'succeeded'` while forgetting to set `response_hash` produces silent corruption: future requests with the same idempotency key get a "succeeded" cache hit, but the cached response is empty. This bug is extremely difficult to detect from production telemetry because the symptom (empty response) looks like a legitimate edge case (the request was OK and the result happened to be empty), not a correctness failure.

### Wave 1 thesis

Wave 1 of Phase 3 promotes application-layer invariants to the DB **before** any consumer ships, so the database is correct from day one and no consumer can be written against incorrect assumptions. W1.1 (ADR-0030, merged 2026-06-29) established the precedent for the `export_jobs` uniqueness invariant: zero consumer impact, hand-written migration, full CI gate. W1.2 applies the same playbook to `idempotency_keys`, addressing both Issue 1 (mutability tracking) and Issue 2 (FSM invariant) in a single coordinated migration. The two changes are coordinated because (a) they share the same target table, (b) the CHECK constraint and the trigger interact at UPDATE time (BEFORE-row triggers fire before CHECK validation, so the trigger correctly updates `updated_at` first), and (c) bundling them avoids a second pre-flight + round-trip cycle.

**ADR convention note.** ADR-0031 is the second ADR stored as a standalone file under `docs/decisions/` (after ADR-0030 established the convention in W1.1). ADRs 0001–0029 remain inline in `DECISIONS.md` and are not being migrated; `DECISIONS.md` carries a one-line cross-link entry per file-based ADR. No further `CONTRIBUTING.md` edits are needed — the file-per-ADR convention is already documented there from W1.1.

---

## Decision

Apply three coordinated changes to `idempotency_keys` in a single Alembic migration `0004_idempotency_keys_invariants.py`:

### Change 1 — Add `updated_at` column

```sql
ALTER TABLE idempotency_keys
  ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now();
```

The `DEFAULT now()` clause backfills any existing rows atomically. Production row count is 0 (verified — no consumer code exists yet); dev/test environments will get a sensible non-NULL value matching the column convention used elsewhere in the schema (`mixins.TimestampMixin`).

### Change 2 — Bind shared `touch_updated_at()` trigger to the table

```sql
CREATE TRIGGER tg_idempotency_keys_biu_touch_updated_at
  BEFORE UPDATE ON idempotency_keys
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
```

The `touch_updated_at()` PL/pgSQL function is already defined in the baseline migration (`0001_baseline.py` lines 89–94) and is shared across 30+ tables via the `_UPDATED_AT_TABLES` tuple. **No new function is created**; the migration only binds the existing function to this table.

The baseline migration's `_UPDATED_AT_TABLES` tuple is **not edited** — baseline migrations are historical and never amended in place. The new trigger lives entirely in migration `0004` and is dropped on downgrade.

### Change 3 — Add CHECK constraint for the status↔response invariant

```sql
ALTER TABLE idempotency_keys
  ADD CONSTRAINT chk_idempotency_keys_response_hash_matches_status
  CHECK ((status = 'in_flight') = (response_hash IS NULL));
```

PostgreSQL evaluates `(boolean) = (boolean)` as biconditional (XNOR), so this CHECK:
- PASSES when `status = 'in_flight'` AND `response_hash IS NULL`
- PASSES when `status IN ('succeeded','failed')` AND `response_hash IS NOT NULL`
- FAILS otherwise — exactly the four illegal states (`in_flight` with hash, terminal without hash, in either direction)

### Why `response_hash` is the sole witness in the CHECK

`response_hash` is the canonical "response was computed" witness:
- `response_payload` may legitimately be NULL on terminal rows (e.g., a `204 No Content` succeeded response, or a `500` with no body). Including it in the CHECK over-specifies the invariant and forces the application to fabricate empty JSON for cases where empty is correct.
- `http_status` is text-typed for portability and is conceptually *metadata about how a response was returned*, not *whether one exists*. Idempotency keys are also used for non-HTTP surfaces (workflow retries, AI generation calls via SDK clients) where `http_status` may always be NULL even on terminal states.
- `response_hash` is the SHA-256 hex digest of the canonicalised response body — it is set if and only if a response was produced, regardless of HTTP status, payload structure, or transport.

The CHECK is therefore narrowly scoped to `response_hash`. Extending it to `response_payload` or `http_status` was rejected (see Alternatives §3).

### ORM mirror

Switch the mixin and add the matching `CheckConstraint` to `__table_args__`:

```python
# backend/app/infrastructure/db/models/operations.py

from sqlalchemy import CheckConstraint, ...
from app.infrastructure.db.mixins import TimestampMixin, UUIDPrimaryKeyMixin

class IdempotencyKey(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "idempotency_keys"
    # ... existing columns unchanged ...
    __table_args__ = (
        UniqueConstraint(...),  # unchanged
        CheckConstraint(
            "(status = 'in_flight') = (response_hash IS NULL)",
            name="chk_idempotency_keys_response_hash_matches_status",
        ),
        Index(...),  # unchanged
        Index(...),  # unchanged
    )
```

`TimestampMixin` (`mixins.py:35–47`) provides both `created_at` AND `updated_at`; the existing `CreatedAtOnlyMixin`-supplied `created_at` is shadowed cleanly. SQLAlchemy's MRO resolves `TimestampMixin.created_at` and `TimestampMixin.updated_at` — there is no double-declaration conflict.

---

## Alternatives Considered

1. **Application-only invariant (status quo).** Leave the CHECK out; enforce in the repository layer. *Rejected per Wave 1 mandate.* The "leave it to the application" stance is exactly what created the gap. Every code path that touches `idempotency_keys` must remember the invariant; DB-level enforcement is one-time and permanent.
2. **Trigger-based invariant instead of CHECK.** Use a `BEFORE INSERT OR UPDATE` trigger that RAISEs on violation. *Rejected.* CHECK is faster (no PL/pgSQL dispatch), declared at the column level (better introspection via `pg_constraint`), and is the standard SQL idiom for FSM invariants. Triggers are appropriate for cross-row invariants (e.g., balance bumps in `credit_ledger`); for single-row column relationships, CHECK is correct.
3. **Extend the CHECK to cover `response_payload` and/or `http_status`.** Require ALL response columns to be NULL ↔ `in_flight`. *Rejected.* `response_payload` is legitimately optional even on terminal states (`204 No Content`); `http_status` is metadata, not a witness, and is NULL on non-HTTP surfaces (CLI replays, SDK calls). Over-specification introduces false positives and forces the application to populate fields it has no value for.
4. **Replace `status` enum with a derived/virtual column.** Drop `status`; derive it from `response_hash IS NOT NULL`. *Rejected.* Loses the `succeeded` vs `failed` distinction (both have `response_hash IS NOT NULL`). The enum carries information the hash alone cannot.
5. **Keep `CreatedAtOnlyMixin`; infer staleness from `created_at` alone.** *Rejected.* Cannot distinguish "in_flight, not yet replied" from "succeeded long ago." Operations dashboards and stuck-job alarms need the transition timestamp. `updated_at` is the project's documented convention for mutable tables (`NAMING_CONVENTIONS.md` §5–§6, `mixins.py:35–47`); using it here aligns with 30+ existing tables.
6. **Defer the CHECK to "the same PR that adds the consumer."** Wait until repository code ships, then add the CHECK in the same PR. *Rejected.* Mixes schema-integrity and consumer-code concerns. W1.1 established the "harden before consumer exists" pattern for a reason: no migration risk, no consumer to coordinate, clean isolated PR.
7. **Add `deleted_at` (soft-delete) at the same time.** *Out of scope.* Purge semantics are app-policy, not DB integrity. If we want soft-delete later, that's a separate ADR with its own justification (storage trade-offs, GDPR implications, query-plan changes). W1.2 intentionally stays narrow.
8. **Add the `resource_type_status` partial-unique watchdog index now.** Listed as Deferred in `INDEX_STRATEGY.md` §14a. *Out of scope.* It's an index for a stuck-job watchdog dashboard that doesn't exist yet (no consumer); appropriate to defer until a real query plan justifies it.

---

## Migration Plan

1. **Branch** `phase3/wave1.2-idempotency-keys-invariants` off `main` (HEAD `a239feb`; baseline tag for rollback floor is `v0.3.0-phase3-w1.1` at the W1.1 merge commit).
2. **Single PR, first commit** containing exactly these files:
   - `backend/alembic/versions/0004_idempotency_keys_invariants.py` — hand-written migration. `revision = "0004_idempotency_keys_invariants"`, `down_revision = "0003_export_jobs_partial_unique"`.
     Upgrade body (sequential, single transaction):
     ```python
     op.execute(
         "ALTER TABLE idempotency_keys "
         "ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now()"
     )
     op.execute(
         "CREATE TRIGGER tg_idempotency_keys_biu_touch_updated_at "
         "BEFORE UPDATE ON idempotency_keys "
         "FOR EACH ROW EXECUTE FUNCTION touch_updated_at()"
     )
     op.execute(
         "ALTER TABLE idempotency_keys "
         "ADD CONSTRAINT chk_idempotency_keys_response_hash_matches_status "
         "CHECK ((status = 'in_flight') = (response_hash IS NULL))"
     )
     ```
     Downgrade body (reverse order):
     ```python
     op.execute(
         "ALTER TABLE idempotency_keys "
         "DROP CONSTRAINT chk_idempotency_keys_response_hash_matches_status"
     )
     op.execute(
         "DROP TRIGGER tg_idempotency_keys_biu_touch_updated_at "
         "ON idempotency_keys"
     )
     op.execute(
         "ALTER TABLE idempotency_keys DROP COLUMN updated_at"
     )
     ```
   - `backend/app/infrastructure/db/models/operations.py` — replace `CreatedAtOnlyMixin` with `TimestampMixin` in the `IdempotencyKey` class bases; remove the now-unused `CreatedAtOnlyMixin` from the import line; add `CheckConstraint` to the SQLAlchemy import; insert the matching `CheckConstraint(...)` into `__table_args__` (placed between the `UniqueConstraint` and the two existing `Index` entries for readability).
   - `docs/decisions/ADR-0031-idempotency-keys-invariants.md` — this ADR (new file).
   - `DECISIONS.md` — append one-line cross-link entry (initially Status: Proposed, flipped to Accepted in commit 2).
   - `docs/database/schema.md` §31 — add `updated_at` row to the column block; append one sentence to the **Indexes** paragraph mentioning the new CHECK constraint; update the §31 reconciliation note to acknowledge that W1.2 reverses the prior `updated_at` omission and explain why (mutability is operationally observable, audit-event narrative was speculative); §37 Q9 row marked **Resolved (Phase 3 W1.2, 2026-06-29)** with constraint name + migration cite; Wave 1 bullet for §31 q9 marked **✅ Done**.
   - `ROADMAP.md` Phase 3 wave table — annotate W1.2 as **✅ Complete (ADR-0031, migration `0004_idempotency_keys_invariants`)** alongside W1.1.
   - `CHANGELOG.md [Unreleased]` — new sub-section "Phase 3 Wave 1.2 — `idempotency_keys` mutability + status↔response invariant (ADR-0031)" with **Added** / **Changed** / **Validated** / **Scope discipline** blocks mirroring W1.1's structure.
3. **Live validation against Supabase** (run from `backend/`, credentials via `.env.validation`):
   - **Pre-upgrade safety check** (also AC #3 below). Run against the live target:
     ```sql
     SELECT COUNT(*) FROM idempotency_keys
     WHERE (status = 'in_flight') <> (response_hash IS NULL);
     ```
     Must return `0` before `alembic upgrade head`. If non-zero, abort — the CHECK constraint would refuse to apply, and the data inconsistency needs investigation first.
   - `alembic upgrade head` → assert via `information_schema.columns` that `idempotency_keys.updated_at` exists (type `timestamp with time zone`, NOT NULL); via `pg_trigger` that `tg_idempotency_keys_biu_touch_updated_at` exists on `idempotency_keys`; via `pg_constraint` that `chk_idempotency_keys_response_hash_matches_status` exists with the expected `consrc` (or `pg_get_constraintdef`) text.
   - `alembic downgrade -1` → assert all three are gone; rev is back to `0003_export_jobs_partial_unique`.
   - `alembic upgrade head` (re-apply) → idempotency proven; rev is back to `0004_idempotency_keys_invariants`.
   - `python scripts/validate_schema.py` → must remain 9/9 (no validator check covers `_UPDATED_AT_TABLES` membership or CHECK constraint presence, so the gate stays green by construction; the new column is automatically picked up by `check_table_parity` from ORM metadata).
   - `python scripts/regenerate_erd.py` + `python scripts/compare_erd.py` → must remain 0 drift (ERD renders entities + FKs only, not CHECK constraints or triggers; adding `updated_at` is a column-shape change that the ERD doesn't display in its current configuration).
4. **Full CI gate locally** — `python scripts/ci_gate.py` returns 10/10 (`.env.validation` present, stages 5–9 execute live).
5. **Status transition on merge.** ADR header and `DECISIONS.md` cross-link both say `Proposed` during PR review. Final pre-merge commit on the branch flips both to `Accepted` in a single atomic commit titled `docs(adr): mark ADR-0031 Accepted`. Same pattern as W1.1.
6. **Wave constraint** — W1.3 (`distributed_locks` lease CHECK) and W1.4 (`usage_records` per-partition request_id uniqueness) are **not touched** by this PR. No changes to `export_jobs` (W1.1 territory). Each remaining wave gets its own branch + ADR.

---

## Rollback

- **Pre-merge (in-development).** `alembic downgrade -1` from rev `0004_idempotency_keys_invariants` to `0003_export_jobs_partial_unique` drops the CHECK, the trigger, and the column cleanly. No data to preserve (table is empty in every current environment).
- **Post-merge (in-development).** `git revert <merge-sha>` plus a new migration `0005_revert_idempotency_keys_invariants.py` whose upgrade body executes the same three DROP statements. The repo convention everywhere else is *never* to edit a merged migration in place; a successor migration is always how schema reversals are recorded.
- **Production rollback after data is present.** Drop in this exact order to avoid a brief window where the trigger touches a column the CHECK would forbid:
  1. `ALTER TABLE idempotency_keys DROP CONSTRAINT chk_idempotency_keys_response_hash_matches_status;` (removes the gate)
  2. `DROP TRIGGER tg_idempotency_keys_biu_touch_updated_at ON idempotency_keys;` (removes auto-bumping)
  3. `ALTER TABLE idempotency_keys DROP COLUMN updated_at;` (removes the column)
  No row-level fixup needed for the column drop (Postgres handles it transactionally). If the application code by then depends on `updated_at`, deploy the rollback for that code FIRST, then run these DDL operations.

---

## Consequences

- **Positive — FSM invariant becomes race-free.** No application code path, present or future, can write inconsistent (status, response_hash) tuples. Silent-corruption bugs in the repository layer fail loudly at commit time with a `CheckViolation` instead of caching empty responses.
- **Positive — mutability becomes audit-tracked.** `updated_at` records the in_flight → terminal transition timestamp, unlocking stuck-job dashboards, SLA telemetry, and post-incident forensics. Cost is 8 bytes per row and one shared trigger (already wired to 30+ tables).
- **Positive — mixin misclassification resolved.** Future contributors reading `TimestampMixin` form the correct mental model: this row is updated. No more "wait, isn't this immutable?" confusion in code review.
- **Positive — establishes the W1.x pattern.** W1.3 and W1.4 will follow this exact shape (pre-flight SELECT, ADR, hand-written migration, round-trip, CI 10/10, status-flip commit).
- **Application-layer contract change.** Once the CHECK is in place, application code MUST follow this contract:
  1. INSERT new in_flight row with `response_hash = NULL`, `response_payload = NULL` (optional), `http_status = NULL` (optional).
  2. To transition to terminal: UPDATE in a single statement setting `status` AND `response_hash` simultaneously, e.g.:
     ```sql
     UPDATE idempotency_keys
       SET status = 'succeeded',
           response_hash = $1,
           response_payload = $2,
           http_status = $3
       WHERE tenant_id = $4 AND key = $5 AND resource_type = $6
         AND status = 'in_flight';
     ```
  3. Code that sets `status` without `response_hash` (or vice versa) raises `psycopg.errors.CheckViolation` at commit. This is the desired behaviour. The Phase 3 repository implementation (Wave 2+) will document and test this contract.
- **Exception translation at the application boundary.** Invariant violations surface as `psycopg.errors.CheckViolation`, not as a domain error. The Phase 4 idempotency middleware will catch and translate at the application boundary (standard pattern; will be documented when the middleware lands).
- **Operational — UPDATE write-amplification.** Each UPDATE now fires the `touch_updated_at` trigger (one column assignment, no I/O) and validates the CHECK (one boolean comparison). Both costs are nanoseconds per row. Irrelevant in practice.
- **Schema validator / ERD.** Both remain green by construction — `validate_schema.py` has no check for `_UPDATED_AT_TABLES` membership or CHECK constraint presence; ERD renders entities + FKs only.
- **Wave constraint enforced.** No changes to `export_jobs` (W1.1 territory), `distributed_locks` (W1.3), or `usage_records` (W1.4). No changes to baseline migration. No changes to application code under `app/application/` or `app/api/`.
- **ADR convention.** Second file-per-ADR after ADR-0030; convention now well-established. `CONTRIBUTING.md` requires no further edits (W1.1 already documented the convention).

---

## Acceptance Criteria

The PR is mergeable when **all** of the following hold:

1. Branch `phase3/wave1.2-idempotency-keys-invariants` exists, cut from `main` HEAD `a239feb` (the W1.1 merge commit).
2. `backend/alembic/versions/0004_idempotency_keys_invariants.py` exists with `revision = "0004_idempotency_keys_invariants"` and `down_revision = "0003_export_jobs_partial_unique"`; up = three sequential `op.execute` calls (ADD COLUMN, CREATE TRIGGER, ADD CONSTRAINT); down = three `op.execute` calls in reverse order (DROP CONSTRAINT, DROP TRIGGER, DROP COLUMN).
3. **Pre-upgrade safety check.** Before any `alembic upgrade head` is run on the W1.2 branch, the following SELECT against the live Supabase target returns `0`:
   ```sql
   SELECT COUNT(*) FROM idempotency_keys
   WHERE (status = 'in_flight') <> (response_hash IS NULL);
   ```
   A non-zero result aborts the upgrade pending investigation — the CHECK constraint would otherwise refuse to apply mid-flight.
4. `IdempotencyKey` in `backend/app/infrastructure/db/models/operations.py` uses `TimestampMixin` (not `CreatedAtOnlyMixin`); the import line is updated; `CheckConstraint` is added to the SQLAlchemy import; `__table_args__` carries the matching `CheckConstraint(..., name="chk_idempotency_keys_response_hash_matches_status")` with the exact same predicate as the migration.
5. `docs/decisions/ADR-0031-idempotency-keys-invariants.md` exists with the full text of this ADR.
6. `DECISIONS.md` contains one new cross-link line referencing the file (title + Accepted status + relative link). During PR review the cross-link reads `Proposed`; the status-flip commit flips it to `Accepted`.
7. `docs/database/schema.md` §31 column block lists `updated_at`; the Indexes paragraph mentions the new CHECK constraint; the §31 reconciliation note is updated to acknowledge that W1.2 reverses the prior `updated_at` omission with stated reasoning; §37 Q9 row marked **Resolved (Phase 3 W1.2, 2026-06-29)** with constraint name + migration cite; Wave 1 bullet for §31 q9 marked ✅ Done.
8. `docs/database/INDEX_STRATEGY.md` is **not modified** (no new index or unique constraint is created; CHECK constraints are not tracked by `INDEX_STRATEGY.md`).
9. `ROADMAP.md` Phase 3 wave table shows **W1.2 ✅ Complete** with ADR + migration cite, alongside W1.1.
10. `CHANGELOG.md [Unreleased]` has a new "Phase 3 Wave 1.2" sub-section under **Added** / **Changed** / **Validated** / **Scope discipline**, mirroring the W1.1 entry's structure.
11. Live Supabase validation passes: `alembic upgrade head → downgrade -1 → upgrade head` round-trip clean; `pg_constraint` shows the CHECK with the expected predicate; `pg_trigger` shows the BIU trigger; `information_schema.columns` shows `updated_at` as `timestamp with time zone NOT NULL`; after downgrade all three are gone; after re-upgrade all three return.
12. `python scripts/validate_schema.py` returns 9/9 unchanged.
13. `python scripts/regenerate_erd.py` + `python scripts/compare_erd.py` returns 0 drift.
14. `python scripts/ci_gate.py` returns 10/10 locally with `.env.validation` loaded.
15. GitHub Actions CI run on the PR returns 10/10 against the pgvector service container.
16. `git diff main...HEAD` touches **only** files enumerated in §Migration Plan step 2. Specifically NOT touched: `export_jobs` ORM/migration (W1.1 territory), `distributed_locks` (W1.3), `usage_records` (W1.4), baseline migration (`0001_baseline.py`), `INDEX_STRATEGY.md`, `CONTRIBUTING.md`, `app/application/`, `app/api/`, `backend/alembic.ini`, `backend/alembic/env.py`, `backend/pyproject.toml`, CI configs.
17. Status-flip commit (`docs(adr): mark ADR-0031 Accepted`) lands as the final commit on the branch before merge, flipping the ADR header and the `DECISIONS.md` cross-link line from `Proposed` to `Accepted` atomically.
