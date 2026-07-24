"""``CreateNotification`` — the idempotent write half of the notification projection (α8.5b.3).

Given already-mapped notification content (recipient + kind + title + body + payload +
``source_event_id``), persist exactly one ``notifications`` row inside its own Unit of
Work. The projection upstream (``NotificationProjection``) owns event→content mapping;
this use case owns only the transactional, idempotent insert.

Invariants:
* **W8.5b.6** — pure projection: it only **writes** notification state derived from an
  immutable event. It never mutates export/render/orchestration state, never re-drives
  the export, never dispatches provider/render work.
* **W8.5b.7** — exactly-once per recipient per source event, **DB-enforced**. A relay
  redelivery drives this again; the partial-unique ``(user_id, source_event_id)`` index
  refuses the second write and the repository raises ``ConflictError``, which is treated
  here as a successful **already-notified no-op** — correctness never depends on an
  application-level pre-check.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

import structlog

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.core.errors import ConflictError

_LOGGER = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class CreateNotificationResult:
    """Outcome of one projection write.

    ``status`` is ``"created"`` (a fresh row was persisted) or ``"duplicate"`` (the
    recipient was already notified for this source event — an idempotent no-op).
    ``notification_id`` is set only on ``"created"``.
    """

    status: str
    notification_id: UUID | None = None


class CreateNotification:
    """Persist one in-app notification, idempotent on ``(user_id, source_event_id)``."""

    def __init__(self, uow: IUnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self,
        *,
        user_id: UUID,
        kind: str,
        title: str,
        body: str | None,
        payload: dict[str, Any],
        source_event_id: UUID | None,
    ) -> CreateNotificationResult:
        async with self._uow:
            try:
                notification = await self._uow.notifications.add(
                    user_id=user_id,
                    kind=kind,
                    title=title,
                    body=body,
                    payload=payload,
                    source_event_id=source_event_id,
                )
                await self._uow.commit()
            except ConflictError:
                # Relay redelivered the same source event (or the recipient row is gone):
                # the DB refused the write. Exactly-once is owned by the constraint, so a
                # refusal is a successful no-op — not an error to retry (W8.5b.7).
                _LOGGER.debug(
                    "notification.duplicate_ignored",
                    user_id=str(user_id),
                    source_event_id=str(source_event_id) if source_event_id else None,
                    kind=kind,
                )
                return CreateNotificationResult(status="duplicate")

        _LOGGER.debug(
            "notification.created",
            notification_id=str(notification.id),
            user_id=str(user_id),
            source_event_id=str(source_event_id) if source_event_id else None,
            kind=kind,
        )
        return CreateNotificationResult(status="created", notification_id=notification.id)


__all__ = ["CreateNotification", "CreateNotificationResult"]
