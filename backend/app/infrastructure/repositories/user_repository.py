"""SQLAlchemy implementation of ``IUserRepository``.

Slice α1 implements only ``count`` and ``exists_by_id`` — enough to
prove the repository → session → DB plumbing works end-to-end without
introducing a ``User`` domain entity. Slice α2 extends both the
interface and this implementation with entity-returning methods.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.interfaces.repositories import IUserRepository
from app.infrastructure.db.models.identity import User


class UserRepository(IUserRepository):
    """User persistence adapter. Soft-deleted rows are excluded."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def count(self) -> int:
        stmt = select(func.count()).select_from(User).where(User.deleted_at.is_(None))
        result = await self._session.execute(stmt)
        return int(result.scalar_one())

    async def exists_by_id(self, user_id: UUID) -> bool:
        stmt = select(User.id).where(User.id == user_id).where(User.deleted_at.is_(None))
        result = await self._session.execute(stmt)
        return result.first() is not None
