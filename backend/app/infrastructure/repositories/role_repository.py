"""SQLAlchemy implementation of ``IRoleRepository``.

α2a needs ``assign_role_by_code`` only, called twice per registration:
once for ``user`` and once for ``owner``. Idempotent via ON CONFLICT
DO NOTHING on the composite primary key ``(role_id, user_id)``.

Role rows themselves are seeded by migration ``0002_seed_system_data``
(see ``INSERT INTO roles`` for the canonical list). Attempting to
assign an unknown role is a caller bug and surfaces as
``NotFoundError``.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.interfaces.repositories import IRoleRepository
from app.core.errors import NotFoundError
from app.infrastructure.db.models.identity import Role, RoleUser


class RoleRepository(IRoleRepository):
    """Role assignment persistence adapter."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def assign_role_by_code(
        self,
        user_id: UUID,
        role_code: str,
        granted_by_user_id: UUID | None = None,
    ) -> None:
        role_id = await self._resolve_role_id(role_code)
        stmt = (
            pg_insert(RoleUser)
            .values(
                role_id=role_id,
                user_id=user_id,
                granted_by_user_id=granted_by_user_id,
            )
            .on_conflict_do_nothing(index_elements=["role_id", "user_id"])
        )
        await self._session.execute(stmt)

    async def _resolve_role_id(self, role_code: str) -> UUID:
        stmt = select(Role.id).where(Role.code == role_code)
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is None:
            raise NotFoundError(
                f"role {role_code!r} does not exist",
                details={"role_code": role_code},
            )
        return row
