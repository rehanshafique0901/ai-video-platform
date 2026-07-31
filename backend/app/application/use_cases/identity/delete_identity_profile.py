"""``DeleteIdentityProfile`` use case (Slice α10.0).

A hard delete (PF10): the children go with the world, and nothing else has to be
consulted first. Generations that were bound to this world keep working — they hold a
snapshot taken at acceptance, not a reference (IDENT-1), and their ``identity_id``
stays behind as the honest record that this world existed and no longer does.
"""

from __future__ import annotations

from uuid import UUID

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.core.errors import NotFoundError


class DeleteIdentityProfile:
    def __init__(self, uow: IUnitOfWork) -> None:
        self._uow = uow

    async def execute(self, *, profile_id: UUID, tenant_id: UUID, owner_user_id: UUID) -> None:
        async with self._uow:
            deleted = await self._uow.identities.delete_profile(
                profile_id, tenant_id, owner_user_id
            )
            if not deleted:
                raise NotFoundError(
                    "identity profile not found", details={"identity_id": str(profile_id)}
                )
            await self._uow.commit()
