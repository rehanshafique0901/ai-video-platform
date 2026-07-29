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

Failure is handled in the two tiers ADR-0053 D5 actually asks for, which are not the same thing:

* **A pass that fails is contained** — logged, counted, and backed off toward a ceiling so a broken
  dependency is retried patiently rather than hot-looped. The worker stays registered and running.
  Evaluating ``found_work`` inside that contained region rather than in the loop is deliberate: the
  predicate is host-side code interpreting worker output, and it is the one place where a drifted
  result type could otherwise kill a task.
* **A supervision task that dies anyway is replaced** — up to a bound, after which the worker is
  *escalated*: logged at critical, flagged in its report, and turned into a non-zero exit by the
  entrypoint. Restarting without a bound would just be a hot loop one level up, and stopping without
  a signal would leave the worker quietly absent, which is precisely what D5 forbids.

Neither tier silences anything, and neither lets one worker's trouble reach another.

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
_DEFAULT_MAX_RESTARTS = 3


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
    restarts: int = 0
    escalated: bool = False


@dataclass(frozen=True, slots=True)
class HostResult:
    """Aggregate outcome, enough for an entrypoint to pick an exit code and nothing more."""

    workers: list[WorkerReport] = field(default_factory=list)

    @property
    def abandoned_any(self) -> bool:
        """True if any worker was cut off mid-pass at its drain budget (ADR-0053 D3)."""
        return any(w.abandoned for w in self.workers)

    @property
    def escalated_any(self) -> bool:
        """True if any worker could not be kept alive (ADR-0053 D5 — never quietly absent)."""
        return any(w.escalated for w in self.workers)


@dataclass(frozen=True, slots=True)
class _PassOutcome:
    """Internal: how one pass ended.

    Deliberately carries no worker result. The ``found_work`` predicate is evaluated inside the
    contained region and only its verdict escapes, so the supervisor never holds — and cannot be
    tempted to inspect — a worker's return value (ADR-0053 invariant 1).
    """

    found_work: bool
    failed: bool
    abandoned: bool


@dataclass(slots=True)
class _Counters:
    """Mutable tallies that survive a supervision restart, so a report covers the whole process."""

    passes: int = 0
    failures: int = 0
    abandoned: bool = False


class WorkerHost:
    """Supervises a fixed set of workers until stopped, then drains within bounded budgets."""

    def __init__(
        self,
        specs: Sequence[WorkerSpec[Any]],
        *,
        liveness: Liveness | None = None,
        failure_backoff_cap: timedelta = _DEFAULT_FAILURE_CAP,
        max_supervisor_restarts: int = _DEFAULT_MAX_RESTARTS,
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
        self._max_restarts = max_supervisor_restarts
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
            asyncio.create_task(self._keep_alive(spec), name=f"worker:{spec.name}")
            for spec in self._specs
        ]
        # ``return_exceptions`` is the last line of defence, not the mechanism: ``_keep_alive`` is
        # already total. Without it, one escaping exception would propagate here and leave every
        # sibling task orphaned and un-drained — a HOST-2 violation caused by the reporting path
        # rather than by any worker.
        settled = await asyncio.gather(*tasks, return_exceptions=True)
        reports = [
            r if isinstance(r, WorkerReport) else self._unreportable(spec, r)
            for spec, r in zip(self._specs, settled, strict=True)
        ]

        result = HostResult(workers=reports)
        _LOGGER.info(
            "worker_host.stopped",
            abandoned=result.abandoned_any,
            escalated=result.escalated_any,
            passes={r.name: r.passes for r in reports},
            failures={r.name: r.failures for r in reports if r.failures},
        )
        return result

    def _unreportable(self, spec: WorkerSpec[Any], error: BaseException | Any) -> WorkerReport:
        _LOGGER.critical("worker.supervisor_unreportable", worker=spec.name, error=repr(error))
        return WorkerReport(name=spec.name, passes=0, failures=0, abandoned=False, escalated=True)

    async def _keep_alive(self, spec: WorkerSpec[Any]) -> WorkerReport:
        """D5 tier two — keep a worker's supervision alive, or make its absence loud.

        ADR-0053 D5 requires that "a task that dies anyway is **restarted**, and a task that cannot
        be kept alive is **escalated** — a worker must never be quietly absent". Tier one (a pass
        that fails) is handled in :meth:`_run_one_pass` by logging and backing off. This is tier
        two: the supervision loop itself dying, which should be impossible and therefore must be
        handled rather than assumed away.

        Restarts are bounded. An unbounded retry of a deterministically broken supervisor is a hot
        loop wearing a different hat, so after ``max_supervisor_restarts`` the worker is escalated:
        logged at critical, flagged in its report, and surfaced by the entrypoint as a non-zero exit
        code. The host keeps scheduling every other worker throughout — losing one worker must never
        cost the platform the other six (HOST-2).
        """
        counters = _Counters()
        restarts = 0

        while True:
            try:
                await self._supervise(spec, counters)
                return WorkerReport(
                    name=spec.name,
                    passes=counters.passes,
                    failures=counters.failures,
                    abandoned=counters.abandoned,
                    restarts=restarts,
                )
            except Exception:
                _LOGGER.error(
                    "worker.supervisor_died",
                    worker=spec.name,
                    restarts=restarts,
                    max_restarts=self._max_restarts,
                    exc_info=True,
                )

            if self._stopping.is_set():
                # Shutting down anyway: replacing the supervisor now would only claim more work.
                return WorkerReport(
                    name=spec.name,
                    passes=counters.passes,
                    failures=counters.failures,
                    abandoned=counters.abandoned,
                    restarts=restarts,
                )

            if restarts >= self._max_restarts:
                _LOGGER.critical(
                    "worker.supervisor_escalated",
                    worker=spec.name,
                    restarts=restarts,
                    reason="supervision could not be kept alive; worker is no longer running",
                )
                return WorkerReport(
                    name=spec.name,
                    passes=counters.passes,
                    failures=counters.failures,
                    abandoned=counters.abandoned,
                    restarts=restarts,
                    escalated=True,
                )

            restarts += 1
            _LOGGER.warning("worker.supervisor_restarted", worker=spec.name, restarts=restarts)

    async def _supervise(self, spec: WorkerSpec[Any], counters: _Counters) -> None:
        """Poll one worker until stop: run a pass, adjust the delay, sleep, repeat.

        Tallies live in ``counters`` rather than locals so a restart resumes the same report
        instead of silently resetting a worker's history to zero.
        """
        # Idle and failure backoff are tracked separately, so a worker that keeps failing backs off
        # even while its queue is full — the realistic way a host would otherwise melt the database.
        idle_delay = spec.interval
        failure_delay = spec.interval

        while not self._stopping.is_set():
            outcome = await self._run_one_pass(spec)
            counters.passes += 1

            if outcome.abandoned:
                counters.abandoned = True
                break

            if outcome.failed:
                counters.failures += 1
                delay = failure_delay
                failure_delay = self._grow(failure_delay, self._failure_cap)
            else:
                failure_delay = spec.interval
                idle_delay = (
                    spec.interval
                    if outcome.found_work
                    else self._grow(idle_delay, spec.idle_ceiling)
                )
                delay = idle_delay

            self._touch(spec.name)

            if self._stopping.is_set():
                break
            await self._sleep(delay)

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
                return _PassOutcome(found_work=False, failed=False, abandoned=True)

        try:
            result = task.result()
        except Exception:
            # ADR-0053 invariant 3 + F5: four of the seven workers let exceptions escape
            # ``run_once()``. Containing that here is what stops a single bad item from removing a
            # capability from the platform until somebody notices and restarts the process.
            # ``CancelledError`` is a ``BaseException`` and deliberately passes through.
            _LOGGER.error("worker.pass_failed", worker=spec.name, exc_info=True)
            return _PassOutcome(found_work=False, failed=True, abandoned=False)

        try:
            return _PassOutcome(found_work=spec.found_work(result), failed=False, abandoned=False)
        except Exception:
            # D5 tier one. The predicate is host-side interpretation of a worker's output, so a
            # broken one (a drifted result type, say) is a failed pass rather than a dead worker:
            # it is logged, counted in ``failures``, and backs off toward the failure cap instead of
            # hot-looping. Evaluating it here rather than in the supervision loop is what keeps that
            # true — outside this block it would kill the task and, through ``gather``, the host.
            _LOGGER.error("worker.found_work_failed", worker=spec.name, exc_info=True)
            return _PassOutcome(found_work=False, failed=True, abandoned=False)

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
