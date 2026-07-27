"""Unit tests for ``AnalyticsProjection`` (Slice α9.0 — the analytics outbox consumer).

The projection maps the publish/export lifecycle outbox events to an analytics ``event_name``
+ neutral property subset and hands each to a fresh ``RecordAnalyticsEvent`` (own UoW per
event). These tests exercise the event→content mapping, owner-targeting (recipient =
``requested_by_user_id``), the **deterministic ``occurred_at = event.occurred_at``** invariant,
and the error posture (ignore-other-types, malformed-payload no-op, unknown-user no-op,
redelivery no-op, DB-error propagation) with a stub use case — no DB.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.application.interfaces.publisher import OutboxEvent
from app.application.use_cases.analytics.analytics_projection import AnalyticsProjection
from app.application.use_cases.analytics.record_analytics_event import RecordAnalyticsEventResult

pytestmark = pytest.mark.unit


class _StubRecord:
    """Records ``execute`` kwargs; configurable to return duplicate/skipped or raise."""

    def __init__(self, *, status: str = "recorded", raises: bool = False):
        self.calls: list[dict[str, Any]] = []
        self._status = status
        self._raises = raises

    async def execute(self, **kwargs: Any) -> RecordAnalyticsEventResult:
        self.calls.append(kwargs)
        if self._raises:
            raise RuntimeError("db down")
        return RecordAnalyticsEventResult(status=self._status)


def _projection(
    *, status: str = "recorded", raises: bool = False
) -> tuple[AnalyticsProjection, list[_StubRecord]]:
    built: list[_StubRecord] = []

    def factory() -> Any:
        stub = _StubRecord(status=status, raises=raises)
        built.append(stub)
        return stub

    return AnalyticsProjection(factory), built


def _event(
    event_type: str,
    payload: dict[str, Any],
    *,
    event_id: UUID | None = None,
    occurred_at: datetime | None = None,
) -> OutboxEvent:
    return OutboxEvent(
        id=event_id or uuid4(),
        aggregate_type="publish_job",
        aggregate_id=uuid4(),
        event_type=event_type,
        event_version="1",
        payload=payload,
        metadata={},
        occurred_at=occurred_at or datetime.now(UTC),
        attempts=0,
    )


def _publish_payload(user_id: UUID, *, status: str = "succeeded") -> dict[str, Any]:
    return {
        "publish_job_id": str(uuid4()),
        "project_id": str(uuid4()),
        "requested_by_user_id": str(user_id),
        "social_account_id": str(uuid4()),
        "platform": "youtube",
        "source_export_job_id": str(uuid4()),
        "source_media_asset_id": str(uuid4()),
        "status": status,
        "version": 3,
    }


def _export_payload(user_id: UUID, *, status: str = "succeeded") -> dict[str, Any]:
    return {
        "export_job_id": str(uuid4()),
        "render_job_id": str(uuid4()),
        "requested_by_user_id": str(user_id),
        "format": "mp4",
        "quality": "hd_1080p",
        "orientation": "horizontal",
        "status": status,
        "version": 2,
    }


@pytest.mark.parametrize(
    ("event_type", "expected_name"),
    [
        ("PublishJobCreated", "publish.created"),
        ("PublishJobSucceeded", "publish.succeeded"),
        ("PublishJobFailed", "publish.failed"),
    ],
)
async def test_publish_events_map_to_names(event_type: str, expected_name: str) -> None:
    projection, built = _projection()
    user_id = uuid4()
    event = _event(event_type, _publish_payload(user_id, status="failed"))

    await projection(event)

    assert len(built) == 1
    (call,) = built[0].calls
    assert call["user_id"] == user_id
    assert call["event_name"] == expected_name
    assert call["source_event_id"] == event.id
    # Deterministic dedupe coordinate — the producing event's timestamp, never now().
    assert call["occurred_at"] == event.occurred_at
    # Neutral identity properties only (no secrets — the event carries none).
    props = call["properties"]
    assert props["source_event_type"] == event_type
    assert props["publish_job_id"] == event.payload["publish_job_id"]
    assert props["platform"] == "youtube"


@pytest.mark.parametrize(
    ("event_type", "expected_name"),
    [
        ("ExportJobCreated", "export.created"),
        ("ExportJobSucceeded", "export.succeeded"),
        ("ExportJobFailed", "export.failed"),
    ],
)
async def test_export_events_map_to_names(event_type: str, expected_name: str) -> None:
    projection, built = _projection()
    user_id = uuid4()
    event = _event(event_type, _export_payload(user_id))

    await projection(event)

    (call,) = built[0].calls
    assert call["user_id"] == user_id
    assert call["event_name"] == expected_name
    assert call["occurred_at"] == event.occurred_at
    props = call["properties"]
    assert props["source_event_type"] == event_type
    assert props["export_job_id"] == event.payload["export_job_id"]
    assert props["format"] == "mp4"
    # Publish-only keys never leak onto an export event.
    assert "platform" not in props


async def test_ignores_unhandled_event_type() -> None:
    projection, built = _projection()
    await projection(_event("WorkflowRunSucceeded", {"requested_by_user_id": str(uuid4())}))
    assert built == []


async def test_malformed_payload_missing_user_is_clean_noop() -> None:
    projection, built = _projection()
    await projection(_event("PublishJobSucceeded", {"publish_job_id": str(uuid4())}))
    assert built == []


async def test_invalid_recipient_uuid_is_clean_noop() -> None:
    projection, built = _projection()
    await projection(_event("ExportJobSucceeded", {"requested_by_user_id": "not-a-uuid"}))
    assert built == []


async def test_unknown_user_skip_is_swallowed() -> None:
    # The use case reports "skipped" (the owning user is gone) → projection returns cleanly.
    projection, built = _projection(status="skipped")
    await projection(_event("PublishJobSucceeded", _publish_payload(uuid4())))
    assert len(built) == 1  # attempted once, no raise


async def test_redelivery_duplicate_is_swallowed() -> None:
    # The use case reports "duplicate" (DB refused the second write) → projection returns
    # cleanly so the relay marks the redelivered event published (exactly-once by index).
    projection, built = _projection(status="duplicate")
    await projection(_event("ExportJobFailed", _export_payload(uuid4(), status="failed")))
    assert len(built) == 1


async def test_genuine_db_error_propagates_for_relay_retry() -> None:
    projection, _ = _projection(raises=True)
    with pytest.raises(RuntimeError):
        await projection(_event("PublishJobSucceeded", _publish_payload(uuid4())))
