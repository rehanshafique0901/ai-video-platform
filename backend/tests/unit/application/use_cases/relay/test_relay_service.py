"""Unit tests for ``RelayService`` (Slice α7.3).

Exercises the relay's fetch → publish → mark loop against in-memory fakes (no DB;
the ``FOR UPDATE SKIP LOCKED`` claim + CHECK guarantees are covered by the
integration suite). Coverage map (α7.3 sign-off Q1–Q3, Q6 + RelayResult):

* U1 — happy path: every fetched event is published + stamped; one commit;
  ``RelayResult(fetched=n, published=n, failed=0, parked=0)``.
* U2 — empty batch: no events → all-zero result, still commits once.
* U3 — publish failure: the row is ``mark_failed`` (attempts++ , last_error set,
  still unpublished → retried), result counts it as ``failed`` not ``parked``.
* U4 — parking: a failure that reaches ``max_attempts`` parks the row (counted in
  ``parked``) and emits the ERROR structured log with the required fields.
* U5 — parked rows (``attempts >= max_attempts``) are excluded from the fetch.
* U6 — ``batch_size`` override caps the fetched batch.
* U7 — events are published in ``occurred_at`` order.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import structlog

from app.application.interfaces.publisher import OutboxEvent, PublisherPort
from app.application.use_cases.relay.relay_service import (
    DEFAULT_MAX_ATTEMPTS,
    RelayService,
)
from tests.unit.application.use_cases.auth._fakes import (
    FakeEventOutboxRepository,
    FakeUnitOfWork,
)

_BASE = datetime(2026, 1, 1, tzinfo=UTC)


class _RecordingPublisher(PublisherPort):
    """Publisher that records delivered events and optionally fails chosen ones."""

    def __init__(self, *, fail_types: set[str] | None = None) -> None:
        self.published: list[OutboxEvent] = []
        self._fail_types = fail_types or set()

    async def publish(self, event: OutboxEvent) -> None:
        if event.event_type in self._fail_types:
            raise RuntimeError(f"cannot publish {event.event_type}")
        self.published.append(event)


async def _seed(
    outbox: FakeEventOutboxRepository,
    *,
    event_type: str = "RenderJobCreated",
    occurred_at: datetime = _BASE,
    attempts: int = 0,
) -> UUID:
    await outbox.add(
        aggregate_type="render_job",
        aggregate_id=uuid4(),
        event_type=event_type,
        payload={"render_job_id": str(uuid4())},
        occurred_at=occurred_at,
    )
    row = outbox.rows[-1]
    row.attempts = attempts
    return row.id


@pytest.mark.unit
async def test_u1_happy_path_publishes_and_marks() -> None:
    outbox = FakeEventOutboxRepository()
    for i in range(3):
        await _seed(outbox, occurred_at=_BASE + timedelta(seconds=i))
    uow = FakeUnitOfWork(outbox=outbox)
    publisher = _RecordingPublisher()

    result = await RelayService(uow=uow, publisher=publisher).relay_once()

    assert (result.fetched, result.published, result.failed, result.parked) == (3, 3, 0, 0)
    assert len(publisher.published) == 3
    assert all(r.published_at is not None for r in outbox.rows)
    assert uow.commits == 1


@pytest.mark.unit
async def test_u2_empty_batch_commits_and_zeroes() -> None:
    outbox = FakeEventOutboxRepository()
    uow = FakeUnitOfWork(outbox=outbox)

    result = await RelayService(uow=uow, publisher=_RecordingPublisher()).relay_once()

    assert (result.fetched, result.published, result.failed, result.parked) == (0, 0, 0, 0)
    assert uow.commits == 1


@pytest.mark.unit
async def test_u3_publish_failure_marks_failed_not_parked() -> None:
    outbox = FakeEventOutboxRepository()
    good = await _seed(outbox, event_type="RenderJobCreated", occurred_at=_BASE)
    bad = await _seed(
        outbox, event_type="RenderJobCanceled", occurred_at=_BASE + timedelta(seconds=1)
    )
    uow = FakeUnitOfWork(outbox=outbox)
    publisher = _RecordingPublisher(fail_types={"RenderJobCanceled"})

    result = await RelayService(uow=uow, publisher=publisher).relay_once()

    assert (result.fetched, result.published, result.failed, result.parked) == (2, 1, 1, 0)
    rows = {r.id: r for r in outbox.rows}
    assert rows[good].published_at is not None
    assert rows[bad].published_at is None  # still unpublished → retried later
    assert rows[bad].attempts == 1
    assert rows[bad].last_error is not None and "RuntimeError" in rows[bad].last_error


@pytest.mark.unit
async def test_u4_failure_reaching_cap_parks_and_logs() -> None:
    outbox = FakeEventOutboxRepository()
    # attempts already at cap-1 → the next failure reaches the cap → parked.
    parked_id = await _seed(
        outbox, event_type="RenderJobCanceled", attempts=DEFAULT_MAX_ATTEMPTS - 1
    )
    uow = FakeUnitOfWork(outbox=outbox)
    publisher = _RecordingPublisher(fail_types={"RenderJobCanceled"})

    with structlog.testing.capture_logs() as logs:
        result = await RelayService(uow=uow, publisher=publisher).relay_once()

    assert (result.fetched, result.published, result.failed, result.parked) == (1, 0, 1, 1)
    assert {r.id: r for r in outbox.rows}[parked_id].attempts == DEFAULT_MAX_ATTEMPTS

    errors = [e for e in logs if e.get("event") == "outbox.publish_failed"]
    assert len(errors) == 1
    ev = errors[0]
    assert ev["log_level"] == "error"
    assert ev["event_id"] == str(parked_id)
    assert ev["event_type"] == "RenderJobCanceled"
    assert ev["attempts"] == DEFAULT_MAX_ATTEMPTS
    assert ev["parked"] is True
    assert ev["exception_type"] == "RuntimeError"
    assert "cannot publish" in ev["exception_message"]
    assert "aggregate_id" in ev


@pytest.mark.unit
async def test_u5_parked_rows_excluded_from_fetch() -> None:
    outbox = FakeEventOutboxRepository()
    await _seed(outbox, attempts=DEFAULT_MAX_ATTEMPTS)  # already parked
    live = await _seed(outbox, occurred_at=_BASE + timedelta(seconds=1))
    uow = FakeUnitOfWork(outbox=outbox)
    publisher = _RecordingPublisher()

    result = await RelayService(uow=uow, publisher=publisher).relay_once()

    assert result.fetched == 1
    assert [e.id for e in publisher.published] == [live]


@pytest.mark.unit
async def test_u6_batch_size_override_caps_fetch() -> None:
    outbox = FakeEventOutboxRepository()
    for i in range(5):
        await _seed(outbox, occurred_at=_BASE + timedelta(seconds=i))
    uow = FakeUnitOfWork(outbox=outbox)
    publisher = _RecordingPublisher()

    result = await RelayService(uow=uow, publisher=publisher).relay_once(batch_size=2)

    assert result.fetched == 2
    assert result.published == 2


@pytest.mark.unit
async def test_u7_published_in_occurred_at_order() -> None:
    outbox = FakeEventOutboxRepository()
    # Seed out of chronological order; the relay must publish oldest-first.
    late = await _seed(outbox, occurred_at=_BASE + timedelta(seconds=10))
    early = await _seed(outbox, occurred_at=_BASE + timedelta(seconds=1))
    uow = FakeUnitOfWork(outbox=outbox)
    publisher = _RecordingPublisher()

    await RelayService(uow=uow, publisher=publisher).relay_once()

    assert [e.id for e in publisher.published] == [early, late]
