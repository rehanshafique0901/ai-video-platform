"""``Notification`` domain entity — the in-app notification projection (α8.5b.3).

A **slim projection** of the ``notifications`` table (``schema.md`` §25 /
``models/notifications.py``). Frozen for value-semantics — the same discipline as
:class:`app.domain.export.export_job.ExportJob`.

A notification is **product state derived from an immutable event** (W8.5b.6): the
projection reads a terminal, already-committed outbox event and writes exactly one row
per recipient per source event (W8.5b.7). ``source_event_id`` is the outbox ``event.id``
that produced the row — the logical dedupe key behind the partial-unique
``(user_id, source_event_id)`` index (nullable so non-event notifications remain
expressible; no FK to the transient outbox).

Only the columns the write path needs are modelled here; the read/query surface
(unread counts, mark-read, archive) lands in the α8.5b.3r follow-up.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID


@dataclass(frozen=True, slots=True)
class Notification:
    """In-app notification — one row of the ``notifications`` table (slim view)."""

    id: UUID
    user_id: UUID
    kind: str
    title: str
    body: str | None
    payload: dict[str, Any]
    source_event_id: UUID | None
    delivered_in_app_at: datetime | None
    created_at: datetime
    updated_at: datetime


__all__ = ["Notification"]
