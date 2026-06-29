# CI Quality Gate

> Authoritative spec for the automated checks every pull request and every push to `main` must pass. Implements ADR-0028. The same orchestrator (`backend/scripts/ci_gate.py`) drives both CI and the local pre-push workflow.

---

## 1. What runs, in what order

Ten stages, **fail-fast** — the first failure stops the run.

| # | Stage                                              | Tool                                              | Needs DB? | Typical runtime |
|---|----------------------------------------------------|---------------------------------------------------|-----------|-----------------|
| 1 | Lint                                               | `ruff check app tests scripts`                    | no        | < 1 s           |
| 2 | Format                                             | `black --check app tests scripts`                 | no        | < 2 s           |
| 3 | Static analysis (types + architecture)             | `mypy --strict` + `lint-imports`                  | no        | 2–10 s          |
| 4 | Unit tests with coverage collection                | `pytest -m unit --cov=app`                        | no        | 1–5 s           |
| 5 | Forward migration                                  | `alembic upgrade head`                            | yes       | 30–90 s         |
| 6 | Reverse migration                                  | `alembic downgrade base`                          | yes       | 10–40 s         |
| 7 | Idempotency check                                  | `alembic upgrade head` (second pass)              | yes       | 30–90 s         |
| 8 | Live schema validator (9 structural checks)        | `scripts/validate_schema.py`                      | yes       | 10–20 s         |
| 9 | ERD regenerate + structural diff                   | `scripts/regenerate_erd.py` → `scripts/compare_erd.py` | yes  | 15–25 s         |
| 10| Coverage threshold enforcement                     | `coverage report` (`fail_under` from `pyproject.toml`) | no   | < 1 s           |

End-to-end runtime: **~3 minutes** in CI (service container), **~2.5 minutes** locally against a low-latency Postgres, **~5 minutes** locally against a cross-region pooled Postgres (e.g. Supabase). The earlier validator was 263 s for stage 8 alone; the pg_catalog rewrite brought it to 17 s.

---

## 2. How to run it

### 2.1 In CI

Automatic. The workflow `.github/workflows/ci.yml` runs on:

- `pull_request` (any base branch)
- `push` to `main`
- `workflow_dispatch` (manual trigger from the Actions tab)

The workflow provisions a `pgvector/pgvector:pg16` service container with `aivp` / `aivp` / `aivp` (user / password / db). `DATABASE_URL` is set automatically. Artefacts (`schema_validation_report.json`, `erd_generated.md`, `erd_diff.json`, `coverage.xml`) are uploaded under the `ci-gate-artefacts` name for 14 days. Coverage is also appended to the GitHub Step Summary so reviewers see the delta in the PR's Checks tab.

### 2.2 Locally — full run

```powershell
cd "ai creation/backend"
.\scripts\run_ci_gate.ps1
```

or, cross-platform:

```bash
cd ai\ creation/backend
python scripts/ci_gate.py
```

Live-DB stages 5–9 require either `DATABASE_URL` exported in the shell or `backend/.env.validation` present (git-ignored, format: `DATABASE_URL=postgresql+psycopg://…`).

### 2.3 Locally — fast pre-push slice

```powershell
.\scripts\run_ci_gate.ps1 -Stages "1-4,10"
```

Stages 1–4 + 10 don't touch the DB and complete in under 10 seconds; this is the recommended pre-push smoke check. The full live-DB run happens in CI.

### 2.4 Locally — only the live-DB slice

```powershell
.\scripts\run_ci_gate.ps1 -Stages "5-9"
```

Useful when iterating on a migration or the schema validator.

---

## 3. Implementation files

| Path                                                          | Role                                                                                |
|---------------------------------------------------------------|--------------------------------------------------------------------------------------|
| `.github/workflows/ci.yml`                                    | GitHub Actions wiring (service container, env vars, artefact upload, summary)        |
| `backend/scripts/ci_gate.py`                                  | Single Python entrypoint that runs all 10 stages in order                            |
| `backend/scripts/run_ci_gate.ps1`                             | PowerShell wrapper for Windows developers; thin convenience layer over `ci_gate.py`  |
| `backend/scripts/validate_schema.py`                          | Stage 8 — 9 structural checks against the live DB (pg_catalog bulk queries)          |
| `backend/scripts/regenerate_erd.py`                           | Stage 9a — emits Mermaid ERD from live schema                                        |
| `backend/scripts/compare_erd.py`                              | Stage 9b — structural diff between generated ERD and `docs/database/ERD.md`          |
| `backend/scripts/_load_env.py`                                | Loads `DATABASE_URL` from `backend/.env.validation` (git-ignored)                    |
| `backend/pyproject.toml` — `[tool.ruff]` / `[tool.black]` / `[tool.mypy]` / `[tool.pytest.ini_options]` / `[tool.coverage.*]` / `[tool.importlinter]` | Per-tool configuration; single source of truth |

---

## 4. Architectural fitness contracts

Encoded in `pyproject.toml` and enforced by `lint-imports` (stage 3). A regression here fails the gate before the live-DB stages have a chance to run.

1. **Domain layer has no infrastructure / application / api dependencies.** `app.domain` may not import any of `app.infrastructure`, `app.application`, `app.api`.
2. **DB models cannot import application or API layers.** `app.infrastructure.db` may not import `app.application` or `app.api`.
3. **API layer talks to application services, never directly to infrastructure.** `app.api` may not import `app.infrastructure`.
4. **Application layer never imports the API layer.** `app.application` may not import `app.api` (no reverse dependencies; orchestrators sit *below* transport).

The packages `app/domain`, `app/application`, and `app/api` are created empty at the close of Phase 2 specifically so these contracts are live the moment any Phase 3 code lands.

---

## 5. Adding a stage

When the project grows new failure modes (e.g. SBOM scanning, license check, container image lint), follow this pattern:

1. Add a new `Stage(...)` entry in `_stages()` inside `ci_gate.py` at the appropriate position.
2. If the stage needs the DB, set `requires_db=True`; the runner skips it gracefully when `DATABASE_URL` is unset locally.
3. Update this document's §1 table and §3 file list.
4. Ship the change in the same PR that introduces the failure mode being checked — never separately, otherwise the gate may stay green while a regression lands.

---

## 6. Failure runbook

When the gate fails:

- **Stage 1 (ruff):** `python -m ruff check --fix app tests scripts` resolves most automatable issues.
- **Stage 2 (black):** `python -m black app tests scripts` formats in place.
- **Stage 3 (mypy):** Read the error messages — strict mode catches missing generic parameters, unused `# type: ignore`, and Pydantic field annotation issues.
- **Stage 3 (lint-imports):** A new import crossed an architectural boundary. The error message names the contract that was violated.
- **Stage 4 (pytest):** Either a real test failure (fix the code) or stale expectations (fix the test).
- **Stages 5–7 (alembic):** Migration is broken. Check the most recent revision file; verify both `upgrade()` and `downgrade()` are reversible and idempotent.
- **Stage 8 (schema validator):** Read `.validation/schema_validation_report.json` — the failing check names the missing object.
- **Stage 9 (ERD diff):** Read `.validation/erd_diff.json` — the `edges_only_in_design` list is the real drift; `edges_only_in_generated` is expected (cluster-split design diagrams elide cross-cluster FKs).
- **Stage 10 (coverage):** Below threshold — add tests or document the uncovered branch with `# pragma: no cover`. **Do not lower the threshold without an ADR.**

---

## 7. Versioned thresholds

| Phase     | Coverage threshold | Reason                                                           |
|-----------|--------------------|------------------------------------------------------------------|
| Phase 2C  | 60 %               | Models + harness only; minimal business logic to test            |
| Phase 3   | 80 %               | Repositories + services have full unit-test surface              |
| Phase 4+  | 85 %               | API + integration layer adds more code that *can* be unit-tested |

The threshold lives in `pyproject.toml` (`[tool.coverage.report] fail_under = N`). Changing it requires an ADR — coverage thresholds are a contract with reviewers, not a knob to twiddle.

---

## 8. Document history

| Date       | Author  | Change                                                                                  |
|------------|---------|------------------------------------------------------------------------------------------|
| 2026-06-28 | curator | Initial version — ADR-0028 ratified at close of Phase 2 Step B; gate green end-to-end.   |
