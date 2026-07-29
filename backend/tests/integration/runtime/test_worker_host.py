"""Integration tests — α9.8 worker runtime host against the live database (Stage 26).

These are the first tests in the repository where background work executes **without a test calling
``run_once()``**. Every other worker test in the suite drives the primitive by hand, which is
exactly how the platform reached α9.7 with seven workers and no production execution path: the
tests proved the work was correct, never that anything would run it.

Here the loop is the subject. A real :class:`WorkerHost` schedules real workers against real
PostgreSQL; the tests seed rows through the real ingress and then only wait.

What is substituted, and why:

* **The provider pipeline** is stubbed, as in Stage 25 — a real generation means ffmpeg plus a
  remote model. Every *persistence* boundary is genuine.
* **The container factories** are replaced by workers bound to the test's sessionmaker. The whole
  integration suite does this, and it is what keeps the shared database clean. The spec shape
  matches ``build_registry`` exactly (whose composition is asserted in the unit tests); scheduling,
  drain, and shutdown here are the production code paths, unmodified.

:func:`test_two_hosts_process_every_event_exactly_once` deliberately breaks the rollback pattern —
replica safety cannot be observed through one shared connection, and that test is the only evidence
for ADR-0053 D4. It commits, then deletes what it seeded.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Self
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import insert, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.application.interfaces.execution_runtime_store import NewGenerationAsset
from app.application.interfaces.generation_runner import IGenerationRunner
from app.application.use_cases.generation.create_generation import CreateGeneration
from app.application.use_cases.generation.generation_worker import GenerationWorker
from app.application.use_cases.generation.read_generations import GetGeneration
from app.application.use_cases.generation.request import GenerateVideoRequest
from app.application.use_cases.generation.request_codec import GenerationRequestSpec
from app.application.use_cases.generation.results import (
    GenerateVideoResult,
    GenerationProvenance,
    GenerationStatus,
)
from app.application.use_cases.relay.relay_service import RelayService
from app.domain.generation.execution_state import ExecutionStatus, GenerationAssetKind
from app.infrastructure.db.models.events import EventOutbox
from app.infrastructure.db.models.identity import Tenant, User
from app.infrastructure.generation.execution_runtime_store import SqlExecutionRuntimeStore
from app.infrastructure.generation.generation_job_store import SqlGenerationJobStore
from app.infrastructure.repositories.distributed_lock_manager import (
    SqlAlchemyDistributedLockManager,
)
from app.infrastructure.repositories.event_outbox_repository import EventOutboxRepository
from app.runtime.worker_host import WorkerHost, WorkerSpec

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

# Tight cadence so the tests observe scheduling rather than wait for it. The interval only
# influences how often a pass starts; every behaviour under test is cadence-independent.
_INTERVAL = timedelta(seconds=0)
_TIMEOUT = 20.0


# --------------------------------------------------------------------------- #
# harness
# --------------------------------------------------------------------------- #


@pytest_asyncio.fixture
async def bound(engine: AsyncEngine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """A sessionmaker bound to one connection whose transaction rolls back on teardown."""
    async with engine.connect() as connection:
        outer_tx = await connection.begin()
        factory = async_sessionmaker(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        try:
            yield factory
        finally:
            await outer_tx.rollback()


class _Uow:
    """Minimal UoW exposing what the generation worker and the relay actually use."""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory
        self._session: AsyncSession | None = None

    async def __aenter__(self) -> Self:
        self._session = self._factory()
        self.locks = SqlAlchemyDistributedLockManager(self._session)
        self.outbox = EventOutboxRepository(self._session)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def commit(self) -> None:
        assert self._session is not None
        await self._session.commit()


def _generation_spec(
    factory: async_sessionmaker[AsyncSession],
    runner: IGenerationRunner,
    *,
    drain_budget: timedelta = timedelta(seconds=30),
) -> WorkerSpec[object]:
    """The generation spec, shaped exactly as ``build_registry`` shapes it."""

    def run_pass() -> object:
        worker = GenerationWorker(
            uow=_Uow(factory),  # type: ignore[arg-type]
            store=SqlGenerationJobStore(factory),
            runner=runner,
            batch_size=1,
        )
        return worker.run_once()

    return WorkerSpec(
        name="generation",
        run_pass=run_pass,  # type: ignore[arg-type]
        found_work=lambda result: result.scanned > 0 or result.reaped > 0,  # type: ignore[attr-defined]
        interval=_INTERVAL,
        idle_ceiling=_INTERVAL,
        drain_budget=drain_budget,
    )


def _relay_spec(
    factory: async_sessionmaker[AsyncSession],
    publisher: _RecordingPublisher,
    *,
    batch_size: int | None = None,
) -> WorkerSpec[object]:
    def run_pass() -> object:
        service = RelayService(
            uow=_Uow(factory),  # type: ignore[arg-type]
            publisher=publisher,  # type: ignore[arg-type]
        )
        # PF7 — ``relay_once`` already exposes a per-call batch override, so the replica test can
        # force contention without touching the frozen relay module.
        return service.relay_once(batch_size=batch_size)

    return WorkerSpec(
        name="relay",
        run_pass=run_pass,  # type: ignore[arg-type]
        found_work=lambda result: result.fetched > 0,  # type: ignore[attr-defined]
        interval=_INTERVAL,
        idle_ceiling=_INTERVAL,
        drain_budget=timedelta(seconds=10),
    )


class _RecordingPublisher:
    """Stands in for the in-process fan-out; records what the relay delivered, and when."""

    def __init__(self) -> None:
        self.delivered: list[UUID] = []
        self._lock = asyncio.Lock()

    async def publish(self, event: object) -> None:
        async with self._lock:
            self.delivered.append(event.id)  # type: ignore[attr-defined]


async def _run_host_until(
    host: WorkerHost, predicate: object, *, timeout: float = _TIMEOUT
) -> object:
    """Start the host, wait for ``predicate()`` to hold, then stop it and return the result."""
    run = asyncio.create_task(host.run())

    async def _wait() -> None:
        while not predicate():  # type: ignore[operator]
            await asyncio.sleep(0.01)

    try:
        await asyncio.wait_for(_wait(), timeout=timeout)
    finally:
        host.request_stop()
    return await asyncio.wait_for(run, timeout=timeout)


async def _seed_owner(factory: async_sessionmaker[AsyncSession]) -> tuple[UUID, UUID]:
    tenant_id, user_id = uuid4(), uuid4()
    async with factory() as session:
        await session.execute(
            insert(Tenant).values(id=tenant_id, name="Host", slug=f"host-{tenant_id}")
        )
        await session.execute(
            insert(User).values(
                id=user_id,
                tenant_id=tenant_id,
                email=f"host-{user_id}@example.com",
                display_name="Host Owner",
            )
        )
        await session.commit()
    return tenant_id, user_id


class _StubRunner(IGenerationRunner):
    """Completes a generation through the real runtime store; optionally blocks first."""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._store = SqlExecutionRuntimeStore(factory)
        self.runs: list[UUID] = []
        self.started = asyncio.Event()
        self.gate: asyncio.Event | None = None

    async def run(self, request: GenerateVideoRequest) -> GenerateVideoResult:
        generation_id = request.generation_id
        assert generation_id is not None
        self.runs.append(generation_id)
        self.started.set()
        if self.gate is not None:
            await self.gate.wait()

        provenance = GenerationProvenance(
            generation_id=generation_id,
            capability="image.generate",
            execution_mode="auto",
            resolver_version="test-resolver",
            chosen_adapter="test-adapter",
            chosen_provider="test-provider",
            planner_version="p1",
        )
        title = request.title or "untitled"
        await self._store.begin(
            generation_id=generation_id,
            request=request,
            provenance=provenance,
            title=title,
            shot_count=1,
        )
        asset_id = await self._store.register_asset(
            NewGenerationAsset(
                generation_id=generation_id,
                asset_kind=GenerationAssetKind.VIDEO,
                storage_backend="local",
                storage_bucket="test",
                storage_key=f"generations/{generation_id}/final.mp4",
                mime_type="video/mp4",
                size_bytes=1024,
                duration_ms=9_000,
            )
        )
        await self._store.complete(
            generation_id=generation_id,
            final_video_asset_id=asset_id,
            storage_backend="local",
            storage_bucket="test",
            storage_key=f"generations/{generation_id}/final.mp4",
            duration_seconds=9.0,
            width=720,
            height=1280,
        )
        return GenerateVideoResult(
            status=GenerationStatus.SUCCEEDED,
            generation_id=generation_id,
            title=title,
            provenance=provenance,
            video_key=f"generations/{generation_id}/final.mp4",
            duration_seconds=9.0,
        )


# --------------------------------------------------------------------------- #
# the slice's reason to exist: work runs without anyone calling run_once()
# --------------------------------------------------------------------------- #


async def test_the_host_drains_a_queued_generation_with_no_manual_poll(
    bound: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, owner_user_id = await _seed_owner(bound)
    store = SqlGenerationJobStore(bound)
    created = await CreateGeneration(store).execute(
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        spec=GenerationRequestSpec(prompt="a kite over a harbour", seed=7, title="Kite"),
    )
    assert created.generation.status == ExecutionStatus.QUEUED.value

    runner = _StubRunner(bound)
    host = WorkerHost([_generation_spec(bound, runner)])

    await _run_host_until(host, lambda: runner.runs)

    view = await GetGeneration(store).execute(
        tenant_id=tenant_id, owner_user_id=owner_user_id, generation_id=created.generation.id
    )
    assert view.status == ExecutionStatus.COMPLETED.value
    assert runner.runs == [created.generation.id]


async def test_the_host_relays_an_outbox_event_with_no_manual_poll(
    bound: async_sessionmaker[AsyncSession],
) -> None:
    """The slice's least visible, most valuable effect: notifications and analytics revive."""
    event_id = uuid4()
    async with bound() as session:
        await session.execute(
            insert(EventOutbox).values(
                id=event_id,
                aggregate_type="generation",
                aggregate_id=uuid4(),
                event_type="generation.completed",
                payload={"probe": True},
                occurred_at=datetime.now(UTC),
            )
        )
        await session.commit()

    publisher = _RecordingPublisher()
    host = WorkerHost([_relay_spec(bound, publisher)])

    await _run_host_until(host, lambda: event_id in publisher.delivered)

    async with bound() as session:
        published_at = await session.scalar(
            select(EventOutbox.published_at).where(EventOutbox.id == event_id)
        )
    assert published_at is not None, "the relay ran but the row was never stamped"


# --------------------------------------------------------------------------- #
# shutdown (ADR-0053 D3)
# --------------------------------------------------------------------------- #


async def test_stop_drains_the_in_flight_item_and_claims_nothing_further(
    bound: async_sessionmaker[AsyncSession],
) -> None:
    """Invariants 4 and 5 against real rows: finish what is started, start nothing new."""
    tenant_id, owner_user_id = await _seed_owner(bound)
    store = SqlGenerationJobStore(bound)
    create = CreateGeneration(store)
    first = await create.execute(
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        spec=GenerationRequestSpec(prompt="first", seed=1, title="First"),
    )
    second = await create.execute(
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        spec=GenerationRequestSpec(prompt="second", seed=2, title="Second"),
    )

    runner = _StubRunner(bound)
    runner.gate = asyncio.Event()  # hold the first generation mid-run
    host = WorkerHost([_generation_spec(bound, runner)])

    run = asyncio.create_task(host.run())
    await asyncio.wait_for(runner.started.wait(), timeout=_TIMEOUT)

    host.request_stop()
    await asyncio.sleep(0.05)  # a host that ignored the stop would claim the second here
    runner.gate.set()
    result = await asyncio.wait_for(run, timeout=_TIMEOUT)

    assert result.abandoned_any is False, "an item inside its budget was cut short"
    assert runner.runs == [first.generation.id], "the host claimed work after being told to stop"

    first_view = await GetGeneration(store).execute(
        tenant_id=tenant_id, owner_user_id=owner_user_id, generation_id=first.generation.id
    )
    second_view = await GetGeneration(store).execute(
        tenant_id=tenant_id, owner_user_id=owner_user_id, generation_id=second.generation.id
    )
    assert first_view.status == ExecutionStatus.COMPLETED.value
    assert second_view.status == ExecutionStatus.QUEUED.value, "the unclaimed item must survive"


# --------------------------------------------------------------------------- #
# replica safety (ADR-0053 D4)
# --------------------------------------------------------------------------- #


async def test_two_hosts_process_every_event_exactly_once(engine: AsyncEngine) -> None:
    """D4 tested rather than asserted: two hosts, one queue, no lost or duplicated work.

    This is the one test that cannot use the rollback harness. Replica safety is a property of two
    *connections* contending, so each host needs its own; a shared connection would serialise the
    contention out of existence and prove nothing. The seeded rows are therefore committed and
    deleted in ``finally``.
    """
    from app.infrastructure.db.session import make_session_factory

    aggregate_id = uuid4()
    event_ids = [uuid4() for _ in range(12)]
    factory = make_session_factory(engine)

    async with factory() as session:
        for event_id in event_ids:
            await session.execute(
                insert(EventOutbox).values(
                    id=event_id,
                    aggregate_type="replica-probe",
                    aggregate_id=aggregate_id,
                    event_type="probe.raised",
                    payload={"probe": True},
                    occurred_at=datetime.now(UTC),
                )
            )
        await session.commit()

    publisher = _RecordingPublisher()
    try:
        # One event per pass, so twelve events require twelve separate claims and the two hosts
        # genuinely contend. With the default batch of 100 a single pass would swallow the lot and
        # the test would prove nothing about concurrency.
        hosts = [WorkerHost([_relay_spec(factory, publisher, batch_size=1)]) for _ in range(2)]
        runs = [asyncio.create_task(host.run()) for host in hosts]

        async def _wait() -> None:
            while len(publisher.delivered) < len(event_ids):
                await asyncio.sleep(0.01)

        try:
            await asyncio.wait_for(_wait(), timeout=_TIMEOUT)
        finally:
            for host in hosts:
                host.request_stop()
        results = await asyncio.wait_for(asyncio.gather(*runs), timeout=_TIMEOUT)

        assert all(r.workers[0].passes > 0 for r in results), "a host never ran"
        assert not any(r.abandoned_any for r in results)

        delivered = [eid for eid in publisher.delivered if eid in set(event_ids)]
        assert sorted(delivered) == sorted(event_ids), "an event was lost"
        assert len(delivered) == len(set(delivered)), "an event was delivered twice"

        async with factory() as session:
            unpublished = await session.scalar(
                select(text("count(*)")).select_from(
                    select(EventOutbox.id)
                    .where(EventOutbox.aggregate_id == aggregate_id)
                    .where(EventOutbox.published_at.is_(None))
                    .subquery()
                )
            )
        assert unpublished == 0
    finally:
        async with factory() as session:
            await session.execute(
                text("DELETE FROM event_outbox WHERE aggregate_id = :aggregate_id"),
                {"aggregate_id": aggregate_id},
            )
            await session.commit()
