# Project Status — AI Video Generation SaaS

Living document. Updated at every approval gate. Authoritative answer to
"where are we?" — supersedes anything in ROADMAP/CHANGELOG when in
conflict (and any drift is itself a defect to be reconciled).

- **Version:** `0.2.2-phase2d-docs-reconciled`
- **Last update:** 2026-06-29
- **Current phase:** **Phase 2D — Documentation Reconciliation** ✅ Approved
  2026-06-29 (no code changes). Phase 2 Step B approved 2026-06-28.
  Phase 2C (CI Quality Gate) gate-green and ratified as ADR-0028 +
  ADR-0029. Manual spot-check 8/8 MATCH (`PHASE2D_SPOT_CHECK.md`).
- **Pre-flight before Phase 3 (completed 2026-06-29):**
  - Workspace is now a git repository rooted at
    `ai creation/` (not at the parent `programming bench/` workspace
    root, which contains unrelated ProgramBench evaluation artefacts).
  - **Initial commit:** `412796f` — *Phase 2D: Documentation
    reconciliation complete (no code changes)*.
  - **Baseline tag:** `v0.2.2-phase2d-docs-reconciled` (annotated)
    pointing at `412796f` on `main`.
  - 80 files tracked. `.env.validation`, `.validation/`, caches, and
    virtualenvs all correctly excluded via the project-root
    `.gitignore` (added during pre-flight).
- **Next gate:** Phase 3 **Wave 1.1** — promote the
  `export_jobs (render_job_id, format, quality, orientation)`
  use-case invariant to a partial-unique DB constraint
  (one PR + one ADR + one migration; CI gate must stay green).
  Remaining waves are sequenced in `ROADMAP.md` Phase 3 Pre-flight §2.

## 1. Phase Summary

| Phase | Title                                     | Status     | Notes |
|-------|-------------------------------------------|------------|-------|
| 1     | Architecture, Folders, Tech Decisions     | ✅ Approved | Revision 3 (CR-1 … CR-13) signed off |
| 2A    | Database — Design Documents               | ✅ Approved | Revision 2 (CR-DB-1 … CR-DB-4) signed off |
| 2B    | Database — SQLAlchemy + Alembic Baseline  | ✅ Approved | 2026-06-28; 9/9 structural checks pass on Supabase Postgres 17.6 + pgvector 0.8.0; ERD round-trip clean (51 entities, 58 design edges); ADR-0027 added |
| 2C    | CI Quality Gate (added by reviewer)       | ✅ Gate green, ratified | 10/10 stages wired; local self-test green; ADR-0028 (intent) + ADR-0029 (operational contract); `CI_QUALITY_GATE.md` documents semantics |
| 2D    | Documentation Reconciliation              | ✅ Completed 2026-06-29 | Docs updated to match validated implementation; `schema.md` §37 catalogues 13 Phase-3 entry decisions; no code changes |
| 3     | Repositories & Services                   | ⬜ Not started | Sequence the 13 deferred decisions in `schema.md` §37 first |
| 4     | Public API (FastAPI, OpenAPI)             | ⬜ Not started | |
| 5     | AI Orchestration                          | ⬜ Not started | Domain entities exist; implementation deferred |
| 6     | Rendering Pipelines                       | ⬜ Not started | Plugin contracts defined; first pipeline (FFmpeg) is Phase 6 |
| 7     | Frontend                                  | ⬜ Not started | |
| 8     | Background Workers / Queues               | ⬜ Not started | Queue priorities seeded in DB |
| 9     | DevOps / CI / CD                          | ⬜ Not started | Schema validator is CI-ready |
| 10    | Hardening, Observability, Launch          | ⬜ Not started | |

## 2. Completed Milestones

- Architecture (`ARCHITECTURE.md`, revision 3) including:
  - 7-plane topology
  - AI provider plugin system (CR-1)
  - Multiple rendering pipelines (CR-2)
  - Separated AI orchestration (CR-3)
  - Event Bus + outbox (CR-4)
  - Storage provider plugin system (CR-5)
  - Project versioning (CR-6)
  - Resumable Workflow Engine (CR-7)
  - Asset Library (CR-8)
  - Feature Flags (CR-9)
  - Explicit Domain Entities (CR-10)
  - AI Model Registry (CR-11)
  - AI Cost Tracking (CR-12)
  - Queue Priorities (CR-13)
- Database design (`docs/database/*`):
  - ERD with 10 clusters, FK summary, aggregate-root coverage check
  - Per-table schema definition, ENUM catalogue, partition rules
  - Index strategy, naming conventions
  - Retention, backup & restore policies
  - 25 ADRs in `DECISIONS.md`
- Backend code skeleton (`backend/`):
  - `pyproject.toml`, `alembic.ini`, `alembic/env.py`, `script.py.mako`
  - 23 model files implementing every table in `schema.md`
  - Baseline migration `0001_baseline.py`
  - Seed migration `0002_seed_system_data.py`
  - Schema validator + ERD regenerator + runner
  - `docker-compose.db.yml` for local pgvector Postgres

## 3. Pending Milestones

### 3.1 Phase 2 Step B — ✅ APPROVED 2026-06-28

- [x] Execute `backend/scripts/run_validation.py` against a live
      pgvector Postgres (Supabase, PostgreSQL 17.6, pgvector 0.8.0).
- [x] Replace §6 of `SCHEMA_VALIDATION.md` with the live results
      (9/9 checks pass; raw report embedded in §6.4).
- [x] Diff `backend/.validation/erd_generated.md` against
      `docs/database/ERD.md` (51/51 entities, 58/58 design edges;
      one design drift reconciled via ADR-0027).
- [x] Reviewer sign-off on Step B.

### 3.2 Phase 2C — CI Quality Gate (prerequisite for Phase 3)

- [x] `backend/pyproject.toml` dev-dependency group with pinned ruff,
      black, mypy, pytest, pytest-cov, plus type stubs.
- [x] Tool configs in `pyproject.toml`: `[tool.ruff]`, `[tool.black]`,
      `[tool.mypy]`, `[tool.pytest.ini_options]`, `[tool.coverage.*]`,
      `[tool.importlinter]`.
- [x] Smoke test suite under `backend/tests/` (test_mixins, test_enums,
      test_metadata, test_models_import) — **24 tests pass; 100 %
      branch coverage on `app/` for Phase 2C scope.**
- [x] `backend/scripts/ci_gate.py` — single-command runner executing
      the 10 stages in order; stages 5–9 auto-skip if no `DATABASE_URL`.
- [x] `backend/scripts/run_ci_gate.ps1` — Windows PowerShell wrapper
      with stage-range pass-through.
- [x] `.github/workflows/ci.yml` — full pipeline with a
      `pgvector/pgvector:pg16` service container; artefact upload;
      coverage in PR summary.
- [x] `CI_QUALITY_GATE.md` describing each stage, runtime budget,
      failure runbook, and coverage threshold roadmap.
- [x] ADR-0028 documenting the gate (`DECISIONS.md`).
- [x] Architectural fitness contracts (import-linter) — four contracts
      enforcing the layered architecture; package skeletons created
      (`app/domain`, `app/application`, `app/api`) so contracts are live
      for the first Phase 3 commit.
- [x] Local self-test: stages 1–4 + 8–10 ran end-to-end green
      (`.validation/ci_gate_stages_1_4.log`,
      `.validation/ci_gate_stages_8_10.log`). Stages 5–7 (migration
      cycle) were deliberately not re-exercised in the self-test to
      avoid re-running migrations against the live target — they are
      wired identically to the proven Step B validation path and will
      execute against the pgvector service container in CI.
- [ ] Reviewer sign-off on Phase 2C → unlocks Phase 3.

## 4. Technical Debt

- **Vector dimensions are hard-coded to 1536.** If a future embedding
  provider returns a different dimension, we'll need an ADR + migration
  to add a second column or model registry-driven dimension. Tracked.
- **No formal RBAC permission table yet.** Seed only ships role *codes*;
  permission mapping currently lives in feature flags + service-layer
  policy. Promote to a dedicated `permissions` + `role_permissions`
  table if compliance audits later require auditable role↔permission
  joins. Captured as deferred ADR (see §6).
- **`render_jobs.progress` is `text`.** Stored as a string fraction
  because we treat it as a display value and refresh it through Redis.
  If we ever need range queries on progress, promote it to `numeric(5,2)`
  with a CHECK constraint.
- **Default partitions present.** Convenient for boot-strap, but in
  production we want to alert on rows landing in `_default` partitions
  and create the missing range partition immediately. Phase 8 will add
  a Celery beat task for this.
- **Windows local-dev path uses Docker Desktop.** `eralchemy2` requires
  graphviz, which is painful on Windows; we side-stepped it with our
  own Mermaid generator (`scripts/regenerate_erd.py`).

## 5. Deferred / Open ADRs

| ID         | Topic                                  | Status   | Notes |
|------------|----------------------------------------|----------|-------|
| ADR-0025   | Defer dedicated `user_preferences` table | Accepted | `users.extra` jsonb is sufficient until Phase 7 |
| (deferred) | Dedicated `permissions` + `role_permissions` | Open | Will write ADR if Phase 4 RBAC requirements demand it |
| (deferred) | Per-tenant Postgres schema isolation   | Open    | Currently single-schema multi-tenant via `tenant_id` |
| (deferred) | Read replica routing strategy          | Open    | Will revisit when QPS justifies |

## 6. Known Risks

| Risk | Mitigation |
|------|------------|
| Migration drift between code and `schema.md` | Schema validator runs in CI from Phase 9; pre-commit hook in Phase 3 |
| HNSW index build cost at high cardinality | Library asset embeddings written async; index build monitored via `pg_stat_progress_create_index` |
| Default partition silently absorbing rows | Beat task + alert in Phase 8 |
| credit_ledger balance trigger lock contention | Per-tenant advisory lock will be considered in Phase 3 if profiling shows contention |
| pgvector availability on managed Postgres | Documented in `BACKUP_RESTORE.md`; cloud deployment targets RDS-Postgres-with-pgvector / Supabase / Crunchy Bridge |
| Idempotency-key table growth | Cleanup beat task (Phase 8) using `expires_at` partial index |

## 7. Open Questions

- Final pricing rows for `ai_model_pricing` need real-world values for
  every seeded model. Currently the seed migration intentionally inserts
  models but **no** pricing rows; `usage_records.pricing_id` is
  nullable so this is non-blocking. Phase 3 will add a curator-only
  endpoint or a separate seed script for pricing rows.
- Confirm whether `audit_log.before_json` / `after_json` should be
  cryptographically signed for compliance customers (SOC 2 Type II
  requirement may push this from "operational" to "tamper-evident
  ledger"). Deferred to Phase 10.

## 8. Progress Checklist (Step B detail)

- [x] Generate SQLAlchemy models
- [x] Review relationships against ERD
- [x] Generate Alembic baseline
- [x] `alembic upgrade head` (live, Supabase Postgres 17.6 + pgvector 0.8.0)
- [x] `alembic downgrade base` (live; only `alembic_version` retained)
- [x] `alembic upgrade head` again (idempotency proven, live)
- [x] Generate seed migration
- [x] Apply seed migration on a clean DB (live; 4 plans, 13 ai_models, 10 feature_flags, 6 roles, 9 plugin registrations, 7 system_settings)
- [x] Introspect implemented schema (live; pg_catalog bulk queries — 17 s runtime)
- [x] Regenerate ERD from implemented schema (live; 13 s runtime)
- [x] Integrity check script (`validate_schema.py`) — rewritten against pg_catalog
- [x] Validation methodology document (`SCHEMA_VALIDATION.md`) — §6 populated with live results
- [x] Live run results pasted into `SCHEMA_VALIDATION.md` §6 (all 9 checks ✅)
- [ ] Reviewer sign-off

## 9. How to Read This File

Sections 1–3 capture *where* we are; §4–§7 capture *what we know we owe
ourselves*; §8 is the working checklist for the active step. Update this
file before pushing the next gate request.
