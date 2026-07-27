"""``AnalyticsProjection`` — publish/export lifecycle events → analytics_events (α9.0).

The first analytics outbox consumer: registered on the in-process ``PublisherPort``
alongside the notification projections, it listens for the owner-attributable publish +
export lifecycle events (``event_schema.HANDLED_EVENT_TYPES``) and projects each into one
``analytics_events`` row for the acting user. A faithful twin of the notification
projections (ADR-0042 — downstream, additive): it *derives read state from immutable events*
and never orchestrates — it never re-drives a job, mutates the producer, or feeds back into
the pipeline (the fan-out rule; a projection must never invoke another projection).

Delivery is at-least-once (the relay redelivers on failure), so the projection is
**idempotent** on ``event.id``: ``source_event_id = event.id`` + the ``(source_event_id,
occurred_at)`` partial-unique index makes a redelivery a no-op (ADR-0048). Crucially,
``occurred_at = event.occurred_at`` (never ``now()``) so the dedupe pair is deterministic
across redeliveries. Event→content mapping (event_name/properties) lives in
``event_schema``; the transactional, tenant-resolving insert lives in
:class:`RecordAnalyticsEvent`.

Error posture (mirrors the notification projections exactly):
* not-applicable event type → clean return (relay stamps it published);
* **malformed payload** (missing/invalid actor id) → log + clean return (a bad immutable
  event is not retryable — never park the relay on it);
* genuine DB failure inside ``RecordAnalyticsEvent`` → propagates (relay records the attempt
  and re-delivers later).

It builds a **fresh** ``RecordAnalyticsEvent`` per event via an injected factory, so each
projection runs in its own Unit of Work. Properties copy only already-neutral identity
fields — no credential, bearer, URL, or bytes (PUB-8 / ADR-0047 C8): the events carry none.
"""

from __future__ import annotations

from collections.abc import Callable
from uuid import UUID

import structlog

from app.application.interfaces.publisher import OutboxEvent
from app.application.use_cases.analytics.event_schema import (
    HANDLED_EVENT_TYPES,
    event_name_for,
    properties_for,
)
from app.application.use_cases.analytics.record_analytics_event import RecordAnalyticsEvent

_LOGGER = structlog.get_logger(__name__)


class AnalyticsProjection:
    """An ``EventHandler`` that projects publish/export events into analytics_events."""

    def __init__(self, record_factory: Callable[[], RecordAnalyticsEvent]) -> None:
        # A factory (not an instance) so each event gets a fresh use case + UoW.
        self._record_factory = record_factory

    async def __call__(self, event: OutboxEvent) -> None:
        if event.event_type not in HANDLED_EVENT_TYPES:
            return  # not applicable — clean return lets the relay mark it published

        event_name = event_name_for(event.event_type)
        if event_name is None:  # defensive; HANDLED_EVENT_TYPES guarantees a mapping
            return

        payload = event.payload
        try:
            user_id = UUID(str(payload["requested_by_user_id"]))
        except (KeyError, ValueError):
            # A malformed event payload is not retryable — log + skip rather than parking the
            # row forever (mirrors the notification projections).
            _LOGGER.error(
                "analytics.bad_event_payload",
                event_id=str(event.id),
                event_type=event.event_type,
            )
            return

        record = self._record_factory()
        result = await record.execute(
            user_id=user_id,
            event_name=event_name,
            properties=properties_for(event.event_type, payload),
            source_event_id=event.id,
            # Deterministic dedupe coordinate — the producing event's timestamp, never now().
            occurred_at=event.occurred_at,
        )
        _LOGGER.debug(
            "analytics.projection_handled",
            event_id=str(event.id),
            event_type=event.event_type,
            user_id=str(user_id),
            status=result.status,
        )


__all__ = ["AnalyticsProjection"]
