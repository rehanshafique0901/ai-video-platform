"""``ListLibraryAssets`` use case (Slice α9.2).

Owner-and-tenant scoped, newest-first, keyset-paginated browse. Entries whose underlying
media is soft-deleted are excluded (α9.2 §7.2). Optional three-state folder filter and an
ANY-of ``tags`` filter (GIN-backed) narrow the result.
"""

from __future__ import annotations

from uuid import UUID

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.application.pagination import Cursor, Page, decode_cursor, encode_cursor
from app.application.use_cases.library._tags import normalize_tags
from app.domain.library.library_asset import LibraryAsset


class ListLibraryAssets:
    def __init__(self, uow: IUnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self,
        *,
        tenant_id: UUID,
        owner_user_id: UUID,
        limit: int,
        folder_id: UUID | None = None,
        filter_by_folder: bool = False,
        tags: tuple[str, ...] | None = None,
        cursor_token: str | None = None,
    ) -> Page[LibraryAsset]:
        after = decode_cursor(cursor_token) if cursor_token else None
        after_key = (after.created_at, after.id) if after is not None else None
        normalized_tags = normalize_tags(tags) if tags else None

        async with self._uow:
            rows = await self._uow.library.list_assets(
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                folder_id=folder_id,
                filter_by_folder=filter_by_folder,
                tags=normalized_tags,
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
