# CHANGELOG

> Keep-a-Changelog style. Each completed phase gets one entry. Pre-release work tracked under **[Unreleased]**.

---

## [Unreleased]

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
