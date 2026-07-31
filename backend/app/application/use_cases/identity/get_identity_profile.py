"""``GetIdentityProfile`` use case (Slice α10.0).

Reads one of the caller's worlds with all its children. A world that is missing, or
another creator's, is the same uniform ``404`` (anti-enumeration).
"""

from __future__ import annotations

from uuid import UUID

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.core.errors import NotFoundError
from app.domain.identity_runtime import IdentityProfile


class GetIdentityProfile:
    def __init__(self, uow: IUnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self, *, profile_id: UUID, tenant_id: UUID, owner_user_id: UUID
    ) -> IdentityProfile:
        async with self._uow:
            profile = await self._uow.identities.get_profile(profile_id, tenant_id, owner_user_id)
        if profile is None:
            raise NotFoundError(
                "identity profile not found", details={"identity_id": str(profile_id)}
            )
        return profile
