"""``DeleteLibraryFolder`` use case (Slice α9.2).

Soft-deletes the caller's own folder. Per α9.2 §7.4:

* A folder with **live sub-folders** cannot be deleted (empty it first) → ``409``.
* Contained **assets are detached** (``library_folder_id = NULL``), never deleted, so
  reuse survives reorganisation.
* Idempotent-by-404: a repeat delete (or an unknown/foreign id) is ``404``.
"""

from __future__ import annotations

from uuid import UUID

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.core.errors import ConflictError, NotFoundError


class DeleteLibraryFolder:
    def __init__(self, uow: IUnitOfWork) -> None:
        self._uow = uow

    async def execute(self, *, folder_id: UUID, tenant_id: UUID, owner_user_id: UUID) -> None:
        async with self._uow:
            folder = await self._uow.library.get_folder(folder_id, tenant_id, owner_user_id)
            if folder is None:
                raise NotFoundError(
                    "library folder not found", details={"folder_id": str(folder_id)}
                )
            if await self._uow.library.folder_has_children(folder_id, tenant_id, owner_user_id):
                raise ConflictError(
                    "library folder is not empty (contains sub-folders)",
                    details={"folder_id": str(folder_id)},
                )
            await self._uow.library.detach_assets_from_folder(folder_id, tenant_id, owner_user_id)
            marked = await self._uow.library.soft_delete_folder(folder_id, tenant_id, owner_user_id)
            if not marked:
                raise NotFoundError(
                    "library folder not found", details={"folder_id": str(folder_id)}
                )
            await self._uow.commit()
