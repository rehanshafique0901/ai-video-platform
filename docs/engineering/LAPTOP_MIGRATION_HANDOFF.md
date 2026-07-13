# Laptop Migration Handoff (Windows → macOS)

> Written 2026-07-13 at release **`v0.4.13-phase3-alpha6.3a`** to make continuing
> this project on a new machine friction-free. This is a *continuation* doc: read
> it top-to-bottom on the new laptop and you will be back to a working dev loop
> plus full project context.

---

## 0. TL;DR — nothing is lost

**All code, history, ADRs, pre-flight docs, CHANGELOG, ROADMAP, and this handoff
are committed and pushed to GitHub.** The migration is just:

1. `git clone` the repo on the Mac.
2. Recreate the *local-only* bits git does not carry (§3).
3. Run the CI gate to confirm green (§4).

Repo: `https://github.com/rehanshafique0901/ai-video-platform`
`main` is at `b8b884e`, tagged **`v0.4.13-phase3-alpha6.3a`**.

Before you wipe the Windows machine, run this once to be 100% sure nothing local
is unpushed:

```powershell
cd C:\dev\ai-video-platform
git status          # must say: "working tree clean" and "up to date with origin/main"
git push            # no-op if already pushed
git push --tags     # ensure all tags are on the remote
git log --oneline -5
```

---

## 1. Current project state (accurate as of this doc)

- **Release:** `v0.4.13-phase3-alpha6.3a` (`main` @ `b8b884e`, no `-dev` suffix).
- **What shipped in α6.3a:** the **Timeline aggregate** — timeline root + tracks,
  a self-contained OCC aggregate fenced on `timelines.version` (ADR-0038),
  excluded from the project version ledger (ADR-0035). Domain entities,
  repository, use cases, DTOs, nested router, unit + integration tests, docs.
- **Migrations:** zero new migrations since the α5 baseline — everything from α5
  onward reuses the existing physical schema (`docs/database/schema.md`).
- **Next milestone:** **α6.3b (Clips)** → `v0.4.14` (see §7).

> Note: `PROJECT_STATUS.md` in the repo root is **stale** (it still reads Phase 2D
> / `v0.2.2`). Treat *this* file + `CHANGELOG.md` + `ROADMAP.md` as the truth for
> "where are we". Refreshing `PROJECT_STATUS.md` is a nice-to-do, not a blocker.

### The four aggregates and their three concurrency postures

| Aggregate | Members | Concurrency | In version ledger? | ADR |
|---|---|---|---|---|
| **Project** | Project + Scenes | Aggregate OCC (`projects.version`) | **Yes** — snapshots / restore / diff / branch | ADR-0035 |
| **Prompt** | Generation inputs | None — last-writer-wins | No | ADR-0036 |
| **Media** | Owner-scoped generated assets | None — last-writer-wins | No | ADR-0037 |
| **Timeline** | Timeline + Tracks (+ Clips, α6.3b) | **Own** OCC (`timelines.version`) | No (outside ledger) | ADR-0038 |

These are *peers* under the project, not a linear pipeline. The Timeline's
"third posture" (its own OCC token, outside the project ledger) is the subtle
invariant α6.3a locked in and the reason clips drop in cleanly next.

---

## 2. Where everything lives (source of truth)

| Thing | Location | Carried by `git clone`? |
|---|---|---|
| Backend code | `backend/app/` | ✅ |
| Tests | `backend/tests/` | ✅ |
| Migrations | `backend/alembic/` | ✅ |
| Physical schema | `docs/database/schema.md` | ✅ |
| ADRs | `docs/decisions/` + inline in `DECISIONS.md` | ✅ |
| Aggregate design docs | `docs/domain/*_AGGREGATE.md` | ✅ |
| Per-slice pre-flights | `docs/engineering/PHASE3_ALPHA*_PREFLIGHT.md` | ✅ |
| API contract | `API_CONTRACT.md` | ✅ |
| Changelog / roadmap | `CHANGELOG.md`, `ROADMAP.md` | ✅ |
| Contributor handbook | `CONTRIBUTING.md`, `rule.md`, `ARCHITECTURE.md` | ✅ |
| CI gate spec | `CI_QUALITY_GATE.md`, `docs/CI_QUALITY_GATE.md` | ✅ |

---

## 3. What `git clone` does NOT carry — hand-carry these

These are git-ignored or live outside the repo. Copy them to the Mac (USB / cloud
drive) **before** decommissioning the Windows laptop.

1. **`backend/.env.validation`** (git-ignored) — holds `DATABASE_URL` for the
   live-DB CI stages and the integration tests. Recreate it on the Mac (§4.4).
   If you have any other `.env` / `.env.local` in `backend/`, copy those too.
2. **Any secrets** — provider API keys, JWT signing keys, Supabase/DB
   credentials. These were never committed (correctly). Note them down.
3. **Cursor chat transcripts** (this AI conversation history) — local, outside
   the repo:
   - `C:\Users\rehan\.cursor\projects\c-dev-ai-video-platform\agent-transcripts\`
   - Copy that whole folder. On the Mac, Cursor stores the equivalent under
     `~/.cursor/projects/<project-hash>/agent-transcripts/`. Chat history does
     not auto-sync across machines, so this manual copy is the only way to keep
     the raw transcripts. **You do not strictly need them** to continue — this
     handoff doc + the committed docs are enough to resume — but keep them if you
     want the literal back-and-forth.
4. **Virtualenv** — do *not* copy it; recreate on the Mac (§4.2). Windows venvs
   don't work on macOS.
5. **`.cursor/rules`** (if any project-specific rules exist under the repo) —
   these live in the repo if committed; the ProgramBench workflow rules under
   `OneDrive\Desktop\programming bench\.cursor\rules\` are a *different* workspace
   and unrelated to this project.

---

## 4. Mac setup (from a clean machine)

### 4.1 Prerequisites
```bash
# Install Homebrew if not present: https://brew.sh
brew install git python@3.12 postgresql@16   # postgres client tools (optional)
# Docker Desktop for Mac (for the local pgvector Postgres) — install from docker.com
xcode-select --install                        # git + build tools, if prompted
```

### 4.2 Clone + virtualenv + deps
```bash
git clone https://github.com/rehanshafique0901/ai-video-platform.git
cd ai-video-platform/backend
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e ".[dev]"        # runtime + lint/type/test tooling (ruff, black, mypy, pytest)
```

### 4.3 Local Postgres (pgvector) for integration tests / DB stages
```bash
# from repo root
docker compose -f backend/docker-compose.db.yml up -d
```

### 4.4 Point the tools at the DB
Create `backend/.env.validation` (git-ignored) with the connection string that
matches the compose file (or your Supabase URL):
```
DATABASE_URL=postgresql+psycopg://<user>:<pass>@localhost:5432/<db>
```
Without `DATABASE_URL`, CI stages 5–9 auto-skip (reported, not failed), and the
`-m integration` tests won't have a DB — so set this up if you want the full loop.

### 4.5 Apply migrations (fresh DB)
```bash
cd backend
alembic upgrade head
```

---

## 5. The dev loop (what "green" means)

Run from `backend/` with the venv active. This is exactly the gate used through
α6.3a:

```bash
ruff check .
black --check .
mypy app
pytest -m unit -q
pytest -m integration -q          # needs DATABASE_URL
```

Or the bundled 10-stage gate (see `CI_QUALITY_GATE.md`):
```bash
python scripts/ci_gate.py                 # full gate
python scripts/ci_gate.py --stages 1-4,10 # fast, no DB
```

`black` is the source of truth for formatting — if `black --check` complains, run
`black .` and commit the result (don't hand-format).

---

## 6. Per-slice workflow + release flow (the rhythm we've been using)

Every slice from α5 onward followed the same shape:

1. **Pre-flight doc** — `docs/engineering/PHASE3_ALPHA<slice>_PREFLIGHT.md`:
   scope, facts grounded against the real schema, decisions, open questions.
2. **Reviewer sign-off** on the open questions (the "Q1/Q2/…" answers).
3. **Cut the branch** `phase3/alpha<slice>-<name>`, bump
   `backend/app/main.py` `version=` to `0.4.x-phase3-alpha<slice>-dev`.
4. **Implement** in dependency order: domain → repo interface → repo impl →
   UoW/fakes wiring → use cases → DTOs + container factories + deps aliases →
   router + mount in `main.py` → unit tests → integration tests → docs
   (ADR + aggregate doc + `API_CONTRACT.md` + `CHANGELOG.md` + `ROADMAP.md`).
5. **CI gate green** (§5).
6. **Commit** (`feat(<ctx>): …`), **push**, **merge to `main`** (`--ff-only`).
7. **Drop `-dev`** in `main.py`, commit `chore(release): finalize …`, push.
8. **Tag** `v0.4.x-phase3-alpha<slice>` (force-move the tag onto the drop-`-dev`
   commit if you tagged before dropping — that's the small gotcha we hit twice).
9. **Delete** the feature branch.

Conventions live in `CONTRIBUTING.md` (branching, commits, coding standards,
import-linter architecture rules) and `rule.md`.

---

## 7. Next milestone — α6.3b (Clips), `v0.4.14`

Second (final) half of the signed-off α6.3 slice. Tracks already exist, so clips
slot in as the leaf of the composition tree `Timeline → Track → Clip`.

Signed-off decisions carried from the α6.3 pre-flight
(`docs/engineering/PHASE3_ALPHA6_3_PREFLIGHT.md`):

- **Children of the timeline aggregate** — every clip create/update/delete is
  fenced on and bumps `timelines.version` (Q13), never `projects.version`.
- **Nested routing** under a track:
  `/projects/{id}/timeline/tracks/{track_id}/clips[/{clip_id}]`.
- **`media_asset_id` link validation** → `422` if not an owned, live media asset
  (reuse the α6.2 link-validation pattern).
- **Overlaps allowed** (Q6) — no exclusion constraint in this slice.
- **OCC**: `version` optional on clip `POST`, required on `PATCH`/`DELETE`,
  `412` on stale; soft delete, idempotent-by-404, 404-before-412.
- **Zero migrations** — the `clips` table already exists in the baseline.
- Extend `TimelineResult` / `TimelinePublic` so the tree embeds each track's
  ordered clips.

Open questions to resolve in the α6.3b pre-flight (ground against the real
`clips` columns in `backend/app/infrastructure/db/models/timeline.py` /
`docs/database/schema.md` first):

- Clip ordering within a track: implicit by start time vs an explicit index.
- Whether a clip's time range must fit within the track/timeline duration or is
  free-form.
- Whether `timelines.duration_seconds` auto-derives from clip extents or stays
  client-set.

**To resume on the Mac:** open a new Cursor chat in the cloned repo and say
*"continue with α6.3b (Clips) — draft the pre-flight"*. Point the assistant at
this file (`docs/engineering/LAPTOP_MIGRATION_HANDOFF.md`) and the α6.3
pre-flight for full context.

---

## 8. Doc index (where to read what)

- **Resume here:** this file.
- **Where are we / rhythm:** `CHANGELOG.md`, `ROADMAP.md`, `CONTRIBUTING.md`.
- **Why decisions were made:** `docs/decisions/ADR-00XX-*.md` + `DECISIONS.md`.
- **Aggregate designs:** `docs/domain/{PROJECT,SCENE,PROMPT,MEDIA,TIMELINE}_AGGREGATE.md`.
- **API surface:** `API_CONTRACT.md`.
- **DB truth:** `docs/database/schema.md`, `ERD.md`.
- **Architecture + rules for AI assistance:** `ARCHITECTURE.md`, `rule.md`.
- **CI:** `CI_QUALITY_GATE.md`.
