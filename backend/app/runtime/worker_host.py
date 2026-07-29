"""``WorkerHost`` — the supervisor that finally makes background work run (α9.8, ADR-0053).

Every poll primitive on the platform — the relay, render, export, enrichment, publish, email, and
generation — was written as a ``run_once()`` library call with **no production caller**. This class
is that caller.

**It knows nothing about any of them.** A pass is an opaque ``Callable[[], Awaitable[object]]`` and
its result is an opaque ``object`` handed straight back to the spec's own ``found_work`` predicate.
The supervisor cannot import a worker, a use case, or a result type, which is what keeps ADR-0053
invariant 1 (*the host schedules; it never decides*) structural rather than aspirational. All type
knowledge lives in ``worker_registry``, the composition root.

The division it implements is ADR-0053 invariant 2, the **worker contract**: a worker processes
available work and nothing else. Scheduling, supervision, restart, runtime bounding, and shutdown
coordination are this module's job. **A worker that raises has not violated its contract** — which
matters, because four of the seven let exceptions escape ``run_once()`` outright.

Two host invariants shape the implementation:

**HOST-1 — registration is immutable for the process lifetime.** The spec set is frozen at
construction and never re-read, so the task set is fixed the moment :meth:`run` begins. Shutdown
drains a known set, liveness refreshes a known set, and configuration changes take effect on the
next process start, exactly as they do for the API.

**HOST-2 — one worker's failure never suppresses another's scheduling.** Each spec gets its own
task, its own backoff state, and its own drain budget; nothing awaits across workers. The only
cross-worker coupling permitted is the stop signal. Collapsing this into a single loop would satisfy
every other requirement here while silently reintroducing the coupling ADR-0053 D2-A rejected.

Shutdown is ADR-0053 D3: **stop claiming, then bounded drain.** On stop, no worker starts another
pass — an idling worker wakes immediately rather than serving out its ceiling — and the single pass
already in flight gets its budget before being cancelled. Abandonment is logged and reported, never
silent, because for generation an abandoned pass is money the creator has already spent (GEN-2).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import structlog

from app.runtime.liveness import Liveness

_LOGGER = structlog.get_logger(__name__)

_BACKOFF_FACTOR = 2.0
_DEFAULT_FAILURE_CAP = timedelta(seconds=300)


@dataclass(frozen=True, slots=True)
class WorkerSpec[T]:
    """One supervised worker, described entirely in terms the supervisor can handle opaquely.

    ``run_pass`` builds *and* runs exactly one pass — a fresh worker per pass, since every
    container factory already returns a new unit of work per call, so nothing leaks between passes.

    ``found_work`` is supplied by the registry precisely so the supervisor never introspects a
    result. The spec is generic in ``T`` so the registry keeps full type checking over the seven
    concrete result types while the host only ever handles ``WorkerSpec[Any]`` — the knowledge stays
    on the registry's side of the boundary instead of being erased on both.
    """

    name: str
    # A coroutine rather than a bare awaitable: the host supervises each pass as its own task, and
    # only a coroutine can be handed to ``create_task``.
    run_pass: Callable[[], Coroutine[Any, Any, T]]
    found_work: Callable[[T], bool]
    interval: timedelta
    idle_ceiling: timedelta
    drain_budget: timedelta


@dataclass(frozen=True, slots=True)
class WorkerReport:
    """What one worker did over the life of the process."""

    name: str
    passes: int
    failures: int
    abandoned: bool


@dataclass(frozen=True, slots=True)
class HostResult:
    """Aggregate outcome, enough for an entrypoint to pick an exit code and nothing more."""

    workers: list[WorkerReport] = field(default_factory=list)

    @property
    def abandoned_any(self) -> bool:
        """True if any worker was cut off mid-pass at its drain budget (ADR-0053 D3)."""
        return any(w.abandoned for w in self.workers)


@dataclass(frozen=True, slots=True)
class _PassOutcome:
    """Internal: how one pass ended. ``result`` is meaningful only when it ended normally."""

    result: Any
    failed: bool
    abandoned: bool


class WorkerHost:
    """Supervises a fixed set of workers until stopped, then drains within bounded budgets."""

    def __init__(
        self,
        specs: Sequence[WorkerSpec[Any]],
        *,
        liveness: Liveness | None = None,
        failure_backoff_cap: timedelta = _DEFAULT_FAILURE_CAP,
    ) -> None:
        if not specs:
            raise ValueError("worker host requires at least one worker")
        names = [spec.name for spec in specs]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"duplicate worker names: {', '.join(duplicates)}")

        # HOST-1: the set is fixed here and never re-read.
        self._specs: tuple[WorkerSpec[Any], ...] = tuple(specs)
        self._liveness = liveness
        self._failure_cap = failure_backoff_cap
        self._stopping = asyncio.Event()

    def request_stop(self) -> None:
        """Ask every worker to finish its current pass and stop. Idempotent; safe from a signal."""
        if not self._stopping.is_set():
            _LOGGER.info("worker_host.stop_requested")
            self._stopping.set()

    async def run(self) -> HostResult:
        """Run every registered worker until stop is requested, then drain and report."""
        _LOGGER.info("worker_host.started", workers=[spec.name for spec in self._specs])

        # HOST-2: one task per worker. Nothing here awaits across workers, so a slow, failing, or
        # wedged worker cannot delay any other worker's next pass.
        tasks = [
            asyncio.create_task(self._supervise(spec), name=f"worker:{spec.name}")
            for spec in self._specs
        ]
        reports = await asyncio.gather(*tasks)

        result = HostResult(workers=list(reports))
        _LOGGER.info(
            "worker_host.stopped",
            abandoned=result.abandoned_any,
            passes={r.name: r.passes for r in reports},
            failures={r.name: r.failures for r in reports if r.failures},
        )
        return result

    async def _supervise(self, spec: WorkerSpec[Any]) -> WorkerReport:
        """Poll one worker forever: run a pass, adjust the delay, sleep, repeat."""
        passes = 0
        failures = 0
        abandoned = False

        # Idle and failure backoff are tracked separately, so a worker that keeps failing backs off
        # even while its queue is full — the realistic way a host would otherwise melt the database.
        idle_delay = spec.interval
        failure_delay = spec.interval

        while not self._stopping.is_set():
            outcome = await self._run_one_pass(spec)
            passes += 1

            if outcome.abandoned:
                abandoned = True
                self._touch(spec.name)
                break

            if outcome.failed:
                failures += 1
                delay = failure_delay
                failure_delay = self._grow(failure_delay, self._failure_cap)
            else:
                failure_delay = spec.interval
                if spec.found_work(outcome.result):
                    idle_delay = spec.interval
                else:
                    idle_delay = self._grow(idle_delay, spec.idle_ceiling)
                delay = idle_delay

            self._touch(spec.name)

            if self._stopping.is_set():
                break
            await self._sleep(delay)

        return WorkerReport(name=spec.name, passes=passes, failures=failures, abandoned=abandoned)

    async def _run_one_pass(self, spec: WorkerSpec[Any]) -> _PassOutcome:
        """Run one pass, applying the drain budget only if stop arrives while it is in flight."""
        task = asyncio.create_task(spec.run_pass(), name=f"pass:{spec.name}")
        stop_watch = asyncio.create_task(self._stopping.wait(), name=f"stop-watch:{spec.name}")
        try:
            await asyncio.wait({task, stop_watch}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stop_watch.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stop_watch

        if not task.done():
            # Stop arrived mid-pass. This is the only place the drain budget applies: a pass that
            # outlives it is cancelled and reported, never left to run past the process's welcome
            # (ADR-0053 D3, invariant 5). Any exception is left to the shared outcome path below.
            with contextlib.suppress(Exception):
                await asyncio.wait_for(task, spec.drain_budget.total_seconds())

            if task.cancelled():
                # Cancelled means ``wait_for`` ran out of budget. Checking the task rather than
                # catching ``TimeoutError`` matters: a pass may raise ``TimeoutError`` of its own
                # (an HTTP or database timeout), and that is an ordinary failure, not abandonment.
                _LOGGER.error(
                    "worker.pass_abandoned",
                    worker=spec.name,
                    drain_budget_seconds=spec.drain_budget.total_seconds(),
                )
                return _PassOutcome(result=None, failed=False, abandoned=True)

        try:
            return _PassOutcome(result=task.result(), failed=False, abandoned=False)
        except Exception:
            # ADR-0053 invariant 3 + F5: four of the seven workers let exceptions escape
            # ``run_once()``. Containing that here is what stops a single bad item from removing a
            # capability from the platform until somebody notices and restarts the process.
            # ``CancelledError`` is a ``BaseException`` and deliberately passes through.
            _LOGGER.error("worker.pass_failed", worker=spec.name, exc_info=True)
            return _PassOutcome(result=None, failed=True, abandoned=False)

    async def _sleep(self, delay: timedelta) -> None:
        """Sleep until the delay elapses or stop is requested, whichever comes first.

        Interruptible on purpose: a worker idling on a two-minute ceiling must not add two minutes
        to a deploy (ADR-0053 D3 — stop claiming *immediately*).
        """
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stopping.wait(), timeout=delay.total_seconds())

    def _touch(self, worker: str) -> None:
        if self._liveness is not None:
            self._liveness.touch(worker)

    @staticmethod
    def _grow(delay: timedelta, ceiling: timedelta) -> timedelta:
        return min(delay * _BACKOFF_FACTOR, ceiling)
