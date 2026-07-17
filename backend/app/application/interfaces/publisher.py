"""Port: outbox event publisher (Slice α7.3).

The relay (``RelayService``) reads unpublished ``event_outbox`` rows and hands
each to a :class:`PublisherPort`. Per the α7.3 sign-off (Q2), the publisher is an
**abstraction** with a default **synchronous, in-process** implementation that
invokes registered handlers; the relay stamps ``published_at`` **only after the
publisher returns successfully**. There is **no broker, no Celery, no Redis** in
α7.3 — a real bus/fan-out is introduced with the first provider (α8.1) behind this
same port, without touching the relay.

:class:`OutboxEvent` is the immutable read-model of one outbox row, shared by the
repository fetch surface (``IEventOutboxRepository.fetch_unpublished``) and the
publisher. The publisher never mutates it and never invents events.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable
from uuid import UUID


@dataclass(frozen=True, slots=True)
class OutboxEvent:
    """Immutable snapshot of one ``event_outbox`` row, as delivered to the publisher.

    ``attempts`` is the count of prior **failed** delivery attempts (0 on the first
    try); the relay uses it to compute whether a fresh failure parks the row
    (``attempts + 1 >= max_attempts``). ``payload`` / ``metadata`` are read-only
    mappings — a consumer that mutates them is a bug.
    """

    id: UUID
    aggregate_type: str
    aggregate_id: UUID
    event_type: str
    event_version: str
    payload: Mapping[str, Any]
    metadata: Mapping[str, Any]
    occurred_at: datetime
    attempts: int


@runtime_checkable
class EventHandler(Protocol):
    """A registered in-process consumer of published events (α7.3 default publisher).

    Called once per event by :class:`PublisherPort`. Raising propagates: the relay
    treats a raised handler as a failed publish and re-delivers on a later pass
    (at-least-once), so handlers **must be idempotent** on ``event.id``.
    """

    async def __call__(self, event: OutboxEvent) -> None: ...


class PublisherPort(ABC):
    """Publish one already-produced outbox event. Success means "safe to mark published"."""

    @abstractmethod
    async def publish(self, event: OutboxEvent) -> None:
        """Deliver ``event`` to its consumers.

        Returns normally on success (the relay then stamps ``published_at``).
        Raises on failure (the relay records ``attempts`` / ``last_error`` and
        leaves the row unpublished for a later pass). MUST NOT swallow delivery
        errors — the relay's at-least-once guarantee depends on a raised failure.
        """
        ...
