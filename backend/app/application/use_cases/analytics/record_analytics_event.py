"""``RecordAnalyticsEvent`` — the idempotent write half of the analytics projection (α9.0).

Given already-mapped analytics content (recipient ``user_id`` + ``event_name`` + neutral
``properties`` + the ``source_event_id`` / ``occurred_at`` dedupe coordinates), resolve the
owner's tenant and persist exactly one ``analytics_events`` row inside its own Unit of Work.
The projection upstream (``AnalyticsProjection``) owns event→content mapping; this use case
owns only the transactional, idempotent insert.

Invariants:
* **Pure projection.** It only **writes** analytics state derived from an immutable,
  already-committed event. It never mutates the frozen publish/export runtime, never
  re-drives a job, never dispatches provider work (ADR-0042 — downstream consumer).
* **Exactly-once, DB-enforced (ADR-0048).** A relay redelivery drives this again; the
  partial-unique ``(source_event_id, occurred_at)`` index refuses the second write and the
  repository raises ``ConflictError``, treated here as a successful **already-recorded
  no-op** — correctness never depends on an application-level pre-check.
* **Deterministic ``occurred_at``.** The caller passes the producing event's ``occurred_at``
  (never ``now()``), so a redelivery targets the identical dedupe pair — the reason the DB
  can enforce exactly-once at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

import structlog

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.core.errors import ConflictError

_LOGGER = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class RecordAnalyticsEventResult:
    """Outcome of one analytics projection write.

    ``status`` is ``"recorded"`` (a fresh row was persisted), ``"duplicate"`` (this source
    event was already recorded — an idempotent no-op), or ``"skipped"`` (the owning user no
    longer exists, so there is no tenant to attribute the event to).
    """

    status: str


class RecordAnalyticsEvent:
    """Persist one analytics event, idempotent on ``(source_event_id, occurred_at)``."""

    def __init__(self, uow: IUnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self,
        *,
        user_id: UUID,
        event_name: str,
        properties: dict[str, Any],
        source_event_id: UUID,
        occurred_at: datetime,
    ) -> RecordAnalyticsEventResult:
        async with self._uow:
            # Resolve the owner's tenant in the same UoW — publish/export events carry the
            # actor's user_id but not tenant_id (AN5). A vanished user → skip cleanly (the
            # event predates a deletion; there is nothing to attribute).
            user = await self._uow.users.get_by_id(user_id)
            if user is None:
                _LOGGER.debug(
                    "analytics.skipped_unknown_user",
                    user_id=str(user_id),
                    source_event_id=str(source_event_id),
                    event_name=event_name,
                )
                return RecordAnalyticsEventResult(status="skipped")

            try:
                await self._uow.analytics.add(
                    tenant_id=user.tenant_id,
                    user_id=user_id,
                    event_name=event_name,
                    properties=properties,
                    source_event_id=source_event_id,
                    occurred_at=occurred_at,
                )
                await self._uow.commit()
            except ConflictError:
                # Relay redelivered the same source event: the DB refused the write.
                # Exactly-once is owned by the constraint, so a refusal is a successful
                # no-op — not an error to retry (ADR-0048).
                _LOGGER.debug(
                    "analytics.duplicate_ignored",
                    user_id=str(user_id),
                    source_event_id=str(source_event_id),
                    event_name=event_name,
                )
                return RecordAnalyticsEventResult(status="duplicate")

        _LOGGER.debug(
            "analytics.recorded",
            user_id=str(user_id),
            source_event_id=str(source_event_id),
            event_name=event_name,
        )
        return RecordAnalyticsEventResult(status="recorded")


__all__ = ["RecordAnalyticsEvent", "RecordAnalyticsEventResult"]
