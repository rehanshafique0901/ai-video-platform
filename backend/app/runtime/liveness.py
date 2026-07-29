"""Touch-file liveness markers for the worker host (α9.8, ADR-0053 D5 / PF9).

The host has no HTTP surface, so a standard readiness probe cannot reach it. A marker file per
worker, refreshed after every pass, lets an exec-style probe tell "idle" from "wedged" — the
distinction logs alone cannot make, because a wedged worker and an idle one are both silent.

**One file per worker**, not one per host: a single stuck worker must be detectable while the
others keep the process looking healthy.

The marker means *the loop is turning*, not *the work succeeded* — it is refreshed after a
contained failure too, because a worker that is failing and retrying is alive. Delivery outcomes
are the workers' business (ADR-0053 invariant 2); liveness is only about the scheduler.
"""

from __future__ import annotations

from pathlib import Path

import structlog

_LOGGER = structlog.get_logger(__name__)


class Liveness:
    """Refreshes one marker file per worker. Never raises — a probe is not worth a crash."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    def touch(self, worker: str) -> None:
        """Refresh ``<directory>/<worker>.alive``. Failures are logged and swallowed.

        A liveness marker is diagnostics. If the directory is read-only or the disk is full, the
        correct behaviour is to keep processing work and let the probe go stale — never to take
        down a worker that is otherwise healthy.
        """
        try:
            self._directory.mkdir(parents=True, exist_ok=True)
            (self._directory / f"{worker}.alive").touch()
        except OSError:
            _LOGGER.warning("worker.liveness_touch_failed", worker=worker, exc_info=True)
