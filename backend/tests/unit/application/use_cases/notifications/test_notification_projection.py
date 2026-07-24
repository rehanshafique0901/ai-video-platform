"""Unit tests for ``NotificationProjection`` (Slice α8.5b.3).

The projection maps ``ExportJobSucceeded`` / ``ExportJobFailed`` outbox events to
notification content and hands each to a fresh ``CreateNotification`` (own UoW per
event). These tests exercise the event→content mapping and the error posture
(ignore-other-types, malformed-payload no-op, redelivery no-op, DB-error propagation)
with a stub use case — no DB.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.application.interfaces.publisher import OutboxEvent
from app.application.use_cases.notifications.create_notification import CreateNotificationResult
from app.application.use_cases.notifications.notification_projection import NotificationProjection

pytestmark = pytest.mark.unit


class _StubCreate:
    """Records ``execute`` kwargs; configurable to return duplicate or raise."""

    def __init__(self, *, result: CreateNotificationResult | None = None, raises: bool = False):
        self.calls: list[dict[str, Any]] = []
        self._result = result or CreateNotificationResult(status="created", notification_id=uuid4())
        self._raises = raises

    async def execute(self, **kwargs: Any) -> CreateNotificationResult:
        self.calls.append(kwargs)
        if self._raises:
            raise RuntimeError("db down")
        return self._result


def _projection(
    *, result: CreateNotificationResult | None = None, raises: bool = False
) -> tuple[NotificationProjection, list[_StubCreate]]:
    built: list[_StubCreate] = []

    def factory() -> Any:
        stub = _StubCreate(result=result, raises=raises)
        built.append(stub)
        return stub

    return NotificationProjection(factory), built


def _event(
    event_type: str, payload: dict[str, Any], *, event_id: UUID | None = None
) -> OutboxEvent:
    return OutboxEvent(
        id=event_id or uuid4(),
        aggregate_type="export_job",
        aggregate_id=uuid4(),
        event_type=event_type,
        event_version="1",
        payload=payload,
        metadata={},
        occurred_at=datetime.now(UTC),
        attempts=0,
    )


def _succeeded_payload(user_id: UUID) -> dict[str, Any]:
    return {
        "export_job_id": str(uuid4()),
        "render_job_id": str(uuid4()),
        "requested_by_user_id": str(user_id),
        "format": "mp4",
        "quality": "hd_1080p",
        "orientation": "horizontal",
        "status": "succeeded",
        "version": 3,
        "output_media_asset_id": str(uuid4()),
        "file_size_bytes": 2048,
    }


def _failed_payload(user_id: UUID) -> dict[str, Any]:
    return {
        "export_job_id": str(uuid4()),
        "render_job_id": str(uuid4()),
        "requested_by_user_id": str(user_id),
        "format": "mp4",
        "quality": "hd_1080p",
        "orientation": "horizontal",
        "status": "failed",
        "version": 3,
        "error": {"code": "transcode_failed", "message": "The export could not be completed."},
    }


async def test_succeeded_event_projects_success_notification() -> None:
    projection, built = _projection()
    user_id = uuid4()
    event = _event("ExportJobSucceeded", _succeeded_payload(user_id))

    await projection(event)

    assert len(built) == 1
    (call,) = built[0].calls
    assert call["user_id"] == user_id
    assert call["kind"] == "export.succeeded"
    assert call["title"] == "Your video is ready"
    assert "hd_1080p" in (call["body"] or "")
    assert "mp4" in (call["body"] or "")
    # source_event_id is the outbox event id (the dedupe coordinate — W8.5b.7).
    assert call["source_event_id"] == event.id
    # payload carries delivery identity + the produced asset id.
    assert call["payload"]["export_job_id"] == event.payload["export_job_id"]
    assert call["payload"]["output_media_asset_id"] == event.payload["output_media_asset_id"]


async def test_failed_event_projects_failure_notification_with_neutral_message() -> None:
    projection, built = _projection()
    user_id = uuid4()
    event = _event("ExportJobFailed", _failed_payload(user_id))

    await projection(event)

    (call,) = built[0].calls
    assert call["kind"] == "export.failed"
    assert call["title"] == "Your video export failed"
    assert call["body"] == "The export could not be completed."
    assert call["source_event_id"] == event.id
    assert call["payload"]["error"] == event.payload["error"]


async def test_failed_event_without_message_uses_generic_body() -> None:
    projection, built = _projection()
    user_id = uuid4()
    payload = _failed_payload(user_id)
    payload["error"] = {"code": "unknown"}  # no message
    await projection(_event("ExportJobFailed", payload))

    (call,) = built[0].calls
    assert call["body"] == "Your video export could not be completed."


async def test_ignores_non_export_event_types() -> None:
    projection, built = _projection()
    await projection(_event("WorkflowRunSucceeded", {"requested_by_user_id": str(uuid4())}))
    # A non-applicable event never builds a use case (clean no-op; relay still publishes).
    assert built == []


async def test_malformed_payload_does_not_raise_or_project() -> None:
    projection, built = _projection()
    # Missing requested_by_user_id → not retryable; projection returns cleanly.
    await projection(_event("ExportJobSucceeded", {"export_job_id": str(uuid4())}))
    assert built == []


async def test_invalid_recipient_uuid_is_clean_noop() -> None:
    projection, built = _projection()
    await projection(_event("ExportJobSucceeded", {"requested_by_user_id": "not-a-uuid"}))
    assert built == []


async def test_redelivery_duplicate_is_swallowed() -> None:
    # The use case reports "duplicate" (DB refused the second write) → projection returns
    # cleanly so the relay marks the redelivered event published (W8.5b.7).
    projection, built = _projection(result=CreateNotificationResult(status="duplicate"))
    await projection(_event("ExportJobSucceeded", _succeeded_payload(uuid4())))
    assert len(built) == 1  # attempted once, no raise


async def test_genuine_db_error_propagates_for_relay_retry() -> None:
    projection, _ = _projection(raises=True)
    with pytest.raises(RuntimeError):
        await projection(_event("ExportJobSucceeded", _succeeded_payload(uuid4())))
