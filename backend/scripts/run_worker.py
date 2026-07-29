"""Worker process entry point — the thing that actually runs background work (α9.8, ADR-0053).

``app.main`` turns HTTP requests into application calls; this turns elapsed time into them. Before
this script existed, nothing did: every worker's ``run_once()`` had zero production callers, so the
outbox never relayed, no video ever rendered, and no email was ever sent outside a test.

Usage::

    python scripts/run_worker.py                        # every enabled worker (ADR-0053 D1-B)
    python scripts/run_worker.py --workers generation   # one process class, e.g. a GPU node
    python scripts/run_worker.py --list

Deployment is one image, many process classes: the same container runs the API or any subset of
workers depending on its command. Splitting a worker onto its own nodes is a deployment change, not
a code change — which is D1-B's whole point.

Two behaviours are load-bearing and easy to lose in a refactor:

**Boot mirrors the API exactly** — ``configure_logging`` then ``container.init``, with the same
``SELECT 1`` fail-fast probe, so a misconfigured ``DATABASE_URL`` kills the process at boot instead
of surfacing as an inexplicable stream of per-pass failures.

**The Windows Selector loop** — ``psycopg``'s async driver cannot use the default Proactor loop.
``run_dev.py`` already solves this; this script mirrors it rather than inventing a second strategy.

Exit codes: ``0`` clean drain, ``1`` startup failure, ``70`` (``EX_SOFTWARE``) a worker could not be
kept alive and was escalated, ``75`` (``EX_TEMPFAIL``) a pass was abandoned at its drain budget. The
last two are separate on purpose. An abandoned pass is work that will be retried, but for generation
it is spend a creator has already paid for (GEN-2), so a deploy that causes one should be visible in
the orchestrator rather than indistinguishable from a clean shutdown. An escalated worker is worse:
it stopped running and stayed stopped, which ADR-0053 D5 forbids doing quietly.
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
from collections.abc import Callable
from pathlib import Path

import structlog
from sqlalchemy import text

from app.core import container
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.runtime.liveness import Liveness
from app.runtime.worker_host import WorkerHost
from app.runtime.worker_registry import WORKER_NAMES, UnknownWorkerError, build_registry

EXIT_OK = 0
EXIT_STARTUP_FAILED = 1
EXIT_WORKER_ESCALATED = 70  # EX_SOFTWARE
EXIT_DRAIN_INCOMPLETE = 75  # EX_TEMPFAIL

_STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM)


def _loop_factory() -> Callable[[], asyncio.AbstractEventLoop] | None:
    """Return an explicit Selector loop factory on Windows, else the default (see ``run_dev``)."""
    if sys.platform == "win32":
        return asyncio.SelectorEventLoop
    return None


def _install_signal_handlers(host: WorkerHost) -> None:
    """Route SIGINT/SIGTERM to a graceful stop request.

    ``add_signal_handler`` is unavailable on Windows and raises there; falling back to
    ``signal.signal`` keeps local development on Windows usable, where deployment signals are not a
    concern anyway.
    """
    loop = asyncio.get_running_loop()
    for sig in _STOP_SIGNALS:
        try:
            loop.add_signal_handler(sig, host.request_stop)
        except NotImplementedError:
            signal.signal(sig, lambda _sig, _frame: host.request_stop())


async def _run(settings: Settings, selected: frozenset[str] | None) -> int:
    logger = structlog.get_logger(__name__)
    try:
        try:
            specs = build_registry(settings, selected=selected)
        except UnknownWorkerError:
            logger.error("worker_process.unknown_worker", exc_info=True)
            return EXIT_STARTUP_FAILED

        if not specs:
            # A coherent request with nothing to do — ``--workers email`` while delivery is
            # disabled. Exiting loudly beats idling forever in a process that can never act.
            logger.error("worker_process.no_workers_enabled", selected=sorted(selected or ()))
            return EXIT_STARTUP_FAILED

        try:
            engine = container.get_engine()
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        except Exception:
            logger.error("worker_process.startup_failed", exc_info=True)
            return EXIT_STARTUP_FAILED

        liveness_dir = settings.worker_liveness_dir
        host = WorkerHost(specs, liveness=Liveness(Path(liveness_dir)) if liveness_dir else None)
        logger.info("worker_process.started", workers=[spec.name for spec in specs])

        _install_signal_handlers(host)
        result = await host.run()

        logger.info(
            "worker_process.stopped",
            abandoned=result.abandoned_any,
            escalated=[w.name for w in result.workers if w.escalated],
        )
        # Escalation outranks an incomplete drain: an abandoned pass is work that will be retried,
        # whereas an escalated worker stopped running and stayed stopped. ADR-0053 D5 forbids that
        # being quiet, and a distinct non-zero exit is what a supervisor or orchestrator can see.
        if result.escalated_any:
            return EXIT_WORKER_ESCALATED
        return EXIT_DRAIN_INCOMPLETE if result.abandoned_any else EXIT_OK
    finally:
        await container.shutdown()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run background workers (α9.8, ADR-0053).")
    parser.add_argument(
        "--workers",
        nargs="+",
        metavar="NAME",
        help=f"Workers to run. Default: all enabled. Known: {', '.join(WORKER_NAMES)}",
    )
    parser.add_argument(
        "--list", action="store_true", help="Print the known worker names and exit."
    )
    args = parser.parse_args()

    if args.list:
        print("\n".join(WORKER_NAMES))
        return EXIT_OK

    settings = get_settings()
    configure_logging(settings.log_level, settings.environment)
    container.init(settings)

    selected = frozenset(args.workers) if args.workers else None
    return asyncio.run(_run(settings, selected), loop_factory=_loop_factory())


if __name__ == "__main__":
    raise SystemExit(main())
