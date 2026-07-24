"""``ListNotifications`` use case (Slice α8.5b.3r).

Contract (API_CONTRACT §1.1 / §6):

    GET /api/v1/notifications?limit=&cursor=
      → 200  { data: [NotificationPublic, ...], meta: { request_id, next_cursor? } }
      → 422  { error: { code: VALIDATION_FAILED, ... } }  (bad cursor)
      → 401  { error: { code: UNAUTHENTICATED, ... } }    (via CurrentUserDep)

The read half of the notifications bounded context: expose the projection α8.5b.3 writes.
Owner-scoped by ``user_id`` (W8.5b.8) — a notification belonging to another principal is
never returned. Ordering is ``created_at DESC, id DESC`` (newest first) — observational
only, independent of read-state (W8.5b.10).

Keyset (cursor) pagination reuses the platform primitive verbatim (Fork B — the α5a
``ListProjects`` shape): decode the opaque cursor into a ``(created_at, id)`` position,
over-fetch ``limit + 1`` to detect a further page, trim, and encode the next cursor.
"""

from __future__ import annotations

from uuid import UUID

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.application.pagination import Cursor, Page, decode_cursor, encode_cursor
from app.domain.notifications.notification import Notification


class ListNotifications:
    """List the authenticated caller's notifications, newest first, keyset-paginated."""

    def __init__(self, uow: IUnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self,
        *,
        user_id: UUID,
        limit: int,
        cursor_token: str | None = None,
    ) -> Page[Notification]:
        # Decode BEFORE opening the UoW: a malformed cursor is a 422 and should not
        # consume a DB connection. ``decode_cursor`` raises ``ValidationFailedError``.
        after = decode_cursor(cursor_token) if cursor_token else None
        after_key = (after.created_at, after.id) if after is not None else None

        async with self._uow:
            rows = await self._uow.notifications.list_for_user(
                user_id,
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
