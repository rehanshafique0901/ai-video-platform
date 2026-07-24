"""``Notification`` domain entity — the in-app notification projection (α8.5b.3 / α8.5b.3r).

A projection of the ``notifications`` table (``schema.md`` §25 /
``models/notifications.py``). Frozen for value-semantics — the same discipline as
:class:`app.domain.export.export_job.ExportJob`.

A notification is **product state derived from an immutable event** (W8.5b.6): the
projection reads a terminal, already-committed outbox event and writes exactly one row
per recipient per source event (W8.5b.7). ``source_event_id`` is the outbox ``event.id``
that produced the row — the logical dedupe key behind the partial-unique
``(user_id, source_event_id)`` index (nullable so non-event notifications remain
expressible; no FK to the transient outbox).

**Read-state (α8.5b.3r).** ``read_at`` and ``archived`` are the mutable *metadata* the
read API manages — they are legitimate domain state, not repository-only implementation
details (α8.5b.3r Fork D / implementation note). Read-state mutations touch only these
fields and never the projection identity, source-event linkage, or delivery provenance
(W8.5b.9); ordering is a pure function of ``(created_at, id)``, independent of ``read_at``
(W8.5b.10). ``archived`` is not yet exposed on the wire (archive is deferred), but it is
modelled here so a full row round-trips faithfully.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID


@dataclass(frozen=True, slots=True)
class Notification:
    """In-app notification — one row of the ``notifications`` table."""

    id: UUID
    user_id: UUID
    kind: str
    title: str
    body: str | None
    payload: dict[str, Any]
    source_event_id: UUID | None
    delivered_in_app_at: datetime | None
    read_at: datetime | None
    archived: bool
    created_at: datetime
    updated_at: datetime


__all__ = ["Notification"]
