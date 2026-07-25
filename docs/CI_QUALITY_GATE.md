# CI Quality Gate — Phase 2C

> The single source of truth for "what does *green* mean on this repo?"
> Authority: **ADR-0028**.

Every pull request and every push to `main` runs the same 14-stage pipeline
(numbered 0–13). Local invocation produces byte-identical output via the same
Python entrypoint so a contributor can reproduce CI on a laptop:

```bash
cd "ai creation/backend"
python scripts/ci_gate.py
```

A red gate blocks merge. There are no opt-outs.

---

## 1. Stage map

| #  | Stage                                | Tool                   | Live DB? | Budget   |
|----|--------------------------------------|------------------------|----------|----------|
| 0  | Provider manifest (capability registry) | `scripts/validate_providers.py` | no  | <  2 s   |
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
| 11 | Provider-catalogue seed round-trip   | `scripts/verify_seed_roundtrip.py` | **yes** | < 60 s |
| 12 | Runtime integration verification     | `pytest -m integration` (Decision-/Execution-plane repos) | **yes** | < 30 s |
| 13 | Generation end-to-end slice          | `pytest -m integration` (full pipeline → MP4) | **yes** | < 60 s |

Total wall-clock budget: **≤ 6 min** in CI; observed Phase 2C run on a warm runner ≈ **3 m 40 s**.

Stages execute strictly in order and fail-fast: a non-zero exit at any
stage aborts the pipeline. **Stage 0** (α8.5c) is a fast, no-DB pre-flight that
validates the capability catalogue + provider manifest under `backend/providers/`
offline (`scripts/validate_providers.py`); it runs *before* every other stage so
a manifest regression fails cheaply, and skips (exit 0) when the manifest is
absent. It is numbered `0` — rather than renumbering 1–10 — so the restoration
guard's "stage 6 = downgrade" / live-DB range 5–9 keying stays exact. Stages 5–9
require a reachable PostgreSQL
with `pgvector` (in CI: a `pgvector/pgvector:pg16` service container; in
local dev: `backend/.env.validation` pointing at Supabase, Neon, or a
local Docker container).

> **Destructive-stage safety.** Stage 6 (`alembic downgrade base`) empties
> the target schema. To keep a transient failure between stage 6 and the
> stage-7 re-upgrade from ever leaving a *persistent* database at `base`, the
> runner provides DB isolation and a self-healing restoration guard — see
> [§2.6](#26-validation-db-isolation--restoration-guard).

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

### 2.6 Validation-DB isolation & restoration guard

Stage 6 is destructive, so a network blip between the downgrade and the
stage-7 re-upgrade could once leave a *shared* validation database empty
until an upgrade was retried by hand. Two independent, complementary
guards (tooling only — no application/runtime change) remove that class of
failure:

**Isolation — target a throwaway database.** The live stages resolve their
database in this precedence order:

1. `--ephemeral-db` / `CI_GATE_EPHEMERAL_DB=1` — the runner starts a
   throwaway `pgvector/pgvector:pg16` Docker container on a random loopback
   port, points `DATABASE_URL` at it for the run, and **always** removes it
   in a `finally` (even on Ctrl-C). A transient failure against a container
   that is about to be deleted is inert.
2. `VALIDATION_DATABASE_URL` — a dedicated validation database. Used verbatim
   for the live stages; the primary `DATABASE_URL` is left untouched.
3. `DATABASE_URL` / `backend/.env.validation` — the legacy path. If a
   destructive stage is selected here, the runner prints a **warning** that
   verification is running against the primary DB and leans on the guard
   below.

**Self-healing — never return success on an unverified DB.** Whenever a
downgrade may have run against a *persistent* DB (i.e. not the ephemeral
container), the runner, on **every** exit path, runs a bounded-retry
`alembic upgrade head` (8 attempts, 6 s backoff — tolerant of transient
connection failures) and then *verifies* `alembic current == heads`. The
gate refuses to print `PASSED` until that verification succeeds:

- `[ OK ] DB restored & verified at head: <rev>` — the DB is confirmed safe.
- `[FAIL] DB NOT restored to head (<rev>) …` — the runner could not confirm
  safety, so the gate fails **even if every stage passed**. It never claims a
  success it cannot verify.

CI already runs against an ephemeral service container, so this primarily
hardens the local flow where `.env.validation` points at a shared Postgres.

### 2.7 Provider-catalogue seed round-trip (11)

α8.5d. After the schema reaches head, `verify_seed_roundtrip.py` proves the
YAML → DB seeder is deterministic and idempotent end-to-end on live Postgres:
empty DB → seed creates everything; re-seed is a zero-write digest
short-circuit; a single manifest change updates exactly one row; a removed
entity is disabled (never deleted); registry metadata (digest, revision,
`catalogue_version`) updates correctly. It touches only the eight catalogue
tables and leaves the DB seeded, so it also populates the validation DB and
cannot disturb the destructive-migration guard.

### 2.8 Runtime integration verification (12)

α8.5e. The final stage answers one question: **"can a freshly created database
actually support the runtime?"** Against a DB that is at head and seeded
(stages 5–7 + 11), it exercises the Decision-plane persistence boundaries end
to end — the catalogue reader, the runtime-state reader, the resolver service,
and the resolution ledger — with each test wrapped in a `SAVEPOINT` that rolls
back on teardown. This is the stage that would have caught the α8.5e
`window` reserved-keyword and raw-SQL reader regressions at PR time rather than
only on the live-DB stages.

> **Scope freeze (governance).** Stage 12 should only grow when a **new runtime
> repository or persistence boundary** is introduced. Business-feature
> integration tests (planner, generation runtime, verification, repair, FFmpeg,
> export, providers, publishing) belong to their **feature slices**, not this
> infrastructure gate. This keeps Stage 12 a fast, stable "does the DB drive the
> runtime plumbing?" check instead of slowly becoming a kitchen sink. When a
> feature slice needs its own live-DB e2e coverage it earns a **dedicated stage**
> (the α8.6 generation slice is Stage 13, §2.9), preserving the one-stage/one-purpose rule.

A companion permanent, offline guard lives in stage 4:
`tests/unit/database/test_migration_reserved_identifiers.py` scans every Alembic
migration and fails on any PostgreSQL reserved keyword (`window`, `user`,
`order`, `group`, `table`, `constraint`, …) used as an **unquoted** identifier.
Quoting (`"window"`) is the sanctioned escape hatch.

### 2.9 Generation end-to-end slice (13)

α8.6 Increment 5. The first **feature-slice** e2e in the gate: against a DB at
head + seeded (stages 5–7 + 11) it drives the whole vertical slice —
Prompt → Planner → Storyboard → Resolver → Image Generator → Verifier → Repair →
Timeline → FFmpeg → MP4 → Execution-Runtime persistence — and asserts the five
Increment-5 acceptance dimensions (functional, persistence, architectural,
reproducibility, explainability) from the database alone. Image bytes come from a
hermetic offline `IImageGenerator` (real PNGs, no provider network), so the stage
is deterministic; it still exercises real ffmpeg/ffprobe and auto-skips when those
binaries are absent. Unlike Stage 12 it **commits** (the Execution-Runtime store
owns its own sessions) and **deletes the rows it created on teardown**, so it
leaves the destructive-migration restoration guard untouched. It lives in its own
stage — not Stage 12 — precisely because of that stage's scope freeze (§2.8).

For **manual** inspection of a run (rows left intact) use `scripts/generate_demo.py`
against a throwaway/ephemeral database; a gated live-provider variant of the e2e
runs only when `AIVP_E2E_POLLINATIONS=1`.

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

For the destructive stages, prefer an isolated database so a transient
failure can never touch a shared instance (see §2.6):

```bash
# Recommended — throwaway Docker container, created + destroyed by the runner.
python scripts/ci_gate.py --stages 5-9 --ephemeral-db

# Or point the live stages at a dedicated validation DB (primary DATABASE_URL
# is left untouched); the restoration guard still runs on every exit.
export VALIDATION_DATABASE_URL="postgresql+psycopg://user:pass@host:5432/validation"
python scripts/ci_gate.py --stages 5-9
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
| 11 seed round-trip | seeder non-idempotent / wrong disable/delete / digest drift | re-read `verify_seed_roundtrip.py`; check `seed_providers.py` upsert + `_canon` |
| 12 runtime integration | reader/resolver/ledger/exec-repo SQL breaks on a real DB; reserved keyword | re-read the failing test; a reserved identifier should have been caught by stage 4's guard |
| 13 generation e2e | pipeline regression (planner/resolver/verify/repair/render/persist) or ffmpeg missing | read the failing assertion + `generation.*` logs; if skipped, install ffmpeg/ffprobe |

---

## 6. Document History

| Date       | Author  | Change |
|------------|---------|--------|
| 2026-06-28 | curator | Initial — Phase 2C, ADR-0028. 10 stages, local + GH Actions runners, pre-commit subset, coverage floor 60 % (raised in Phase 3). |
| 2026-07-24 | curator | §2.6 added — validation-DB isolation (`--ephemeral-db` / `VALIDATION_DATABASE_URL`) + self-healing restoration guard (bounded-retry `upgrade head` + head verification on every exit). Tooling-only; no stage changes, no version bump. |
| 2026-07-25 | curator | Stage map updated to 13 stages (0–12): documented stage 11 (α8.5d provider-catalogue seed round-trip) and stage 12 (α8.5e runtime integration verification) in §2.7/§2.8, added the Stage 12 scope-freeze governance note and the reserved-identifier stage-4 guard, and extended the failure table. Docs-only. |
| 2026-07-25 | curator | Stage map extended to 14 stages (0–13): added stage 13 (α8.6 Increment 5 generation end-to-end slice, §2.9) as the first feature-slice e2e — kept out of stage 12 to honour its scope freeze; clarified stage 12 now also covers the execution-runtime repos. Docs + `ci_gate.py`. |
