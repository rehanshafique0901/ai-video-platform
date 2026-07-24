"""SQLAlchemy implementation of ``INotificationRepository`` (Slice α8.5b.3).

The write sink of the notification projection: one ``INSERT`` per delivered export
terminal event. The adapter never reads events, never orchestrates, and never mutates
export/render state — it only persists product state derived from an immutable event
(W8.5b.6).

**Exactly-once is DB-owned (W8.5b.7).** :meth:`add` maps the partial-unique
``uq_notifications_user_id_source_event_id`` violation to ``ConflictError`` so the use
case resolves a relay redelivery as an already-notified no-op — the constraint, not
subscriber control flow, is the race-safe backstop. Mirrors the shape of
``MediaRepository.add`` (deterministic-key idempotency + ``ConflictError`` recovery).

Only the write path ships here; the read/query surface (list, unread counts, mark-read,
archive) is deferred to α8.5b.3r.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.interfaces.repositories import INotificationRepository
from app.core.errors import ConflictError
from app.domain.notifications.notification import Notification as NotificationEntity
from app.infrastructure.db.models.notifications import Notification as NotificationRow


class NotificationRepository(INotificationRepository):
    """Notification persistence adapter (write path only, α8.5b.3)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(
        self,
        *,
        user_id: UUID,
        kind: str,
        title: str,
        body: str | None,
        payload: dict[str, Any],
        source_event_id: UUID | None,
    ) -> NotificationEntity:
        row = NotificationRow(
            user_id=user_id,
            kind=kind,
            title=title,
            body=body,
            payload=payload,
            source_event_id=source_event_id,
            # In-app "delivery" = the committed, visible row (Fork A). Email stays NULL.
            delivered_in_app_at=func.now(),
        )
        self._session.add(row)
        try:
            await self._session.flush()
        except IntegrityError as e:
            # 23505 on ``uq_notifications_user_id_source_event_id`` → this recipient was
            # already notified for this source event (relay redelivery). Surface as
            # ConflictError so ``CreateNotification`` maps it to an idempotent no-op
            # (W8.5b.7 — exactly-once is enforced by the DB, not control flow).
            raise ConflictError(
                "notification already exists for this recipient + source event",
                details={"constraint": _extract_constraint_name(e) or "unknown"},
            ) from e
        await self._session.refresh(row)
        return _row_to_entity(row)


def _row_to_entity(row: NotificationRow) -> NotificationEntity:
    return NotificationEntity(
        id=row.id,
        user_id=row.user_id,
        kind=row.kind,
        title=row.title,
        body=row.body,
        payload=dict(row.payload),
        source_event_id=row.source_event_id,
        delivered_in_app_at=row.delivered_in_app_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _extract_constraint_name(exc: IntegrityError) -> str | None:
    """Best-effort extraction of the failed constraint name from psycopg.

    Mirrors the helper in ``media_repository.py`` / ``project_repository.py``.
    """
    orig = getattr(exc, "orig", None)
    diag = getattr(orig, "diag", None)
    name = getattr(diag, "constraint_name", None)
    return str(name) if name else None
