"""``CountUnreadNotifications`` use case (Slice α8.5b.3r).

Contract:

    GET /api/v1/notifications/unread-count
      → 200  { data: { count: N }, meta }
      → 401  { error: { code: UNAUTHENTICATED, ... } }  (via CurrentUserDep)

The badge-count read: how many of the caller's notifications are unread and not archived.
Owner-scoped by ``user_id`` (W8.5b.8); matches the ``ix_notifications_user_id_unread``
partial-index predicate so the count is an index-only scan. Side-effect-free.
"""

from __future__ import annotations

from uuid import UUID

from app.application.interfaces.unit_of_work import IUnitOfWork


class CountUnreadNotifications:
    """Return the authenticated caller's unread notification count."""

    def __init__(self, uow: IUnitOfWork) -> None:
        self._uow = uow

    async def execute(self, *, user_id: UUID) -> int:
        async with self._uow:
            return await self._uow.notifications.count_unread(user_id)
