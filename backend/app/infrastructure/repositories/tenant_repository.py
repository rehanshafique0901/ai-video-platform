"""SQLAlchemy implementation of ``ITenantRepository``.

α2a-first. Slots: ``add``, ``get_by_id``, ``exists_by_slug``. Soft-
deleted tenants are excluded from lookups. ``add`` translates the
unique-constraint violation on ``uq_tenants_slug`` into a
``ConflictError`` so the ``RegisterUser`` use case can retry with a
fresh random slug.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.interfaces.repositories import ITenantRepository
from app.core.errors import ConflictError
from app.domain.identity.tenant import Tenant as TenantEntity
from app.infrastructure.db.models.identity import Tenant as TenantRow


class TenantRepository(ITenantRepository):
    """Tenant persistence adapter. Soft-deleted rows are excluded."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, tenant: TenantEntity) -> TenantEntity:
        row = TenantRow(
            id=tenant.id,
            name=tenant.name,
            slug=tenant.slug,
            plan_tier=tenant.plan_tier,
        )
        # Wrap the INSERT in a SAVEPOINT so that a slug-collision
        # ``IntegrityError`` rolls back only this attempt, leaving the
        # outer UoW transaction viable for the ``RegisterUser`` use
        # case to retry with a fresh random slug.
        async with self._session.begin_nested():
            self._session.add(row)
            try:
                await self._session.flush()
            except IntegrityError as e:
                raise ConflictError(
                    "tenant slug already taken",
                    details={"slug": tenant.slug},
                ) from e
        await self._session.refresh(row)
        return _row_to_entity(row)

    async def get_by_id(self, tenant_id: UUID) -> TenantEntity | None:
        stmt = (
            select(TenantRow).where(TenantRow.id == tenant_id).where(TenantRow.deleted_at.is_(None))
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        return _row_to_entity(row) if row is not None else None

    async def exists_by_slug(self, slug: str) -> bool:
        stmt = (
            select(TenantRow.id).where(TenantRow.slug == slug).where(TenantRow.deleted_at.is_(None))
        )
        result = await self._session.execute(stmt)
        return result.first() is not None


def _row_to_entity(row: TenantRow) -> TenantEntity:
    return TenantEntity(
        id=row.id,
        name=row.name,
        slug=row.slug,
        plan_tier=row.plan_tier,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
