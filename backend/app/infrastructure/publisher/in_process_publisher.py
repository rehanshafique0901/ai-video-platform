"""Synchronous in-process publisher (Slice α7.3 default ``PublisherPort``).

The α7.3 sign-off (Q2) fixes the relay's publish target as an **in-process**
publisher that invokes registered handlers synchronously — **no broker, no
Celery, no Redis**. This implementation fans one event out to an ordered tuple of
:class:`EventHandler`s, awaiting each in turn. If any handler raises, the
exception propagates: the relay treats that as a failed publish (records
``attempts`` / ``last_error`` and retries later), so **handlers must be idempotent
on ``event.id``** (at-least-once).

α7.3 registers **zero** handlers by default — the relay marks events published
after a successful (empty) fan-out, exercising the whole delivery/accounting path
without any consumer. Real consumers (and, at α8.1, a broker-backed publisher
behind this same port) are wired in later slices without touching the relay.
"""

from __future__ import annotations

from collections.abc import Sequence

import structlog

from app.application.interfaces.publisher import EventHandler, OutboxEvent, PublisherPort

_LOGGER = structlog.get_logger(__name__)


class InProcessPublisher(PublisherPort):
    """Fan an event out to registered in-process handlers, synchronously and in order."""

    def __init__(self, handlers: Sequence[EventHandler] = ()) -> None:
        self._handlers: tuple[EventHandler, ...] = tuple(handlers)

    async def publish(self, event: OutboxEvent) -> None:
        # Deliver in registration order. A handler that raises aborts the publish
        # (the relay will mark_failed + retry) — we do NOT swallow, because the
        # at-least-once contract depends on failures surfacing to the relay.
        for handler in self._handlers:
            await handler(event)
        _LOGGER.debug(
            "outbox.published",
            event_id=str(event.id),
            event_type=event.event_type,
            aggregate_type=event.aggregate_type,
            aggregate_id=str(event.aggregate_id),
            handlers=len(self._handlers),
        )
