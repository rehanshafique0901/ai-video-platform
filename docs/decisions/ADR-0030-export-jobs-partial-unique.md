# ADR-0030 — Promote `export_jobs (render_job_id, format, quality, orientation)` Uniqueness to a Partial-Unique DB Constraint

**Status:** Accepted (Phase 3, Wave 1.1, 2026-06-29). Validated end-to-end against Supabase Postgres 17.6 + pgvector 0.8.0; full 10-stage CI gate green; alembic forward/reverse/idempotency round-trip clean; schema validator 9/9; ERD comparator 0 drift; pre-upgrade safety check returned `export_jobs_active_rowcount = 0`.
**Supersedes / refines:** `docs/database/schema.md` §37 Q8 (prior default: "enforce in use-case layer"); `docs/database/INDEX_STRATEGY.md` §8 row `uq_export_jobs_render_job_id_format_quality_orientation` (Deferred → Implemented).
**Wave:** Phase 3 W1.1 (Schema integrity — promote use-case invariants into the DB).

---

## Context

`export_jobs` carries an implicit invariant: **at most one live or successfully-completed export per `(render_job_id, format, quality, orientation)` tuple**. The Phase 2A draft and `INDEX_STRATEGY.md` §8 already named this as `uq_export_jobs_render_job_id_format_quality_orientation (partial active)`, but Phase 2B deliberately deferred enforcement (`schema.md` §37 q8 default verdict: "enforce in use-case layer"). Phase 3 Wave 1 ("promote use-case invariants into the DB before they accumulate workarounds") revisits that verdict.

Three forces push the constraint down to the database now, before the export use-case is written in Phase 4:

1. **No use-case code exists yet** (`app/application/` is an empty package skeleton from Phase 2C). Promoting the invariant now means the Phase 4 use-case is written *against* a DB-enforced rule rather than duplicating the check in Python first and later having to remove it.
2. **Race-condition class.** A "check-then-insert" in the use-case layer (`SELECT … WHERE status IN (active set)` followed by `INSERT`) is racy under concurrent retry storms — exactly the workload export jobs see when a render finishes and downstream workflow steps fan out simultaneously. Postgres uniqueness is the only race-free guarantor.
3. **Cheap to enforce, cheap to remove.** A partial-unique btree on one FK + three small enums over a table with low expected cardinality per `render_job_id` (≤ 12 combinations — 4 formats × 4 qualities × 3 orientations, in practice far fewer per render) is sub-millisecond on insert and trivial to drop in a future migration if the invariant ever softens.

**ADR convention note.** ADR-0030 is the **first ADR stored as a standalone file** under `docs/decisions/` (filename `ADR-0030-export-jobs-partial-unique.md`). ADRs 0001–0029 remain inline in `DECISIONS.md` and are not being migrated; extracting them is explicitly out of scope for Wave 1 and would be its own no-code-impact PR if ever pursued. All Phase-3-and-later ADRs use the file-per-ADR convention, and `DECISIONS.md` carries a one-line cross-link entry per file-based ADR (title + status + relative link) so a single-file reader still discovers them. `CONTRIBUTING.md` §1 / §6 are updated in the same PR as this ADR to reflect the new convention.

---

## Decision

Add a single partial-unique index to `export_jobs`:

```sql
CREATE UNIQUE INDEX uq_export_jobs_render_job_id_format_quality_orientation
ON export_jobs (render_job_id, format, quality, orientation)
WHERE status IN ('queued', 'running', 'succeeded');
```

- **Scope** — rows where `status` is in the *active-or-fulfilled* set. `failed` and `canceled` rows are deliberately excluded so a user or system can retry an export after failure or cancellation. The `succeeded` inclusion is justified by `export_jobs` being the **canonical artefact row** for that export configuration: `download_count`, `last_downloaded_at`, `file_size_bytes`, and `output_media_asset_id` all live on it (`schema.md` §17 calls these out as the rationale for those columns), so two `succeeded` rows for the same key would split the "my downloads" feed and the storage-quota accounting. This whitelist matches `INDEX_STRATEGY.md` §8's "partial active" phrasing and the precedent set by `uq_subscriptions_tenant_id_active` (`schema.md` §20, `WHERE status IN ('active','trialing','past_due')`). `export_jobs` has no `deleted_at` column to scope on — render/export jobs intentionally do not use `SoftDeleteMixin` (`schema.md` §17 reconciliation note: "operationally terminal records, not soft-deletable user objects").
- **Naming** — `uq_export_jobs_render_job_id_format_quality_orientation`, already declared in `INDEX_STRATEGY.md` §8 and following `NAMING_CONVENTIONS.md` §3 (`uq_<table>_<columns>`).
- **Shape** — declared on the ORM as `Index(name, *cols, unique=True, postgresql_where=text(...))` inside `ExportJob.__table_args__`. This is the established codebase pattern for partial-unique indexes (already used across the existing ORM for soft-delete-scoped uniqueness, single-active-record uniqueness, and effective-window uniqueness — `validate_schema.py`'s `check_unique_constraints` consumes both `UniqueConstraint` objects and `Index(unique=True)` objects via `index.unique`, and `check_indexes` picks them up via `idx.name`, so no validator change is needed).
- **No `UniqueConstraint`** — PostgreSQL does not support `WHERE` clauses on table-level `UNIQUE` constraints; partial uniqueness must be expressed as a `CREATE UNIQUE INDEX … WHERE …`. The ORM equivalent is `Index(unique=True, postgresql_where=…)`. This is a mandatory shape, not a preference.
- **No `CONCURRENTLY`** — the table is empty at the time of upgrade in every environment that currently exists (the live Supabase validation target as of 2026-06-29 has zero `export_jobs` rows; the Migration Plan §1 step below makes that an explicit pre-upgrade gate). If/when the constraint ever needs to be rebuilt against a populated production table, that operation lives in its own follow-up migration with `CREATE INDEX CONCURRENTLY` per `INDEX_STRATEGY.md` §17.

---

## Alternatives Considered

1. **Full unique index (no `WHERE` clause).** Would prohibit retrying an export after `failed` / `canceled`. Breaks the "retry after failure" UX the export status enum was explicitly designed to support. *Rejected.*
2. **Narrower scope `WHERE status IN ('queued','running')` (in-flight only).** Permits two `succeeded` rows for the same key to coexist, splitting the canonical-artefact accounting described above. *Rejected.*
3. **Status-quo: enforce only at use-case layer.** Racy under concurrent fan-out; forces every consumer (UI re-submit, workflow retry, admin re-export, future cron sweep) to coordinate via a shared Python lock or risk duplicates. *Rejected per Wave 1 mandate.*
4. **Table-level `UniqueConstraint`.** Cannot carry a `WHERE` clause in Postgres. *Not feasible.*
5. **Include `requested_by_user_id` in the key.** Would allow two different users to request the same export of the same render — but the export *produces a shared `output_media_asset_id`*, so a second copy is pure waste. *Rejected.*
6. **`CHECK` constraint with a subquery.** Postgres `CHECK` cannot reference other rows. *Not feasible.*
7. **Defer to Phase 8 (Rendering Engine).** Wave 1's entire rationale is that schema integrity should land before any consumer code, exactly to avoid having to refactor consumers later. *Rejected.*

---

## Migration Plan

1. **Branch** `phase3/wave1.1-export-jobs-partial-unique` off `main` (HEAD `159cc24`; baseline tag `v0.2.2-phase2d-docs-reconciled` at `412796f` is the rollback floor).
2. **Single PR, first commit** containing:
   - `backend/alembic/versions/0003_export_jobs_partial_unique.py` — hand-written (Alembic autogenerate does not reliably emit partial-unique indexes via `postgresql_where`; it produces a vanilla unique constraint instead). `revision = "0003_export_jobs_partial_unique"`, `down_revision = "0002_seed_system_data"`. Upgrade body:
     ```python
     op.create_index(
         "uq_export_jobs_render_job_id_format_quality_orientation",
         "export_jobs",
         ["render_job_id", "format", "quality", "orientation"],
         unique=True,
         postgresql_where=sa.text("status IN ('queued','running','succeeded')"),
     )
     ```
     Downgrade body:
     ```python
     op.drop_index(
         "uq_export_jobs_render_job_id_format_quality_orientation",
         table_name="export_jobs",
     )
     ```
   - `backend/app/infrastructure/db/models/jobs.py` — append the matching `Index(..., unique=True, postgresql_where=text(...))` to `ExportJob.__table_args__`.
   - `docs/decisions/ADR-0030-export-jobs-partial-unique.md` — this ADR (new file; also creates the `docs/decisions/` directory).
   - `DECISIONS.md` — append one-line cross-link entry pointing at the file.
   - `docs/database/schema.md` §17 — update the `export_jobs` reconciliation note (the "not implemented; Phase-3 decision" bullet flips to "Implemented via ADR-0030 / migration `0003`; see `INDEX_STRATEGY.md` §8"); §37 Q8 row marked Resolved with the migration cite.
   - `docs/database/INDEX_STRATEGY.md` §8 — move the row from **Deferred (Phase 3)** → **Implemented**; justification cites ADR-0030 and migration `0003`. §18 reconciliation summary counts adjusted (+1 implemented, −1 deferred).
   - `ROADMAP.md` Phase 3 wave table — annotate W1.1 as **✅ Complete (ADR-0030, migration 0003)**.
   - `CHANGELOG.md [Unreleased]` — new sub-section "Phase 3 Wave 1.1 — `export_jobs` partial-unique constraint (ADR-0030)" with **Added** / **Changed** entries.
   - `CONTRIBUTING.md` — three small text edits acknowledging file-per-ADR going forward:
     - §1 ground rule 2 — "New libraries require an ADR (either as a new file in `docs/decisions/` or appended to `DECISIONS.md` for compatibility with ADR-0001 through ADR-0029)."
     - §6 documentation policy — "Add an ADR for any non-trivial trade-off (see `docs/decisions/` for the file-per-ADR convention introduced in ADR-0030; older ADRs live inline in `DECISIONS.md`)."
     - New sub-bullet under §6 — pointer to ADR-0030 as the convention-change record.
3. **Live validation against Supabase** (run from `backend/`, credentials via `.env.validation`):
   - **Pre-upgrade safety check.** Confirm `SELECT COUNT(*) FROM export_jobs WHERE status IN ('queued','running','succeeded')` returns `0` against the live target before `alembic upgrade head`. If non-zero, abort — the upgrade would fail mid-flight, and a populated table requires the production-rollback variant (`CREATE INDEX CONCURRENTLY`) instead of the in-development path.
   - `alembic upgrade head` → assert `pg_indexes` row for `uq_export_jobs_render_job_id_format_quality_orientation` exists with `indexdef` containing `WHERE … status = ANY` over the three values.
   - `alembic downgrade -1` → assert that exact index is gone; nothing else in `pg_indexes` changed (diff = exactly one row removed).
   - `alembic upgrade head` (re-apply) → idempotency proven.
   - `python scripts/validate_schema.py` → must remain 9/9 (the new unique index lifts `check_unique_constraints` and `check_indexes` expected sets by one each, which the validator picks up automatically from the updated ORM metadata).
   - `python scripts/regenerate_erd.py` + `python scripts/compare_erd.py` → must remain 0 drift (ERD reflects entities + FKs, not non-FK indexes).
4. **Full CI gate locally** — `python scripts/ci_gate.py` returns 10/10 (`.env.validation` is present, so stages 5–9 execute live). Push, open PR against `main` → GitHub Actions runs the identical gate against the pgvector service container.
5. **Status transition on merge.** The ADR header and the `DECISIONS.md` cross-link entry both initially say `Proposed` during PR review. The final pre-merge commit on this branch flips both to `Accepted` in a single atomic commit titled `docs(adr): mark ADR-0030 Accepted`. This keeps the merge timeline traceable via `git log --grep "ADR-0030 Accepted"` and prevents an "Accepted" ADR from existing in an unmerged branch.
6. **Wave constraint** — W1.2 (`idempotency_keys` CHECK + `updated_at`), W1.3 (`distributed_locks` lease CHECK), and W1.4 (`usage_records` per-partition request_id uniqueness) are **not touched** by this PR. Each gets its own branch + ADR.

---

## Rollback

- **Pre-merge (in-development).** `alembic downgrade -1` from rev `0003_export_jobs_partial_unique` to `0002_seed_system_data` drops the index cleanly. No data to preserve (table is empty in every current environment).
- **Post-merge (in-development).** `git revert <merge-sha>` plus a new migration `0004_revert_export_jobs_partial_unique.py` whose upgrade body is `op.drop_index(...)`. The repo convention everywhere else is *never* to edit a merged migration in place; a successor migration is always how schema reversals are recorded. The revert can later be re-reverted under a successor ADR if policy reverses.
- **Production rollback after data is present.** Drop with `op.execute("DROP INDEX CONCURRENTLY IF EXISTS uq_export_jobs_render_job_id_format_quality_orientation")` in a non-transactional Alembic migration. No row-level fixup needed (dropping an index never touches data). The only consequence is that the application layer must re-introduce the use-case-layer guard it removed when the constraint was promoted.

---

## Consequences

- **Positive — race-free uniqueness.** One less Python invariant for the Phase 4 export use-case to enforce and test; one less class of cross-region replication anomaly; `INDEX_STRATEGY.md` §8 deferred-count drops by one; establishes the migration + ADR + doc-update pattern that Wave 1.2 / 1.3 / 1.4 will reuse.
- **Re-export of a `succeeded` job now requires DELETE-then-create at the application layer.** Today the same workflow holds in the (yet-to-be-written) use-case layer; this ADR only moves enforcement to the DB. The Phase 4 export use-case must explicitly choose between two semantics on conflict — **(a)** reject the re-export attempt with a domain error and ask the caller to delete the existing succeeded row first, or **(b)** perform an explicit DELETE-and-recreate inside a single transaction (which also requires deciding what happens to the old `output_media_asset_id`: orphan-and-retain, soft-mark, or hard-delete). Either policy is valid. **This ADR does NOT promise "re-export overwrites old export";** that semantic choice belongs to the Phase 4 export use-case ADR. We are only promising that two simultaneous-or-fulfilled exports of the same `(render_job_id, format, quality, orientation)` cannot coexist.
- **Exception translation at the application boundary.** Duplicate-export attempts will surface as `psycopg.errors.UniqueViolation` rather than a domain-level "duplicate export" error. The Phase 4 use-case will need to catch and translate at the application boundary (standard pattern; will be documented in `CONTRIBUTING.md` once the use-case lands).
- **Operational.** Tiny write-amplification on the insert path (one extra btree page touch). Negligible.
- **Schema validator / ERD.** Both remain green by construction — the partial-unique declared via `Index(unique=True, postgresql_where=…)` is the established codebase pattern for partial-unique indexes, and the ERD ignores non-FK indexes.
- **ADR convention.** This is the first file-per-ADR; all Phase-3-and-later ADRs follow the same convention; ADR-0001 through ADR-0029 remain inline in `DECISIONS.md` and are not migrated. `CONTRIBUTING.md` §1 / §6 are updated in the same PR.
- **Wave constraint enforced.** No changes to `idempotency_keys`, `distributed_locks`, or `usage_records`.

---

## Acceptance Criteria

The PR is mergeable when **all** of the following hold:

1. Branch `phase3/wave1.1-export-jobs-partial-unique` exists, cut from `main` HEAD `159cc24`.
2. `backend/alembic/versions/0003_export_jobs_partial_unique.py` exists with `revision = "0003_export_jobs_partial_unique"` and `down_revision = "0002_seed_system_data"`; up = `create_index(unique=True, postgresql_where=text("status IN ('queued','running','succeeded')"))`; down = `drop_index`.
3. `ExportJob.__table_args__` in `backend/app/infrastructure/db/models/jobs.py` carries the matching `Index(...)` declaration (identical name, columns, `unique=True`, and `postgresql_where`).
4. `docs/decisions/ADR-0030-export-jobs-partial-unique.md` exists with the full text of this ADR.
5. `DECISIONS.md` contains one new cross-link line referencing the file (title + Accepted status + relative link). During PR review the cross-link reads `Proposed`; the status-flip commit (Migration Plan §5) flips it to `Accepted`.
6. `docs/database/schema.md` §17 updated (reconciliation bullet flipped from "Phase-3 decision" to "Implemented via ADR-0030 / migration 0003"); §37 Q8 row marked Resolved.
7. `docs/database/INDEX_STRATEGY.md` §8 row moved Deferred → Implemented; §18 summary counts adjusted (+1 implemented, −1 deferred).
8. `ROADMAP.md` Phase 3 wave table shows W1.1 as ✅ Complete with ADR + migration cite.
9. `CHANGELOG.md [Unreleased]` has a new "Phase 3 Wave 1.1" sub-section under **Added** / **Changed**.
10. **Pre-upgrade safety check.** `SELECT COUNT(*) FROM export_jobs WHERE status IN ('queued','running','succeeded')` against the live Supabase target returns `0` before `alembic upgrade head` is run. A non-zero result aborts the upgrade and forces the production-rollback variant (`CREATE INDEX CONCURRENTLY` in a non-transactional follow-up migration).
11. Live Supabase validation passes: `alembic upgrade head → downgrade -1 → upgrade head` round-trip clean; `validate_schema.py` 9/9; `compare_erd.py` 0 drift.
12. `python scripts/ci_gate.py` returns 10/10 locally with `.env.validation` loaded.
13. GitHub Actions CI run on the PR returns 10/10 against the pgvector service container.
14. `git diff main...HEAD` touches **only** files enumerated in §Migration Plan step 2 (no edits to `idempotency_keys`, `distributed_locks`, `usage_records`, or anything else outside W1.1 scope). The CONTRIBUTING.md edits in step 2 are included; nothing else is.
15. Status-flip commit (`docs(adr): mark ADR-0030 Accepted`) lands as the final commit on the branch before merge, flipping the ADR header and the `DECISIONS.md` cross-link line from `Proposed` to `Accepted` atomically.
