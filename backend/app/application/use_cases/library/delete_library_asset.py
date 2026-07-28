"""``DeleteLibraryAsset`` use case (Slice α9.2).

Soft-deletes the caller's own library asset. The underlying ``media_asset`` is untouched
(the library is a sibling over media). Idempotent-by-404: a repeat delete — or an
unknown / foreign id — is ``404``.
"""

from __future__ import annotations

from uuid import UUID

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.core.errors import NotFoundError


class DeleteLibraryAsset:
    def __init__(self, uow: IUnitOfWork) -> None:
        self._uow = uow

    async def execute(self, *, asset_id: UUID, tenant_id: UUID, owner_user_id: UUID) -> None:
        async with self._uow:
            marked = await self._uow.library.soft_delete_asset(asset_id, tenant_id, owner_user_id)
            if not marked:
                raise NotFoundError(
                    "library asset not found", details={"library_asset_id": str(asset_id)}
                )
            await self._uow.commit()
