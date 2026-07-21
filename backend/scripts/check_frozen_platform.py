"""Orchestration platform freeze guard (ADR-0042).

Fails when a change set touches a **frozen** orchestration module (the stable
platform API defined in ADR-0042 §D1) *without* a conscious override marker. Its
purpose is not to make core changes impossible — it is to make them
**deliberate**: a frozen-path change must cite the ADR that authorised it, so the
clean α7.1→α8.3 separation cannot decay one convenient shortcut at a time.

Single source of truth
-----------------------
``FROZEN_PATHS`` below is the machine-readable mirror of the ADR-0042 §D1 table.
When a future ADR *intentionally* extends the platform, update both together.

Change detection
----------------
The guard compares the current change set against a base ref:

    python scripts/check_frozen_platform.py --base main      # local, pre-merge
    python scripts/check_frozen_platform.py --base origin/main

Base resolution order: ``--base`` > ``$FREEZE_BASE_REF`` > ``origin/main`` >
``main``. The change set is the union of committed changes in ``base..HEAD`` plus
any staged / unstaged / untracked working-tree changes, so it behaves the same
whether run on a feature branch before fast-forwarding or inside CI.

Override marker
---------------
A frozen-path change is authorised by either:

  * a commit-message trailer ``Freeze-Override: ADR-XXXX <reason>`` in ``base..HEAD``, or
  * the environment variable ``ALLOW_FROZEN_CHANGES=1`` (local, pre-commit iteration).

Exit code
---------
``0`` — no frozen path touched, or an override was provided.
``1`` — frozen paths touched without an override (prints the offending files).
``2`` — the guard could not determine a base ref (treated as a soft pass in CI
        for the initial-push edge case; see ``--strict``).
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Frozen surface — mirror of ADR-0042 §D1. Repo-relative POSIX paths.
# A path matches if a changed file equals it or (for a trailing "/") sits under
# it. Concrete provider *adapters* and all new-capability surfaces are NOT here
# by design — they are the growth surfaces new work plugs into (ADR-0042 §D1).
# ---------------------------------------------------------------------------
FROZEN_PATHS: tuple[str, ...] = (
    # Runner + resume + completion (the async orchestration loop)
    "backend/app/application/use_cases/workflow/advance_workflow_run.py",
    "backend/app/application/use_cases/workflow/resume_workflow_run.py",
    "backend/app/application/use_cases/workflow/completion_engine.py",
    "backend/app/application/use_cases/workflow/_events.py",
    # Dispatch + provider registry + provider contracts (ports & neutral DTOs)
    "backend/app/infrastructure/ai/dispatcher.py",
    "backend/app/infrastructure/ai/providers/ports.py",
    "backend/app/infrastructure/ai/providers/registry.py",
    "backend/app/application/interfaces/providers.py",
    "backend/app/application/interfaces/provider_dispatcher.py",
    # Usage recording (service + pricing + port)
    "backend/app/application/use_cases/usage/usage_recorder_service.py",
    "backend/app/application/use_cases/usage/accounting.py",
    "backend/app/application/interfaces/usage_recorder.py",
    # Relay + distributed locks
    "backend/app/application/use_cases/relay/relay_service.py",
    "backend/app/infrastructure/repositories/distributed_lock_manager.py",
    "backend/app/application/interfaces/locks.py",
    # Workflow registry + aggregate + status enums (lifecycle + checkpoint owner)
    "backend/app/domain/workflow/registry.py",
    "backend/app/domain/workflow/workflow_run.py",
    "backend/app/domain/workflow/workflow_run_status.py",
    "backend/app/domain/workflow/workflow_step_status.py",
)

OVERRIDE_TRAILER = "Freeze-Override:"


def _c(code: str, text: str) -> str:
    if not (sys.stdout.isatty() and os.environ.get("NO_COLOR") is None):
        return text
    return f"\033[{code}m{text}\033[0m"


def _git(*args: str) -> tuple[int, str]:
    proc = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode, (proc.stdout or "")


def _rev_exists(ref: str) -> bool:
    if not ref or set(ref) == {"0"}:  # all-zero sha == "no parent" (initial push)
        return False
    code, _ = _git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    return code == 0


def _resolve_base(explicit: str | None) -> str | None:
    candidates = [
        explicit,
        os.environ.get("FREEZE_BASE_REF"),
        "origin/main",
        "main",
    ]
    for ref in candidates:
        if ref and _rev_exists(ref):
            return ref
    return None


def _changed_files(base: str) -> set[str]:
    """Union of committed (base..HEAD) and working-tree changes, repo-relative."""

    files: set[str] = set()
    # Committed on this branch since it diverged from base (three-dot uses the
    # merge-base so unrelated base commits don't count as "changed here").
    for flag in (f"{base}...HEAD",):
        code, out = _git("diff", "--name-only", flag)
        if code == 0:
            files.update(line.strip() for line in out.splitlines() if line.strip())
    # Staged + unstaged working-tree changes.
    for extra in (["diff", "--name-only", "HEAD"], ["diff", "--name-only", "--cached"]):
        code, out = _git(*extra)
        if code == 0:
            files.update(line.strip() for line in out.splitlines() if line.strip())
    # Untracked (new files dropped into a frozen package would matter).
    code, out = _git("ls-files", "--others", "--exclude-standard")
    if code == 0:
        files.update(line.strip() for line in out.splitlines() if line.strip())
    return files


def _is_frozen(path: str) -> bool:
    for frozen in FROZEN_PATHS:
        if frozen.endswith("/"):
            if path == frozen.rstrip("/") or path.startswith(frozen):
                return True
        elif path == frozen:
            return True
    return False


def _has_override(base: str) -> tuple[bool, str]:
    if os.environ.get("ALLOW_FROZEN_CHANGES", "").strip().lower() in {"1", "true", "yes"}:
        return True, "ALLOW_FROZEN_CHANGES env"
    code, out = _git("log", "--format=%B", f"{base}..HEAD")
    if code == 0:
        for line in out.splitlines():
            if line.strip().startswith(OVERRIDE_TRAILER):
                return True, line.strip()
    return False, ""


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", help="base ref to diff against (default: auto)")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="exit 1 (not 2) when a base ref cannot be resolved",
    )
    args = parser.parse_args(argv)

    base = _resolve_base(args.base)
    if base is None:
        msg = "freeze-guard: could not resolve a base ref (no origin/main or main)"
        if args.strict:
            print(_c("31", msg))
            return 1
        print(_c("33", f"{msg} — skipping (use --strict to fail)"))
        return 0

    changed = _changed_files(base)
    hits = sorted(p for p in changed if _is_frozen(p))

    if not hits:
        print(_c("32", f"freeze-guard: OK — no frozen orchestration paths changed (base {base})"))
        return 0

    overridden, marker = _has_override(base)
    if overridden:
        print(_c("33", f"freeze-guard: {len(hits)} frozen path(s) changed — OVERRIDE accepted"))
        print(f"  marker: {marker}")
        for p in hits:
            print(f"    · {p}")
        return 0

    print(_c("31", f"freeze-guard: BLOCKED — {len(hits)} frozen orchestration path(s) changed:"))
    for p in hits:
        print(_c("31", f"    · {p}"))
    print()
    print("These modules are the frozen platform API (ADR-0042 §D1). If this change is")
    print("an allowed class (bug/security/perf/observability/docs) or is authorised by a")
    print("new ADR (§D2), record it consciously via one of:")
    print("  • a commit trailer:  Freeze-Override: ADR-XXXX <reason>")
    print("  • env for local work: ALLOW_FROZEN_CHANGES=1")
    print("See docs/decisions/ADR-0042-orchestration-platform-freeze.md")
    return 1


if __name__ == "__main__":
    # Run from the repo root regardless of invocation cwd so git & path matching
    # are stable.
    repo_root = Path(__file__).resolve().parent.parent.parent
    os.chdir(repo_root)
    raise SystemExit(main(sys.argv[1:]))
