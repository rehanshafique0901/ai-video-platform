"""Unit tests — α9.7 `GenerationWorker` (ADR-0052 D2).

Every test here defends the same invariant: **one generation execution is one external spend
opportunity.** Claiming is exactly-once, an abandoned run is terminalised rather than retried,
and the lease is renewed for as long as the run lasts.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Self

import pytest

from app.application.interfaces.generation_runner import IGenerationRunner
from app.application.use_cases.generation.generation_worker import (
    LOST_WORKER_REASON,
    GenerationWorker,
)
from app.application.use_cases.generation.request import GenerateVideoRequest
from app.application.use_cases.generation.request_codec import GenerationRequestSpec
from app.application.use_cases.generation.results import (
    GenerateVideoResult,
    GenerationProvenance,
    GenerationStatus,
)
from app.domain.generation.execution_state import ExecutionStatus
from tests.unit.application.use_cases.generation._ingress_fakes import (
    FakeGenerationJobStore,
    FakeGenerationRunner,
    FakeLockManager,
)

pytestmark = pytest.mark.unit


class _FakeUnitOfWork:
    """Just enough UoW to hand the worker a lock manager."""

    def __init__(self, locks: FakeLockManager) -> None:
        self.locks = locks

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    async def commit(self) -> None:
        return None


def _worker(
    store: FakeGenerationJobStore,
    runner: IGenerationRunner,
    locks: FakeLockManager | None = None,
    **kwargs: object,
) -> tuple[GenerationWorker, FakeLockManager]:
    lock_manager = locks or FakeLockManager()
    worker = GenerationWorker(
        uow=_FakeUnitOfWork(lock_manager),  # type: ignore[arg-type]
        store=store,
        runner=runner,
        **kwargs,  # type: ignore[arg-type]
    )
    return worker, lock_manager


# ---- phase 1 — claim and run ------------------------------------------------ #


async def test_claims_and_runs_a_queued_generation() -> None:
    store = FakeGenerationJobStore()
    row = store.seed(spec=GenerationRequestSpec(prompt="a lighthouse at dusk", seed=9))
    runner = FakeGenerationRunner()
    worker, _ = _worker(store, runner)

    result = await worker.run_once()

    assert result.scanned == 1
    assert store.rows[row.id].status == ExecutionStatus.PLANNING.value
    # The claimed row's stored request is what actually reaches the pipeline.
    assert runner.requests[0].prompt == "a lighthouse at dusk"
    assert runner.requests[0].identity.seed == 9
    assert runner.requests[0].generation_id == row.id


async def test_skips_a_generation_whose_lease_is_held() -> None:
    store = FakeGenerationJobStore()
    row = store.seed()
    runner = FakeGenerationRunner()
    locks = FakeLockManager(held={f"generation:{row.id}"})
    worker, _ = _worker(store, runner, locks)

    result = await worker.run_once()

    assert result.scanned == 0
    assert runner.requests == []
    # Untouched: another worker owns it, and it must stay claimable by that worker alone.
    assert store.rows[row.id].status == ExecutionStatus.QUEUED.value


async def test_losing_the_claim_cas_does_not_run_the_generation() -> None:
    """The lease is not the only guard: a racing worker that already CAS'd wins."""
    store = FakeGenerationJobStore()
    row = store.seed()
    runner = FakeGenerationRunner()
    worker, _ = _worker(store, runner)

    # Simulate the racer settling the CAS between our scan and our claim.
    original_claim = store.claim

    async def _steal(*, generation_id: object) -> None:
        store.rows[row.id].status = ExecutionStatus.PLANNING.value
        return await original_claim(generation_id=generation_id)  # type: ignore[arg-type]

    store.claim = _steal  # type: ignore[assignment]

    result = await worker.run_once()

    assert result.scanned == 0
    assert runner.requests == []


async def test_a_failing_generation_is_never_requeued() -> None:
    """max_attempts = 1 is structural: nothing writes a row back to ``queued``."""
    store = FakeGenerationJobStore()
    row = store.seed()
    runner = FakeGenerationRunner(status=GenerationStatus.FAILED)
    worker, _ = _worker(store, runner)

    await worker.run_once()

    assert store.rows[row.id].status != ExecutionStatus.QUEUED.value
    assert await store.list_claimable(limit=10) == []
    # A second poll must not re-run it — that would be a second spend.
    await worker.run_once()
    assert len(runner.requests) == 1


async def test_one_broken_generation_does_not_abort_the_batch() -> None:
    store = FakeGenerationJobStore()
    now = datetime.now(UTC)
    first = store.seed(created_at=now - timedelta(minutes=2))
    second = store.seed(created_at=now - timedelta(minutes=1))

    class _ExplodesOnce(IGenerationRunner):
        def __init__(self) -> None:
            self.seen: list[GenerateVideoRequest] = []

        async def run(self, request: GenerateVideoRequest) -> GenerateVideoResult:
            self.seen.append(request)
            if request.generation_id == first.id:
                raise RuntimeError("provider exploded")
            gid = request.generation_id
            assert gid is not None
            return GenerateVideoResult(
                status=GenerationStatus.SUCCEEDED,
                generation_id=gid,
                title="ok",
                provenance=GenerationProvenance(
                    generation_id=gid,
                    capability="image.generate",
                    execution_mode="auto",
                    resolver_version="test",
                ),
            )

    runner = _ExplodesOnce()
    worker, _ = _worker(store, runner)

    result = await worker.run_once()

    assert len(runner.seen) == 2
    assert result.scanned == 2
    # The broken one is terminalised so it cannot linger unobservable.
    assert store.rows[first.id].status == ExecutionStatus.FAILED.value
    assert store.rows[second.id].status == ExecutionStatus.PLANNING.value


# ---- phase 0 — reap --------------------------------------------------------- #


async def test_reaps_a_stale_claimed_generation_as_failed() -> None:
    store = FakeGenerationJobStore()
    row = store.seed(
        status=ExecutionStatus.GENERATING.value,
        updated_at=datetime.now(UTC) - timedelta(hours=1),
    )
    worker, _ = _worker(store, FakeGenerationRunner())

    result = await worker.run_once()

    assert result.reaped == 1
    assert store.rows[row.id].status == ExecutionStatus.FAILED.value
    assert store.rows[row.id].failure_reason == LOST_WORKER_REASON


async def test_reaping_never_reruns_the_generation() -> None:
    """A crashed run may already have been paid for; re-running would spend again."""
    store = FakeGenerationJobStore()
    row = store.seed(
        status=ExecutionStatus.GENERATING.value,
        updated_at=datetime.now(UTC) - timedelta(hours=1),
    )
    runner = FakeGenerationRunner()
    worker, _ = _worker(store, runner)

    await worker.run_once()

    assert runner.requests == []
    assert store.rows[row.id].status == ExecutionStatus.FAILED.value


async def test_does_not_reap_inside_the_grace_window() -> None:
    store = FakeGenerationJobStore()
    row = store.seed(status=ExecutionStatus.GENERATING.value, updated_at=datetime.now(UTC))
    worker, _ = _worker(store, FakeGenerationRunner(), reap_grace=timedelta(seconds=120))

    result = await worker.run_once()

    assert result.reaped == 0
    assert store.rows[row.id].status == ExecutionStatus.GENERATING.value


async def test_does_not_reap_a_run_whose_lease_is_still_live() -> None:
    """A live lease means a slow worker, not a dead one — even past the staleness cutoff."""
    store = FakeGenerationJobStore()
    row = store.seed(
        status=ExecutionStatus.RENDERING.value,
        updated_at=datetime.now(UTC) - timedelta(hours=1),
    )
    locks = FakeLockManager(held={f"generation:{row.id}"})
    worker, _ = _worker(store, FakeGenerationRunner(), locks)

    result = await worker.run_once()

    assert result.reaped == 0
    assert store.rows[row.id].status == ExecutionStatus.RENDERING.value


async def test_does_not_reap_a_queued_generation() -> None:
    """A queued row was never claimed and never spent: it must stay claimable."""
    store = FakeGenerationJobStore()
    row = store.seed(updated_at=datetime.now(UTC) - timedelta(hours=1))
    worker, _ = _worker(store, FakeGenerationRunner())

    result = await worker.run_once()

    assert result.reaped == 0
    assert store.rows[row.id].id == row.id


# ---- heartbeat -------------------------------------------------------------- #


async def test_lease_is_renewed_while_a_long_run_is_in_flight() -> None:
    """A lease that lapsed mid-run would let a second worker double the spend."""
    store = FakeGenerationJobStore()
    row = store.seed()
    release = asyncio.Event()

    class _SlowRunner(IGenerationRunner):
        async def run(self, request: GenerateVideoRequest) -> GenerateVideoResult:
            await release.wait()
            gid = request.generation_id
            assert gid is not None
            return GenerateVideoResult(
                status=GenerationStatus.SUCCEEDED,
                generation_id=gid,
                title="slow",
                provenance=GenerationProvenance(
                    generation_id=gid,
                    capability="image.generate",
                    execution_mode="auto",
                    resolver_version="test",
                ),
            )

    worker, locks = _worker(store, _SlowRunner(), heartbeat=timedelta(seconds=0.01))
    task = asyncio.create_task(worker.run_once())
    # Let several heartbeat windows elapse while the run is blocked.
    await asyncio.sleep(0.08)
    release.set()
    await task

    assert locks.renewals.count(f"generation:{row.id}") >= 2


async def test_a_lost_lease_does_not_abort_the_run() -> None:
    """Cancelling would not refund what has already been spent, so the run finishes."""
    store = FakeGenerationJobStore()
    store.seed()
    release = asyncio.Event()
    finished = False

    class _SlowRunner(IGenerationRunner):
        async def run(self, request: GenerateVideoRequest) -> GenerateVideoResult:
            nonlocal finished
            await release.wait()
            finished = True
            gid = request.generation_id
            assert gid is not None
            return GenerateVideoResult(
                status=GenerationStatus.SUCCEEDED,
                generation_id=gid,
                title="slow",
                provenance=GenerationProvenance(
                    generation_id=gid,
                    capability="image.generate",
                    execution_mode="auto",
                    resolver_version="test",
                ),
            )

    locks = FakeLockManager()
    locks.renew_fails = True
    worker, _ = _worker(store, _SlowRunner(), locks, heartbeat=timedelta(seconds=0.01))
    task = asyncio.create_task(worker.run_once())
    await asyncio.sleep(0.05)
    release.set()
    result = await task

    assert finished is True
    assert result.scanned == 1


async def test_batch_size_bounds_the_scan() -> None:
    store = FakeGenerationJobStore()
    for _ in range(5):
        store.seed()
    runner = FakeGenerationRunner()
    worker, _ = _worker(store, runner, batch_size=2)

    result = await worker.run_once()

    assert result.scanned == 2
    assert len(runner.requests) == 2
