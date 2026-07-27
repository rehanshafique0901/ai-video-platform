"""``PublishNotificationProjection`` — publish terminal events → in-app notifications (α8.9a).

The deferred DQ7 fan-out: registered on the in-process ``PublisherPort`` alongside the
export :class:`NotificationProjection`, it listens for **`PublishJobSucceeded`** and
**`PublishJobFailed`** and projects each into exactly one in-app ``Notification`` per
recipient. A faithful twin of :class:`NotificationProjection` (α8.5b.3) for the publishing
context: it *derives read state from immutable events* and never orchestrates — it never
re-drives a publish, mutates the publish job, or feeds back into the producer (the fan-out
rule; a projection must never invoke another projection).

Delivery is at-least-once (the relay redelivers on failure), so the projection is
**idempotent** on ``event.id``: the recipient + ``source_event_id`` uniqueness makes a
redelivery a no-op (the same `(user_id, source_event_id)` partial-unique index that backs
the export projection). Content mapping (kind/title/body/payload) lives here; the
transactional insert lives in :class:`CreateNotification` (reused unchanged).

Error posture (mirrors the export projection exactly):
* not-applicable event type → clean return (relay stamps it published);
* **malformed payload** (missing/invalid recipient) → log + clean return (a bad immutable
  event is not retryable — never park the relay on it);
* genuine DB failure inside ``CreateNotification`` → propagates (relay records the attempt
  and re-delivers later).

It builds a **fresh** ``CreateNotification`` per event via an injected factory, so each
projection runs in its own Unit of Work (no session reuse across events). Payloads copy only
the already-neutral event fields — no credential, bearer, provider URL, or bytes (PUB-8 /
ADR-0047 C8): the publish events themselves carry none.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any
from uuid import UUID

import structlog

from app.application.interfaces.publisher import OutboxEvent
from app.application.use_cases.notifications.create_notification import CreateNotification
from app.application.use_cases.publishing._events import (
    EVENT_PUBLISH_JOB_FAILED,
    EVENT_PUBLISH_JOB_SUCCEEDED,
)

_LOGGER = structlog.get_logger(__name__)

_HANDLED_EVENT_TYPES = frozenset({EVENT_PUBLISH_JOB_SUCCEEDED, EVENT_PUBLISH_JOB_FAILED})

_KIND_SUCCEEDED = "publish.succeeded"
_KIND_FAILED = "publish.failed"


class PublishNotificationProjection:
    """An ``EventHandler`` that projects publish terminal events into in-app notifications."""

    def __init__(self, create_factory: Callable[[], CreateNotification]) -> None:
        # A factory (not an instance) so each event gets a fresh use case + UoW.
        self._create_factory = create_factory

    async def __call__(self, event: OutboxEvent) -> None:
        if event.event_type not in _HANDLED_EVENT_TYPES:
            return  # not applicable — clean return lets the relay mark it published

        payload = event.payload
        try:
            user_id = UUID(str(payload["requested_by_user_id"]))
        except (KeyError, ValueError):
            # A malformed publish event payload is not retryable — log + skip rather than
            # parking the row forever (mirrors the export projection).
            _LOGGER.error(
                "notification.bad_event_payload",
                event_id=str(event.id),
                event_type=event.event_type,
            )
            return

        content = (
            _succeeded_content(payload)
            if event.event_type == EVENT_PUBLISH_JOB_SUCCEEDED
            else _failed_content(payload)
        )

        create = self._create_factory()
        result = await create.execute(
            user_id=user_id,
            kind=content.kind,
            title=content.title,
            body=content.body,
            payload=content.payload,
            source_event_id=event.id,
        )
        _LOGGER.debug(
            "notification.projection_handled",
            event_id=str(event.id),
            event_type=event.event_type,
            user_id=str(user_id),
            status=result.status,
        )


class _Content:
    """Mapped notification content (kind/title/body/payload) for one publish event."""

    __slots__ = ("kind", "title", "body", "payload")

    def __init__(self, *, kind: str, title: str, body: str | None, payload: dict[str, Any]) -> None:
        self.kind = kind
        self.title = title
        self.body = body
        self.payload = payload


def _identity_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """The delivery-identity subset common to both publish notifications (no secrets)."""
    return {
        "publish_job_id": payload.get("publish_job_id"),
        "project_id": payload.get("project_id"),
        "social_account_id": payload.get("social_account_id"),
        "platform": payload.get("platform"),
    }


def _succeeded_content(payload: Mapping[str, Any]) -> _Content:
    body_payload = _identity_payload(payload)
    body_payload["platform_post_id"] = payload.get("platform_post_id")
    body_payload["platform_post_url"] = payload.get("platform_post_url")
    body_payload["published_at"] = payload.get("published_at")
    platform = payload.get("platform") or "the platform"
    return _Content(
        kind=_KIND_SUCCEEDED,
        title="Your video was published",
        body=f"Your video was published to {platform}.",
        payload=body_payload,
    )


def _failed_content(payload: Mapping[str, Any]) -> _Content:
    body_payload = _identity_payload(payload)
    error = payload.get("error")
    body_payload["error"] = error
    # The event error is already neutral (PUB-8 / C8 — no credential/provider internals).
    message = None
    if isinstance(error, dict):
        raw = error.get("message")
        message = str(raw) if raw else None
    return _Content(
        kind=_KIND_FAILED,
        title="Your video couldn't be published",
        body=message or "Your video could not be published.",
        payload=body_payload,
    )


__all__ = ["PublishNotificationProjection"]
