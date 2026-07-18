"""SQLAlchemy implementation of ``IModelPricingRepository`` (Slice α7.5, CR-11).

Read-only resolution of the ``ai_model_pricing`` row effective at a point in time
for one ``(model_id, unit)``. Pricing is immutable + append-only: each change
inserts a new row with a fresh ``effective_from`` (and closes the prior row's
``effective_to``). "Effective at ``at``" means ``effective_from <= at`` and
(``effective_to IS NULL`` **or** ``effective_to > at``); the most recent
``effective_from`` wins when historical windows are adjacent.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.interfaces.repositories import IModelPricingRepository
from app.application.interfaces.usage_recorder import EffectivePrice
from app.infrastructure.db.models.ai_models import AIModelPricing


class ModelPricingRepository(IModelPricingRepository):
    """Read-only ``ai_model_pricing`` accessor (effective-at-time resolution)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_effective(
        self, *, model_id: UUID, unit: str, at: datetime
    ) -> EffectivePrice | None:
        stmt = (
            select(AIModelPricing)
            .where(
                AIModelPricing.model_id == model_id,
                AIModelPricing.unit == unit,
                AIModelPricing.effective_from <= at,
                or_(
                    AIModelPricing.effective_to.is_(None),
                    AIModelPricing.effective_to > at,
                ),
            )
            .order_by(AIModelPricing.effective_from.desc())
            .limit(1)
        )
        row = (await self._session.execute(stmt)).scalars().first()
        if row is None:
            return None
        return EffectivePrice(
            pricing_id=row.id,
            unit=row.unit,
            price_per_unit=Decimal(str(row.price_per_unit)),
            currency=row.currency,
        )
