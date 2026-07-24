"""``MarkAllNotificationsRead`` use case (Slice α8.5b.3r).

Contract:

    POST /api/v1/notifications/read-all
      → 200  { data: { updated: N }, meta }
      → 401  { error: { code: UNAUTHENTICATED, ... } }  (via CurrentUserDep)

Action verb (Fork C). Marks every unread, non-archived notification of the caller read in
one bulk CAS: writes only ``read_at`` (W8.5b.9) and never reshuffles the feed (W8.5b.10).
Returns the number of rows affected; a second call returns ``0`` (idempotent no-op).
Owner-scoped by ``user_id`` (W8.5b.8).
"""

from __future__ import annotations

from uuid import UUID

from app.application.interfaces.unit_of_work import IUnitOfWork


class MarkAllNotificationsRead:
    """Mark all the authenticated caller's unread notifications read; return the count."""

    def __init__(self, uow: IUnitOfWork) -> None:
        self._uow = uow

    async def execute(self, *, user_id: UUID) -> int:
        async with self._uow:
            updated = await self._uow.notifications.mark_all_read(user_id)
            await self._uow.commit()
        return updated
