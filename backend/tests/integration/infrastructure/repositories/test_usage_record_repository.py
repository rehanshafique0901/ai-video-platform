"""Integration tests for the Usage Recorder persistence seam (Slice α7.5).

Runs against the live database inside a SAVEPOINT that rolls back on teardown, so
no rows persist in the shared Supabase instance. Exercises the real SQL the unit
suite cannot: the **partitioned** ``usage_records`` insert (ADR-0019), the
per-partition ADR-0033 ``uq_<child>_request_id`` unique index, and the
effective-at-time ``ai_model_pricing`` resolution (CR-11). Two tests drive the
full :class:`UsageRecorderService` against a session-bound Unit of Work so the
account → price → insert → replay path is validated end-to-end on real rows.

Coverage map (α7.5 pre-flight §5.3):

* U1 — ``UsageRecordRepository.insert`` lands a row in the correct monthly
  partition and ``get_by_request_id`` reads it back with the priced fields intact.
* U2 — ``ModelPricingRepository.get_effective`` picks the row whose
  ``[effective_from, effective_to)`` window covers ``at`` (newest wins), and
  returns ``None`` for an unpriced ``(model, unit)``.
* U3 — a second insert with the **same** ``request_id`` (same partition) raises
  :class:`DuplicateRequestIdError`; the original row survives (ADR-0033 dedupe).
* U4 — two inserts with ``request_id IS NULL`` both persist (the ADR-0033 index is
  partial: ``WHERE request_id IS NOT NULL``) — system calls never collide.
* U5 — ``UsageRecorderService.record`` prices a terminal IMAGE call end-to-end
  against a seeded ``ai_model_pricing`` row and persists the primary axis.
* U6 — a replayed ``record`` (same ``request_id``) returns the pre-existing row
  with ``idempotent_replay=True`` and writes no second row.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import TracebackType
from typing import Self
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.interfaces.providers import Capability, ProviderStatus, ProviderUsage
from app.application.interfaces.usage_recorder import (
    DuplicateRequestIdError,
    NewUsageRecord,
    RecordUsageCommand,
)
from app.application.use_cases.usage.usage_recorder_service import UsageRecorderService
from app.infrastructure.db.models.ai_models import AIModel, AIModelPricing
from app.infrastructure.db.models.identity import Tenant
from app.infrastructure.db.models.usage import UsageRecord
from app.infrastructure.repositories.model_pricing_repository import ModelPricingRepository
from app.infrastructure.repositories.usage_record_repository import UsageRecordRepository

pytestmark = pytest.mark.integration


# --------------------------------------------------------------------------- #
# Seed helpers                                                                #
# --------------------------------------------------------------------------- #
async def _seed_tenant(session: AsyncSession) -> UUID:
    tenant_id = uuid4()
    await session.execute(
        insert(Tenant).values(id=tenant_id, name="UR Test", slug=f"ur-{tenant_id}")
    )
    await session.flush()
    return tenant_id


async def _seed_model(session: AsyncSession, *, kind: str = "image") -> UUID:
    model_id = uuid4()
    await session.execute(
        insert(AIModel).values(
            id=model_id,
            model_key=f"mk-{model_id}",
            provider="test",
            vendor_model_id="v1",
            kind=kind,
            status="available",
        )
    )
    await session.flush()
    return model_id


async def _seed_pricing(
    session: AsyncSession,
    *,
    model_id: UUID,
    unit: str,
    price: str,
    effective_from: datetime,
    effective_to: datetime | None = None,
    currency: str = "USD",
) -> UUID:
    pricing_id = uuid4()
    await session.execute(
        insert(AIModelPricing).values(
            id=pricing_id,
            model_id=model_id,
            effective_from=effective_from,
            effective_to=effective_to,
            unit=unit,
            price_per_unit=Decimal(price),
            currency=currency,
        )
    )
    await session.flush()
    return pricing_id


def _new_record(
    *,
    tenant_id: UUID,
    model_id: UUID,
    unit: str,
    unit_count: str,
    occurred_at: datetime,
    request_id: str | None,
    estimated_cost: str = "0",
    images_count: int | None = None,
) -> NewUsageRecord:
    return NewUsageRecord(
        tenant_id=tenant_id,
        model_id=model_id,
        status="success",
        unit=unit,
        unit_count=Decimal(unit_count),
        estimated_cost=Decimal(estimated_cost),
        currency="USD",
        occurred_at=occurred_at,
        request_id=request_id,
        images_count=images_count,
    )


# --------------------------------------------------------------------------- #
# Session-bound Unit of Work — drives the service against the SAVEPOINT session #
# --------------------------------------------------------------------------- #
class _SessionUnitOfWork:
    """Minimal UoW exposing only the recorder's two repos on the test session.

    ``commit`` flushes (not a real COMMIT) so writes stay inside the test's
    SAVEPOINT and vanish on teardown, while still being visible to the follow-up
    reads the service performs. The recorder touches no other repository (W7.5.1),
    so only ``usage`` + ``model_pricing`` are wired.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self.usage = UsageRecordRepository(session)
        self.model_pricing = ModelPricingRepository(session)

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
        await self._session.flush()

    async def rollback(self) -> None:
        await self._session.flush()


async def _count_rows(session: AsyncSession, *, request_id: str) -> int:
    stmt = select(func.count()).select_from(UsageRecord).where(UsageRecord.request_id == request_id)
    return int((await session.execute(stmt)).scalar_one())


# --------------------------------------------------------------------------- #
# U1 — partitioned insert + read-back                                          #
# --------------------------------------------------------------------------- #
async def test_u1_partitioned_insert_lands_and_reads_back(session: AsyncSession) -> None:
    tenant_id = await _seed_tenant(session)
    model_id = await _seed_model(session)
    repo = UsageRecordRepository(session)
    request_id = f"req-{uuid4()}"
    occurred_at = datetime.now(UTC)

    view = await repo.insert(
        _new_record(
            tenant_id=tenant_id,
            model_id=model_id,
            unit="image",
            unit_count="2",
            estimated_cost="0.08",
            images_count=2,
            occurred_at=occurred_at,
            request_id=request_id,
        )
    )

    assert view.id is not None
    assert view.unit == "image"
    assert view.unit_count == Decimal("2")
    assert view.estimated_cost == Decimal("0.08")
    assert view.images_count == 2

    fetched = await repo.get_by_request_id(request_id)
    assert fetched is not None
    assert fetched.id == view.id
    assert fetched.tenant_id == tenant_id
    assert fetched.model_id == model_id
    assert fetched.currency == "USD"
    assert fetched.status == "success"


# --------------------------------------------------------------------------- #
# U2 — effective-at-time pricing resolution                                    #
# --------------------------------------------------------------------------- #
async def test_u2_pricing_effective_resolution(session: AsyncSession) -> None:
    model_id = await _seed_model(session)
    repo = ModelPricingRepository(session)

    now = datetime.now(UTC)
    old_from = now - timedelta(days=60)
    switch = now - timedelta(days=30)
    # Historic window [old_from, switch); current open window [switch, ∞).
    await _seed_pricing(
        session,
        model_id=model_id,
        unit="image",
        price="0.02",
        effective_from=old_from,
        effective_to=switch,
    )
    current_id = await _seed_pricing(
        session,
        model_id=model_id,
        unit="image",
        price="0.04",
        effective_from=switch,
    )

    # At now → current (open) window wins.
    effective_now = await repo.get_effective(model_id=model_id, unit="image", at=now)
    assert effective_now is not None
    assert effective_now.pricing_id == current_id
    assert effective_now.price_per_unit == Decimal("0.04000000")
    assert effective_now.currency == "USD"

    # Inside the historic window → the closed row.
    effective_past = await repo.get_effective(
        model_id=model_id, unit="image", at=switch - timedelta(days=1)
    )
    assert effective_past is not None
    assert effective_past.price_per_unit == Decimal("0.02000000")

    # Unpriced unit → None (drives the service's Q5 "missing pricing" branch).
    assert await repo.get_effective(model_id=model_id, unit="video_second", at=now) is None


# --------------------------------------------------------------------------- #
# U3 — duplicate request_id → DuplicateRequestIdError (ADR-0033)               #
# --------------------------------------------------------------------------- #
async def test_u3_duplicate_request_id_raises(session: AsyncSession) -> None:
    tenant_id = await _seed_tenant(session)
    model_id = await _seed_model(session)
    repo = UsageRecordRepository(session)
    request_id = f"req-{uuid4()}"
    occurred_at = datetime.now(UTC)

    first = await repo.insert(
        _new_record(
            tenant_id=tenant_id,
            model_id=model_id,
            unit="image",
            unit_count="1",
            occurred_at=occurred_at,
            request_id=request_id,
            images_count=1,
        )
    )

    # Same request_id + same partition (same occurred_at) → the per-partition
    # partial-unique index fires; the repo surfaces it as a typed replay signal.
    with pytest.raises(DuplicateRequestIdError):
        await repo.insert(
            _new_record(
                tenant_id=tenant_id,
                model_id=model_id,
                unit="image",
                unit_count="1",
                occurred_at=occurred_at,
                request_id=request_id,
                images_count=1,
            )
        )

    # The savepoint rollback left exactly the original row usable.
    assert await _count_rows(session, request_id=request_id) == 1
    fetched = await repo.get_by_request_id(request_id)
    assert fetched is not None
    assert fetched.id == first.id


# --------------------------------------------------------------------------- #
# U4 — NULL request_id rows coexist (partial index)                            #
# --------------------------------------------------------------------------- #
async def test_u4_null_request_id_coexist(session: AsyncSession) -> None:
    tenant_id = await _seed_tenant(session)
    model_id = await _seed_model(session)
    repo = UsageRecordRepository(session)
    occurred_at = datetime.now(UTC)

    a = await repo.insert(
        _new_record(
            tenant_id=tenant_id,
            model_id=model_id,
            unit="image",
            unit_count="1",
            occurred_at=occurred_at,
            request_id=None,
            images_count=1,
        )
    )
    b = await repo.insert(
        _new_record(
            tenant_id=tenant_id,
            model_id=model_id,
            unit="image",
            unit_count="1",
            occurred_at=occurred_at,
            request_id=None,
            images_count=1,
        )
    )

    assert a.id != b.id


# --------------------------------------------------------------------------- #
# U5 — service prices a terminal call end-to-end                               #
# --------------------------------------------------------------------------- #
async def test_u5_service_prices_terminal_image_end_to_end(session: AsyncSession) -> None:
    tenant_id = await _seed_tenant(session)
    model_id = await _seed_model(session)
    await _seed_pricing(
        session,
        model_id=model_id,
        unit="image",
        price="0.05",
        effective_from=datetime.now(UTC) - timedelta(days=1),
    )
    service = UsageRecorderService(uow=_SessionUnitOfWork(session))  # type: ignore[arg-type]
    request_id = f"req-{uuid4()}"

    view = await service.record(
        RecordUsageCommand(
            tenant_id=tenant_id,
            model_id=model_id,
            status=ProviderStatus.SUCCEEDED,
            request_id=request_id,
            capability=Capability.IMAGE,
            usage=ProviderUsage(unit="image", quantity=3),
        )
    )

    assert view.idempotent_replay is False
    assert view.unit == "image"
    assert view.unit_count == Decimal("3")
    # 3 images × $0.05 = $0.15.
    assert view.estimated_cost == Decimal("0.15000000")
    assert view.currency == "USD"

    row = await UsageRecordRepository(session).get_by_request_id(request_id)
    assert row is not None
    assert row.images_count == 3
    assert row.pricing_id is not None


# --------------------------------------------------------------------------- #
# U6 — service replay is idempotent (no second row)                            #
# --------------------------------------------------------------------------- #
async def test_u6_service_replay_is_idempotent(session: AsyncSession) -> None:
    tenant_id = await _seed_tenant(session)
    model_id = await _seed_model(session)
    await _seed_pricing(
        session,
        model_id=model_id,
        unit="image",
        price="0.05",
        effective_from=datetime.now(UTC) - timedelta(days=1),
    )
    service = UsageRecorderService(uow=_SessionUnitOfWork(session))  # type: ignore[arg-type]
    request_id = f"req-{uuid4()}"
    command = RecordUsageCommand(
        tenant_id=tenant_id,
        model_id=model_id,
        status=ProviderStatus.SUCCEEDED,
        request_id=request_id,
        capability=Capability.IMAGE,
        usage=ProviderUsage(unit="image", quantity=1),
        occurred_at=datetime.now(UTC),
    )

    first = await service.record(command)
    second = await service.record(command)

    assert first.idempotent_replay is False
    assert second.idempotent_replay is True
    assert second.id == first.id
    assert await _count_rows(session, request_id=request_id) == 1
