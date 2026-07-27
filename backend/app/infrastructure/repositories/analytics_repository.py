"""SQLAlchemy implementation of ``IAnalyticsRepository`` (α9.0 — Creator Analytics).

Two responsibilities, both strictly additive over the dormant, partitioned
``analytics_events`` table:

* :meth:`add` — the write sink of the outbox analytics projection: one ``INSERT`` per
  already-completed publish/export action. **Exactly-once is DB-owned (ADR-0048):** the
  partial-unique ``uq_analytics_events_source_event_id`` over ``(source_event_id,
  occurred_at)`` is mapped to ``ConflictError`` so the use case resolves a relay redelivery
  as an already-recorded no-op. Mirrors ``NotificationRepository.add``.
* :meth:`summary_for_owner` — the owner-scoped read aggregate behind ``GET
  /analytics/summary``: per-``event_name`` counts over a half-open ``[since, until)`` window,
  served off ``ix_analytics_events_user_id_occurred_at``.

The adapter never reads outbox events, never orchestrates, and never mutates the frozen
publish/export runtime.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.interfaces.repositories import IAnalyticsRepository
from app.core.errors import ConflictError
from app.domain.analytics.analytics_event import AnalyticsEventCount
from app.infrastructure.db.models.analytics import AnalyticsEvent as AnalyticsEventRow


class AnalyticsRepository(IAnalyticsRepository):
    """Analytics persistence adapter (α9.0 write projection + owner-scoped read)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        event_name: str,
        properties: dict[str, Any],
        source_event_id: UUID,
        occurred_at: datetime,
    ) -> None:
        # ``id`` is set explicitly (rather than leaning on the server default + a RETURNING
        # refresh) because the table is partitioned and the caller never needs the row back —
        # a plain append. ``occurred_at`` is the producing event's timestamp (deterministic),
        # which also selects the monthly partition (ADR-0048).
        row = AnalyticsEventRow(
            id=uuid4(),
            tenant_id=tenant_id,
            user_id=user_id,
            event_name=event_name,
            properties=properties,
            source_event_id=source_event_id,
            occurred_at=occurred_at,
        )
        self._session.add(row)
        try:
            await self._session.flush()
        except IntegrityError as e:
            # 23505 on ``uq_analytics_events_source_event_id`` → this source event was
            # already recorded (relay redelivery). Surface as ConflictError so
            # ``RecordAnalyticsEvent`` maps it to an idempotent no-op (ADR-0048 —
            # exactly-once is enforced by the DB, not control flow).
            raise ConflictError(
                "analytics event already recorded for this source event",
                details={"constraint": _extract_constraint_name(e) or "unknown"},
            ) from e

    async def summary_for_owner(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        since: datetime,
        until: datetime,
    ) -> list[AnalyticsEventCount]:
        # Half-open window so adjacent windows never double-count a boundary event. Scoped by
        # (tenant_id, user_id); the (user_id, occurred_at) partial index bounds the scan.
        stmt = text(
            """
            SELECT event_name, COUNT(*) AS n
              FROM analytics_events
             WHERE tenant_id = :tenant_id
               AND user_id = :user_id
               AND occurred_at >= :since
               AND occurred_at < :until
             GROUP BY event_name
            """
        )
        result = await self._session.execute(
            stmt,
            {
                "tenant_id": tenant_id,
                "user_id": user_id,
                "since": since,
                "until": until,
            },
        )
        return [AnalyticsEventCount(event_name=r.event_name, count=int(r.n)) for r in result.all()]


def _extract_constraint_name(exc: IntegrityError) -> str | None:
    """Best-effort extraction of the failed constraint name from psycopg.

    Mirrors the helper in ``notification_repository.py`` / ``media_repository.py``.
    """
    orig = getattr(exc, "orig", None)
    diag = getattr(orig, "diag", None)
    name = getattr(diag, "constraint_name", None)
    return str(name) if name else None
