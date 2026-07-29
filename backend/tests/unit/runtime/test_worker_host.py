"""Unit tests — α9.8 ``WorkerHost`` (ADR-0053).

Every test here drives the supervisor with **fake specs only**. That is not merely convenient: if
the host can be exercised without a container, a database, or a single real worker, then it
demonstrably knows nothing about any of them, which is ADR-0053 PF2 (*the supervisor is
result-agnostic; the registry is the only type-aware component*) proved rather than asserted.

The bulk of the file defends **HOST-2** — one worker's failure never suppresses another's
scheduling — because that is the invariant a future "simplification" is most likely to erase. Any
refactor that collapses the seven tasks into one loop, or awaits passes in sequence, passes every
other test here and fails these.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from app.runtime.liveness import Liveness
from app.runtime.worker_host import WorkerHost, WorkerSpec

pytestmark = pytest.mark.unit

_FAST = timedelta(seconds=0)


def _spec(
    name: str,
    run_pass: Callable[[], Awaitable[Any]],
    *,
    found_work: Callable[[Any], bool] = lambda _result: True,
    interval: timedelta = _FAST,
    idle_ceiling: timedelta | None = None,
    drain_budget: timedelta = timedelta(seconds=5),
) -> WorkerSpec[Any]:
    return WorkerSpec(
        name=name,
        run_pass=run_pass,
        found_work=found_work,
        interval=interval,
        idle_ceiling=idle_ceiling if idle_ceiling is not None else interval,
        drain_budget=drain_budget,
    )


@contextlib.asynccontextmanager
async def _driving(coro: Coroutine[Any, Any, None]) -> AsyncIterator[None]:
    """Run a driver coroutine alongside the host, holding a strong reference to its task."""
    task = asyncio.create_task(coro)
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def _stop_after(host: WorkerHost, passes: list[Any], target: int) -> None:
    """Let the host run until ``passes`` reaches ``target``, then ask it to stop."""
    while len(passes) < target:
        await asyncio.sleep(0)
    host.request_stop()


def _record_sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Capture the host's inter-pass delays without waiting for them in real time.

    The host sleeps via ``wait_for(stopping.wait(), delay)``, so the requested timeout *is* the
    backoff decision. Recording it lets a backoff curve be asserted exactly, in milliseconds.
    """
    delays: list[float] = []
    original = asyncio.wait_for

    async def _record(awaitable: Any, timeout: float | None) -> Any:
        if timeout is not None:
            delays.append(timeout)
        return await original(awaitable, timeout)

    monkeypatch.setattr(asyncio, "wait_for", _record)
    return delays


# --------------------------------------------------------------------------------------
# Construction (HOST-1)
# --------------------------------------------------------------------------------------


async def test_rejects_an_empty_worker_set() -> None:
    with pytest.raises(ValueError, match="at least one worker"):
        WorkerHost([])


async def test_rejects_duplicate_worker_names() -> None:
    async def _noop() -> object:
        return None

    with pytest.raises(ValueError, match="duplicate worker names: relay"):
        WorkerHost([_spec("relay", _noop), _spec("relay", _noop)])


async def test_host1_registration_is_immutable_for_the_process_lifetime() -> None:
    """HOST-1 — mutating the caller's list after construction changes nothing that runs."""
    ran: list[str] = []

    async def _relay() -> object:
        ran.append("relay")
        return None

    async def _latecomer() -> object:  # pragma: no cover - must never be scheduled
        ran.append("latecomer")
        return None

    specs = [_spec("relay", _relay)]
    host = WorkerHost(specs)

    # The host froze the set at construction; the caller's list is no longer connected to it.
    specs.append(_spec("latecomer", _latecomer))

    async with _driving(_stop_after(host, ran, 3)):
        result = await host.run()

    assert "latecomer" not in ran
    assert [r.name for r in result.workers] == ["relay"]


# --------------------------------------------------------------------------------------
# Failure containment (ADR-0053 invariant 3, F5)
# --------------------------------------------------------------------------------------


async def test_a_raising_pass_does_not_kill_the_task() -> None:
    """The four workers that let exceptions escape ``run_once()`` must not remove a capability."""
    attempts: list[int] = []

    async def _flaky() -> object:
        attempts.append(len(attempts))
        if len(attempts) == 1:
            raise RuntimeError("ffmpeg exploded")
        return None

    host = WorkerHost([_spec("render", _flaky)])
    async with _driving(_stop_after(host, attempts, 3)):
        result = await host.run()

    assert len(attempts) >= 3, "the worker kept running after the failure"
    assert result.workers[0].failures == 1


async def test_a_permanently_failing_worker_backs_off_and_is_capped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[int] = []

    async def _broken() -> object:
        attempts.append(len(attempts))
        raise RuntimeError("database is down")

    host = WorkerHost(
        [_spec("publish", _broken, interval=timedelta(seconds=1))],
        failure_backoff_cap=timedelta(seconds=4),
    )
    delays = _record_sleeps(monkeypatch)

    async with _driving(_stop_after(host, attempts, 4)):
        await host.run()

    growth = [d for d in delays if d in (1.0, 2.0, 4.0)]
    assert growth[:3] == [1.0, 2.0, 4.0], f"expected a capped doubling curve, saw {delays}"
    assert max(growth) <= 4.0, "backoff exceeded its cap"


# --------------------------------------------------------------------------------------
# HOST-2 — failure and slowness are contained to the worker that produced them
# --------------------------------------------------------------------------------------


async def test_host2_a_slow_worker_does_not_delay_a_fast_one() -> None:
    """The D2 regression: a sequential tick would make the fast worker wait on the slow one."""
    fast: list[int] = []
    slow_started = asyncio.Event()

    async def _fast() -> object:
        fast.append(1)
        return None

    async def _slow() -> object:
        slow_started.set()
        await asyncio.sleep(30)  # never completes within the test
        return None

    host = WorkerHost([_spec("fast", _fast), _spec("slow", _slow, drain_budget=_FAST)])

    async def _driver() -> None:
        await slow_started.wait()
        while len(fast) < 25:
            await asyncio.sleep(0)
        host.request_stop()

    async with _driving(_driver()):
        await asyncio.wait_for(host.run(), timeout=5)

    assert len(fast) >= 25, "the fast worker was throttled by the slow one"


async def test_host2_a_failing_worker_does_not_change_a_healthy_workers_pass_count() -> None:
    healthy: list[int] = []
    broken: list[int] = []

    async def _healthy() -> object:
        healthy.append(1)
        return None

    async def _broken() -> object:
        broken.append(1)
        raise RuntimeError("permanently broken")

    host = WorkerHost(
        [
            _spec("healthy", _healthy),
            # A long failure backoff would stall a shared scheduler after the first failure.
            _spec("broken", _broken, interval=timedelta(seconds=10)),
        ],
        failure_backoff_cap=timedelta(seconds=60),
    )
    async with _driving(_stop_after(host, healthy, 20)):
        await asyncio.wait_for(host.run(), timeout=5)

    assert len(healthy) >= 20
    assert len(broken) >= 1


async def test_host2_a_wedged_worker_does_not_stop_others_being_scheduled() -> None:
    """A worker stuck for its whole drain budget must not freeze the rest of the host."""
    ticks: list[int] = []
    wedged_entered = asyncio.Event()

    async def _wedged() -> object:
        wedged_entered.set()
        await asyncio.sleep(30)
        return None

    async def _ticker() -> object:
        ticks.append(1)
        return None

    host = WorkerHost(
        [
            _spec("wedged", _wedged, drain_budget=timedelta(seconds=0.05)),
            _spec("ticker", _ticker),
        ]
    )

    async def _driver() -> None:
        await wedged_entered.wait()
        while len(ticks) < 15:
            await asyncio.sleep(0)
        host.request_stop()

    async with _driving(_driver()):
        result = await asyncio.wait_for(host.run(), timeout=5)

    assert len(ticks) >= 15
    assert result.abandoned_any is True


# --------------------------------------------------------------------------------------
# Idle backoff (ADR-0053 D2)
# --------------------------------------------------------------------------------------


async def test_idle_backoff_grows_to_the_ceiling_and_resets_when_work_appears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    results: list[bool] = [False, False, False, True, False]
    seen: list[bool] = []

    async def _pass() -> object:
        value = results[len(seen)] if len(seen) < len(results) else False
        seen.append(value)
        return value

    host = WorkerHost(
        [
            _spec(
                "email",
                _pass,
                found_work=lambda result: bool(result),
                interval=timedelta(seconds=1),
                idle_ceiling=timedelta(seconds=4),
            )
        ]
    )
    delays = _record_sleeps(monkeypatch)

    async with _driving(_stop_after(host, seen, 5)):
        await host.run()

    curve = [d for d in delays if d in (1.0, 2.0, 4.0)]
    # Three idle passes double 1 → 2 → 4 and stick at the ceiling; the pass that finds work resets
    # the delay to the base interval, which is the behaviour that keeps a busy queue responsive.
    assert curve[:4] == [2.0, 4.0, 4.0, 1.0], f"expected reset after work, saw {delays}"


# --------------------------------------------------------------------------------------
# Shutdown (ADR-0053 D3, invariants 4 and 5)
# --------------------------------------------------------------------------------------


async def test_stop_starts_no_further_passes() -> None:
    """Invariant 4 — the host must never begin an item it has already decided not to finish."""
    passes: list[int] = []

    async def _pass() -> object:
        passes.append(1)
        return None

    host = WorkerHost([_spec("relay", _pass)])
    async with _driving(_stop_after(host, passes, 5)):
        await host.run()

    settled = len(passes)
    await asyncio.sleep(0.05)
    assert len(passes) == settled, "a pass started after the host returned"


async def test_an_in_flight_pass_within_its_budget_completes_untouched() -> None:
    completed = asyncio.Event()
    started = asyncio.Event()

    async def _pass() -> object:
        started.set()
        await asyncio.sleep(0.05)
        completed.set()
        return None

    host = WorkerHost([_spec("generation", _pass, drain_budget=timedelta(seconds=5))])

    async def _driver() -> None:
        await started.wait()
        host.request_stop()

    async with _driving(_driver()):
        result = await asyncio.wait_for(host.run(), timeout=5)

    assert completed.is_set(), "a pass inside its drain budget was cut short"
    assert result.abandoned_any is False


async def test_an_in_flight_pass_exceeding_its_budget_is_abandoned_and_reported() -> None:
    """Invariant 5 — abandonment is bounded and observable, never silent."""
    started = asyncio.Event()
    finished = False

    async def _pass() -> object:
        nonlocal finished
        started.set()
        await asyncio.sleep(30)
        finished = True  # pragma: no cover - the point is that this never runs
        return None

    host = WorkerHost([_spec("generation", _pass, drain_budget=timedelta(seconds=0.05))])

    async def _driver() -> None:
        await started.wait()
        host.request_stop()

    async with _driving(_driver()):
        result = await asyncio.wait_for(host.run(), timeout=5)

    assert finished is False
    assert result.abandoned_any is True
    assert result.workers[0].abandoned is True


async def test_a_pass_raising_its_own_timeout_during_a_drain_is_a_failure_not_an_abandonment() -> (
    None
):
    """``TimeoutError`` from the worker is an ordinary failure; only the budget abandons a pass."""
    started = asyncio.Event()

    async def _pass() -> object:
        started.set()
        await asyncio.sleep(0.01)
        raise TimeoutError("the destination API timed out")

    host = WorkerHost([_spec("publish", _pass, drain_budget=timedelta(seconds=5))])

    async def _driver() -> None:
        await started.wait()
        host.request_stop()

    async with _driving(_driver()):
        result = await asyncio.wait_for(host.run(), timeout=5)

    assert result.abandoned_any is False, "a worker's own timeout was mistaken for a drain overrun"
    assert result.workers[0].failures == 1


async def test_an_idling_worker_wakes_immediately_on_stop() -> None:
    """A worker parked on a long ceiling must not add that ceiling to a deploy."""
    passes: list[int] = []

    async def _pass() -> object:
        passes.append(1)
        return None

    host = WorkerHost(
        [
            _spec(
                "enrichment",
                _pass,
                found_work=lambda _result: False,
                interval=timedelta(seconds=120),
                idle_ceiling=timedelta(seconds=120),
            )
        ]
    )

    async def _driver() -> None:
        while not passes:
            await asyncio.sleep(0)
        host.request_stop()

    async with _driving(_driver()):
        # Would take 120s if the sleep were not interruptible.
        await asyncio.wait_for(host.run(), timeout=5)


# --------------------------------------------------------------------------------------
# Liveness (ADR-0053 D5 / PF9)
# --------------------------------------------------------------------------------------


async def test_liveness_marker_is_touched_after_successful_and_failed_passes(
    tmp_path: Path,
) -> None:
    """The marker means "the loop is turning", so a contained failure still refreshes it."""
    attempts: list[int] = []

    async def _flaky() -> object:
        attempts.append(1)
        raise RuntimeError("still alive, just failing")

    host = WorkerHost([_spec("render", _flaky)], liveness=Liveness(tmp_path))
    async with _driving(_stop_after(host, attempts, 2)):
        await host.run()

    assert (tmp_path / "render.alive").exists()


async def test_liveness_failure_never_takes_down_a_worker(tmp_path: Path) -> None:
    passes: list[int] = []

    async def _pass() -> object:
        passes.append(1)
        return None

    # A file where the directory should be: mkdir fails on every touch.
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory")

    host = WorkerHost([_spec("relay", _pass)], liveness=Liveness(blocked))
    async with _driving(_stop_after(host, passes, 3)):
        result = await host.run()

    assert len(passes) >= 3
    assert result.workers[0].failures == 0
