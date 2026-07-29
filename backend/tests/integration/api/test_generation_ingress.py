"""Integration tests for α9.7 Generation Ingress against the live database.

Drives the **real** stack — ``CreateGeneration`` → ``SqlGenerationJobStore`` →
``GenerationWorker`` → ``SqlExecutionRuntimeStore`` — inside a SAVEPOINT. The provider
pipeline itself is stubbed (a real run means ffmpeg plus a remote image model), but every
*persistence* boundary this slice introduces is exercised for real, because that is exactly
where the slice's guarantees live:

* the migration `0016` columns, indexes, and the partial unique index behind create idempotency;
* **GEN-1** — ``begin()`` adopting an ingress row without overwriting an ingress-owned column;
* the owner-scoped read model, including the invisibility of legacy ownerless rows;
* the claim CAS and the reaper, which together enforce "one execution, one spend opportunity";
* the **F3** authorisation fix on the promotion read path.

Each test seeds its own tenant/user, so tests are independent and nothing leaks between them.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Self
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.application.interfaces.execution_runtime_store import NewGenerationAsset
from app.application.interfaces.generation_job_store import CancelOutcome
from app.application.interfaces.generation_runner import IGenerationRunner
from app.application.use_cases.generation.create_generation import CreateGeneration
from app.application.use_cases.generation.generation_worker import (
    LOST_WORKER_REASON,
    GenerationWorker,
)
from app.application.use_cases.generation.read_generations import (
    CancelGeneration,
    GetGeneration,
    ListGenerations,
)
from app.application.use_cases.generation.request import GenerateVideoRequest
from app.application.use_cases.generation.request_codec import GenerationRequestSpec
from app.application.use_cases.generation.results import (
    GenerateVideoResult,
    GenerationProvenance,
    GenerationStatus,
)
from app.core.errors import NotFoundError
from app.domain.generation.execution_state import ExecutionStatus, GenerationAssetKind
from app.domain.generation.identity import IdentityProfile
from app.infrastructure.db.models.identity import Tenant, User
from app.infrastructure.generation.execution_runtime_store import SqlExecutionRuntimeStore
from app.infrastructure.generation.generation_job_store import SqlGenerationJobStore
from app.infrastructure.generation.generation_reader import GenerationReader
from app.infrastructure.repositories.distributed_lock_manager import (
    SqlAlchemyDistributedLockManager,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


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
    """Minimal UoW exposing only what the generation worker uses: the lock manager."""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory
        self._session: AsyncSession | None = None

    async def __aenter__(self) -> Self:
        self._session = self._factory()
        self.locks = SqlAlchemyDistributedLockManager(self._session)
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


async def _seed_owner(factory: async_sessionmaker[AsyncSession]) -> tuple[UUID, UUID]:
    """Create an isolated tenant + user; return ``(tenant_id, owner_user_id)``."""
    tenant_id, user_id = uuid4(), uuid4()
    async with factory() as session:
        await session.execute(
            insert(Tenant).values(id=tenant_id, name="Gen", slug=f"gen-{tenant_id}")
        )
        await session.execute(
            insert(User).values(
                id=user_id,
                tenant_id=tenant_id,
                email=f"gen-{user_id}@example.com",
                display_name="Generation Owner",
            )
        )
        await session.commit()
    return tenant_id, user_id


def _spec(prompt: str = "a paper boat on a rainy street", seed: int = 5) -> GenerationRequestSpec:
    return GenerationRequestSpec(prompt=prompt, seed=seed, title="Boat")


def _provenance(generation_id: UUID) -> GenerationProvenance:
    return GenerationProvenance(
        generation_id=generation_id,
        capability="image.generate",
        execution_mode="auto",
        resolver_version="test-resolver",
        chosen_adapter="test-adapter",
        chosen_provider="test-provider",
        planner_version="p1",
    )


class _PipelineRunner(IGenerationRunner):
    """Stubs the provider work but drives the **real** execution-runtime persistence.

    That is the point: the runtime's ``begin()`` must adopt the ingress row rather than
    collide with it, and only a real database can prove the upsert behaves.
    """

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        *,
        succeed: bool = True,
    ) -> None:
        self._store = SqlExecutionRuntimeStore(factory)
        self._succeed = succeed
        self.runs: list[UUID] = []

    async def run(self, request: GenerateVideoRequest) -> GenerateVideoResult:
        generation_id = request.generation_id
        assert generation_id is not None
        self.runs.append(generation_id)
        provenance = _provenance(generation_id)
        await self._store.begin(
            generation_id=generation_id,
            request=request,
            provenance=provenance,
            title=request.title or "untitled",
            shot_count=2,
        )
        if not self._succeed:
            await self._store.fail(generation_id=generation_id, reason="provider refused")
            return GenerateVideoResult(
                status=GenerationStatus.FAILED,
                generation_id=generation_id,
                title=request.title or "untitled",
                provenance=provenance,
                reason="provider refused",
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
                duration_ms=18_000,
            )
        )
        await self._store.complete(
            generation_id=generation_id,
            final_video_asset_id=asset_id,
            storage_backend="local",
            storage_bucket="test",
            storage_key=f"generations/{generation_id}/final.mp4",
            duration_seconds=18.0,
            width=720,
            height=1280,
        )
        return GenerateVideoResult(
            status=GenerationStatus.SUCCEEDED,
            generation_id=generation_id,
            title=request.title or "untitled",
            provenance=provenance,
            video_key=f"generations/{generation_id}/final.mp4",
            duration_seconds=18.0,
        )


def _worker(
    factory: async_sessionmaker[AsyncSession],
    runner: IGenerationRunner,
    **kwargs: object,
) -> GenerationWorker:
    return GenerationWorker(
        uow=_Uow(factory),  # type: ignore[arg-type]
        store=SqlGenerationJobStore(factory),
        runner=runner,
        **kwargs,  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------- #
# the full path
# --------------------------------------------------------------------------- #


async def test_queue_then_execute_then_read(
    bound: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, owner_user_id = await _seed_owner(bound)
    store = SqlGenerationJobStore(bound)

    created = await CreateGeneration(store).execute(
        tenant_id=tenant_id, owner_user_id=owner_user_id, spec=_spec()
    )
    assert created.created is True
    assert created.generation.status == ExecutionStatus.QUEUED.value
    assert created.generation.promotable is False

    runner = _PipelineRunner(bound)
    result = await _worker(bound, runner).run_once()

    assert result.scanned == 1
    assert runner.runs == [created.generation.id]

    view = await GetGeneration(store).execute(
        tenant_id=tenant_id, owner_user_id=owner_user_id, generation_id=created.generation.id
    )
    assert view.status == ExecutionStatus.COMPLETED.value
    assert view.promotable is True
    assert view.duration_seconds == pytest.approx(18.0)
    assert view.shot_count == 2


async def test_a_failed_run_is_terminal_and_not_reclaimed(
    bound: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, owner_user_id = await _seed_owner(bound)
    store = SqlGenerationJobStore(bound)
    created = await CreateGeneration(store).execute(
        tenant_id=tenant_id, owner_user_id=owner_user_id, spec=_spec()
    )

    runner = _PipelineRunner(bound, succeed=False)
    await _worker(bound, runner).run_once()
    # A second poll must not re-run it: max_attempts = 1, and a retry costs real money.
    await _worker(bound, runner).run_once()

    assert runner.runs == [created.generation.id]
    view = await GetGeneration(store).execute(
        tenant_id=tenant_id, owner_user_id=owner_user_id, generation_id=created.generation.id
    )
    assert view.status == ExecutionStatus.FAILED.value
    assert view.promotable is False


# --------------------------------------------------------------------------- #
# GEN-1 — begin() is state initialisation, never an ingress overwrite
# --------------------------------------------------------------------------- #


async def test_begin_preserves_every_ingress_owned_column(
    bound: async_sessionmaker[AsyncSession],
) -> None:
    """GEN-1(3)(4) — ingress owns identity; the runtime owns execution state."""
    tenant_id, owner_user_id = await _seed_owner(bound)
    store = SqlGenerationJobStore(bound)
    created = await CreateGeneration(store).execute(
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        spec=_spec("the original prompt", seed=11),
        idempotency_key="key-gen1",
    )
    gen_id = created.generation.id

    before = await _row(bound, gen_id)
    await store.claim(generation_id=gen_id)

    # The runtime initialises its own state, with a *different* request object — nothing it
    # writes may be allowed to rebind the generation to another owner or another request.
    runtime = SqlExecutionRuntimeStore(bound)
    await runtime.begin(
        generation_id=gen_id,
        request=GenerateVideoRequest(
            prompt="a prompt the runtime made up",
            identity=_identity(999),
            generation_id=gen_id,
        ),
        provenance=_provenance(gen_id),
        title="runtime title",
        shot_count=3,
    )

    after = await _row(bound, gen_id)
    assert after["tenant_id"] == before["tenant_id"]
    assert after["owner_user_id"] == before["owner_user_id"]
    assert after["idempotency_key"] == before["idempotency_key"] == "key-gen1"
    assert after["request"] == before["request"]
    assert after["created_at"] == before["created_at"]
    assert after["prompt"] == "the original prompt"
    # …while runtime-owned fields were initialised.
    assert after["shot_count"] == 3
    assert after["resolver_version"] == "test-resolver"
    assert after["status"] == ExecutionStatus.PLANNING.value


async def test_begin_is_idempotent(bound: async_sessionmaker[AsyncSession]) -> None:
    """GEN-1(5) — repeated calls converge; they never duplicate or corrupt the row."""
    tenant_id, owner_user_id = await _seed_owner(bound)
    store = SqlGenerationJobStore(bound)
    created = await CreateGeneration(store).execute(
        tenant_id=tenant_id, owner_user_id=owner_user_id, spec=_spec()
    )
    gen_id = created.generation.id
    runtime = SqlExecutionRuntimeStore(bound)
    request = GenerateVideoRequest(prompt="p", identity=_identity(1), generation_id=gen_id)

    for _ in range(3):
        await runtime.begin(
            generation_id=gen_id,
            request=request,
            provenance=_provenance(gen_id),
            title="t",
            shot_count=2,
        )

    async with bound() as session:
        count = (
            await session.execute(
                text("SELECT count(*) FROM generations WHERE id = CAST(:id AS uuid)"),
                {"id": str(gen_id)},
            )
        ).scalar_one()
    assert count == 1


async def test_begin_still_creates_a_row_for_a_direct_caller(
    bound: async_sessionmaker[AsyncSession],
) -> None:
    """GEN-1(1) — the demo script and the E2E test path never go through ingress."""
    gen_id = uuid4()
    runtime = SqlExecutionRuntimeStore(bound)

    await runtime.begin(
        generation_id=gen_id,
        request=GenerateVideoRequest(prompt="direct", identity=_identity(3), generation_id=gen_id),
        provenance=_provenance(gen_id),
        title="direct",
        shot_count=1,
    )

    row = await _row(bound, gen_id)
    assert row["prompt"] == "direct"
    # Unowned by construction — and therefore invisible to every owner-scoped read.
    assert row["owner_user_id"] is None


# --------------------------------------------------------------------------- #
# ownership
# --------------------------------------------------------------------------- #


async def test_another_owner_cannot_read_or_cancel(
    bound: async_sessionmaker[AsyncSession],
) -> None:
    tenant_a, owner_a = await _seed_owner(bound)
    tenant_b, owner_b = await _seed_owner(bound)
    store = SqlGenerationJobStore(bound)
    created = await CreateGeneration(store).execute(
        tenant_id=tenant_a, owner_user_id=owner_a, spec=_spec()
    )

    with pytest.raises(NotFoundError):
        await GetGeneration(store).execute(
            tenant_id=tenant_b, owner_user_id=owner_b, generation_id=created.generation.id
        )
    with pytest.raises(NotFoundError):
        await CancelGeneration(store).execute(
            tenant_id=tenant_b, owner_user_id=owner_b, generation_id=created.generation.id
        )
    page = await ListGenerations(store).execute(tenant_id=tenant_b, owner_user_id=owner_b, limit=10)
    assert page.items == []
    # Still queued: a foreign cancel must not have taken effect.
    mine = await GetGeneration(store).execute(
        tenant_id=tenant_a, owner_user_id=owner_a, generation_id=created.generation.id
    )
    assert mine.status == ExecutionStatus.QUEUED.value


async def test_legacy_ownerless_generations_are_invisible(
    bound: async_sessionmaker[AsyncSession],
) -> None:
    """ADR-0052 D1 — invisibility preserves ownership correctness; it is not data loss."""
    tenant_id, owner_user_id = await _seed_owner(bound)
    legacy_id = uuid4()
    async with bound() as session:
        await session.execute(
            text(
                """
                INSERT INTO generations (id, status, prompt, execution_mode)
                VALUES (CAST(:id AS uuid), 'completed', 'legacy prompt', 'auto')
                """
            ),
            {"id": str(legacy_id)},
        )
        await session.commit()

    store = SqlGenerationJobStore(bound)
    assert (
        await store.get_owned(
            tenant_id=tenant_id, owner_user_id=owner_user_id, generation_id=legacy_id
        )
        is None
    )
    assert await store.list_owned(tenant_id=tenant_id, owner_user_id=owner_user_id, limit=10) == []
    # The row is still there — nothing was destroyed, it simply has no owner to attribute it to.
    assert (await _row(bound, legacy_id))["prompt"] == "legacy prompt"


async def test_legacy_ownerless_generations_are_not_promotable(
    bound: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, owner_user_id = await _seed_owner(bound)
    legacy_id = uuid4()
    async with bound() as session:
        await session.execute(
            text(
                """
                INSERT INTO generations (id, status, prompt, execution_mode)
                VALUES (CAST(:id AS uuid), 'completed', 'legacy', 'auto')
                """
            ),
            {"id": str(legacy_id)},
        )
        await session.commit()

    reader = GenerationReader(bound)
    assert (
        await reader.load_final_video(
            generation_id=legacy_id, tenant_id=tenant_id, owner_user_id=owner_user_id
        )
        is None
    )


async def test_f3_a_foreign_generation_is_not_readable_for_promotion(
    bound: async_sessionmaker[AsyncSession],
) -> None:
    """The α9.7 authorisation fix: knowing an id is no longer enough to promote it."""
    tenant_a, owner_a = await _seed_owner(bound)
    tenant_b, owner_b = await _seed_owner(bound)
    store = SqlGenerationJobStore(bound)
    created = await CreateGeneration(store).execute(
        tenant_id=tenant_a, owner_user_id=owner_a, spec=_spec()
    )
    await _worker(bound, _PipelineRunner(bound)).run_once()

    reader = GenerationReader(bound)
    mine = await reader.load_final_video(
        generation_id=created.generation.id, tenant_id=tenant_a, owner_user_id=owner_a
    )
    theirs = await reader.load_final_video(
        generation_id=created.generation.id, tenant_id=tenant_b, owner_user_id=owner_b
    )

    assert mine is not None and mine.has_final_video
    assert theirs is None


# --------------------------------------------------------------------------- #
# idempotency, pagination, cancel, reaping
# --------------------------------------------------------------------------- #


async def test_idempotency_key_yields_one_row_and_one_run(
    bound: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, owner_user_id = await _seed_owner(bound)
    store = SqlGenerationJobStore(bound)
    uc = CreateGeneration(store)

    first = await uc.execute(
        tenant_id=tenant_id, owner_user_id=owner_user_id, spec=_spec(), idempotency_key="k"
    )
    second = await uc.execute(
        tenant_id=tenant_id, owner_user_id=owner_user_id, spec=_spec(), idempotency_key="k"
    )

    assert first.created is True
    assert second.created is False
    assert second.generation.id == first.generation.id

    runner = _PipelineRunner(bound)
    await _worker(bound, runner).run_once()
    assert runner.runs == [first.generation.id]


async def test_the_unique_index_refuses_a_duplicate_key(
    bound: async_sessionmaker[AsyncSession],
) -> None:
    """The database owns the race, not application dedup (ADR-0048 posture)."""
    tenant_id, owner_user_id = await _seed_owner(bound)
    store = SqlGenerationJobStore(bound)
    await store.create(
        generation_id=uuid4(),
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        spec=_spec(),
        idempotency_key="dup",
    )

    # Bypass the read-first fast path to hit the constraint directly, exactly as two
    # concurrent creates would.
    outcome = await store.create(
        generation_id=uuid4(),
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        spec=_spec(),
        idempotency_key="dup",
    )

    assert outcome.created is False
    async with bound() as session:
        count = (
            await session.execute(
                text(
                    "SELECT count(*) FROM generations "
                    "WHERE owner_user_id = CAST(:o AS uuid) AND idempotency_key = 'dup'"
                ),
                {"o": str(owner_user_id)},
            )
        ).scalar_one()
    assert count == 1


async def test_keyset_pagination_has_no_duplicates_or_gaps(
    bound: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, owner_user_id = await _seed_owner(bound)
    store = SqlGenerationJobStore(bound)
    uc = CreateGeneration(store)
    created = [
        (
            await uc.execute(
                tenant_id=tenant_id, owner_user_id=owner_user_id, spec=_spec(f"prompt {i}")
            )
        ).generation.id
        for i in range(5)
    ]

    lister = ListGenerations(store)
    seen: list[UUID] = []
    cursor: str | None = None
    for _ in range(4):
        page = await lister.execute(
            tenant_id=tenant_id, owner_user_id=owner_user_id, limit=2, cursor_token=cursor
        )
        seen.extend(v.id for v in page.items)
        cursor = page.next_cursor
        if cursor is None:
            break

    assert cursor is None
    assert sorted(map(str, seen)) == sorted(map(str, created))


async def test_cancel_before_claim_prevents_execution(
    bound: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, owner_user_id = await _seed_owner(bound)
    store = SqlGenerationJobStore(bound)
    created = await CreateGeneration(store).execute(
        tenant_id=tenant_id, owner_user_id=owner_user_id, spec=_spec()
    )

    view = await CancelGeneration(store).execute(
        tenant_id=tenant_id, owner_user_id=owner_user_id, generation_id=created.generation.id
    )
    runner = _PipelineRunner(bound)
    await _worker(bound, runner).run_once()

    assert view.status == ExecutionStatus.CANCELLED.value
    # Cancelling before the claim is the one moment it can be guaranteed free.
    assert runner.runs == []


async def test_cancel_after_claim_is_refused(bound: async_sessionmaker[AsyncSession]) -> None:
    tenant_id, owner_user_id = await _seed_owner(bound)
    store = SqlGenerationJobStore(bound)
    created = await CreateGeneration(store).execute(
        tenant_id=tenant_id, owner_user_id=owner_user_id, spec=_spec()
    )
    await store.claim(generation_id=created.generation.id)

    outcome = await store.cancel_queued(
        tenant_id=tenant_id, owner_user_id=owner_user_id, generation_id=created.generation.id
    )

    assert outcome is CancelOutcome.NOT_CANCELLABLE


async def test_an_abandoned_run_is_terminalised_and_never_rerun(
    bound: async_sessionmaker[AsyncSession],
) -> None:
    """The crash path ADR-0052's matrix promises: visible spend, never a silent repeat."""
    tenant_id, owner_user_id = await _seed_owner(bound)
    store = SqlGenerationJobStore(bound)
    created = await CreateGeneration(store).execute(
        tenant_id=tenant_id, owner_user_id=owner_user_id, spec=_spec()
    )
    gen_id = created.generation.id
    # Simulate a worker that claimed the row, started generating, and died.
    await store.claim(generation_id=gen_id)
    async with bound() as session:
        await session.execute(
            text(
                "UPDATE generations SET status = 'generating', updated_at = :stale "
                "WHERE id = CAST(:id AS uuid)"
            ),
            {"id": str(gen_id), "stale": datetime.now(UTC) - timedelta(hours=2)},
        )
        await session.commit()

    runner = _PipelineRunner(bound)
    result = await _worker(bound, runner, reap_grace=timedelta(seconds=60)).run_once()

    assert result.reaped == 1
    assert runner.runs == []
    view = await GetGeneration(store).execute(
        tenant_id=tenant_id, owner_user_id=owner_user_id, generation_id=gen_id
    )
    assert view.status == ExecutionStatus.FAILED.value
    assert view.failure_reason == LOST_WORKER_REASON


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _identity(seed: int) -> IdentityProfile:
    return IdentityProfile(seed=seed)


async def _row(factory: async_sessionmaker[AsyncSession], generation_id: UUID) -> dict[str, object]:
    async with factory() as session:
        row = (
            (
                await session.execute(
                    text("SELECT * FROM generations WHERE id = CAST(:id AS uuid)"),
                    {"id": str(generation_id)},
                )
            )
            .mappings()
            .one()
        )
    return dict(row)
