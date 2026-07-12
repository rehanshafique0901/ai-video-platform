# ROADMAP

> Governed by [`rule.md`](./rule.md). All phases must be executed in order. Each phase has an explicit **Exit Criteria**; the next phase cannot begin until the prior one is approved by the user.

---

## Status Snapshot

| Phase | Title | Status |
|---|---|---|
| 1 | Architecture & Folder Structure | **APPROVED (Rev 3, 2026-06-28)** |
| 2 | Database — Step A: Design Docs | **APPROVED (Rev 2, 2026-06-28)** |
| 2 | Database — Step B: Models & Migration | **✅ APPROVED 2026-06-28** — live validation green (9/9 checks, ERD clean); ADR-0027 added |
| 2C | CI Quality Gate | **🟦 IMPLEMENTATION COMPLETE — awaiting reviewer sign-off** — 10/10 stages wired; gate green end-to-end (ADR-0028) |
| 3 | Authentication | **🟦 IN PROGRESS** — α1 (arch bootstrap) ✅ 2026-06-30 (v0.4.0), α2a (auth: register + login) ✅ 2026-07-01 (v0.4.1), α2b (refresh + logout) ✅ 2026-07-01 (v0.4.2), α3 (`GET /users/me` read + `CurrentUserDep`) ✅ (v0.4.3), α4 (`PATCH /users/me` mutation) ✅ 2026-07-10 (v0.4.4), α5a (Projects create + read) ✅ 2026-07-11 (v0.4.5), α5b (Projects update + soft-delete) ✅ 2026-07-11 (v0.4.6), α5c (Scenes CRUD + reorder) ✅ 2026-07-11 (v0.4.7), α5d.1 (Project Versions capture + read) ✅ 2026-07-12 (v0.4.8), α5d.2 (Project Version restore + diff; Aggregate OCC Rule) ✅ 2026-07-12 (v0.4.9), α5d.3 (Project Version branch — fork to a new project) ✅ 2026-07-12 (v0.4.10) |
| 4 | Backend APIs | not started |
| 5 | Frontend | not started |
| 6 | AI Pipeline | not started |
| 7 | Timeline Editor | not started |
| 8 | Rendering Engine | not started |
| 9 | Deployment | not started |
| 10 | Testing | not started |

---

## Phase 1 — Architecture & Folder Structure

**Deliverables:** `ARCHITECTURE.md`, `ROADMAP.md`, `DECISIONS.md`, `CHANGELOG.md`, `CONTRIBUTING.md`, `API_CONTRACT.md`.

**Exit criteria:**
- All 13 change requests addressed (CR-1 … CR-13).
- Every bounded context has a named aggregate root.
- The 17 ADRs are listed in `DECISIONS.md`.
- The approval gate in `ARCHITECTURE.md` §15 receives an explicit "approved".

---

## Phase 2 — Database

Phase 2 is split into two steps per the **Database First** rule.

### Step A — Design Documents (review-first, no code)

Produce and obtain approval before writing any ORM or migration code:

- `docs/database/NAMING_CONVENTIONS.md` — table / column / index / constraint naming rules.
- `docs/database/ERD.md` — Mermaid Entity Relationship Diagram covering every aggregate root in `ARCHITECTURE.md` §6.
- `docs/database/schema.md` — table-by-table schema with columns, types, nullability, defaults, FKs with explicit `ON DELETE` policy, uniqueness constraints, check constraints.
- `docs/database/INDEX_STRATEGY.md` — every index justified by a query pattern from `API_CONTRACT.md`; composite, partial, GIN, BRIN, and pgvector indexes called out.
- `docs/database/RETENTION_POLICY.md` — what we keep, for how long, and how it is purged.
- `docs/database/BACKUP_RESTORE.md` — backup cadence, restore drills, PITR target.

**Step A exit criteria:**
- ERD covers every aggregate; reviewer confirms normalisation (no derived data outside ledger / cached `balance_after` on each ledger row).
- Every FK has a documented `ON DELETE` behaviour.
- Every aggregate root has a uniqueness constraint.
- Immutable ledger pattern used for `credit_ledger`, `ai_model_pricing`, `project_versions`, `usage_records`, `event_outbox`, `event_log`.
- Partitioning strategy documented for `usage_records`, `analytics_events`, `event_log`, and (optional) `logs`.
- pgvector usage limited to `library_assets.embedding` and `agent_memory.embedding`.

### Step B — SQLAlchemy Models & Alembic Baseline

Begins only after Step A is approved. Execution proceeds in this strict order; pause at each numbered checkpoint before continuing.

1. **Generate SQLAlchemy models only** — `app/infrastructure/db/models/` mirroring the approved schema, no relationships yet beyond what's strictly necessary for FK validity. ✅ done
2. **Review all relationships** — add and validate `relationship()` declarations, back-populates, cascade options, and load strategies. Cross-reference each against `ERD.md` cluster diagrams. ✅ documented in `SCHEMA_VALIDATION.md` §3
3. **Generate the Alembic baseline migration** — `alembic/versions/0001_baseline.py` creating every table, index, ENUM, partition (with the `partition.create_lead_days` system setting honoured for the first batch), and extension (`pgcrypto`, `citext`, `pg_trgm`, `vector`, `btree_gin`). ✅ done
4. **Run `alembic upgrade head`** on an empty database. Pending live run.
5. **Run `alembic downgrade base`** — verifies the migration is fully reversible; no leftover ENUMs, partitions, or extensions (extensions are intentionally retained per ADR — see `SCHEMA_VALIDATION.md` §4). Pending live run.
6. **Run `alembic upgrade head` again** — verifies migrations are idempotent and that a fresh upgrade after a clean downgrade still produces an identical schema. *Added to the order per user instruction.* Pending live run.
7. **Generate seed data** in `alembic/versions/0002_seed_system_data.py` for plans, feature flags, provider plugin registrations, built-in AI models, default roles, and system settings. All inserts use `ON CONFLICT DO NOTHING` for idempotency. ✅ done
8. **Run automated schema validation** — `scripts/validate_schema.py` introspects the implemented schema and runs nine checks: extensions, table parity vs ORM metadata, partition coverage, foreign keys (with `ON DELETE` action match), unique constraints / unique indexes, every documented index (including imperative GIN/HNSW), immutability triggers on append-only tables, pgvector column scope, and the `credit_ledger` balance trigger. *Added to the order per user instruction.* Pending live run.
9. **Regenerate the ERD from the implemented schema** — `scripts/regenerate_erd.py` emits a deterministic Mermaid diagram. Diff against `docs/database/ERD.md`; positional / ordering diffs are acceptable, structural diffs require an entry in `SCHEMA_VALIDATION.md` §7. Pending live run.
10. **Produce `SCHEMA_VALIDATION.md`** summarising methodology, checks performed, live results, and any acknowledged deviations. ✅ done — §6 populated from a live run against Supabase Postgres 17.6 + pgvector 0.8.0 on 2026-06-28.
11. **Pause for approval** before creating repositories or services. ⬅ waiting here.

**Step B exit criteria:**
- `alembic upgrade head` and `alembic downgrade base` both succeed on a pgvector-enabled Postgres. ✅ done (Supabase 17.6 + pgvector 0.8.0).
- A second `alembic upgrade head` after `downgrade base` succeeds without manual intervention (idempotency). ✅ done.
- `scripts/validate_schema.py` reports `all_passed: true` for all nine checks. ✅ done — 9/9.
- Generated ERD has zero structural diff against `docs/database/ERD.md`. ✅ done — 51/51 entities and 58/58 design edges; one billing-cluster drift was reconciled via ADR-0027 and the corresponding ERD/schema corrections.
- Seed data inserts cleanly; running the seed migration twice is a no-op. ✅ done (`ON CONFLICT DO NOTHING`).
- `import-linter` confirms no model imports `app/api/*` or `app/application/*` (one-way dependency). ⬜ deferred to Phase 9 CI gate (import-linter not yet wired; no `app/api` or `app/application` exists yet so this is currently vacuous).
- No repositories, no services, no routers, no business logic — only models, migrations, seed, and validation scripts. ✅ confirmed.

---

## Phase 2C — CI Quality Gate (added by reviewer at close of Phase 2)

**Status:** ✅ Gate green; ratified as ADR-0028 (intent) + ADR-0029 (operational contract).

Every pull request and every push to `main` runs the ten-stage gate, fail-fast:

1. `ruff check` — lint
2. `black --check` — format
3. `mypy --strict` + `lint-imports` — static analysis (types + architecture)
4. `pytest -m unit --cov=app` — unit tests + coverage collection
5. `alembic upgrade head` — forward migration
6. `alembic downgrade base` — reverse migration
7. `alembic upgrade head` (idempotency) — re-apply
8. `scripts/validate_schema.py` — 9 live structural checks
9. `scripts/regenerate_erd.py` → `scripts/compare_erd.py` — ERD round-trip + diff
10. `coverage report` — threshold enforcement (`fail_under = 60` until Phase 3)

Implementation: `.github/workflows/ci.yml` runs `backend/scripts/ci_gate.py`; the same script runs locally via `backend/scripts/run_ci_gate.ps1` or `python scripts/ci_gate.py`. Architectural fitness contracts (import-linter) enforce the layered architecture even before Phase 3 code lands. See `CI_QUALITY_GATE.md` for the full spec and ADR-0028 for the rationale.

**Exit criteria:** Reviewer sign-off on `CI_QUALITY_GATE.md` + ADR-0028. No repository, service, router, or other Phase 3 code is written until this approval is recorded.

---

## Phase 2D — Documentation Reconciliation (no code changes)

**Status:** ✅ Completed 2026-06-29.

Per the architectural audit reviewer feedback ("documentation should catch up to the implementation, not the other way around"), Phase 2D reconciles the design documents with the validated implementation under the rule:

> *Implementation is the source of truth if it has passed migrations, validation, and CI, unless there is explicit evidence that the implementation violates an accepted ADR or functional requirement.*

Scope (documentation only):
- `docs/database/schema.md` — §16/17/18/19/20/22/25/26/27/31/32/33 reconciled; new §37 catalogues 13 Phase-3 entry decisions.
- `docs/database/ERD.md` — Clusters 6/7/8/9/10 column shapes reconciled.
- `docs/database/INDEX_STRATEGY.md` — full rewrite; each row labelled `Implemented` or `Deferred (Phase N)`.
- `docs/database/BACKUP_RESTORE.md` — `_backup_sentinel` shape reconciled.
- `DECISIONS.md` — duplicate ADR-0028 renumbered to ADR-0029.

Out of scope (intentionally untouched per reviewer rule):
- ORM models, Alembic migrations, database schema, seed data, CI gate.

**Exit criteria:** Reviewer confirmation that schema.md/ERD.md match the ORM; INDEX_STRATEGY internally consistent with the implemented schema; no duplicate ADR numbers; CI gate still passes with no code changes. ✅ Met 2026-06-29 (`PHASE2D_SPOT_CHECK.md` 8/8 MATCH; CI gate 10/10 green).

---

## Phase 3 — Repositories, Services, Auth Foundation

**Pre-flight (completed 2026-06-29):**

1. **Initialise version control + tag the baseline.** ✅ Done.
   The repository is rooted at `ai creation/` (the parent
   `programming bench/` workspace root contains unrelated
   ProgramBench evaluation artefacts and was deliberately excluded).
   A project-root `.gitignore` was authored before the first commit
   to keep secrets (`.env.validation`), caches (`.pytest_cache/`,
   `.mypy_cache/`, `.ruff_cache/`, `.import_linter_cache/`), and
   validation outputs (`.validation/`) out of the index.

   - **Branch:** `main`
   - **Initial commit:** `412796f` — *Phase 2D: Documentation
     reconciliation complete (no code changes)*
   - **Baseline tag:** `v0.2.2-phase2d-docs-reconciled` (annotated)
   - **Tracked files:** 80
   - **Working tree:** clean

   This commit is the rollback / reference point for everything that
   follows. Any Phase 3 work that proves untenable can be undone with
   `git reset --hard v0.2.2-phase2d-docs-reconciled`.

   **Reproduction command sequence (for future reference):**
   ```powershell
   cd "C:\Users\rehan\OneDrive\Desktop\programming bench\ai creation"
   git init
   git checkout -b main
   git add .
   git commit -m "Phase 2D: Documentation reconciliation complete (no code changes)"
   git tag -a v0.2.2-phase2d-docs-reconciled -m "Phase 2D baseline: documentation reconciled with validated implementation; CI gate 10/10; spot-check 8/8."
   ```

2. **Sequence the 13 deferred decisions** catalogued in
   `schema.md` §37 into the four waves below rather than tackling
   them in parallel:

| Wave | Theme | Items |
|---|---|---|
| **W1** | Schema integrity (correctness) | **W1.1 ✅ Complete (ADR-0030, migration `0003_export_jobs_partial_unique`, 2026-06-29)** — `export_jobs (render_job_id, format, quality, orientation)` partial-unique DB constraint promotion. **W1.2 ✅ Complete (ADR-0031, migration `0004_idempotency_keys_invariants`, 2026-06-29)** — `idempotency_keys` `updated_at` column + auto-bump trigger + `chk_idempotency_keys_response_hash_matches_status` CHECK. **W1.3 ✅ Complete (ADR-0032, migration `0005_distributed_locks_lease`, 2026-06-29)** — `distributed_locks` `chk_distributed_locks_lease_until_after_acquired_at` CHECK enforcing `lease_until > acquired_at`. **W1.4 ✅ Complete (ADR-0033, migration `0007_usage_records_request_id_unique`, 2026-06-30)** — `usage_records` per-partition partial-unique `(request_id) WHERE request_id IS NOT NULL` indexes named `uq_<child>_request_id` (per-child by PostgreSQL design); first migration-coupled ADR to reference `docs/engineering/RUNBOOK_WAVE.md` in place of inlining operational steps. **Wave 1 closes with this tag (`v0.3.4-phase3-w1.4`).** |
| **W2** | Data model evolution | workflow_runs correlation/paused/version/queue, audit_log.reason, provider_settings.kind, render_jobs.progress type |
| **W3** | Performance (read-side) | Deferred indexes, relationship() adoption, ERD cross-cluster edge policy |
| **W4** | Business / product policy | auth_role retention, cost_reconciliations immutability |

> **Engineering checkpoint — `v0.3.3-infra` (2026-06-30).** Non-feature release between W1.3 and W1.4 bundling three workflow fixes discovered while shipping W1.1–W1.3: (1) `backend/scripts/ci_gate.py` autoloads `DATABASE_URL` from `backend/.env.validation` (no manual PowerShell workaround needed for local CI runs), (2) `alembic_version.version_num` widened to `VARCHAR(255)` via migration `0006_widen_alembic_version_num` (no more migration-ID length gymnastics), (3) `.cursor/` fully gitignored (no more accidental staging during commit amends). Adds `docs/engineering/RUNBOOK_WAVE.md` as the canonical Phase 3 Wave procedure; ADR-0033 (Wave 1.4) is the first ADR to reference the runbook in place of inlining operational steps (merged as part of `v0.3.4-phase3-w1.4`, 2026-06-30).

Each wave produces its own ADR(s); a wave does not start until the previous wave merges. The CI gate must stay green at the close of every wave's final PR.

### Phase 3 deliverables (unchanged)

Per reviewer guidance at the close of Phase 2:

- **Repositories** (`app/infrastructure/repositories/*`) — persistence only (CRUD + queries). **No business rules.**
- **Application services** (`app/application/*`) — use-case orchestration + transactions; compose repositories + domain entities.
- **Domain entities** (`app/domain/*`) — own business invariants and validation; framework-free.
- **External providers** (OpenAI, Veo, Runway, ElevenLabs, …) — stay behind the plugin interfaces defined in Phase 1 (`BasePlugin` + capability ABCs).

Concrete deliverables:

- Argon2id password hashing.
- Access JWT (15 min) + refresh JWT (30 days) with rotation + reuse detection.
- Email verification + password reset flows.
- Google OAuth (Authorization Code + PKCE).
- RBAC roles (`user`, `pro`, `business`, `enterprise`, `admin`).
- Session table for refresh tokens.
- Coverage threshold raised to 80 % once repositories carry unit tests.

**Exit criteria:** auth flows pass integration tests; CI gate green for every PR (including all 10 stages 5–9 against the pgvector service container); security review against OWASP ASVS L2.

---

## Phase 4 — Backend APIs

- All routers listed in `ARCHITECTURE.md` §4 → `app/api/v1/routers/`.
- OpenAPI 3.1 spec auto-generated; consistent envelope; per-endpoint pagination & filtering.
- Webhooks signed (HMAC) and idempotent.
- WebSocket endpoints for `progress`, `workflows`, `timeline`.
- Plugin registry exposed read-only at `/plugins`; admin write at `/admin/plugins`.

**Exit criteria:** every endpoint in `API_CONTRACT.md` is implemented; OpenAPI schema diff = zero.

---

## Phase 5 — Frontend

- Next.js 15 app router shell + auth flows.
- Dashboard, projects list, project detail, settings, billing, credits.
- Asset Library page (CR-8).
- Pipelines picker (CR-2) and Workflow status page (CR-7).
- Admin: flags, plugins, models, usage, queues.

**Exit criteria:** all routes render with real backend data; Lighthouse ≥ 90 on key pages.

---

## Phase 6 — AI Pipeline

- All ten agents (`script`, `analysis`, `storyboard`, `prompt`, `image`, `video`, `voice`, `subtitle`, `render`, `seo`).
- All provider plugins for the initial vendor list.
- AI Model Registry populated (CR-11).
- Usage Recorder middleware live for every plugin call (CR-12).
- Pipelines A, B, C implemented and registered (CR-2).
- LangGraph checkpointer persisting to Postgres (CR-7).

**Exit criteria:** Pipelines A/B/C produce a finished render end-to-end on a sample script; usage records present for every call.

---

## Phase 7 — Timeline Editor

- React-based non-linear editor: tracks, clips, transitions, playhead.
- Drag / drop / split / trim / merge / replace / move / lock / zoom / undo / redo.
- Real-time preview synced with the timeline.
- Collaborative-ready data model (CRDT or OT in a later phase).

**Exit criteria:** a 60-clip timeline edits at 60 FPS in Chrome; undo/redo tree exposes versioning hooks for CR-6.

---

## Phase 8 — Rendering Engine

- FFmpeg / MoviePy / OpenCV-based renderer.
- Transitions, subtitle burner, audio mix, color pipeline.
- Export to MP4 / MOV / GIF / WebM at 1080p / 2K / 4K, vertical / horizontal / square.
- Storage Provider plugins (CR-5) wired (Local, S3, R2, Azure Blob, GCS).

**Exit criteria:** 30s reference timeline renders within budget on the default queue.

---

## Phase 9 — Deployment

- Dockerfiles for backend, worker, frontend.
- `docker-compose.yml` (local) and `docker-compose.prod.yml`.
- GitHub Actions CI: lint, type-check, tests, build, push images.
- CD pipeline with environment promotion (local → staging → prod).
- Observability: Prometheus, OpenTelemetry, Sentry, structlog JSON logs.
- Per-environment feature-flag rule data.

**Exit criteria:** green pipeline pushes to staging; smoke tests pass.

---

## Phase 10 — Testing

- Unit (domain + application layers).
- Integration (DB + Redis + providers via mocks).
- API contract tests (OpenAPI schemathesis).
- UI tests (Playwright).
- Performance tests (k6) for hot endpoints + render throughput.
- Coverage target: 80% lines, 100% on domain layer.

**Exit criteria:** all suites green in CI; coverage gate enforced.

---

## Post-Phase Milestones (not numbered phases)

- **M1 — Public Beta.** Restricted invite, limited providers, billing live.
- **M2 — General Availability.** All providers, full pipelines, SLA backed.
- **M3 — Enterprise Mode.** SSO, audit log export, dedicated tenancy, custom contracts.
- **M4 — Plugin Marketplace.** Third-party providers via signed entry_points packages (already enabled by CR-1 architecture).
- **M5 — Realtime Collaboration.** CRDT layer over the timeline (versioning model already supports it via CR-6).
