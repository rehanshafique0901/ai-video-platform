"""SQLAlchemy implementation of ``IProviderSettingsRepository`` (Slice α7.4).

Read-only resolution of one ``(provider, key)`` value from ``provider_settings``
with **tenant-shadows-global** precedence: a tenant-scoped row wins over the
global (``tenant_id IS NULL``) row, which is the fallback. This is the minimal
config read seam signed off for α7.4 (Q4) — no fallback/priority/weighting/health
ordering, no writes. The two partial unique indexes on the table guarantee at most
one candidate per scope, so a single ordered query suffices.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.interfaces.repositories import IProviderSettingsRepository
from app.infrastructure.db.models.configuration import ProviderSetting


class ProviderSettingsRepository(IProviderSettingsRepository):
    """Read-only ``provider_settings`` accessor (tenant row shadows global row)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_value(
        self, provider: str, key: str, tenant_id: UUID | None = None
    ) -> Mapping[str, Any] | None:
        # Candidates: the global row (tenant_id IS NULL) and, when a tenant is
        # given, that tenant's row. Order tenant-first so a tenant override shadows
        # the global fallback; LIMIT 1 takes the winner.
        stmt = select(ProviderSetting.value).where(
            ProviderSetting.provider == provider,
            ProviderSetting.key == key,
        )
        if tenant_id is None:
            stmt = stmt.where(ProviderSetting.tenant_id.is_(None))
        else:
            # NB: ``IN (tenant_id, NULL)`` would NOT match the global row (SQL never
            # matches NULL by equality); use an explicit OR so the global row is a
            # real fallback candidate.
            stmt = stmt.where(
                or_(
                    ProviderSetting.tenant_id == tenant_id,
                    ProviderSetting.tenant_id.is_(None),
                )
            ).order_by(
                ProviderSetting.tenant_id.is_(None)  # False (tenant) sorts before True (global)
            )
        stmt = stmt.limit(1)

        result = await self._session.execute(stmt)
        value = result.scalar_one_or_none()
        return value
