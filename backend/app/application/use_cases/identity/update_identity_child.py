"""``UpdateIdentityChild`` use case (Slice α10.0).

Edits one character, location or prop in place. The child's stable key is not among the
editable fields: the planner and shot records carry it, so renaming ``name`` must never
break a reference (§3). Judgements in order: ``404`` for the world, ``412`` for the
version, ``404`` for an unknown key, then a same-value no-op returns the world unchanged
without bumping it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from app.application.interfaces.repositories import IIdentityRepository
from app.application.interfaces.unit_of_work import IUnitOfWork
from app.application.use_cases.identity._children import ChildKind, child_keys, children_of
from app.core.errors import NotFoundError, ValidationFailedError, VersionConflictError
from app.domain.identity_runtime import IdentityProfile, IdentityValidationError


class UpdateIdentityChild:
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
        changes: Mapping[str, Any],
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

            current = next(c for c in children_of(profile, kind) if c.key == key)
            effective = {k: v for k, v in changes.items() if getattr(current, k) != v}
            if not effective:
                return profile

            try:
                updated = await _update(
                    self._uow.identities,
                    kind,
                    profile_id,
                    tenant_id,
                    owner_user_id,
                    expected_version=expected_version,
                    key=key,
                    changes=effective,
                )
            except IdentityValidationError as e:
                raise ValidationFailedError(str(e)) from e
            if updated is None:
                raise VersionConflictError("Resource has been modified.")
            await self._uow.commit()
        return updated


async def _update(
    repo: IIdentityRepository,
    kind: ChildKind,
    profile_id: UUID,
    tenant_id: UUID,
    owner_user_id: UUID,
    *,
    expected_version: int,
    key: str,
    changes: Mapping[str, Any],
) -> IdentityProfile | None:
    if kind == "character":
        return await repo.update_character(
            profile_id,
            tenant_id,
            owner_user_id,
            expected_version=expected_version,
            character_key=key,
            changes=changes,
        )
    if kind == "location":
        return await repo.update_location(
            profile_id,
            tenant_id,
            owner_user_id,
            expected_version=expected_version,
            location_key=key,
            changes=changes,
        )
    return await repo.update_prop(
        profile_id,
        tenant_id,
        owner_user_id,
        expected_version=expected_version,
        prop_key=key,
        changes=changes,
    )
