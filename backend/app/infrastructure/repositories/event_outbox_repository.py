"""SQLAlchemy implementation of ``IEventOutboxRepository`` (Slices α7.1 + α7.3).

Writes domain events into the transactional outbox (``event_outbox``, CR-4)
within the caller's UnitOfWork so state + intent-to-publish commit atomically
(blueprint §6 / D9). α7.1 added :meth:`add` (production); **α7.3 adds the relay
read/mark surface** — :meth:`fetch_unpublished` (``FOR UPDATE SKIP LOCKED``
batching over the ``ix_event_outbox_unpublished_occurred_at`` partial index),
:meth:`mark_published`, and :meth:`mark_failed` (at-least-once retry accounting).
``event_outbox`` is a **mutable** table (not in the baseline ``reject_mutation``
set), so those ``UPDATE``s of ``published_at`` / ``attempts`` / ``last_error`` are
exactly the columns the schema provides for the relay. The row's ``metadata``
column is mapped to the ORM attribute ``metadata_json`` (``metadata`` is reserved
by SQLAlchemy's Declarative base).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.interfaces.publisher import OutboxEvent
from app.application.interfaces.repositories import IEventOutboxRepository
from app.infrastructure.db.models.events import EventOutbox as EventOutboxRow


class EventOutboxRepository(IEventOutboxRepository):
    """Transactional-outbox writer (α7.1) + relay read/mark surface (α7.3)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(
        self,
        *,
        aggregate_type: str,
        aggregate_id: UUID,
        event_type: str,
        payload: dict[str, Any],
        occurred_at: datetime,
        event_version: str = "1.0",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        row = EventOutboxRow(
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            event_type=event_type,
            event_version=event_version,
            payload=payload,
            metadata_json=metadata if metadata is not None else {},
            occurred_at=occurred_at,
        )
        self._session.add(row)
        # Flush so the INSERT participates in the caller's transaction and any
        # constraint violation surfaces here (not at commit). ``published_at`` is
        # left NULL (unpublished); ``id`` / ``attempts`` are DB-populated.
        await self._session.flush()

    # ---- relay read/mark surface (α7.3) --------------------------------

    async def fetch_unpublished(self, *, limit: int, max_attempts: int) -> list[OutboxEvent]:
        # Best-effort chronological (occurred_at), tie-broken by id for a
        # deterministic total order within a batch (ADR-0041 D9 never promises a
        # global order). ``attempts < max_attempts`` excludes parked poison rows so
        # one bad event cannot head-of-line-block the queue (α7.3 Q3).
        # ``FOR UPDATE SKIP LOCKED`` lets concurrent relay passes claim disjoint
        # batches; the locks release when the relay's transaction commits.
        stmt = (
            select(EventOutboxRow)
            .where(EventOutboxRow.published_at.is_(None))
            .where(EventOutboxRow.attempts < max_attempts)
            .order_by(EventOutboxRow.occurred_at.asc(), EventOutboxRow.id.asc())
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_row_to_event(r) for r in rows]

    async def mark_published(self, *, event_id: UUID, published_at: datetime) -> None:
        await self._session.execute(
            update(EventOutboxRow)
            .where(EventOutboxRow.id == event_id)
            .values(published_at=published_at)
        )

    async def mark_failed(self, *, event_id: UUID, error: str) -> None:
        # Atomic in-DB increment so ``attempts`` is correct even if the fetched
        # snapshot were stale. ``published_at`` stays NULL → retried next pass.
        await self._session.execute(
            update(EventOutboxRow)
            .where(EventOutboxRow.id == event_id)
            .values(attempts=EventOutboxRow.attempts + 1, last_error=error)
        )


def _row_to_event(row: EventOutboxRow) -> OutboxEvent:
    return OutboxEvent(
        id=row.id,
        aggregate_type=row.aggregate_type,
        aggregate_id=row.aggregate_id,
        event_type=row.event_type,
        event_version=row.event_version,
        payload=dict(row.payload),
        metadata=dict(row.metadata_json),
        occurred_at=row.occurred_at,
        attempts=row.attempts,
    )
