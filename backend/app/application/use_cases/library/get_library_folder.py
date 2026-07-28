"""``GetLibraryFolder`` use case (Slice α9.2).

Fetch one folder owned by the caller; missing / soft-deleted / another's collapse to a
uniform ``404`` (anti-enumeration).
"""

from __future__ import annotations

from uuid import UUID

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.core.errors import NotFoundError
from app.domain.library.library_folder import LibraryFolder


class GetLibraryFolder:
    def __init__(self, uow: IUnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self, *, folder_id: UUID, tenant_id: UUID, owner_user_id: UUID
    ) -> LibraryFolder:
        async with self._uow:
            folder = await self._uow.library.get_folder(folder_id, tenant_id, owner_user_id)
        if folder is None:
            raise NotFoundError("library folder not found", details={"folder_id": str(folder_id)})
        return folder
