"""DTOs for ``/api/v1/notifications/*`` endpoints (α8.5b.3r read API).

* :class:`NotificationPublic` — the response projection of a
  :class:`app.domain.notifications.notification.Notification`. Field selection is
  deliberate (same discipline as ``schemas/projects.py``): only attributes the API is
  contractually allowed to return are declared here, so adding a field to the domain
  entity never leaks onto the wire unless this DTO is edited. ``read_at`` is the read-state
  the client renders; ``delivered_email_at`` and ``archived`` are intentionally omitted —
  email is a later slice (α8.5b.4) and archive is deferred, so exposing them now would
  advertise a contract the endpoint does not yet honour.
* :class:`UnreadCountPublic` — ``GET /notifications/unread-count`` body (the badge count).
* :class:`MarkAllReadResult` — ``POST /notifications/read-all`` body (rows affected).

There is **no request DTO**: the four endpoints take no body (read-state is server-owned;
list paging is query params). This mirrors the read-model completion scope (A1) — no inbox
authoring surface.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel


class NotificationPublic(BaseModel):
    """Public projection of :class:`app.domain.notifications.notification.Notification`.

    ``read_at`` is ``None`` for an unread notification and the read instant once marked.
    ``source_event_id`` is the outbox event that produced the row (provenance, read-only).
    ``archived`` / ``delivered_email_at`` are intentionally not exposed (deferred / later
    slice).
    """

    id: UUID
    user_id: UUID
    kind: str
    title: str
    body: str | None
    payload: dict[str, Any]
    source_event_id: UUID | None
    read_at: datetime | None
    created_at: datetime
    updated_at: datetime


class UnreadCountPublic(BaseModel):
    """``GET /notifications/unread-count`` body — the unread badge count."""

    count: int


class MarkAllReadResult(BaseModel):
    """``POST /notifications/read-all`` body — the number of notifications marked read."""

    updated: int
