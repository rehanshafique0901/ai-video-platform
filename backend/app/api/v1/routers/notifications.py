"""``/api/v1/notifications/*`` HTTP router (α8.5b.3r — notification read API).

Four endpoints, all authenticated via :data:`CurrentUserDep` (the sole authentication
seam — no repository access, no business logic in this module). This is the read/query
completion of the notifications bounded context whose write projection landed in α8.5b.3:

* ``GET  /notifications``                     → 200, list the caller's notifications,
  newest first, keyset-paginated via ``?limit=`` + opaque ``?cursor=`` (reuses the α5a
  pagination primitive — Fork B).
* ``GET  /notifications/unread-count``        → 200, the unread badge count.
* ``POST /notifications/{id}/read``           → 200, mark one notification read
  (action verb — Fork C; idempotent; 404 if missing / not the caller's — W8.5b.8).
* ``POST /notifications/read-all``            → 200, mark all unread read (returns count).

Notifications are addressed to a **user** (ownership is ``user_id`` alone — no
``tenant_id`` scope, unlike projects/media). Read-state mutations write only ``read_at``
(W8.5b.9) and never reorder the feed (W8.5b.10). The router stays thin: DTO projection +
envelope; ownership scoping, pagination, and idempotency live in the use cases /
repository. Errors (``NotFoundError`` → 404, ``ValidationFailedError`` → 422) are rendered
by the handlers in ``app.core.errors``; this module contains no try / except.

Route ordering note: the literal ``/unread-count`` and ``/read-all`` paths are declared
**before** the parameterised ``/{notification_id}/read`` so FastAPI never mis-captures
them as a ``notification_id`` — though they never could here (``unread-count`` is not a
UUID, and ``read-all`` has no ``/read`` suffix), the explicit ordering keeps the intent
obvious.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from app.api.v1.deps import (
    CountUnreadNotificationsDep,
    CurrentUserDep,
    ListNotificationsDep,
    MarkAllNotificationsReadDep,
    MarkNotificationReadDep,
)
from app.api.v1.helpers import envelope
from app.api.v1.schemas.notifications import (
    MarkAllReadResult,
    NotificationPublic,
    UnreadCountPublic,
)
from app.domain.notifications.notification import Notification

router = APIRouter(prefix="/notifications", tags=["notifications"])


def _to_public(notification: Notification) -> NotificationPublic:
    """Project a domain ``Notification`` into the wire DTO ``NotificationPublic``."""
    return NotificationPublic(
        id=notification.id,
        user_id=notification.user_id,
        kind=notification.kind,
        title=notification.title,
        body=notification.body,
        payload=notification.payload,
        source_event_id=notification.source_event_id,
        read_at=notification.read_at,
        created_at=notification.created_at,
        updated_at=notification.updated_at,
    )


@router.get("")
async def list_notifications(
    request: Request,
    current_user: CurrentUserDep,
    use_case: ListNotificationsDep,
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = Query(default=None),
) -> JSONResponse:
    """List the caller's notifications, newest first, keyset-paginated.

    ``limit`` is clamped to ``1..100`` by FastAPI (out of range → 422 before the handler).
    ``cursor`` is the opaque token from a prior response's ``meta.next_cursor``; a
    malformed token is a 422 (``ValidationFailedError``). ``meta.next_cursor`` is present
    iff a further page exists. Only the caller's own notifications are returned (W8.5b.8);
    ordering is observational and unaffected by read-state (W8.5b.10).
    """
    page = await use_case.execute(
        user_id=current_user.id,
        limit=limit,
        cursor_token=cursor,
    )
    return JSONResponse(
        content=envelope(
            [_to_public(n) for n in page.items],
            request,
            next_cursor=page.next_cursor,
        )
    )


@router.get("/unread-count")
async def unread_count(
    request: Request,
    current_user: CurrentUserDep,
    use_case: CountUnreadNotificationsDep,
) -> JSONResponse:
    """Return the caller's unread (non-archived) notification count for the badge."""
    count = await use_case.execute(user_id=current_user.id)
    return JSONResponse(content=envelope(UnreadCountPublic(count=count), request))


@router.post("/read-all")
async def mark_all_read(
    request: Request,
    current_user: CurrentUserDep,
    use_case: MarkAllNotificationsReadDep,
) -> JSONResponse:
    """Mark all the caller's unread notifications read; return the number affected.

    Idempotent: a second call returns ``updated: 0``. Writes only ``read_at`` (W8.5b.9)
    and never reorders the feed (W8.5b.10).
    """
    updated = await use_case.execute(user_id=current_user.id)
    return JSONResponse(content=envelope(MarkAllReadResult(updated=updated), request))


@router.post("/{notification_id}/read")
async def mark_read(
    notification_id: UUID,
    request: Request,
    current_user: CurrentUserDep,
    use_case: MarkNotificationReadDep,
) -> JSONResponse:
    """Mark one of the caller's notifications read (idempotent action verb).

    Returns ``200`` with the updated ``NotificationPublic`` (a repeat mark-read is a
    ``200`` no-op). A missing notification — or one belonging to another user — yields a
    uniform ``404`` (anti-enumeration, W8.5b.8). Non-UUID path → 422; missing auth → 401.
    """
    notification = await use_case.execute(
        user_id=current_user.id,
        notification_id=notification_id,
    )
    return JSONResponse(content=envelope(_to_public(notification), request))
