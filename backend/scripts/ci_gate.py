"""Cross-platform CI quality gate runner.

Executes — in order, fail-fast — the stages mandated by ADR-0028 (+ the α8.5c
provider pre-flight):

    0. validate_providers        (capability/provider manifest — offline, no DB)
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
   11. seed_roundtrip            (α8.5d — provider-catalogue seed idempotency)
   12. runtime_integration       (α8.5e/α8.6 — readers + resolver + ledger + exec repos)
   13. generation_e2e            (α8.6 Increment 5 — full Prompt→…→FFmpeg→MP4 slice)
   14. publishing_integration    (α8.6a/b — social accounts + publish runtime + APIs)
   15. asset_promotion_bridge    (α8.8 — generation_assets → media_assets X8 seam)
   16. publish_notifications     (α8.9a — publish terminal events → in-app notifications)

Stage 0 is a fast, no-DB pre-flight (α8.5c) that runs before everything so a
manifest regression fails cheaply before the DB round-trip. It is numbered 0 —
rather than renumbering 1-10 — precisely so the destructive-stage restoration
guard, which keys on "stage 6 = downgrade" and the live-DB range 5-9, stays
correct. Stage 11 (α8.5d seed round-trip) is likewise appended at the end — it
needs the schema at head (post stage-7) and seeds only the eight catalogue
tables, so it can never disturb the destructive-migration guard. Stage 12
(α8.5e runtime integration) runs last: it needs the schema at head and the
catalogue seeded (stage 11), then exercises the Decision- + Execution-plane
repositories (catalogue/runtime readers → resolver → resolution ledger → exec
runtime repos) against the live DB so a "works locally, fails in CI" runtime
regression cannot reach main. Its tests roll back inside a SAVEPOINT, so it too
leaves the guard untouched. Stage 13 (α8.6 Increment 5) then runs the generation
feature slice end-to-end (Prompt→Planner→Resolver→Generate→Verify→Repair→
Timeline→FFmpeg→MP4→persistence); it is kept OUT of Stage 12 to honour that
stage's infrastructure-only scope freeze. It commits (its store owns its own
sessions) and deletes the rows it created on teardown, and auto-skips when
ffmpeg/ffprobe are absent — so it also leaves the restoration guard untouched.

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
    python scripts/ci_gate.py --ephemeral-db # live stages on a throwaway container
    python scripts/ci_gate.py --no-color     # plain output for CI logs

Exit code is 0 iff every executed stage succeeded.

Validation-DB safety
--------------------

Stage 6 (``alembic downgrade base``) is *destructive*: a transient failure
between it and the stage-7 re-upgrade can leave a **persistent** database
empty. Two independent guards make this safe:

* **Isolation** — target a throwaway database so a transient failure is
  inert. Precedence: ``--ephemeral-db`` / ``CI_GATE_EPHEMERAL_DB=1`` (a
  Docker ``pgvector/pgvector:pg16`` created before and destroyed after the
  run) → ``VALIDATION_DATABASE_URL`` (a dedicated DB) → ``DATABASE_URL``.
  The first two never touch the primary ``DATABASE_URL``.
* **Self-healing** — whenever a downgrade may have run against a *persistent*
  DB, a bounded-retry ``alembic upgrade head`` runs on **every** exit path
  (success, failure, or Ctrl-C) and the gate refuses to report success until
  it has *verified* the DB is back at head.
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

# Validation-DB isolation + restoration guard tunables.
_EPHEMERAL_IMAGE = "pgvector/pgvector:pg16"
_EPHEMERAL_READY_TIMEOUT_S = 60
_RESTORE_RETRIES = 8
_RESTORE_BACKOFF_S = 6


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


# ---------------------------------------------------------------------------
# Validation-DB isolation + restoration guard
#
# Destructive migration verification (stage 6 `downgrade base`) must never be
# able to leave a *persistent* database in an intermediate state — the failure
# mode observed once when a transient network error hit between stage 6 and the
# stage-7 re-upgrade against the shared validation DB. See the module docstring
# for the two mitigations (isolation + self-healing) implemented here.
# ---------------------------------------------------------------------------


def _revisions(cmd_tail: list[str]) -> set[str]:
    """Return the set of alembic revision ids printed by ``alembic <cmd_tail>``.

    Used to compare ``current`` against ``heads``. On any non-zero exit
    (e.g. a transient connection failure) an empty set is returned, which the
    caller treats as "not at head" and therefore worth another restore pass.
    """

    proc = subprocess.run(
        [sys.executable, "-m", "alembic", *cmd_tail],
        cwd=BACKEND_ROOT,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return set()
    return {line.split()[0] for line in proc.stdout.splitlines() if line.strip()}


def _verify_db_at_head() -> tuple[bool, str]:
    """Return ``(at_head, head_revision)`` for the target database."""

    heads = _revisions(["heads"])
    current = _revisions(["current"])
    at_head = bool(heads) and current == heads
    return at_head, ", ".join(sorted(heads)) if heads else "<unknown>"


def _ensure_db_at_head() -> tuple[bool, str]:
    """Bounded-retry ``alembic upgrade head``, tolerant of transient failures.

    Returns ``(restored, head_revision)``. This is the self-healing guard: it
    runs on every exit path once a downgrade may have executed, so a network
    blip can no longer leave a persistent validation DB at ``base``.
    """

    rev = "<unknown>"
    for attempt in range(1, _RESTORE_RETRIES + 1):
        rc = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=BACKEND_ROOT,
            env=os.environ.copy(),
            check=False,
        ).returncode
        if rc == 0:
            ok, rev = _verify_db_at_head()
            if ok:
                return True, rev
        if attempt < _RESTORE_RETRIES:
            print(
                _c(
                    "33",
                    f"  restore attempt {attempt}/{_RESTORE_RETRIES} did not reach "
                    f"head — retrying in {_RESTORE_BACKOFF_S}s…",
                ),
                flush=True,
            )
            time.sleep(_RESTORE_BACKOFF_S)
    ok, rev = _verify_db_at_head()
    return ok, rev


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def _stop_ephemeral_db(container_id: str) -> None:
    subprocess.run(
        ["docker", "rm", "-f", container_id],
        capture_output=True,
        text=True,
        check=False,
    )


def _wait_ephemeral_ready(container_id: str) -> None:
    deadline = time.time() + _EPHEMERAL_READY_TIMEOUT_S
    while time.time() < deadline:
        rc = subprocess.run(
            ["docker", "exec", container_id, "pg_isready", "-U", "aivp", "-d", "aivp"],
            capture_output=True,
            text=True,
            check=False,
        ).returncode
        if rc == 0:
            return
        time.sleep(1)
    _stop_ephemeral_db(container_id)
    raise RuntimeError(f"ephemeral DB did not become ready within {_EPHEMERAL_READY_TIMEOUT_S}s")


def _start_ephemeral_db() -> tuple[str, str]:
    """Start a throwaway ``pgvector`` Postgres; return ``(container_id, url)``.

    The container publishes 5432 on a random loopback host port and is created
    with ``--rm``; the caller must still call :func:`_stop_ephemeral_db` in a
    ``finally`` so it is removed even if the run is interrupted.
    """

    name = f"aivp-ci-gate-{os.getpid()}"
    print(
        _c("36", f"  starting ephemeral Postgres ({_EPHEMERAL_IMAGE})…"),
        flush=True,
    )
    run = subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            name,
            "-e",
            "POSTGRES_USER=aivp",
            "-e",
            "POSTGRES_PASSWORD=aivp",
            "-e",
            "POSTGRES_DB=aivp",
            "-p",
            "127.0.0.1::5432",
            _EPHEMERAL_IMAGE,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if run.returncode != 0:
        raise RuntimeError(run.stderr.strip() or "docker run failed")
    container_id = run.stdout.strip()
    inspect = subprocess.run(
        [
            "docker",
            "inspect",
            "--format",
            '{{ (index (index .NetworkSettings.Ports "5432/tcp") 0).HostPort }}',
            container_id,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    port = inspect.stdout.strip()
    if inspect.returncode != 0 or not port:
        _stop_ephemeral_db(container_id)
        raise RuntimeError("could not determine ephemeral DB host port")
    _wait_ephemeral_ready(container_id)
    url = f"postgresql+psycopg://aivp:aivp@127.0.0.1:{port}/aivp"
    print(_c("32", f"  ephemeral Postgres ready on 127.0.0.1:{port}"), flush=True)
    return container_id, url


def _stages() -> list[Stage]:
    py = sys.executable
    return [
        Stage(
            number=0,
            title="provider manifest (capability registry)",
            cmd=[
                py,
                "scripts/validate_providers.py",
                ".validation/provider_validation_report.json",
            ],
        ),
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
        Stage(
            number=11,
            title="provider catalogue seed round-trip",
            cmd=[py, "scripts/verify_seed_roundtrip.py"],
            requires_db=True,
        ),
        # Stage 12 (α8.5e / α8.6) — Integration Runtime Verification. Proves a
        # freshly migrated + seeded database (stages 5-7 + 11) can actually drive
        # the runtime: the raw-SQL catalogue/runtime readers materialise snapshots,
        # the resolver resolves against them, the resolution ledger persists
        # provenance, and the Execution Runtime ledger/asset/model-cache repos
        # round-trip (enum casts, jsonb, self-FK lineage, ON CONFLICT upserts).
        # Narrow, Decision- + Execution-plane repositories only, so it stays fast
        # and answers one question: "can a real DB support the runtime?" Each test
        # runs inside a SAVEPOINT that rolls back on teardown. Business-feature
        # e2e tests do NOT belong here (Stage 12 scope freeze) — see Stage 13.
        Stage(
            number=12,
            title="runtime integration verification",
            cmd=[
                py,
                "-m",
                "pytest",
                "-m",
                "integration",
                "tests/integration/infrastructure/repositories/test_catalogue_reader.py",
                "tests/integration/infrastructure/repositories/test_runtime_state_reader.py",
                "tests/integration/infrastructure/repositories/test_resolver_integration.py",
                # α8.6 Increment 4 — Execution Runtime ledger/asset/model-cache repos.
                "tests/integration/infrastructure/repositories/"
                "test_execution_runtime_repositories.py",
            ],
            requires_db=True,
        ),
        # Stage 13 (α8.6 Increment 5) — Generation feature-slice end-to-end. Kept
        # OUT of Stage 12 to honour that stage's scope freeze (infrastructure /
        # persistence boundaries only): this is a *business-feature* test that
        # drives the whole vertical slice — Prompt → Planner → Resolver → Generate
        # → Verify → Repair → Timeline → FFmpeg → MP4 → persistence — against a DB
        # at head + seeded (stages 5-7 + 11). The offline image generator keeps it
        # hermetic (no provider network); it still exercises real ffmpeg/ffprobe.
        # Unlike Stage 12 it commits (the Execution-Runtime store owns its own
        # sessions) and deletes the rows it created on teardown, so it too leaves
        # the destructive-migration restoration guard untouched. Auto-skips if
        # ffmpeg/ffprobe are absent.
        Stage(
            number=13,
            title="generation end-to-end slice",
            cmd=[
                py,
                "-m",
                "pytest",
                "-m",
                "integration",
                "tests/integration/infrastructure/generation/test_generation_end_to_end.py",
            ],
            requires_db=True,
        ),
        # Stage 14 (α8.6a + α8.6b) — Publishing integration. Proves the publishing
        # bounded context persists and enforces its boundaries against a real DB at
        # head (stages 5-7):
        #   α8.6a — the SocialAccount repository round-trips owner-scoped
        #   upsert/list/revoke; the envelope-encrypting SocialCredential service
        #   stores/authorizes/revokes with NO plaintext token ever landing in the
        #   database (ADR-0047 C1/C6); and the /social-accounts router enforces auth +
        #   validation end-to-end.
        #   α8.6b — the PublishJob repository round-trips owner-scoped create/CAS + the
        #   (source_media_asset, social_account) idempotency backstop + source
        #   resolution; the publish runtime drives create → worker → succeeded with
        #   terminal events against the Mock destination (credential-blind, PUB-5); and
        #   the /publish-jobs router enforces auth + validation.
        # Kept OUT of Stage 12 (its scope freeze) and Stage 13 (generation slice) —
        # publishing is its own bounded context, so it gets its own stage per
        # PUBLISHING_RUNTIME_CONTRACT.md §13. Each test rolls back on teardown; no
        # destructive-migration guard interaction.
        Stage(
            number=14,
            title="publishing integration verification",
            cmd=[
                py,
                "-m",
                "pytest",
                "-m",
                "integration",
                "tests/integration/infrastructure/repositories/"
                "test_social_account_repository.py",
                "tests/integration/infrastructure/publishing/" "test_social_credential_service.py",
                "tests/integration/api/test_social_accounts.py",
                "tests/integration/infrastructure/repositories/test_publish_job_repository.py",
                "tests/integration/infrastructure/publishing/test_publish_runtime_end_to_end.py",
                "tests/integration/api/test_publish_jobs.py",
            ],
            requires_db=True,
        ),
        # Stage 15 (α8.8) — Asset Promotion Bridge. Proves the ADR-0046 X8 seam against a
        # real DB at head: the `PromoteGenerationAssets` use case reads a committed
        # execution-plane generation via the read-only GenerationReader, copies the
        # finished bytes through the storage resolver, and registers an owner-scoped
        # media_assets(source='generated') row via a real SqlAlchemyUnitOfWork — then a
        # re-promotion is an idempotent noop (storage-coordinate uniqueness, no migration).
        # Kept OUT of Stage 12 (infra-only freeze), Stage 13 (generation slice), and Stage
        # 14 (publishing) — promotion is the cross-plane bridge, so it gets its own stage.
        # Like Stage 13 it commits (its UoW + reader own their sessions) and deletes the
        # rows it created on teardown, so it leaves the destructive-migration guard untouched.
        Stage(
            number=15,
            title="asset promotion bridge integration",
            cmd=[
                py,
                "-m",
                "pytest",
                "-m",
                "integration",
                "tests/integration/infrastructure/media/test_asset_promotion_bridge.py",
            ],
            requires_db=True,
        ),
        # Stage 16 (α8.9a) — Publish Notifications (deferred DQ7 fan-out). Proves the
        # publish notification projection against a real DB at head: a PublishJobSucceeded /
        # PublishJobFailed outbox event is projected — through the reused CreateNotification
        # writer — into exactly one owner-scoped notifications row, with the DB-owned
        # (user_id, source_event_id) index enforcing exactly-once under redelivery, and the
        # projected notification visible through the real /api/v1/notifications read API.
        # Kept OUT of Stage 14 (publishing runtime) since this is the notifications bounded
        # context reacting to publish events — a downstream fan-out consumer, not the publish
        # runtime. Like Stages 13-15 the writer commits its own UoW and the tests delete the
        # rows they created on teardown, so the destructive-migration guard is untouched.
        Stage(
            number=16,
            title="publish notifications integration",
            cmd=[
                py,
                "-m",
                "pytest",
                "-m",
                "integration",
                "tests/integration/infrastructure/notifications/"
                "test_publish_notification_projection.py",
            ],
            requires_db=True,
        ),
    ]


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def _parse_stage_range(arg: str | None, valid: set[int]) -> set[int]:
    if not arg:
        return set(valid)
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
    return selected & valid


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
    parser.add_argument(
        "--ephemeral-db",
        action="store_true",
        help=(
            "run the live-DB stages against a throwaway Docker Postgres "
            "(pgvector/pgvector:pg16) created before and destroyed after the run "
            "— isolates destructive migration verification from any shared DB"
        ),
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
    selected = _parse_stage_range(args.stages, {s.number for s in stages})

    # --- Resolve the target database for the live-DB stages (5-9) ----------
    # Precedence: --ephemeral-db  >  VALIDATION_DATABASE_URL  >  DATABASE_URL /
    # backend/.env.validation. The first two never touch the primary
    # DATABASE_URL, so destructive verification is isolated from any shared DB.
    live_selected = any(s.requires_db and s.number in selected for s in stages)
    ephemeral_requested = args.ephemeral_db or os.environ.get(
        "CI_GATE_EPHEMERAL_DB", ""
    ).strip().lower() in {"1", "true", "yes"}
    ephemeral_cid: str | None = None
    using_isolated_db = False

    if ephemeral_requested and live_selected:
        if not _docker_available():
            print(_c("31", "error: --ephemeral-db requested but 'docker' is not on PATH"))
            return 1
        try:
            ephemeral_cid, ephemeral_url = _start_ephemeral_db()
        except RuntimeError as exc:
            print(_c("31", f"error: could not start ephemeral DB: {exc}"))
            return 1
        os.environ["DATABASE_URL"] = ephemeral_url
        using_isolated_db = True
    elif os.environ.get("VALIDATION_DATABASE_URL"):
        os.environ["DATABASE_URL"] = os.environ["VALIDATION_DATABASE_URL"]
        using_isolated_db = True
        print(
            _c(
                "36",
                "note: live-DB stages target VALIDATION_DATABASE_URL "
                "(dedicated validation DB; primary DATABASE_URL left untouched)",
            )
        )
    elif not os.environ.get("DATABASE_URL") and (BACKEND_ROOT / ".env.validation").exists():
        # Populate DATABASE_URL from backend/.env.validation if present.
        # Without this, stages 5-9 would silently fall back to alembic.ini's
        # localhost URL. _load_env.load() is idempotent.
        from _load_env import load as _load_validation_env

        _load_validation_env()

    db_available = bool(os.environ.get("DATABASE_URL"))
    if not db_available:
        print(
            _c(
                "33",
                "note: DATABASE_URL not set and backend/.env.validation absent - live-DB stages will be skipped",
            )
        )

    # A downgrade (stage 6) against a *persistent* DB is the only thing that
    # can leave the schema mid-flight; when that runs unisolated, warn and lean
    # on the restoration guard below.
    destructive_selected = 6 in selected
    if destructive_selected and db_available and not using_isolated_db:
        print(
            _c(
                "33",
                "\nWARNING: destructive migration verification (stage 6 "
                "'downgrade base') will run against the primary DATABASE_URL.\n"
                "         A restoration guard re-upgrades to head on exit, but "
                "for full isolation prefer --ephemeral-db or "
                "VALIDATION_DATABASE_URL.",
            )
        )

    results: list[StageResult] = []
    started = time.time()
    restore_ok: bool | None = None
    restored_rev = ""

    try:
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
    finally:
        # Guaranteed cleanup — runs on success, failure, and Ctrl-C.
        if ephemeral_cid is not None:
            print(_c("36", "\ntearing down ephemeral Postgres…"), flush=True)
            _stop_ephemeral_db(ephemeral_cid)
        elif destructive_selected and db_available:
            print(
                _c("36", "\nverifying validation DB is restored to head…"),
                flush=True,
            )
            restore_ok, restored_rev = _ensure_db_at_head()

    elapsed_total_ms = int((time.time() - started) * 1000)
    print("\n" + "=" * 78)
    print(_c("1", f"CI gate summary  ·  {elapsed_total_ms} ms total"))
    print("=" * 78)
    overall_pass = True
    for r in results:
        _result(f"stage {r.number} · {r.title}", r.passed, r.skipped, r.elapsed_ms)
        if not r.skipped and not r.passed:
            overall_pass = False
    if restore_ok is True:
        print(_c("32", f"[ OK ] DB restored & verified at head: {restored_rev}"))
    elif restore_ok is False:
        overall_pass = False
        print(
            _c(
                "31",
                f"[FAIL] DB NOT restored to head ({restored_rev}) — run "
                "'alembic upgrade head' manually before trusting this database",
            )
        )
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
