"""``ListIdentityProfiles`` use case (Slice α10.0).

Owner-and-tenant scoped, newest-first, keyset-paginated browse of the caller's worlds —
the same cursor contract the library slice uses. Each listed world carries its children,
because a world without its cast is not recognisable as the one the creator authored.
"""

from __future__ import annotations

from uuid import UUID

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.application.pagination import Cursor, Page, decode_cursor, encode_cursor
from app.domain.identity_runtime import IdentityProfile


class ListIdentityProfiles:
    def __init__(self, uow: IUnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self,
        *,
        tenant_id: UUID,
        owner_user_id: UUID,
        limit: int,
        cursor_token: str | None = None,
    ) -> Page[IdentityProfile]:
        after = decode_cursor(cursor_token) if cursor_token else None
        after_key = (after.created_at, after.id) if after is not None else None

        async with self._uow:
            rows = await self._uow.identities.list_profiles(
                tenant_id,
                owner_user_id,
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
