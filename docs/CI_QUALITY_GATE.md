# CI Quality Gate — Phase 2C

> The single source of truth for "what does *green* mean on this repo?"
> Authority: **ADR-0028**.

Every pull request and every push to `main` runs the same 10-stage pipeline.
Local invocation produces byte-identical output via the same Python
entrypoint so a contributor can reproduce CI on a laptop:

```bash
cd "ai creation/backend"
python scripts/ci_gate.py
```

A red gate blocks merge. There are no opt-outs.

---

## 1. Stage map

| #  | Stage                                | Tool                   | Live DB? | Budget   |
|----|--------------------------------------|------------------------|----------|----------|
| 1  | Lint                                 | `ruff check`           | no       | <  3 s   |
| 2  | Format                               | `black --check`        | no       | <  3 s   |
| 3  | Static analysis                      | `mypy` + import-linter | no       | < 15 s   |
| 4  | Unit tests + coverage instrumentation| `pytest -m unit --cov` | no       | < 10 s   |
| 5  | Forward migration                    | `alembic upgrade head` | **yes**  | < 90 s   |
| 6  | Reverse migration                    | `alembic downgrade base` | **yes**| < 60 s   |
| 7  | Idempotency                          | `alembic upgrade head` | **yes**  | < 90 s   |
| 8  | Schema validator                     | `scripts/validate_schema.py` | **yes** | < 30 s |
| 9  | ERD comparison                       | `scripts/regenerate_erd.py` + `compare_erd.py` | **yes** | < 30 s |
| 10 | Coverage threshold                   | `coverage report`      | no       | <  2 s   |

Total wall-clock budget: **≤ 6 min** in CI; observed Phase 2C run on a warm runner ≈ **3 m 40 s**.

Stages execute strictly in order and fail-fast: a non-zero exit at any
stage aborts the pipeline. Stages 5–9 require a reachable PostgreSQL
with `pgvector` (in CI: a `pgvector/pgvector:pg16` service container; in
local dev: `backend/.env.validation` pointing at Supabase, Neon, or a
local Docker container).

---

## 2. Why these stages?

### 2.1 Lint, format, types, tests (1–4 + 10)

Standard fast inner loop. No surprises:

- **Ruff** chosen over flake8/isort/pylint for speed and one-binary
  ergonomics. Rule selection (`E/F/I/B/UP/N/SIM/C4/RUF`) trades a small
  amount of noise for catching real bugs — bare-except, unused imports,
  comprehension misuse, naming violations, pyupgrade idioms.
- **Black** keeps formatting debates off PRs. `--check` mode in CI; the
  local pre-commit hook is also `--check` so the human stays in control
  of when the file is rewritten.
- **mypy** runs in strict mode against `app/`. The legacy validator
  scripts under `scripts/` and the test suite under `tests/` are
  excluded because they use dynamic patterns (raw SQL, fixture
  parametrisation) where strict typing fights the developer.
- **import-linter** runs alongside mypy because the rule set
  (`app.domain` cannot import infrastructure; `app.infrastructure.db`
  cannot import api/application) is architectural fitness and belongs
  in the static-analysis stage. Phase 3 will activate the third rule:
  `app.application` cannot import api.
- **pytest** runs only tests marked `unit` in stage 4; integration
  coverage is owned by stages 5–9. The marker split keeps stage 4 < 10 s.
- **Coverage** is split into instrumentation (stage 4 produces `.coverage`)
  and threshold enforcement (stage 10 reads it). The threshold is
  intentionally low at Phase 2C (60 %); it is raised in Phase 3.

### 2.2 Live migration round-trip (5–7)

The three-step upgrade → downgrade → upgrade dance is exactly what
proved that the Phase 2B migrations were reversible AND idempotent.
Wiring it into CI means any future migration that breaks either property
is caught at PR time, not in production. The reviewer's Phase 2B sign-off
explicitly called this out as the kind of check "that gives confidence
the schema is stable" — it stays in CI permanently.

### 2.3 Schema validator (8)

The nine structural checks introduced in Phase 2B
(`validate_schema.py`). All `pg_catalog`-based; runtime ≈ 17 s on a
cross-region Supabase pooler, < 5 s against a local container.
Catches:

- a renamed table that the migration applied but the ORM didn't update,
- a missing imperative `GIN`/`HNSW` index,
- a partition parent without children,
- an immutable table whose trigger was accidentally dropped,
- a `vector` column added outside the two approved tables,
- a `credit_ledger` insert that would violate balance monotonicity.

### 2.4 ERD comparison (9)

The structural diff between the live-regenerated ERD and the
hand-authored design ERD (`docs/database/ERD.md`). Entities must match
exactly. Every design-declared FK edge must be present in the
implementation. Additional FKs in the implementation are fine (the
cluster-split design intentionally omits cross-cluster edges for
readability); missing design edges fail the gate.

### 2.5 Coverage threshold (10)

Coverage is enforced AFTER the live-DB stages so a developer running
locally without a database still gets to see stages 1–4 results before
the threshold gate either passes or fails. The threshold is recorded in
`backend/pyproject.toml` (`[tool.coverage.report] fail_under`).

| Phase     | `fail_under` |
|-----------|--------------|
| 2C        | 60 %         |
| 3         | 80 %         |
| 9 (CI freeze) | 85 %      |

---

## 3. Running the gate locally

```bash
# Full pipeline (mirrors CI exactly).
python scripts/ci_gate.py

# Iterate on stages 1–4 while writing code.
python scripts/ci_gate.py --stages 1-4

# Only the live-DB stages (after migration work).
python scripts/ci_gate.py --stages 5-9

# Disable colour for piping into a file or non-TTY tools.
python scripts/ci_gate.py --no-color > .validation/ci.log
```

Behaviour when `DATABASE_URL` is unset *and* `backend/.env.validation`
does not exist: stages 5–9 are reported as `SKIP`. The runner exits 0
on a skip; it never silently "passes" them.

To prepare for live stages locally:

```bash
# Option A — point at any reachable Postgres+pgvector.
export DATABASE_URL="postgresql+psycopg://user:pass@host:5432/db?sslmode=require"

# Option B — drop a one-line file at backend/.env.validation (git-ignored)
echo 'DATABASE_URL=postgresql+psycopg://aivp:aivp@localhost:5432/aivp' \
  > backend/.env.validation
```

---

## 4. Pre-commit (subset of stage 1–2)

`.pre-commit-config.yaml` ships the *fast* subset of the gate so the
checks that complete in well under a second per file run at commit time.
Slow checks (mypy, pytest, live DB) stay in CI only.

```bash
pip install pre-commit
pre-commit install                  # one-time per clone
pre-commit run --all-files          # full-tree dry run
```

A pre-commit failure blocks the commit locally. CI re-runs the same
checks anyway, so the hook is convenience, not authority.

---

## 5. When the gate fails

| Failing stage | Likely cause                                       | Where to look |
|---------------|----------------------------------------------------|---------------|
| 1 ruff        | dead import, B-rule violation, naming              | the file & line shown in the report |
| 2 black       | unformatted file                                   | run `black <file>` locally; commit |
| 3 mypy        | new type annotation regression                     | re-add an annotation or fix the call |
| 3 import-linter| domain layer importing infrastructure             | move the helper / introduce a port |
| 4 pytest      | smoke assertion (e.g. naive datetime, missing FK ON DELETE) | re-read `tests/test_metadata.py` for the contract |
| 4 coverage    | new code path without tests                        | add a unit test; do not lower the threshold |
| 5 alembic up  | migration syntax / missing partition / drift       | `alembic check`; re-run locally |
| 6 alembic down | irreversible op smuggled into migration           | every `op.create_*` must have a paired `op.drop_*` |
| 7 alembic up  | non-idempotent DDL                                 | guard with `IF NOT EXISTS` or move into the baseline |
| 8 validator   | structural regression                              | re-read `SCHEMA_VALIDATION.md` §6.2; check `pg_catalog` |
| 9 ERD diff    | design ERD claims a FK the implementation does not have | fix one side; add an ADR if intentional |
| 10 coverage   | overall coverage below `fail_under`                | add tests, do not lower the floor |

---

## 6. Document History

| Date       | Author  | Change |
|------------|---------|--------|
| 2026-06-28 | curator | Initial — Phase 2C, ADR-0028. 10 stages, local + GH Actions runners, pre-commit subset, coverage floor 60 % (raised in Phase 3). |
