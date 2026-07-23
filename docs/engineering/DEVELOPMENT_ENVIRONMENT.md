# Development Environment

Operational knowledge for working on this repository locally (and with the Cursor
agent). Keep it short; it exists to prevent a specific class of "my tools stopped
working" failures, not to duplicate the READMEs.

---

## Correct project root

The canonical checkout lives at:

```
/Users/rehanshifque/dev/ai-video-platform
```

The backend is a `backend/` subfolder with its own virtualenv. **All git and
tooling commands are run from the repo root or `backend/`.**

> **Why this is called out:** the Cursor workspace root was once pointed at a
> stale **Windows-style path** (`C:/dev/ai-video-platform`) that does not exist on
> this machine. Because the shell sandbox profile grants write access to the
> workspace directory, a non-existent root made `sandbox-exec` fail to initialize
> ("Failed to execute sandbox-exec: No such file or directory") and made the
> agent's file search (Glob/Grep) resolve against the bogus root. See
> [Recovery](#recovery-stale-or-windows-workspace-root) below.

---

## Startup checklist

After (re)opening the editor, before doing any work, confirm the environment:

```bash
pwd                        # → /Users/rehanshifque/dev/ai-video-platform
git branch --show-current  # → the branch you expect (e.g. main)
git status                 # → clean, or the changes you expect
```

If `pwd` is not the macOS project path, **fix the workspace before anything else**
(see Recovery).

---

## Virtual environment

New shells do **not** auto-activate the venv. Either activate it:

```bash
source backend/.venv/bin/activate
```

…or invoke tools directly without activating:

```bash
backend/.venv/bin/python -m pytest ...
backend/.venv/bin/ruff check ...
backend/.venv/bin/mypy ...
```

A `command not found: python` almost always means the venv is not active.

---

## Quality gate

Run from `backend/` with the venv active. This is the same gate CI enforces:

```bash
cd backend
ruff check app tests
black --check app tests
mypy app
lint-imports                                   # import-linter: layered architecture contracts
python -m pytest -q -m unit                    # unit suite (fast; no DB)
python scripts/check_frozen_platform.py --base main   # ADR-0042 orchestration freeze guard
```

Notes:
- `-m unit` selects the DB-free unit tests. Running the whole suite without a
  marker will also pick up integration tests (which need Postgres) and can appear
  to "hang" while waiting on a database.
- The **freeze guard** must stay green with **zero override markers** on every
  feature branch (ADR-0042). If it flags a change to a frozen orchestration path,
  stop and reconsider the design rather than adding an override.

---

## Recovery: stale or Windows workspace root

If tools fail with the `sandbox-exec` error, or file search reports a path like
`C:/dev/ai-video-platform`, the workspace root is wrong. Fix it by one of:

1. **Reopen the folder** in Cursor directly from the macOS path
   `/Users/rehanshifque/dev/ai-video-platform` (most reliable).
2. If using the agent, re-root it to the correct absolute path (the
   `move_agent_to_root` app-control action) and re-point the active branch to one
   that exists on `origin` first (its migration step runs `git fetch origin
   <branch>`).
3. As a last resort, fully quit and reopen Cursor.

After recovery, re-run the [startup checklist](#startup-checklist).

---

## Common failure modes

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `Failed to execute sandbox-exec: No such file or directory` | Workspace root points at a non-existent (e.g. Windows) path | Recovery, above |
| Agent Glob/Grep error `Path does not exist: C:/...` | Same stale workspace root | Recovery, above |
| `command not found: python` | venv not activated | `source backend/.venv/bin/activate` |
| `pytest` seems to hang with no output | Ran the full suite (incl. integration) without Postgres | Use `-m unit`, or start the DB |
| Freeze guard fails on a frozen path | Change touched an ADR-0042 module | Redesign additively; do **not** override |

---

## Release ritual (per runtime slice)

For reference, the standard sequence used for each `α8.x` runtime slice:

1. Feature branch `phase3/alphaX.Y-...`; bump version to `…-dev`.
2. Implement additively; keep the full gate + freeze guard green.
3. Feature commit (keep `-dev`).
4. Finalize commit dropping `-dev` → `chore(release): finalize vX.Y.Z-… (drop -dev)`.
5. `git checkout main && git merge --ff-only <branch>` (must fast-forward).
6. `git push origin main`.
7. Annotated tag `vX.Y.Z-…` and push it.
8. Delete the local feature branch (`git branch -d`).
9. Verify: `main == origin/main`, tag on the release commit, clean tree, version
   string reports the released value.
