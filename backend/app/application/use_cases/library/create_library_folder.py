"""``CreateLibraryFolder`` use case (Slice α9.2).

Creates a library folder owned by the caller. When a ``parent_folder_id`` is given it
must be one of the caller's own live folders (else a uniform ``404`` —
anti-enumeration). A duplicate live ``name`` under the same parent is a ``409``
(``uq_library_folders_parent_folder_id_name``).
"""

from __future__ import annotations

from uuid import UUID

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.core.errors import ConflictError, NotFoundError
from app.domain.library.library_folder import LibraryFolder


class CreateLibraryFolder:
    def __init__(self, uow: IUnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self,
        *,
        tenant_id: UUID,
        owner_user_id: UUID,
        name: str,
        parent_folder_id: UUID | None = None,
    ) -> LibraryFolder:
        async with self._uow:
            if parent_folder_id is not None:
                parent = await self._uow.library.get_folder(
                    parent_folder_id, tenant_id, owner_user_id
                )
                if parent is None:
                    raise NotFoundError(
                        "library folder not found",
                        details={"folder_id": str(parent_folder_id)},
                    )
            # App-level uniqueness pre-check — the DB index does not constrain root
            # folders (parent IS NULL, NULL-distinct); this closes that gap. The index
            # remains the race-safe backstop for the non-null-parent case.
            if await self._uow.library.folder_name_conflicts(
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                parent_folder_id=parent_folder_id,
                name=name,
            ):
                raise ConflictError("library folder already exists")
            folder = await self._uow.library.add_folder(
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                parent_folder_id=parent_folder_id,
                name=name,
            )
            await self._uow.commit()
        return folder
