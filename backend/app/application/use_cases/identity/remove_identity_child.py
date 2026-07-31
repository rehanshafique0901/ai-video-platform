"""``RemoveIdentityChild`` use case (Slice α10.0).

Removes one character, location or prop from the caller's world and bumps the world's
version with it (PF8). ``404`` for the world or an unknown key, ``412`` for a stale
version. Generations already bound to this world are unaffected: they hold a snapshot
taken at acceptance (IDENT-1).
"""

from __future__ import annotations

from uuid import UUID

from app.application.interfaces.repositories import IIdentityRepository
from app.application.interfaces.unit_of_work import IUnitOfWork
from app.application.use_cases.identity._children import ChildKind, child_keys
from app.core.errors import NotFoundError, VersionConflictError
from app.domain.identity_runtime import IdentityProfile


class RemoveIdentityChild:
    def __init__(self, uow: IUnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self,
        *,
        profile_id: UUID,
        tenant_id: UUID,
        owner_user_id: UUID,
        expected_version: int,
        kind: ChildKind,
        key: str,
    ) -> IdentityProfile:
        async with self._uow:
            profile = await self._uow.identities.get_profile(profile_id, tenant_id, owner_user_id)
            if profile is None:
                raise NotFoundError(
                    "identity profile not found", details={"identity_id": str(profile_id)}
                )
            if profile.version != expected_version:
                raise VersionConflictError("Resource has been modified.")
            if key not in child_keys(profile, kind):
                raise NotFoundError(f"{kind} not found", details={"kind": kind, "key": key})

            updated = await _remove(
                self._uow.identities,
                kind,
                profile_id,
                tenant_id,
                owner_user_id,
                expected_version=expected_version,
                key=key,
            )
            if updated is None:
                raise VersionConflictError("Resource has been modified.")
            await self._uow.commit()
        return updated


async def _remove(
    repo: IIdentityRepository,
    kind: ChildKind,
    profile_id: UUID,
    tenant_id: UUID,
    owner_user_id: UUID,
    *,
    expected_version: int,
    key: str,
) -> IdentityProfile | None:
    if kind == "character":
        return await repo.remove_character(
            profile_id,
            tenant_id,
            owner_user_id,
            expected_version=expected_version,
            character_key=key,
        )
    if kind == "location":
        return await repo.remove_location(
            profile_id,
            tenant_id,
            owner_user_id,
            expected_version=expected_version,
            location_key=key,
        )
    return await repo.remove_prop(
        profile_id,
        tenant_id,
        owner_user_id,
        expected_version=expected_version,
        prop_key=key,
    )
