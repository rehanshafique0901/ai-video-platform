"""SQLAlchemy implementation of ``IEventOutboxRepository`` (Slice α7.1).

Writes domain events into the transactional outbox (``event_outbox``, CR-4)
within the caller's UnitOfWork so state + intent-to-publish commit atomically
(blueprint §6 / D9). α7.1 only *produces* rows (``RenderJobCreated`` /
``RenderJobCanceled``); the relay that publishes them and stamps ``published_at``
is a later slice. The row's ``metadata`` column is mapped to the ORM attribute
``metadata_json`` (``metadata`` is reserved by SQLAlchemy's Declarative base).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.interfaces.repositories import IEventOutboxRepository
from app.infrastructure.db.models.events import EventOutbox as EventOutboxRow


class EventOutboxRepository(IEventOutboxRepository):
    """Append-only outbox writer. Publication/relay is a later slice."""

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
