"""``ListProjects`` use case (Slice α5a).

Contract (API_CONTRACT §3.2):

    GET /api/v1/projects?limit=&cursor=
      → 200  { data: [ProjectPublic, ...], meta: { request_id, next_cursor? } }
      → 422  { error: { code: VALIDATION_FAILED, ... } }  (bad limit / cursor)
      → 401  { error: { code: UNAUTHENTICATED, ... } }    (via CurrentUserDep)

Owner-and-tenant scoped (α5a D5): only the caller's own live projects
are returned. Ordering is ``created_at DESC, id DESC`` — newest first,
with ``id`` as the deterministic tie-break that makes keyset pagination
stable (α5a D14).

Keyset (cursor) pagination (α5a D6): the use case owns the semantics.
It decodes the opaque incoming cursor into a ``(created_at, id)``
position, asks the repository for ``limit + 1`` rows to detect whether a
further page exists, trims the overflow row, and — if there was one —
encodes the next-page cursor from the last returned row. A ``None``
``next_cursor`` on the resulting :class:`Page` signals the last page.
"""

from __future__ import annotations

from uuid import UUID

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.application.pagination import Cursor, Page, decode_cursor, encode_cursor
from app.domain.projects.project import Project


class ListProjects:
    """List the authenticated caller's projects, newest first, keyset-paginated."""

    def __init__(self, uow: IUnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self,
        *,
        owner_user_id: UUID,
        tenant_id: UUID,
        limit: int,
        cursor_token: str | None = None,
    ) -> Page[Project]:
        # Decode BEFORE opening the UoW: a malformed cursor is a 422 and
        # should not consume a DB connection. ``decode_cursor`` raises
        # ``ValidationFailedError`` on any bad token.
        after = decode_cursor(cursor_token) if cursor_token else None
        after_key = (after.created_at, after.id) if after is not None else None

        async with self._uow:
            # Over-fetch by one to detect a further page without a
            # second COUNT query.
            rows = await self._uow.projects.list_owned(
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
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
