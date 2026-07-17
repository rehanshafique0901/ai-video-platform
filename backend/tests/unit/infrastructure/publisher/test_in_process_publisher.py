"""Unit tests for ``InProcessPublisher`` (Slice α7.3).

The α7.3 default ``PublisherPort``: a synchronous in-process fan-out to registered
handlers (no broker). Coverage:

* P1 — zero handlers: ``publish`` succeeds (the relay then marks the event
  published after an empty, successful fan-out).
* P2 — every registered handler is invoked exactly once with the event.
* P3 — handlers are invoked in registration order.
* P4 — a handler that raises propagates (the relay treats it as a failed publish);
  no ``publish`` swallowing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.application.interfaces.publisher import OutboxEvent
from app.infrastructure.publisher.in_process_publisher import InProcessPublisher


def _event() -> OutboxEvent:
    return OutboxEvent(
        id=uuid4(),
        aggregate_type="render_job",
        aggregate_id=uuid4(),
        event_type="RenderJobCreated",
        event_version="1.0",
        payload={"k": "v"},
        metadata={},
        occurred_at=datetime.now(UTC),
        attempts=0,
    )


@pytest.mark.unit
async def test_p1_no_handlers_publish_succeeds() -> None:
    publisher = InProcessPublisher()
    await publisher.publish(_event())  # no raise


@pytest.mark.unit
async def test_p2_all_handlers_invoked_once() -> None:
    seen_a: list[OutboxEvent] = []
    seen_b: list[OutboxEvent] = []

    async def handler_a(event: OutboxEvent) -> None:
        seen_a.append(event)

    async def handler_b(event: OutboxEvent) -> None:
        seen_b.append(event)

    publisher = InProcessPublisher([handler_a, handler_b])
    ev = _event()
    await publisher.publish(ev)

    assert seen_a == [ev]
    assert seen_b == [ev]


@pytest.mark.unit
async def test_p3_handlers_invoked_in_order() -> None:
    order: list[str] = []

    async def first(event: OutboxEvent) -> None:
        order.append("first")

    async def second(event: OutboxEvent) -> None:
        order.append("second")

    publisher = InProcessPublisher([first, second])
    await publisher.publish(_event())

    assert order == ["first", "second"]


@pytest.mark.unit
async def test_p4_handler_exception_propagates() -> None:
    async def boom(event: OutboxEvent) -> None:
        raise RuntimeError("handler failed")

    publisher = InProcessPublisher([boom])
    with pytest.raises(RuntimeError, match="handler failed"):
        await publisher.publish(_event())
