"""``UpdateLibraryAsset`` use case (Slice α9.2).

Version-fenced partial update of the caller's own library asset — mirrors
``UpdateProject`` (404-before-412):

1. ``get_asset`` → ``None`` → ``404`` (missing / not-yours / soft-deleted, incl. a
   soft-deleted underlying media asset — α9.2 §7.2).
2. ``version`` != ``expected_version`` → ``412``.
3. Same-value no-op → return the current row (``200``), no write.
4. Re-file under a folder that is not the caller's own live folder → ``404``.
5. CAS ``update_asset``; a ``None`` return (concurrent bump/delete) → ``412``.

Mutable fields: ``name`` / ``description`` / ``tags`` / ``library_folder_id``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.application.use_cases.library._tags import normalize_tags
from app.core.errors import NotFoundError, VersionConflictError
from app.domain.library.library_asset import LibraryAsset


class UpdateLibraryAsset:
    def __init__(self, uow: IUnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self,
        *,
        asset_id: UUID,
        tenant_id: UUID,
        owner_user_id: UUID,
        expected_version: int,
        changes: Mapping[str, Any],
    ) -> LibraryAsset:
        async with self._uow:
            asset = await self._uow.library.get_asset(asset_id, tenant_id, owner_user_id)
            if asset is None:
                raise NotFoundError(
                    "library asset not found", details={"library_asset_id": str(asset_id)}
                )
            if asset.version != expected_version:
                raise VersionConflictError("Resource has been modified.")

            resolved = dict(changes)
            if "tags" in resolved:
                resolved["tags"] = normalize_tags(resolved["tags"])
            effective = {k: v for k, v in resolved.items() if getattr(asset, k) != v}
            if not effective:
                return asset

            if "library_folder_id" in effective and effective["library_folder_id"] is not None:
                folder = await self._uow.library.get_folder(
                    effective["library_folder_id"], tenant_id, owner_user_id
                )
                if folder is None:
                    raise NotFoundError(
                        "library folder not found",
                        details={"folder_id": str(effective["library_folder_id"])},
                    )

            updated = await self._uow.library.update_asset(
                asset_id,
                tenant_id,
                owner_user_id,
                expected_version=expected_version,
                changes=effective,
            )
            if updated is None:
                raise VersionConflictError("Resource has been modified.")
            await self._uow.commit()
        return updated
