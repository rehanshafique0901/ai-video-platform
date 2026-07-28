"""``GetLibraryAsset`` use case (Slice α9.2).

Fetch one library asset owned by the caller; missing / soft-deleted / another's — and
entries whose underlying media is soft-deleted (α9.2 §7.2) — collapse to a uniform ``404``.
"""

from __future__ import annotations

from uuid import UUID

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.core.errors import NotFoundError
from app.domain.library.library_asset import LibraryAsset


class GetLibraryAsset:
    def __init__(self, uow: IUnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self, *, asset_id: UUID, tenant_id: UUID, owner_user_id: UUID
    ) -> LibraryAsset:
        async with self._uow:
            asset = await self._uow.library.get_asset(asset_id, tenant_id, owner_user_id)
        if asset is None:
            raise NotFoundError(
                "library asset not found", details={"library_asset_id": str(asset_id)}
            )
        return asset
