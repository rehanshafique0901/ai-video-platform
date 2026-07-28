"""``ListLibraryFolders`` use case (Slice α9.2).

Owner-and-tenant scoped, newest-first, keyset-paginated (mirrors ``ListProjects``).
The optional parent filter is three-state: unset → all folders; ``parent_folder_id``
set with ``filter_by_parent=True`` → that parent's children; ``filter_by_parent=True``
with ``None`` → root folders.
"""

from __future__ import annotations

from uuid import UUID

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.application.pagination import Cursor, Page, decode_cursor, encode_cursor
from app.domain.library.library_folder import LibraryFolder


class ListLibraryFolders:
    def __init__(self, uow: IUnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self,
        *,
        tenant_id: UUID,
        owner_user_id: UUID,
        limit: int,
        parent_folder_id: UUID | None = None,
        filter_by_parent: bool = False,
        cursor_token: str | None = None,
    ) -> Page[LibraryFolder]:
        after = decode_cursor(cursor_token) if cursor_token else None
        after_key = (after.created_at, after.id) if after is not None else None

        async with self._uow:
            rows = await self._uow.library.list_folders(
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                parent_folder_id=parent_folder_id,
                filter_by_parent=filter_by_parent,
                limit=limit + 1,
                after=after_key,
            )

        has_next = len(rows) > limit
        items = rows[:limit]
        next_cursor: str | None = None
        if has_next and items:
            last = items[-1]
            next_cursor = encode_cursor(Cursor(created_at=last.created_at, id=last.id))
        return Page(items=items, next_cursor=next_cursor)
