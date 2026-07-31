"""``UpdateIdentityProfile`` use case (Slice α10.0).

Version-fenced partial update of the caller's own world root — 404-before-412, exactly as
``UpdateLibraryAsset`` does it:

1. missing / another creator's → ``404``.
2. ``version`` != ``expected_version`` → ``412``.
3. same-value no-op → return the world unchanged, no write and no version bump.
4. CAS returned nothing (a concurrent write landed first) → ``412``.

Root fields only: ``name``, ``seed`` and the look (``global_style``, ``camera_style``,
``lighting``, ``color_palette``, ``negative_prompt``). Children are edited through their
own operations, which fence on this same version (PF8).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.core.errors import NotFoundError, ValidationFailedError, VersionConflictError
from app.domain.generation.identity import GlobalStyle
from app.domain.identity_runtime import IdentityProfile, IdentityValidationError


class UpdateIdentityProfile:
    def __init__(self, uow: IUnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self,
        *,
        profile_id: UUID,
        tenant_id: UUID,
        owner_user_id: UUID,
        expected_version: int,
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

            effective = {k: v for k, v in changes.items() if getattr(profile, k) != v}
            if not effective:
                return profile
            # The column is text (no new Postgres enum — ADR-0055); the domain and the
            # authoring surface own the validation.
            if isinstance(effective.get("global_style"), GlobalStyle):
                effective["global_style"] = effective["global_style"].value

            try:
                updated = await self._uow.identities.update_profile(
                    profile_id,
                    tenant_id,
                    owner_user_id,
                    expected_version=expected_version,
                    changes=effective,
                )
            except IdentityValidationError as e:
                raise ValidationFailedError(str(e)) from e
            if updated is None:
                raise VersionConflictError("Resource has been modified.")
            await self._uow.commit()
        return updated
