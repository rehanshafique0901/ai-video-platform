"""α8.6 Increment 4 — the persistent Execution Runtime store (SQL implementation).

Implements :class:`IExecutionRuntimeStore` by composing the raw-SQL generation
ledger + asset repositories, the reused resolution-ledger writer, and the ORM
event outbox — all over short, per-step transactions (generation is long-running,
so no single transaction spans the whole run). Lifecycle events are emitted
through the outbox **co-transactionally** with the state change that produced them
(contract §6).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.interfaces.capability_resolver import CapabilityResolution
from app.application.interfaces.execution_runtime_store import (
    IExecutionRuntimeStore,
    NewGenerationAsset,
    ShotRecord,
)
from app.application.interfaces.resolution_ledger import ExecutionOutcome
from app.application.use_cases.generation.events import (
    AGGREGATE_TYPE,
    EVENT_EXPORT_COMPLETED,
    EVENT_GENERATION_STARTED,
    EVENT_REPAIR_SUCCEEDED,
    EVENT_SHOT_GENERATED,
    EVENT_VERIFICATION_FAILED,
    EVENT_VIDEO_RENDERED,
)
from app.application.use_cases.generation.request import GenerateVideoRequest
from app.application.use_cases.generation.results import GenerationProvenance
from app.domain.generation.execution_state import ExecutionStatus
from app.infrastructure.repositories.event_outbox_repository import EventOutboxRepository
from app.infrastructure.repositories.generation_asset_repository import GenerationAssetRepository
from app.infrastructure.repositories.generation_ledger_repository import GenerationLedgerRepository
from app.infrastructure.repositories.resolution_ledger_writer import ResolutionLedgerWriter


class SqlExecutionRuntimeStore(IExecutionRuntimeStore):
    """Persist the generation lifecycle + artefacts + provenance + events."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def begin(
        self,
        *,
        generation_id: UUID,
        request: GenerateVideoRequest,
        provenance: GenerationProvenance,
        title: str,
        shot_count: int,
    ) -> None:
        async with self._session_factory() as session:
            await GenerationLedgerRepository(session).insert_generation(
                generation_id=generation_id,
                request=request,
                provenance=provenance,
                title=title,
                shot_count=shot_count,
            )
            await self._emit(
                session,
                generation_id,
                EVENT_GENERATION_STARTED,
                {
                    "title": title,
                    "shot_count": shot_count,
                    "execution_mode": request.execution_mode.value,
                    "chosen_adapter": provenance.chosen_adapter,
                },
            )
            await session.commit()

    async def set_status(self, *, generation_id: UUID, status: ExecutionStatus) -> None:
        async with self._session_factory() as session:
            await GenerationLedgerRepository(session).update_status(
                generation_id=generation_id, status=status
            )
            await session.commit()

    async def record_resolution(
        self,
        *,
        generation_id: UUID,
        resolution: CapabilityResolution,
        outcome: ExecutionOutcome,
    ) -> None:
        if resolution.resolution is None:
            return  # nothing to persist without the underlying pure-resolver decision
        async with self._session_factory() as session:
            await ResolutionLedgerWriter(session).record(
                generation_id=generation_id,
                resolution=resolution.resolution,
                chosen_adapter=resolution.top.adapter_id if resolution.top else None,
                execution_result=outcome,
            )
            await session.commit()

    async def register_asset(self, asset: NewGenerationAsset) -> UUID:
        async with self._session_factory() as session:
            asset_id = await GenerationAssetRepository(session).insert(asset)
            await session.commit()
            return asset_id

    async def record_shot(self, shot: ShotRecord) -> None:
        async with self._session_factory() as session:
            await GenerationLedgerRepository(session).insert_shot(shot)
            if shot.accepted:
                await self._emit(
                    session,
                    shot.generation_id,
                    EVENT_SHOT_GENERATED,
                    {"shot_number": shot.shot_number, "asset_id": _opt_uuid(shot.asset_id)},
                )
                if shot.repair_count > 0:
                    await self._emit(
                        session,
                        shot.generation_id,
                        EVENT_REPAIR_SUCCEEDED,
                        {"shot_number": shot.shot_number, "repair_count": shot.repair_count},
                    )
            else:
                await self._emit(
                    session,
                    shot.generation_id,
                    EVENT_VERIFICATION_FAILED,
                    {"shot_number": shot.shot_number, "reason": shot.reason or ""},
                )
            await session.commit()

    async def complete(
        self,
        *,
        generation_id: UUID,
        final_video_asset_id: UUID,
        storage_backend: str,
        storage_bucket: str,
        storage_key: str,
        duration_seconds: float | None,
        width: int | None,
        height: int | None,
    ) -> None:
        async with self._session_factory() as session:
            await GenerationLedgerRepository(session).mark_completed(
                generation_id=generation_id,
                final_video_asset_id=final_video_asset_id,
                video_backend=storage_backend,
                video_bucket=storage_bucket,
                video_key=storage_key,
                duration_seconds=duration_seconds,
                width=width,
                height=height,
            )
            await self._emit(
                session,
                generation_id,
                EVENT_VIDEO_RENDERED,
                {"asset_id": str(final_video_asset_id), "duration_seconds": duration_seconds},
            )
            await self._emit(
                session,
                generation_id,
                EVENT_EXPORT_COMPLETED,
                {"storage_key": storage_key, "storage_backend": storage_backend},
            )
            await session.commit()

    async def fail(self, *, generation_id: UUID, reason: str) -> None:
        async with self._session_factory() as session:
            await GenerationLedgerRepository(session).mark_failed(
                generation_id=generation_id, reason=reason
            )
            await session.commit()

    @staticmethod
    async def _emit(
        session: AsyncSession,
        generation_id: UUID,
        event_type: str,
        payload: dict[str, object],
    ) -> None:
        await EventOutboxRepository(session).add(
            aggregate_type=AGGREGATE_TYPE,
            aggregate_id=generation_id,
            event_type=event_type,
            payload=payload,
            occurred_at=datetime.now(UTC),
        )


def _opt_uuid(value: UUID | None) -> str | None:
    return str(value) if value else None
