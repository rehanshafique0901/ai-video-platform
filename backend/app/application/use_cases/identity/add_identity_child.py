"""``AddIdentityChild`` use case (Slice α10.0).

Adds one character, location or prop to the caller's world. One use case for the three
kinds because they are governed identically (``_children``): fenced on the root's version,
capped by what the Decision plane can honour, keyed uniquely inside the profile.

Order of judgements, mirroring the root update: ``404`` (missing / another creator's),
then ``412`` (stale version), then ``422`` (the cap), then ``409`` (the key is taken).
The cap is checked here rather than left to the aggregate so the creator is told *which*
limit they met instead of receiving a rejected write.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from app.application.interfaces.repositories import IIdentityRepository
from app.application.interfaces.unit_of_work import IUnitOfWork
from app.application.use_cases.identity._children import CAPS, ChildKind, child_keys
from app.core.errors import (
    ConflictError,
    NotFoundError,
    ValidationFailedError,
    VersionConflictError,
)
from app.domain.identity_runtime import IdentityProfile, IdentityValidationError


class AddIdentityChild:
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
        name: str,
        attributes: Mapping[str, Any] | None = None,
    ) -> IdentityProfile:
        extra = dict(attributes or {})
        async with self._uow:
            profile = await self._uow.identities.get_profile(profile_id, tenant_id, owner_user_id)
            if profile is None:
                raise NotFoundError(
                    "identity profile not found", details={"identity_id": str(profile_id)}
                )
            if profile.version != expected_version:
                raise VersionConflictError("Resource has been modified.")

            existing = child_keys(profile, kind)
            if len(existing) + 1 > CAPS[kind]:
                raise ValidationFailedError(
                    f"at most {CAPS[kind]} {kind}s per profile",
                    details={"kind": kind, "cap": CAPS[kind]},
                )
            if key in existing:
                raise ConflictError(
                    f"{kind} key already used in this profile",
                    details={"kind": kind, "key": key},
                )

            try:
                updated = await _add(
                    self._uow.identities,
                    kind,
                    profile_id,
                    tenant_id,
                    owner_user_id,
                    expected_version=expected_version,
                    key=key,
                    name=name,
                    extra=extra,
                )
            except IdentityValidationError as e:
                raise ValidationFailedError(str(e)) from e
            if updated is None:
                raise VersionConflictError("Resource has been modified.")
            await self._uow.commit()
        return updated


async def _add(
    repo: IIdentityRepository,
    kind: ChildKind,
    profile_id: UUID,
    tenant_id: UUID,
    owner_user_id: UUID,
    *,
    expected_version: int,
    key: str,
    name: str,
    extra: Mapping[str, Any],
) -> IdentityProfile | None:
    if kind == "character":
        return await repo.add_character(
            profile_id,
            tenant_id,
            owner_user_id,
            expected_version=expected_version,
            character_key=key,
            name=name,
            **extra,
        )
    if kind == "location":
        return await repo.add_location(
            profile_id,
            tenant_id,
            owner_user_id,
            expected_version=expected_version,
            location_key=key,
            name=name,
            **extra,
        )
    return await repo.add_prop(
        profile_id,
        tenant_id,
        owner_user_id,
        expected_version=expected_version,
        prop_key=key,
        name=name,
        **extra,
    )
