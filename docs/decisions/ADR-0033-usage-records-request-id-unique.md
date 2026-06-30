# ADR-0033 — Promote `usage_records` per-partition `request_id` Uniqueness to a Database Constraint

**Status:** Proposed (Phase 3 W1.4, 2026-06-30). Will flip to Accepted on the final pre-merge commit once the live validation summary below is populated.
**Wave:** Phase 3 W1.4 (Schema integrity — closes Wave 1).
**Resolves:** `docs/database/schema.md` §37 Q6 (the W1.4-specific framing).
**Builds on:** ADR-0019 (CR-12 — Immutable AI Cost Tracking); ADR-0021 (CR-DB-1 — First-Class Idempotency Framework, defense-in-depth retention of per-table idempotency columns).
**Operational steps:** `docs/engineering/RUNBOOK_WAVE.md` (per CONTRIBUTING.md §6 — first ADR to reference the runbook in place of inlining operational steps).

---

## Context

Phase 3 Wave 1 promotes use-case-layer invariants into the database before they accumulate workarounds. W1.1 (ADR-0030, `export_jobs` partial-unique), W1.2 (ADR-0031, `idempotency_keys` FSM CHECK + `updated_at`), and W1.3 (ADR-0032, `distributed_locks` lease CHECK) shipped this pattern across three single-PR migrations. W1.4 is the wave-closing item and resolves `schema.md` §37 Q6: *"Should `usage_records` add a per-partition partial-unique `(request_id)` index, or rely on `idempotency_keys`?"*

### Architectural-review process preceding this ADR

The W1.4 pre-flight surfaced an architectural ambiguity that earlier waves had not: two repository document lineages describe related-but-not-identical patterns for `usage_records.request_id` enforcement.

1. **Phase 3 wave-planning artifacts** (`ROADMAP.md` line 173; `schema.md` §37 Q6 + §37 Wave 1 epilogue; `INDEX_STRATEGY.md` line 147 deferred-index entry; ADR-0030 line 94; ADR-0031 lines 197 + 218; ADR-0032 lines 102 + 119; `backend/alembic/versions/0006_widen_alembic_version_num.py` lines 13–14; `CHANGELOG.md` v0.3.3-infra entry lines 23–24; `docs/engineering/RUNBOOK_WAVE.md` line 349; `.cursor/rules/project-overview.mdc` line 43) consistently anticipate a W1.4 migration named `0007_usage_records_request_id_unique` adding `request_id` uniqueness.

2. **Earlier architectural documents** (`schema.md` §18 Step-A draft; `schema.md` §31 line 1175 CR-12 use-case row; `API_CONTRACT.md` line 233 webhook handlers; `ARCHITECTURE.md` §8k.1 lines 1320–1366 CR-12 domain spec) describe a broader `(provider, request_id)` idempotency pattern at the application/architectural layer.

A read-only architectural review (7 turns, 2026-06-30) examined both lineages without reconciling them. The review's findings:

- `schema.md` §37 Q6's column header is literally "Default if undecided" (not "Decision"); the row's `"rely on idempotency_keys"` cell is the no-op fallback, not a binding commitment. §18 explicitly punts to "a Phase-3 decision (§37)." `INDEX_STRATEGY.md` uses conditional language ("if needed"). The Phase 2D documents *deferred* the question — they did not decide it.
- Every wave-era document anticipates W1.4 as an *implementation* wave, not a documentation-only architectural-clarification wave. The strongest single statement is ADR-0031 line 218: *"W1.3 and W1.4 will follow this exact shape (pre-flight SELECT, ADR, hand-written migration, round-trip, CI 10/10, status-flip commit)."* The v0.3.3-infra migration was scoped specifically to make `0007_usage_records_request_id_unique` (35 chars) fit, after W1.3 hit the `VARCHAR(32)` ceiling.
- The constraint shape implied by the wave-planning artifacts is single-column `(request_id)`. The shape implied by the broader architectural documents is `(provider, request_id)` — but no wave-era document anticipates the `provider_id` column add, FK, denormalisation, ERD update, or CR-12 producer obligation that the architectural shape would require. The architectural pattern is documented; the architectural pattern's implementation in W1.4 is not.
- `(model_id, request_id)` was considered during the review and rejected: zero matches across the workspace; not a documented option; strictly weaker than `(provider, request_id)` (every duplicate the latter rejects, the former rejects too, plus same-provider-different-model-same-request_id cases; the inverse does not hold).

The Phase 3 wave-planning artifacts consistently anticipate a `request_id`-based W1.4 implementation. Earlier architectural documents describe provider-scoped idempotency at the application level. W1.4 implements the scope reflected in the Phase 3 planning artifacts without attempting to reconcile that broader architectural question.

### Producer-absence observation

`usage_records.request_id` has zero non-model callsites in `backend/app/` today. CR-12 (the Usage Recorder middleware named in `schema.md` §31 line 1175 and `ARCHITECTURE.md` §8k.1) has not been built. W1.4 is structurally distinct from W1.1–W1.3 in this respect: those waves promoted invariants that some piece of application code *already* assumed; W1.4 establishes a database invariant for a producer that is documented but not yet implemented. This is acceptable because ADR-0019 ("each call produces exactly one immutable `UsageRecord`") and ADR-0021 ("per-table idempotency columns … remain in place as defense in depth") together establish the invariant as a documented architectural intent; W1.4 promotes that intent to a DB-level guarantee in advance of the producer.

---

## Decision

Add one per-partition partial-unique index to every child partition of `usage_records`:

```sql
CREATE UNIQUE INDEX uq_<child>_request_id
  ON <child> (request_id) WHERE request_id IS NOT NULL;
```

- **Predicate:** `(request_id) WHERE request_id IS NOT NULL`. Partial because `request_id` is nullable (system-initiated calls without a vendor id leave it NULL and must be allowed to coexist).
- **Naming:** `uq_<child>_request_id` per child — e.g. `uq_usage_records_y2025m12_request_id`, `uq_usage_records_default_request_id`. Pattern matches `INDEX_STRATEGY.md` line 147's `uq_usage_records_<part>_request_id` placeholder. Longest expected name is `uq_usage_records_y2028m01_request_id` = 38 characters (PostgreSQL 63-char identifier limit; comfortable headroom).
- **Scope:** the index is created on every existing child partition (26 monthly + 1 DEFAULT = 27 today) and must be added to every future child partition by whatever creates them (see §Implementation Notes — Future-partition contract).
- **Single-column by deliberate choice.** Broader provider-scoped idempotency semantics remain documented elsewhere in the architecture; W1.4 implements the scope reflected in the Phase 3 planning artifacts. Any future move to `(provider, request_id)` would be a separate architectural decision and migration (see §Future Considerations).

---

## Alternatives Considered

1. **`(model_id, request_id)`.** *Rejected.* Not documented anywhere in the repository (zero matches across the workspace for `model_id,\s*request_id`). Strictly weaker than `(provider, request_id)` — misses the "same provider, different `model_id`, same `request_id`" CR-12-violation class. Would require its own architectural ADR amending ADR-0019's "exactly one UsageRecord per call." Not in W1.4 scope.

2. **`(provider, request_id)` with `provider_id` column add.** *Rejected for W1.4 scope.* Architecturally documented (`schema.md` §31 line 1175; `API_CONTRACT.md` line 233; `ARCHITECTURE.md` §8k.1) but no wave-era planning artifact anticipates W1.4 introducing the column add, FK, denormalisation, ERD edge update, or CR-12 producer obligation. Choosing this shape in W1.4 would be a *scope expansion* of W1.4 beyond every documented planning artifact's anticipation. Reserved for a future separate architectural decision and migration if implementation evidence later justifies it (see §Future Considerations).

3. **Top-level parent-partition unique index `(request_id, occurred_at)`.** *Rejected.* PostgreSQL allows this because the unique key includes the partition-key column. But the semantic — "unique across rows sharing the exact same `request_id` and `occurred_at`" — is functionally useless: vendors don't emit two records at the same microsecond, so the constraint never fires. Too weak to be worth shipping.

4. **`CREATE UNIQUE INDEX ON ONLY usage_records (request_id) WHERE …` with per-child `ATTACH PARTITION`.** *Rejected.* PostgreSQL's partition-key rule for unique indexes applies regardless of `ON ONLY` — the parent declaration is rejected because `request_id` does not include `occurred_at`. The `ATTACH PARTITION` workflow for partitioned indexes presupposes a successful parent declaration. Not a viable path for `(request_id)` uniqueness.

5. **Documentation-only architectural-clarification ADR (W1.4 ships no migration).** *Rejected.* Every wave-era document anticipates W1.4 as an implementation wave with a `0007_usage_records_request_id_unique` migration. A documentation-only ADR would contradict that anticipated shape. The architectural-review process produced this ADR; the implementation it describes is the wave's deliverable.

6. **Reaffirm Phase 2D's "rely on `idempotency_keys`" default; ship no migration.** *Rejected.* `idempotency_keys` (§31) covers client→API replay keyed on `(tenant_id, Idempotency-Key, resource_type)`; it does not substitute for vendor-side replay protection keyed on `request_id`. The two protect different surfaces. Reaffirming would also contradict the wave-era documents' consistent anticipation of an implementation wave.

7. **Bundle additional invariants (e.g. NOT NULL on `request_id` once CR-12 exists; CHECK on `request_id` format).** *Rejected.* Per the W1.1–W1.3 precedent and the `adrs-and-architecture.mdc` "one decision per ADR" rule, W1.4 ships the §37 Q6 invariant verbatim — single-column `(request_id)` partial-unique — and nothing else. Successor ADRs can extend if needed.

---

## Migration Plan

Operational steps follow `docs/engineering/RUNBOOK_WAVE.md` §2–§4 verbatim; this ADR enumerates only the W1.4-specific deliverables.

**In-scope files** (single PR, single commit until the pre-merge status-flip commit):

1. `backend/alembic/versions/0007_usage_records_request_id_unique.py` — hand-written single revision. `revision = "0007_usage_records_request_id_unique"`, `down_revision = "0006_widen_alembic_version_num"`. Upgrade body iterates `pg_inherits` children of `usage_records` and creates one partial-unique index per child (idempotent via `IF NOT EXISTS`). Downgrade mirrors with `DROP INDEX IF EXISTS`. Hand-written rather than `alembic revision --autogenerate` because autogenerate cannot express per-child partition-level DDL and would not preserve the `WHERE request_id IS NOT NULL` partial predicate.
2. `backend/app/infrastructure/db/models/usage.py` — single inline comment near the existing `Index("ix_usage_records_request_id", "request_id")` declaration documenting that the per-partition unique index has no parent ORM counterpart by PostgreSQL design (see §Implementation Notes — ORM declaration intentionally absent). No `CheckConstraint` or `Index` declaration is added.
3. `backend/scripts/validate_schema.py` — extension only. Add `check_usage_records_per_partition_unique_indexes` function (~30 LoC) and wire it into `run_all_checks`. See §Implementation Notes — Validator extension.
4. `docs/decisions/ADR-0033-usage-records-request-id-unique.md` — this ADR (new file).
5. `DECISIONS.md` — append one-line cross-link entry pointing at the file, sorted by ADR number after the ADR-0032 entry.
6. `docs/database/schema.md` — §18 reconciliation note: append the architectural-review wording (verbatim from the W1.4 architectural review's locked-in phrasing). §37 Q6 row: mark **Resolved (Phase 3 W1.4, 2026-06-30)** with full constraint details. §37 Wave 1 epilogue bullet for §18 q6: mark **✅ Done — Phase 3 W1.4 (ADR-0033, migration `0007_usage_records_request_id_unique`)**. The §18 schema box (lines 638–668), the §18 indexes line (line 673), and the §31 use-case table (line 1175) are **not** modified — neither the column shape nor the broader CR-12 app-layer key is changed by this wave.
7. `docs/database/INDEX_STRATEGY.md` line 147 — status `Deferred (Phase 3)` → `Implemented (Phase 3 W1.4)`; rationale expanded to document the per-child mechanic, the PostgreSQL partition-key rule that requires per-child declaration, and the conservative wording on the relationship to broader architectural documents.
8. `ROADMAP.md` line 173 (Phase 3 wave table W1 cell) — annotate W1.4 as **✅ Complete (ADR-0033, migration `0007_usage_records_request_id_unique`, 2026-06-30)**.
9. `CHANGELOG.md` `[Unreleased]` — new sub-section "Phase 3 Wave 1.4 — `usage_records` per-partition `(request_id)` uniqueness (ADR-0033)" with **Added** / **Changed** / **Validated** / **Not modified** entries mirroring the W1.1/W1.2/W1.3 entries.

**Explicitly NOT in scope for this PR:**
- `provider_id` column on `usage_records`; FK to `providers`; ERD Cluster 7 update; ARCH §8k.1 or `schema.md` §31 amendments; `API_CONTRACT.md` amendments. (Broader architectural pattern, not W1.4 scope.)
- Rolling-window partition helper. (Forward contract documented; helper itself is separate scope.)
- Modifications to `idempotency_keys` (W1.2 territory) or `distributed_locks` (W1.3 territory).
- Application code changes (`backend/app/application/`, `backend/app/api/`). CR-12 is not built; W1.4 does not anticipate its design.

**Live validation against Supabase** (run from `backend/`, credentials via `.env.validation`):

- **Pre-upgrade safety check.** `SELECT request_id, count(*) FROM usage_records WHERE request_id IS NOT NULL GROUP BY request_id HAVING count(*) > 1` must return zero rows. The table is empty in every current environment; this is expected to be trivially zero, run for audit-trail completeness and to prove the production-rollback variant (`ADD CONSTRAINT … NOT VALID` + later `VALIDATE CONSTRAINT`) is not required.
- `alembic upgrade head` → applies `0007`; `pg_indexes` shows 27 new unique partial indexes (one per child) named per the `uq_<child>_request_id` pattern with `indexdef` containing the expected `WHERE (request_id IS NOT NULL)` predicate.
- `alembic downgrade -1` → `pg_indexes` shows the 27 indexes removed; the parent's non-unique `ix_usage_records_request_id` is unaffected.
- `alembic upgrade head` (re-apply) → idempotency proven (`IF NOT EXISTS` guards).
- `python scripts/validate_schema.py` → must report **all checks PASS**, with the new `check_usage_records_per_partition_unique_indexes` reporting `27/27 partitions carry uq_<child>_request_id` (26 monthly + DEFAULT).
- `python scripts/regenerate_erd.py` + `python scripts/compare_erd.py` → must remain 0 drift (ERD tracks entities + FKs; per-child unique indexes are invisible to it).

**Full CI gate locally.** `python scripts/ci_gate.py` returns 10/10 (`.env.validation` present, stages 5–9 run live). Push, open PR → GitHub Actions runs the identical gate against the pgvector service container.

**Status transition on merge.** ADR header and `DECISIONS.md` cross-link both initially `Proposed`. The final pre-merge commit on the branch flips both to `Accepted` and adds the validation summary to the ADR Status line in a single atomic commit titled `docs(adr): mark ADR-0033 Accepted`.

**Tag on merge.** Annotated tag `v0.3.4-phase3-w1.4` pushed pointing at the merge commit. Mirrors `v0.3.0-phase3-w1.1`, `v0.3.1-phase3-w1.2`, `v0.3.2-phase3-w1.3`. Wave 1 of Phase 3 closes with this tag.

---

## Rollback

- **Pre-merge (in-development).** `alembic downgrade -1` from `0007_usage_records_request_id_unique` to `0006_widen_alembic_version_num` drops the 27 indexes cleanly. No data to preserve (table is empty in every current environment). Alternatively, `git restore .` + branch delete discards all uncommitted changes.
- **Post-merge (in-development).** `git revert <merge-sha>` plus a new migration `0008_revert_usage_records_request_id_unique.py` whose upgrade body drops the indexes (mirror loop with `DROP INDEX IF EXISTS`) and whose downgrade re-adds them. Project convention everywhere else is *never* to edit a merged migration in place.
- **Production rollback after data is present.** Same as post-merge above. Per-partition `DROP INDEX` is fast and non-blocking on PostgreSQL; no `CONCURRENTLY` variant required for the drop. The only consequence is that CR-12 (whenever built) must enforce the invariant at the application layer alone until a successor migration restores DB-level enforcement.

There is no "rollback caught by stuck data" scenario for this constraint at present (the table is empty). Once CR-12 ships and data accumulates, dropping the constraint remains safe; *re-adding* it after data accumulates would require the `ADD CONSTRAINT … NOT VALID` + later `VALIDATE CONSTRAINT` two-step under a separate follow-up migration (out of scope here).

---

## Consequences

### Positive

- **Defense-in-depth duplicate detection on the vendor-replay surface.** Any future writer (CR-12 when it ships, future feature branches, maintenance scripts, replica failovers with stale code) is held to the per-partition `request_id` uniqueness invariant. Surfaces as `psycopg.errors.IntegrityError` with a self-describing constraint name (`uq_<child>_request_id`) at the violating call site rather than as silent duplicate billing rows downstream.
- **Resolves `schema.md` §37 Q6 verbatim.** No bundling, no scope expansion, no architectural drift.
- **Closes Wave 1.** W1.1, W1.2, W1.3 promoted invariants on `export_jobs`, `idempotency_keys`, and `distributed_locks`. W1.4 completes the wave. Wave 2 (data model evolution) can begin.
- **First migration-coupled ADR to reference `RUNBOOK_WAVE.md`** in place of inlining operational steps, per the convention established in CONTRIBUTING.md §6 during `v0.3.3-infra`. Validates the runbook against a real Wave (success metric set in `CHANGELOG.md` line 13–14: *"W1.4 must require fewer manual steps than W1.3"*).

### Implementation Notes

#### Per-child mechanic — PostgreSQL behaving correctly

The per-child unique-index pattern is PostgreSQL's standard and correct mechanism for partitioned tables whose uniqueness predicate omits the partition key. PostgreSQL's restriction — *"unique indexes on a partitioned parent must include the partition-key columns"* — is a deliberate semantic, not a limitation we are working around. The restriction exists because PostgreSQL can only enforce uniqueness within each partition individually for arbitrary predicates; requiring the partition key in any parent-level unique constraint is how PostgreSQL ensures uniqueness is enforceable at the storage layer it owns. W1.4 implements the constraint in the form PostgreSQL is designed to accept — one unique index per child partition.

#### ORM declaration intentionally absent

W1.1, W1.2, and W1.3 each declared their new constraint in `__table_args__` so the ORM mirrors the migration exactly. W1.4 cannot follow this pattern: SQLAlchemy's `Index(..., unique=True, postgresql_where=...)` on `UsageRecord.__table_args__` would translate to a parent-level `CREATE UNIQUE INDEX ON usage_records (request_id) WHERE …` which PostgreSQL rejects (partition-key rule). SQLAlchemy has no facility for declaring per-child indexes on a partitioned parent's ORM model — the children themselves are created by raw SQL in the baseline migration's `DO $$` block and are not ORM-modelled. The W1.4 inline comment in `backend/app/infrastructure/db/models/usage.py` documents this deliberate absence at the call site.

#### Validator extension — tooling-level CI visibility

`backend/scripts/validate_schema.py::load_snapshot` (lines 277–296) deliberately excludes partition children from its bulk index query (`NOT EXISTS (SELECT 1 FROM pg_inherits ...)`) to avoid hundreds of round-trips against Supabase. For the 99% case (indexes declared at parent level and propagated by Postgres native inheritance), parent-only visibility is sufficient. The W1.4 per-child unique indexes are the 1% case: they exist only on children by PostgreSQL design, and would be invisible to the unmodified validator. `check_usage_records_per_partition_unique_indexes` is added to `validate_schema.py` as a targeted tooling addition that scans `pg_inherits` for `usage_records` children and asserts each carries `uq_<child>_request_id` with `indisunique=true`. **This is not a database-level requirement, a workaround for a PostgreSQL limitation, or a substitute for ORM declaration; it is a CI-visibility addition that surfaces the per-child indexes the bulk-snapshot path elides for performance reasons.**

#### Future-partition contract

PostgreSQL native inheritance auto-propagates *non-unique* parent-level indexes to new child partitions (this is how `ix_usage_records_request_id` and the three composite parent indexes already work). It does **not** auto-propagate per-child unique indexes — those must be added explicitly per child. W1.4 establishes a forward contract: whatever creates new `usage_records` child partitions (currently only the baseline migration's `_create_initial_partitions`; in the future, the rolling-window partition helper referenced at `schema.md` §18 line 671 but not yet implemented) must add the matching `uq_<child>_request_id` index when the partition is created. The new `check_usage_records_per_partition_unique_indexes` validator check enforces this at CI time — any new partition missing the index fails the check.

### Operational

- One additional partial-unique index per partition to maintain on `INSERT`s into `usage_records`. Negligible cost — partial-unique index lookup on a single text column is sub-millisecond, dwarfed by the disk I/O of the write itself. The `WHERE request_id IS NOT NULL` predicate means NULL-valued inserts skip the index entirely.
- Cross-provider request_id namespace coincidence (vendor A and vendor B independently using the same `request_id` string within the same month) would manifest as a spurious `IntegrityError`. Risk is theoretical — major vendors namespace their request_ids (`req_*` for OpenAI, `msg_*` for Anthropic, `cs_*` for Stripe) — and is acknowledged here rather than mitigated. If evidence of a real collision emerges, mitigation is a successor ADR (see §Future Considerations).
- Schema validator / ERD: validator gains one new check (now ~9 → 9 with the per-partition unique check; the count nomenclature in `validate_schema.py` is preserved by adding the new check as a peer of the existing 9 rather than altering them). ERD comparator unchanged (per-child unique indexes are invisible to ERD by design).

### Scope discipline (per Wave 1 isolation constraint)

No changes to `export_jobs` (W1.1 territory), `idempotency_keys` (W1.2), or `distributed_locks` (W1.3). No changes to baseline migration `0001_baseline.py` (migrations are never amended in place; the new indexes are added entirely by migration `0007` and dropped on its downgrade). No changes to application code under `backend/app/application/`, `backend/app/api/`, `backend/app/infrastructure/ai/`. No changes to `docs/database/ERD.md`, `CONTRIBUTING.md` (file-per-ADR convention established by W1.1; ADR-0033 is the fourth adopter), or `pyproject.toml`.

---

## Future Considerations

If CR-12 (Usage Recorder middleware) implementation later demonstrates that provider-scoped enforcement is required at the database level, that change will be handled by a separate architectural decision and migration — not by retroactively reinterpreting Wave 1.4. The broader `(provider, request_id)` idempotency pattern documented in `schema.md` §18 (Step-A draft), §31 line 1175 (CR-12 app-layer key), `API_CONTRACT.md` line 233 (webhook handlers), and `ARCHITECTURE.md` §8k.1 (UsageRecord domain spec) remains in place at the application and architectural layers. W1.4 neither implements nor forecloses any future database-level promotion of that pattern.

---

## Acceptance Criteria

The PR is mergeable when **all** of the following hold:

1. Branch `phase3/wave1.4-usage-records-request-id-unique` exists locally and on `origin`, cut from `main` at `a426e5d` (the `v0.3.3-infra` merge commit).
2. `docs/decisions/ADR-0033-usage-records-request-id-unique.md` exists with this content and (post-status-flip) `Status: Accepted (Phase 3 W1.4, 2026-06-30)` with the live validation summary populated in the Status line.
3. `backend/alembic/versions/0007_usage_records_request_id_unique.py` exists with `revision = "0007_usage_records_request_id_unique"`, `down_revision = "0006_widen_alembic_version_num"`, both `upgrade()` and `downgrade()` bodies, and the index-name pattern `uq_<child>_request_id` with predicate `(request_id) WHERE request_id IS NOT NULL`.
4. `backend/app/infrastructure/db/models/usage.py` carries the inline documentation comment near the existing `Index("ix_usage_records_request_id", "request_id")` line explaining the deliberate absence of an ORM declaration for the per-child unique indexes and pointing at this ADR + the validator extension.
5. `backend/scripts/validate_schema.py` defines `check_usage_records_per_partition_unique_indexes` and wires it into `run_all_checks`. The new check passes against the live target after `alembic upgrade head` with `27/27 partitions carry uq_<child>_request_id` (26 monthly + DEFAULT).
6. `docs/database/schema.md` §18 reconciliation note has been amended to add the architectural-review conservative wording. The §18 schema box, indexes line, and §31 use-case table are unchanged.
7. `docs/database/schema.md` §37 Q6 row is marked **Resolved (Phase 3 W1.4, 2026-06-30)** with full constraint details (index-name pattern, predicate, migration ID, ADR ID, conservative wording on the relationship to broader architectural documents), mirroring Q8 (W1.1), Q9 (W1.2), and Q10 (W1.3).
8. `docs/database/schema.md` §37 epilogue Wave 1 bullet for §18 q6 reads `✅ Done — Phase 3 W1.4 (ADR-0033, migration 0007_usage_records_request_id_unique, 2026-06-30)`.
9. `docs/database/INDEX_STRATEGY.md` line 147 status flipped to `Implemented (Phase 3 W1.4)` with rationale expanded per §Implementation Notes; index name pattern `uq_usage_records_<part>_request_id` recorded.
10. `ROADMAP.md` line 173 Wave 1 table annotates W1.4 as `✅ Complete (ADR-0033, migration 0007_usage_records_request_id_unique, 2026-06-30)`.
11. `CHANGELOG.md` `[Unreleased]` section has a Phase 3 W1.4 sub-section mirroring the W1.1/W1.2/W1.3/v0.3.3-infra entries (Added / Changed / Validated / Not modified / Scope discipline) and using the architectural-review conservative wording when describing the relationship to broader architectural documents.
12. `DECISIONS.md` has a one-line cross-link entry for ADR-0033 immediately after the ADR-0032 entry, sorted by ADR number, with the same Status convention.
13. **Pre-upgrade safety SELECT** against live Supabase returns zero rows (no duplicate non-NULL `request_id` within any month).
14. `alembic upgrade head` applies migration `0007`; `pg_indexes` shows exactly 27 new partial-unique indexes (one per child) with `indexdef` containing the expected partial predicate.
15. `alembic downgrade -1` reverts cleanly; `pg_indexes` shows the 27 indexes removed; `ix_usage_records_request_id` (the parent's non-unique propagating index) is unchanged.
16. `alembic upgrade head` re-applies cleanly (idempotency); `pg_indexes` shows the 27 indexes present again.
17. `python scripts/validate_schema.py` reports all checks PASS, including the new `check_usage_records_per_partition_unique_indexes` with `27/27`.
18. `python scripts/ci_gate.py` reports 10/10 PASSED locally against Supabase.
19. GitHub Actions on the PR reports 10/10 green.
20. After merge, annotated tag `v0.3.4-phase3-w1.4` exists locally and on `origin`, pointing at the merge commit; local + remote `phase3/wave1.4-usage-records-request-id-unique` branches are deleted; `git fetch --prune` is clean.
21. `git diff main...HEAD` touches **only** files enumerated in §Migration Plan (no edits to `export_jobs`, `idempotency_keys`, `distributed_locks`, baseline migration, application code, or any other out-of-scope file).
22. Status-flip commit (`docs(adr): mark ADR-0033 Accepted`) lands as the final commit on the branch before merge, flipping the ADR header (with validation summary added) and the `DECISIONS.md` cross-link line from `Proposed` to `Accepted` atomically.
