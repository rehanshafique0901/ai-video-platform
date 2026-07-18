"""SQLAlchemy implementation of ``IUsageRecordRepository`` (Slice α7.5).

Append-only writer for ``usage_records`` (ADR-0019, partitioned monthly by
``occurred_at``) with ADR-0033 idempotency on ``request_id``. The insert runs
inside a **SAVEPOINT** (``begin_nested``) so that a unique-violation on the
per-partition ``uq_<child>_request_id`` rolls back only the failed insert — not the
caller's whole transaction — leaving the session usable for the follow-up
``get_by_request_id`` the recorder does to recover the existing row.

A NULL ``request_id`` never collides (the ADR-0033 index is partial:
``WHERE request_id IS NOT NULL``), so system-initiated calls always insert.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.interfaces.repositories import IUsageRecordRepository
from app.application.interfaces.usage_recorder import (
    DuplicateRequestIdError,
    NewUsageRecord,
    UsageRecordRow,
)
from app.infrastructure.db.models.usage import UsageRecord


class UsageRecordRepository(IUsageRecordRepository):
    """Idempotent append-only ``usage_records`` writer + request_id reader."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def insert(self, new: NewUsageRecord) -> UsageRecordRow:
        row = UsageRecord(
            tenant_id=new.tenant_id,
            model_id=new.model_id,
            status=new.status,
            unit=new.unit,
            unit_count=new.unit_count,
            estimated_cost=new.estimated_cost,
            currency=new.currency,
            occurred_at=new.occurred_at,
            credits_consumed=new.credits_consumed,
            request_id=new.request_id,
            pricing_id=new.pricing_id,
            tokens_prompt=new.tokens_prompt,
            tokens_completion=new.tokens_completion,
            images_count=new.images_count,
            seconds_generated=new.seconds_generated,
            project_id=new.project_id,
            scene_id=new.scene_id,
            prompt_id=new.prompt_id,
            user_id=new.user_id,
            workflow_run_id=new.workflow_run_id,
            workflow_step_id=new.workflow_step_id,
            latency_ms=new.latency_ms,
            error_code=new.error_code,
            extra=dict(new.extra),
        )
        try:
            # SAVEPOINT: a request_id collision rolls back only this insert, so the
            # caller's transaction survives for the recovery SELECT below.
            async with self._session.begin_nested():
                self._session.add(row)
                await self._session.flush()
        except IntegrityError as exc:
            # Only treat as an ADR-0033 replay when a row for this request_id truly
            # exists (the savepoint has rolled back, so this read is safe); any other
            # integrity error (bad FK, etc.) propagates unchanged.
            if (
                new.request_id is not None
                and await self.get_by_request_id(new.request_id) is not None
            ):
                raise DuplicateRequestIdError(new.request_id) from exc
            raise
        return _row_to_view(row)

    async def get_by_request_id(self, request_id: str) -> UsageRecordRow | None:
        stmt = (
            select(UsageRecord)
            .where(UsageRecord.request_id == request_id)
            .order_by(UsageRecord.occurred_at.desc())
            .limit(1)
        )
        row = (await self._session.execute(stmt)).scalars().first()
        return _row_to_view(row) if row is not None else None


def _dec(value: object) -> Decimal:
    """Numeric columns round-trip as ``Decimal``; coerce defensively (mypy sees float)."""
    return Decimal(str(value))


def _row_to_view(row: UsageRecord) -> UsageRecordRow:
    return UsageRecordRow(
        id=row.id,
        occurred_at=row.occurred_at,
        tenant_id=row.tenant_id,
        model_id=row.model_id,
        request_id=row.request_id,
        unit=row.unit,
        unit_count=_dec(row.unit_count),
        estimated_cost=_dec(row.estimated_cost),
        currency=row.currency,
        status=row.status,
        pricing_id=row.pricing_id,
        credits_consumed=_dec(row.credits_consumed),
        tokens_prompt=row.tokens_prompt,
        tokens_completion=row.tokens_completion,
        images_count=row.images_count,
        seconds_generated=(
            _dec(row.seconds_generated) if row.seconds_generated is not None else None
        ),
    )
