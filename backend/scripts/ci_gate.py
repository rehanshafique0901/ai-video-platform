"""Cross-platform CI quality gate runner.

Executes — in order, fail-fast — the ten stages mandated by ADR-0028:

    1. ruff check                (lint)
    2. black --check             (format)
    3. mypy + lint-imports       (static analysis)
    4. pytest                    (unit tests)
    5. alembic upgrade head      (forward migration)
    6. alembic downgrade base    (reverse migration)
    7. alembic upgrade head      (idempotency)
    8. validate_schema           (live structural checks)
    9. compare_erd               (generated vs design ERD)
   10. coverage                  (threshold enforcement)

Stages 1–4 and 10 are cheap and run on every PR. Stages 5–9 require a
live PostgreSQL with pgvector reachable via ``DATABASE_URL`` (in CI this
comes from a service container; locally it can point at Supabase
through ``.env.validation``). When ``DATABASE_URL`` is unset, stages
5–9 are *skipped* and reported as such — they are not silently
"passed", but they also do not fail the gate, so that contributors
without a local Postgres can still iterate on stages 1–4.

Usage
-----

    python scripts/ci_gate.py                # run everything
    python scripts/ci_gate.py --stages 1-4   # iterate locally on lint/types/tests
    python scripts/ci_gate.py --stages 5-9   # only live-DB stages
    python scripts/ci_gate.py --no-color     # plain output for CI logs

Exit code is 0 iff every executed stage succeeded.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = BACKEND_ROOT.parent


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

_USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _c(code: str, text: str) -> str:
    if not _USE_COLOR:
        return text
    return f"\033[{code}m{text}\033[0m"


def _banner(num: int, total: int, title: str) -> None:
    bar = "-" * 78
    print(f"\n{bar}")
    print(f"{_c('1;36', f'[{num}/{total}] {title}')}")
    print(bar, flush=True)


def _result(label: str, passed: bool, skipped: bool, elapsed_ms: int) -> None:
    if skipped:
        tag = _c("33", "SKIP")
    elif passed:
        tag = _c("32", " OK ")
    else:
        tag = _c("31", "FAIL")
    print(f"[{tag}] {label:<32} ({elapsed_ms} ms)")


# ---------------------------------------------------------------------------
# Stage definitions
# ---------------------------------------------------------------------------


@dataclass
class StageResult:
    number: int
    title: str
    passed: bool
    skipped: bool
    elapsed_ms: int
    skip_reason: str = ""


@dataclass
class Stage:
    number: int
    title: str
    # A stage either runs a subprocess (``cmd``) or invokes ``func``.
    cmd: Sequence[str] | None = None
    func: object = None
    # If ``requires_db`` is True, the stage is skipped when ``DATABASE_URL``
    # is empty. Stages 1–4 and 10 never require a DB.
    requires_db: bool = False
    # Working directory for the subprocess; defaults to backend root.
    cwd: Path = field(default_factory=lambda: BACKEND_ROOT)
    # Environment overlay; merged on top of ``os.environ``.
    env: dict[str, str] = field(default_factory=dict)


def _run_subprocess(stage: Stage) -> bool:
    env = os.environ.copy()
    env.update(stage.env)
    assert stage.cmd is not None
    try:
        proc = subprocess.run(
            list(stage.cmd),
            cwd=stage.cwd,
            env=env,
            check=False,
        )
    except FileNotFoundError as e:
        print(_c("31", f"command not found: {e}"))
        return False
    return proc.returncode == 0


def _run_compare_erd() -> bool:
    """Run the ERD comparator with the freshly generated artefact."""

    cmd = [
        sys.executable,
        str(BACKEND_ROOT / "scripts" / "compare_erd.py"),
        str(BACKEND_ROOT / ".validation" / "erd_generated.md"),
        str(REPO_ROOT / "docs" / "database" / "ERD.md"),
        str(BACKEND_ROOT / ".validation" / "erd_diff.json"),
    ]
    return subprocess.run(cmd, check=False).returncode == 0


def _run_static_analysis() -> bool:
    """Stage 3: mypy followed by lint-imports.

    Both tools are pure static analysis over ``app/``; running them in the
    same stage keeps the gate's "static" gate easy to identify in CI logs
    and ensures an import-contract regression cannot land just because
    mypy was happy.
    """

    mypy_ok = (
        subprocess.run(
            [sys.executable, "-m", "mypy"],
            cwd=BACKEND_ROOT,
            check=False,
        ).returncode
        == 0
    )
    if not mypy_ok:
        return False
    # import-linter ships as the `lint-imports` console script (no
    # `python -m importlinter` entry point). Resolve it relative to the
    # active interpreter so we don't depend on PATH.
    if (BACKEND_ROOT / ".venv" / "Scripts" / "lint-imports.exe").exists():
        lint_cmd = [str(BACKEND_ROOT / ".venv" / "Scripts" / "lint-imports.exe")]
    elif (BACKEND_ROOT / ".venv" / "bin" / "lint-imports").exists():
        lint_cmd = [str(BACKEND_ROOT / ".venv" / "bin" / "lint-imports")]
    elif shutil.which("lint-imports"):
        lint_cmd = ["lint-imports"]
    else:
        # Fall back to the package's internal CLI entrypoint module path.
        lint_cmd = [
            sys.executable,
            "-c",
            "from importlinter.cli import lint_imports; lint_imports()",
        ]
    linter_ok = subprocess.run(lint_cmd, cwd=BACKEND_ROOT, check=False).returncode == 0
    return linter_ok


def _run_coverage_report() -> bool:
    """Re-emit the coverage report and enforce the threshold.

    Stage 4 already wrote ``.coverage`` via ``pytest --cov``; here we just
    read it back and check ``fail_under``. Splitting this out keeps stage
    4's output focussed on test results and gives the threshold its own
    fail-line in CI logs.
    """

    if not (BACKEND_ROOT / ".coverage").exists():
        print(_c("33", "no .coverage file found — did stage 4 run?"))
        return False
    return (
        subprocess.run(
            [sys.executable, "-m", "coverage", "report"],
            cwd=BACKEND_ROOT,
            check=False,
        ).returncode
        == 0
    )


def _stages() -> list[Stage]:
    py = sys.executable
    return [
        Stage(
            number=1,
            title="ruff check (lint)",
            cmd=[py, "-m", "ruff", "check", "app", "tests", "scripts"],
        ),
        Stage(
            number=2,
            title="black --check (format)",
            cmd=[py, "-m", "black", "--check", "app", "tests", "scripts"],
        ),
        Stage(
            number=3,
            title="mypy + import-linter (static)",
            func=_run_static_analysis,
        ),
        Stage(
            number=4,
            title="pytest -m unit (+coverage)",
            cmd=[
                py,
                "-m",
                "pytest",
                "-m",
                "unit",
                "--cov=app",
                "--cov-report=term-missing:skip-covered",
                "--cov-report=xml:.validation/coverage.xml",
                "--no-cov-on-fail",
            ],
        ),
        Stage(
            number=5,
            title="alembic upgrade head",
            cmd=[py, "-m", "alembic", "upgrade", "head"],
            requires_db=True,
        ),
        Stage(
            number=6,
            title="alembic downgrade base",
            cmd=[py, "-m", "alembic", "downgrade", "base"],
            requires_db=True,
        ),
        Stage(
            number=7,
            title="alembic upgrade head (idempotency)",
            cmd=[py, "-m", "alembic", "upgrade", "head"],
            requires_db=True,
        ),
        Stage(
            number=8,
            title="schema validator",
            cmd=[
                py,
                "scripts/validate_schema.py",
                ".validation/schema_validation_report.json",
            ],
            requires_db=True,
        ),
        Stage(
            number=9,
            title="ERD comparison",
            func=_run_compare_erd,
            cmd=[
                py,
                "scripts/regenerate_erd.py",
                ".validation/erd_generated.md",
            ],
            requires_db=True,
        ),
        Stage(
            number=10,
            title="coverage report",
            func=_run_coverage_report,
        ),
    ]


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def _parse_stage_range(arg: str | None, total: int) -> set[int]:
    if not arg:
        return set(range(1, total + 1))
    selected: set[int] = set()
    for chunk in arg.split(","):
        chunk = chunk.strip()
        m = re.fullmatch(r"(\d+)-(\d+)", chunk)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
            selected.update(range(lo, hi + 1))
        elif chunk.isdigit():
            selected.add(int(chunk))
        else:
            raise SystemExit(f"invalid stage selector: {chunk!r}")
    return selected & set(range(1, total + 1))


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--stages",
        help="comma-separated stage numbers or ranges (e.g. '1-4,10'). Default = all.",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="disable ANSI colour codes (useful for CI logs without an attached TTY)",
    )
    args = parser.parse_args(argv)

    if args.no_color:
        global _USE_COLOR
        _USE_COLOR = False

    # Validation artefact directory must exist before stages 4 / 8 / 9 try
    # to write into it. ``mkdir(parents=True, exist_ok=True)`` is safe for
    # cold checkouts.
    (BACKEND_ROOT / ".validation").mkdir(parents=True, exist_ok=True)

    stages = _stages()
    total = len(stages)
    selected = _parse_stage_range(args.stages, total)

    db_available = (
        bool(os.environ.get("DATABASE_URL")) or (BACKEND_ROOT / ".env.validation").exists()
    )
    # Populate DATABASE_URL from backend/.env.validation if present. Without
    # this, stages 5-9 would silently fall back to alembic.ini's localhost URL.
    # _load_env.load() is idempotent and resolves via sys.path[0] (backend/scripts).
    if db_available and not os.environ.get("DATABASE_URL"):
        from _load_env import load as _load_validation_env

        _load_validation_env()
    if not db_available:
        print(
            _c(
                "33",
                "note: DATABASE_URL not set and backend/.env.validation absent - live-DB stages will be skipped",
            )
        )

    results: list[StageResult] = []
    started = time.time()

    for stage in stages:
        if stage.number not in selected:
            continue
        _banner(stage.number, total, stage.title)
        skipped = False
        skip_reason = ""
        ok = True
        t0 = time.time()
        if stage.requires_db and not db_available:
            skipped = True
            skip_reason = "DATABASE_URL absent"
        elif stage.func is not None:
            # Stage 9 needs to regenerate the ERD first (subprocess), then
            # run the comparator (callable). Keep the order explicit.
            if stage.cmd is not None:
                ok = _run_subprocess(stage)
            if ok:
                ok = bool(stage.func())  # type: ignore[operator]
        else:
            ok = _run_subprocess(stage)
        elapsed_ms = int((time.time() - t0) * 1000)
        _result(stage.title, ok, skipped, elapsed_ms)
        results.append(
            StageResult(
                number=stage.number,
                title=stage.title,
                passed=ok,
                skipped=skipped,
                elapsed_ms=elapsed_ms,
                skip_reason=skip_reason,
            )
        )
        if not skipped and not ok:
            # Fail-fast: do not run subsequent stages on first failure.
            break

    elapsed_total_ms = int((time.time() - started) * 1000)
    print("\n" + "=" * 78)
    print(_c("1", f"CI gate summary  ·  {elapsed_total_ms} ms total"))
    print("=" * 78)
    overall_pass = True
    for r in results:
        _result(f"stage {r.number} · {r.title}", r.passed, r.skipped, r.elapsed_ms)
        if not r.skipped and not r.passed:
            overall_pass = False
    if not overall_pass:
        print(_c("31", "\nResult: FAILED"))
        return 1
    # If anything was skipped, surface it as a warning but exit 0 so the
    # local-dev path stays unblocked.
    if any(r.skipped for r in results):
        print(_c("33", "\nResult: PASSED (some stages were skipped — see notes)"))
    else:
        print(_c("32", "\nResult: PASSED"))
    return 0


if __name__ == "__main__":
    # Hard-fail early if mandatory dev tools are missing — gives a clearer
    # error than letting subprocess explode mid-run.
    missing = [
        tool
        for tool in ("ruff", "black", "mypy", "pytest", "alembic", "coverage")
        if shutil.which(tool) is None
        and not (BACKEND_ROOT / ".venv" / "Scripts" / f"{tool}.exe").exists()
        and not (BACKEND_ROOT / ".venv" / "bin" / tool).exists()
    ]
    if missing:
        # Don't fail outright — they may be invokable via ``python -m`` and
        # the subprocess driver explicitly uses ``sys.executable -m TOOL``
        # for that reason. Just warn so a fresh checkout knows.
        print(
            _c(
                "33",
                f"note: the following tools are not on PATH and will be invoked via 'python -m': {missing}",
            )
        )
    raise SystemExit(main(sys.argv[1:]))
