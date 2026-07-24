"""``MarkNotificationRead`` use case (Slice α8.5b.3r).

Contract:

    POST /api/v1/notifications/{notification_id}/read
      → 200  { data: NotificationPublic, meta }
      → 404  { error: { code: NOT_FOUND, ... } }        (missing / not yours)
      → 401  { error: { code: UNAUTHENTICATED, ... } }  (via CurrentUserDep)

Action verb, not a resource mutation (Fork C) — mirrors ``POST …/render-jobs/{id}/cancel``.
Marks one of the caller's notifications read: writes only ``read_at`` (W8.5b.9), never the
projection identity, source-event linkage, or delivery provenance. A foreign / missing id
is a uniform ``404`` (anti-enumeration, W8.5b.8). A repeat mark-read of an already-read
notification is an idempotent ``200`` (the repository returns the unchanged row).
"""

from __future__ import annotations

from uuid import UUID

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.core.errors import NotFoundError
from app.domain.notifications.notification import Notification


class MarkNotificationRead:
    """Mark one of the authenticated caller's notifications read (idempotent)."""

    def __init__(self, uow: IUnitOfWork) -> None:
        self._uow = uow

    async def execute(self, *, user_id: UUID, notification_id: UUID) -> Notification:
        async with self._uow:
            notification = await self._uow.notifications.mark_read(user_id, notification_id)
            await self._uow.commit()
        if notification is None:
            # Missing or another principal's — indistinguishable (W8.5b.8).
            raise NotFoundError(
                "notification not found",
                details={"notification_id": str(notification_id)},
            )
        return notification
