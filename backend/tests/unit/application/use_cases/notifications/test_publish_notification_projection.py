"""Unit tests for ``PublishNotificationProjection`` (Slice α8.9a — deferred DQ7 fan-out).

The projection maps ``PublishJobSucceeded`` / ``PublishJobFailed`` outbox events to
notification content and hands each to a fresh ``CreateNotification`` (own UoW per event).
These tests exercise the event→content mapping, the owner-targeting (recipient =
``requested_by_user_id``), and the error posture (ignore-other-types, malformed-payload
no-op, redelivery no-op, DB-error propagation) with a stub use case — no DB. A faithful
twin of ``test_notification_projection.py`` for the publishing context.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.application.interfaces.publisher import OutboxEvent
from app.application.use_cases.notifications.create_notification import CreateNotificationResult
from app.application.use_cases.notifications.publish_notification_projection import (
    PublishNotificationProjection,
)

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
) -> tuple[PublishNotificationProjection, list[_StubCreate]]:
    built: list[_StubCreate] = []

    def factory() -> Any:
        stub = _StubCreate(result=result, raises=raises)
        built.append(stub)
        return stub

    return PublishNotificationProjection(factory), built


def _event(
    event_type: str, payload: dict[str, Any], *, event_id: UUID | None = None
) -> OutboxEvent:
    return OutboxEvent(
        id=event_id or uuid4(),
        aggregate_type="publish_job",
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
        "publish_job_id": str(uuid4()),
        "project_id": str(uuid4()),
        "requested_by_user_id": str(user_id),
        "social_account_id": str(uuid4()),
        "platform": "youtube",
        "source_export_job_id": str(uuid4()),
        "source_media_asset_id": str(uuid4()),
        "status": "succeeded",
        "version": 3,
        "platform_post_id": "yt-abc123",
        "platform_post_url": "https://youtube.com/watch?v=abc123",
        "published_at": "2026-07-27T10:30:00+00:00",
    }


def _failed_payload(user_id: UUID) -> dict[str, Any]:
    return {
        "publish_job_id": str(uuid4()),
        "project_id": str(uuid4()),
        "requested_by_user_id": str(user_id),
        "social_account_id": str(uuid4()),
        "platform": "youtube",
        "source_export_job_id": str(uuid4()),
        "source_media_asset_id": str(uuid4()),
        "status": "failed",
        "version": 3,
        "error": {"code": "upload_rejected", "message": "The destination rejected the upload."},
    }


async def test_succeeded_event_projects_success_notification() -> None:
    projection, built = _projection()
    user_id = uuid4()
    event = _event("PublishJobSucceeded", _succeeded_payload(user_id))

    await projection(event)

    assert len(built) == 1
    (call,) = built[0].calls
    # Owner-targeting: the notification is addressed to requested_by_user_id, nobody else.
    assert call["user_id"] == user_id
    assert call["kind"] == "publish.succeeded"
    assert call["title"] == "Your video was published"
    assert "youtube" in (call["body"] or "")
    # source_event_id is the outbox event id (the dedupe coordinate).
    assert call["source_event_id"] == event.id
    # payload carries publish identity + the platform post identity.
    assert call["payload"]["publish_job_id"] == event.payload["publish_job_id"]
    assert call["payload"]["platform_post_url"] == event.payload["platform_post_url"]
    assert call["payload"]["platform_post_id"] == event.payload["platform_post_id"]


async def test_failed_event_projects_failure_notification_with_neutral_message() -> None:
    projection, built = _projection()
    user_id = uuid4()
    event = _event("PublishJobFailed", _failed_payload(user_id))

    await projection(event)

    (call,) = built[0].calls
    assert call["user_id"] == user_id
    assert call["kind"] == "publish.failed"
    assert call["title"] == "Your video couldn't be published"
    assert call["body"] == "The destination rejected the upload."
    assert call["source_event_id"] == event.id
    assert call["payload"]["error"] == event.payload["error"]


async def test_failed_event_without_message_uses_generic_body() -> None:
    projection, built = _projection()
    payload = _failed_payload(uuid4())
    payload["error"] = {"code": "unknown"}  # no message
    await projection(_event("PublishJobFailed", payload))

    (call,) = built[0].calls
    assert call["body"] == "Your video could not be published."


async def test_ignores_non_publish_event_types() -> None:
    projection, built = _projection()
    # Another aggregate's terminal event is not applicable — clean no-op.
    await projection(_event("ExportJobSucceeded", {"requested_by_user_id": str(uuid4())}))
    assert built == []


async def test_ignores_publish_created_event() -> None:
    projection, built = _projection()
    # Only the two TERMINAL publish events are projected — never PublishJobCreated.
    await projection(_event("PublishJobCreated", _succeeded_payload(uuid4())))
    assert built == []


async def test_malformed_payload_does_not_raise_or_project() -> None:
    projection, built = _projection()
    # Missing requested_by_user_id → not retryable; projection returns cleanly.
    await projection(_event("PublishJobSucceeded", {"publish_job_id": str(uuid4())}))
    assert built == []


async def test_invalid_recipient_uuid_is_clean_noop() -> None:
    projection, built = _projection()
    await projection(_event("PublishJobSucceeded", {"requested_by_user_id": "not-a-uuid"}))
    assert built == []


async def test_redelivery_duplicate_is_swallowed() -> None:
    # The use case reports "duplicate" (DB refused the second write) → projection returns
    # cleanly so the relay marks the redelivered event published (exactly-once by index).
    projection, built = _projection(result=CreateNotificationResult(status="duplicate"))
    await projection(_event("PublishJobSucceeded", _succeeded_payload(uuid4())))
    assert len(built) == 1  # attempted once, no raise


async def test_genuine_db_error_propagates_for_relay_retry() -> None:
    projection, _ = _projection(raises=True)
    with pytest.raises(RuntimeError):
        await projection(_event("PublishJobFailed", _failed_payload(uuid4())))
