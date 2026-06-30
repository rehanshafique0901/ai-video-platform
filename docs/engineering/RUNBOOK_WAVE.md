# Wave Runbook

> **Purpose.** This runbook documents the end-to-end procedure for executing
> a single Phase 3 Wave (W1.1, W1.2, etc.) from pre-flight through to tag.
> It exists so every Wave follows the same deterministic process without
> the ADR having to re-describe operational steps.
>
> **Scope.** Wave-style work only: a single architectural decision, one
> Alembic migration, ORM + doc alignment, validation, merge, tag, cleanup.
> Other release types (infrastructure cleanup, hotfix, etc.) use their
> own runbooks under `docs/engineering/`.
>
> **Convention** (per `CONTRIBUTING.md` §6, codified in v0.3.3-infra): ADRs
> explain **why** a decision was made; this runbook explains **how** the
> decision is executed. New Wave ADRs should reference this runbook by
> name rather than duplicating its steps.

---

## Section 1 — Pre-flight

Performed BEFORE creating a branch. All checks are read-only.

1. **Confirm `main` is current and the working tree is clean.**

   ```powershell
   git checkout main
   git pull origin main --ff-only
   git status                # must report "nothing to commit, working tree clean"
   git log --oneline -1      # records the commit the Wave starts from
   ```

2. **Confirm the previous Wave's tag is intact and points at `main`'s HEAD.**

   ```powershell
   git tag --list "v0.3.*" --sort=-v:refname
   git rev-list -n 1 v0.3.<N-1>-phase3-w1.<X-1>
   ```

   The tag's commit must match the SHA printed by step 1's `git log`.

3. **Confirm GitHub Actions is green on `main`.** Open the Actions tab
   for the repository and verify the latest run on `main` shows ✅.
   A red latest-run blocks the Wave until the regression is fixed on
   `main`.

4. **Read the affected ORM model, the baseline migration, and any prior
   migration that touches the same table.** Identify the invariant
   being promoted and any documentation that already references it
   (typically `docs/database/schema.md` and
   `docs/database/INDEX_STRATEGY.md`).

5. **Run a callsite scan for the affected ORM class.** A typical Wave
   touches one table; verify no application code depends on the
   absence of the invariant being promoted (e.g., that no use case is
   silently relying on duplicate rows being allowed).

   ```powershell
   rg "from app\.infrastructure\.db\.models.* import .*<ClassName>"
   rg "<ClassName>\(" --type py
   ```

6. **Draft the ADR.** New file under `docs/decisions/` following the
   file-per-ADR convention (ADR-0030 onwards). Required sections:
   Context, Decision, Alternatives Considered, Migration Plan,
   Rollback, Consequences, Acceptance Criteria. Status starts at
   **Proposed**.

7. **Request approval of the ADR before any code change.** No branch
   is cut until the ADR is approved as-is.

---

## Section 2 — Development

Performed ON the Wave branch. All changes are committable.

1. **Cut the branch from `main` at the verified commit.**

   ```powershell
   git checkout -b phase3/wave1.<X>-<short-slug>
   git branch --show-current
   git log --oneline -1      # confirms branch is at main's HEAD
   ```

2. **Write the Alembic migration by hand.** Do NOT use
   `alembic revision --autogenerate` for Wave migrations — it does not
   preserve CHECK expression text or trigger DDL, and it cannot emit
   partial-unique indexes via `postgresql_where`. The migration filename
   uses the next available revision number and matches the table name
   plus a short descriptor (e.g. `0007_usage_records_request_id_unique.py`).
   Revision IDs may use descriptive slugs of any reasonable length —
   the `alembic_version.version_num` ceiling was widened to
   `VARCHAR(255)` in v0.3.3-infra.

3. **Update the ORM model** to mirror the migration exactly. Add the
   matching constraint, column, or index declaration to `__table_args__`
   or the column list. The ORM is the source of truth that the
   migration encodes.

4. **Update documentation in this order:**

   - `docs/database/schema.md` — update the affected table's section
     (column block, invariant paragraph, reconciliation note); mark
     the §37 row as **Resolved** if the Wave addresses one.
   - `docs/database/INDEX_STRATEGY.md` — only if the Wave adds or
     reorganizes indexes. CHECK constraints and triggers are not
     tracked here.
   - `CHANGELOG.md` — add a new sub-section under `[Unreleased]` for
     the Wave with Added / Changed / Validated / Not modified / Scope
     discipline blocks (see W1.1/W1.2/W1.3 entries for the canonical
     template).

5. **Run the pre-upgrade safety SELECT** against the live database
   target BEFORE invoking `alembic upgrade head`. The SELECT must
   return zero violators of the new invariant. The exact SELECT for
   each Wave is recorded in its ADR's §Acceptance Criteria. Use
   stdin-piped Python (avoids the `python -c` argument-quoting hell
   on PowerShell), and normalize the SQLAlchemy-style URL to the
   `postgresql://` form that raw psycopg expects. Example for a
   CHECK constraint, run from `backend/`:

   ```powershell
   $check = @"
   from scripts._load_env import load
   import os, psycopg
   load()
   # DATABASE_URL is stored in SQLAlchemy form (postgresql+psycopg://...)
   # because that is what alembic + the ORM consume. Raw psycopg.connect()
   # only accepts the libpq scheme, so strip the driver prefix.
   url = os.environ['DATABASE_URL'].replace('postgresql+psycopg://', 'postgresql://')
   conn = psycopg.connect(url)
   cur = conn.cursor()
   cur.execute('SELECT COUNT(*) FROM <table> WHERE NOT (<predicate>)')
   print('violators:', cur.fetchone()[0])
   "@
   $check | python -
   ```

   A non-zero count blocks `upgrade head` and requires either data
   cleanup or switching to the production-rollback variant
   (`ADD CONSTRAINT ... NOT VALID` followed by a later `VALIDATE
   CONSTRAINT`).

---

## Section 3 — Verification

Performed before the first commit reaches `origin`.

1. **Alembic round-trip** against the live target.

   ```powershell
   cd backend
   python -m alembic upgrade head
   python -m alembic downgrade -1
   python -m alembic upgrade head    # idempotency proof
   ```

   The forward + reverse + forward sequence must apply cleanly.
   Inspect `pg_constraint`, `pg_trigger`, or `pg_indexes` (as
   appropriate to the Wave) to confirm the expected diff: exactly one
   object added on forward, exactly one removed on reverse.

2. **Run the full CI gate locally.**

   ```powershell
   cd backend
   .\scripts\run_ci_gate.ps1
   ```

   Result must read **PASSED** with all 10 stages green. `DATABASE_URL`
   loading is automatic from `backend/.env.validation`; no manual
   environment setup required.

3. **Verify `git status` is clean** before staging — no stray files
   from validation runs. The `.validation/` output directory is
   git-ignored.

---

## Section 4 — Release

Performed in two commits.

1. **Commit 1 — feature commit.** Stage exactly the engineering files:

   - The new ADR (status still `Proposed`)
   - The new Alembic migration
   - The ORM update
   - The `schema.md` / `INDEX_STRATEGY.md` / `CHANGELOG.md` updates

   ```powershell
   git add docs/decisions/ADR-<NNNN>-*.md `
           backend/alembic/versions/<NNNN>_*.py `
           backend/app/infrastructure/db/models/<file>.py `
           docs/database/schema.md `
           CHANGELOG.md
   git status                 # verify exact staging
   git commit                 # use the Wave commit message template
   ```

2. **Commit 2 — status-flip commit.** A separate small commit that
   flips the ADR's status from `Proposed` to `Accepted`, adds the
   one-line cross-link in `DECISIONS.md`, and adds the `ROADMAP.md`
   annotation. Keeping this commit separate from the feature commit
   makes ADR approval visible in the git history as its own event.

   ```powershell
   git add docs/decisions/ADR-<NNNN>-*.md DECISIONS.md ROADMAP.md
   git commit -m "docs(adr): mark ADR-<NNNN> Accepted"
   ```

3. **Push and open the PR.**

   ```powershell
   git push -u origin phase3/wave1.<X>-<short-slug>
   ```

   Open the PR via the GitHub UI; title follows Conventional Commits
   (`feat(db): ...`); description links the ADR; PR checklist items
   match the ADR's §Acceptance Criteria.

4. **Wait for CI to pass** (10/10 stages green on the PR's check run).

5. **Pre-merge checkpoint — verify the status-flip commit is on the
   branch.** Before clicking Merge, run:

   ```powershell
   git log --oneline main..HEAD
   ```

   Output must show exactly **two** commits, and the most recent must
   be `docs(adr): mark ADR-NNNN Accepted`. If only one commit appears
   (or the most recent is the feature commit), the status-flip is
   missing — push it first per Section 4.2 and re-run this check
   before merging. Merging without the status-flip leaves the ADR at
   `Proposed` on `main` and forces a recovery commit on `main` after
   the fact, which is recoverable but costs roughly four extra commands
   (W1.4 hit this on 2026-06-30; see the W1.4 retrospective).

6. **Merge via the GitHub UI** (merge commit, not squash — preserves
   the two-commit structure). Record the merge commit SHA.

7. **Tag the merge commit.** The working tree must be clean before
   switching to `main` — any uncommitted change (e.g., a deferred
   status-flip edit) will block `git checkout main`, and the
   `git pull origin main --ff-only` that follows would otherwise
   fast-forward the still-current branch instead of `main`. Verify
   with `git status` first.

   ```powershell
   git status                        # working tree must be clean
   git checkout main
   git pull origin main --ff-only
   git tag -a v0.3.<N>-phase3-w1.<X> <merge-sha> `
       -m "Phase 3 Wave 1.<X> — <short title>"
   git push origin v0.3.<N>-phase3-w1.<X>
   ```

8. **Branch cleanup.**

   ```powershell
   git branch -d phase3/wave1.<X>-<short-slug>
   git push origin --delete phase3/wave1.<X>-<short-slug>
   git fetch --prune
   git log --oneline --decorate -7    # verify tag + merge present, branch absent
   git branch -a                       # verify only main remote-tracking remains
   ```

---

## Section 5 — Recovery

When things go wrong DURING a Wave.

### 5.1 Alembic `upgrade head` fails

- **Symptom:** SQL error from `op.execute(...)`.
- **First check:** Did the pre-upgrade safety SELECT return zero? If
  you skipped it, run it now. Non-zero violators mean the migration
  cannot apply directly — switch to the production-rollback variant
  (`ADD CONSTRAINT ... NOT VALID`, then `VALIDATE CONSTRAINT` in a
  follow-up migration after data is cleaned).
- **Second check:** Is `alembic_version` in a bad state? Inspect with
  `SELECT * FROM alembic_version;` — if it contains a partial or
  aborted revision, manually clear it before retrying.
- **Recovery:** If a forward migration failed mid-way, run
  `alembic downgrade base` to reset, then re-attempt after fixing the
  migration.

### 5.2 Ad-hoc validation SQL against the live target

When you need to run a one-off SELECT (e.g., the pre-upgrade safety
check) and PowerShell's `python -c` argument-quoting fights you, pipe
the script through stdin instead (`python -`). Always run from
`backend/` so `scripts/` is on the Python import path, and always
normalize the SQLAlchemy-style URL (`postgresql+psycopg://...`) to
the `postgresql://` scheme that raw psycopg accepts — the env file
stores the SQLAlchemy form because that's what alembic + the ORM
consume.

```powershell
$snippet = @"
from scripts._load_env import load
import os, psycopg
load()
url = os.environ['DATABASE_URL'].replace('postgresql+psycopg://', 'postgresql://')
conn = psycopg.connect(url)
cur = conn.cursor()
cur.execute('SELECT COUNT(*) AS violators FROM <table> WHERE NOT (<predicate>)')
print(cur.fetchone())
"@
$snippet | python -
```

Three failure modes recorded here so the next contributor doesn't
re-discover them:

- **`SyntaxError: unterminated string literal`** when using triple-
  quoted SQL inside `python -c "$snippet"` — PowerShell mangles
  consecutive `"` characters during argument parsing. Switch to
  `$snippet | python -` to bypass the argument layer entirely.
- **Silent variable substitution / `SyntaxError` from
  `python -c "...$variable..."` (e.g.,
  `python -c "import x; x.run('$sql')"`).** PowerShell expands
  `$variable` into the double-quoted argument string *before* `python`
  is ever launched, so by the time the Python interpreter parses the
  source it sees the expanded text — any quote, parenthesis, or
  newline inside `$variable` is now lexed as Python syntax, which
  typically produces `SyntaxError: unterminated string literal` or
  worse, a silently-mutated SQL statement. Single-quoted PowerShell
  strings would suppress expansion but then collide with the inner
  Python quoting, so the only robust pattern is stdin: build the
  snippet as a here-string and pipe it (`$snippet | python -`), which
  bypasses PowerShell's argument-quoting and expansion layers entirely
  and lets Python read raw bytes from stdin.
- **`psycopg.ProgrammingError: invalid connection option
  "postgresql+psycopg://..."`** when passing the env file's
  DATABASE_URL straight to `psycopg.connect()` — psycopg3 does not
  recognize SQLAlchemy's `+driver` URL extension. Apply the
  `.replace('postgresql+psycopg://', 'postgresql://')` normalization
  shown above before connecting.

### 5.3 Accidentally staged file (wrong scope)

```powershell
git reset HEAD <path>      # unstage the file
git status                  # confirm
```

Then re-run `git add` with the correct paths.

### 5.4 PR CI failure

- **lint / format / static / unit:** Fix locally, re-run
  `.\scripts\run_ci_gate.ps1 -Stages "1-4"`, push the fix.
- **migration / validator / ERD (stages 5–9):** Re-run
  `.\scripts\run_ci_gate.ps1 -Stages "5-9"` locally; if it passes
  locally but not in CI, check that the GitHub Actions service
  container is reachable (rare; usually retry succeeds).
- **coverage:** Add tests or remove unreachable branches. Do not
  lower the threshold to make a PR pass.

---

## Section 6 — Lessons Learned

Historical context that explains why the current procedure looks the
way it does. None of the items below describe a workaround the
runbook still requires; they are recorded so future contributors
understand the "why" of certain design choices.

### 6.1 The `VARCHAR(32)` Alembic version_num ceiling (resolved 2026-06-30)

Alembic's default `alembic_version.version_num` column is `VARCHAR(32)`.
Phase 3 Wave 1.3 attempted a migration with revision ID
`0005_distributed_locks_lease_check` (34 chars) and failed at
`alembic upgrade head` with `psycopg.errors.StringDataRightTruncation`.
The Wave's in-flight recovery was to rename the migration to
`0005_distributed_locks_lease` (28 chars) to fit.

The structural fix was deferred to `v0.3.3-infra`, which widens the
column to `VARCHAR(255)` via migration `0006_widen_alembic_version_num`.
Wave migrations from W1.4 onwards can use descriptive slugs of any
reasonable length without abbreviation gymnastics — Section 2.2 above
records this directly as the current procedure.

### 6.2 The `.cursor/` accidental-staging incidents (resolved 2026-06-30)

During Wave 1.3's amend cycle, `git add -A` swept `.cursor/rules/*.mdc`
files into a feature commit unintentionally. The in-flight recovery
was `git reset HEAD .cursor/` before amending.

The structural fix in `v0.3.3-infra` added `.cursor/` (whole directory)
to `.gitignore`, replacing the prior partial ignore
(`.cursor/state/` + `.cursor/cache/`). The "keep workspace rules
tracked" intent that the partial ignore implied was aspirational —
no rules had ever actually been committed in practice — so the cleaner
approach was to ignore the whole directory and rely on `git add -f`
for any intentional rule sharing in the future. The accidental-staging
incident is now impossible to recur via `git add -A`.

### 6.3 The `ci_gate.py` env-load gap (resolved 2026-06-30)

Through Waves 1.1–1.3, running `scripts/ci_gate.py` locally for stages
5–9 required a manual PowerShell preamble to source
`backend/.env.validation` into the session before invoking the gate.
The gate itself checked the FILE'S existence for its `db_available`
flag but never actually loaded variables from it, so the `alembic`,
`validate_schema`, and `regenerate_erd` subprocesses inherited an
empty env and fell back to `alembic.ini`'s localhost URL.

The fix in `v0.3.3-infra` added a six-line conditional in `ci_gate.py`
that invokes the existing `_load_env.load()` function whenever the
file is present but `DATABASE_URL` is not yet exported. The PowerShell
wrapper (`run_ci_gate.ps1`) deliberately does NOT source the env
file itself — there is one source of truth for env loading, and it
lives in Python.

### 6.4 Why ADRs and Runbooks are separate

Through Waves 1.1–1.3, the operational checklists embedded in each
ADR grew larger than the architectural content itself. By
`v0.3.3-infra` it was clear the operational content was identical
Wave-to-Wave while the architectural content was rightly unique.

Splitting them — codified in `CONTRIBUTING.md` §6 — keeps ADRs
focused on _why_ a decision was made and runbooks focused on _how_
it is executed, with neither doc type drifting into the other's job.
This runbook is the first artefact under the new convention; the
Wave 1.4 ADR (ADR-0033) is the first ADR to reference it directly
(merged as part of `v0.3.4-phase3-w1.4`, 2026-06-30).
