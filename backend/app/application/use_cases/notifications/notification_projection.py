"""``NotificationProjection`` — export terminal events → in-app notifications (α8.5b.3).

Registered on the in-process ``PublisherPort``, it listens for **`ExportJobSucceeded`**
and **`ExportJobFailed`** and projects each into exactly one in-app ``Notification`` per
recipient. It is deliberately named a **projection**, not a service or workflow: it
*derives read state from immutable events* and never orchestrates — the same posture as
``GeneratedMediaIngestionSubscriber`` but for the distribution context.

Delivery is at-least-once (the relay redelivers on failure), so the projection is
**idempotent** on ``event.id``: the recipient + ``source_event_id`` uniqueness makes a
redelivery a no-op (W8.5b.7). Content mapping (kind/title/body/payload) lives here; the
transactional insert lives in :class:`CreateNotification`.

Error posture (mirrors the ingestion subscriber):
* not-applicable event type → clean return (relay stamps it published);
* **malformed payload** (missing/invalid recipient) → log + clean return (a bad
  immutable event is not retryable — never park the relay on it);
* genuine DB failure inside ``CreateNotification`` → propagates (relay records the
  attempt and re-delivers later).

It builds a **fresh** ``CreateNotification`` per event via an injected factory, so each
projection runs in its own Unit of Work (no session reuse across events).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any
from uuid import UUID

import structlog

from app.application.interfaces.publisher import OutboxEvent
from app.application.use_cases.export._events import (
    EVENT_EXPORT_JOB_FAILED,
    EVENT_EXPORT_JOB_SUCCEEDED,
)
from app.application.use_cases.notifications.create_notification import CreateNotification

_LOGGER = structlog.get_logger(__name__)

_HANDLED_EVENT_TYPES = frozenset({EVENT_EXPORT_JOB_SUCCEEDED, EVENT_EXPORT_JOB_FAILED})


class NotificationProjection:
    """An ``EventHandler`` that projects export terminal events into in-app notifications."""

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
            # A malformed export event payload is not retryable — log + skip rather than
            # parking the row forever (mirrors the ingestion subscriber).
            _LOGGER.error(
                "notification.bad_event_payload",
                event_id=str(event.id),
                event_type=event.event_type,
            )
            return

        content = (
            _succeeded_content(payload)
            if event.event_type == EVENT_EXPORT_JOB_SUCCEEDED
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
    """Mapped notification content (kind/title/body/payload) for one export event."""

    __slots__ = ("kind", "title", "body", "payload")

    def __init__(self, *, kind: str, title: str, body: str | None, payload: dict[str, Any]) -> None:
        self.kind = kind
        self.title = title
        self.body = body
        self.payload = payload


def _identity_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """The delivery-identity subset common to both export notifications."""
    return {
        "export_job_id": payload.get("export_job_id"),
        "render_job_id": payload.get("render_job_id"),
        "format": payload.get("format"),
        "quality": payload.get("quality"),
        "orientation": payload.get("orientation"),
    }


def _succeeded_content(payload: Mapping[str, Any]) -> _Content:
    body_payload = _identity_payload(payload)
    body_payload["output_media_asset_id"] = payload.get("output_media_asset_id")
    quality = payload.get("quality") or "video"
    fmt = payload.get("format") or "file"
    return _Content(
        kind="export.succeeded",
        title="Your video is ready",
        body=f"Your {quality} {fmt} export is ready to download.",
        payload=body_payload,
    )


def _failed_content(payload: Mapping[str, Any]) -> _Content:
    body_payload = _identity_payload(payload)
    error = payload.get("error")
    body_payload["error"] = error
    # The event error is already neutral (W8.5.2 — no provider/orchestration internals).
    message = None
    if isinstance(error, dict):
        raw = error.get("message")
        message = str(raw) if raw else None
    return _Content(
        kind="export.failed",
        title="Your video export failed",
        body=message or "Your video export could not be completed.",
        payload=body_payload,
    )


__all__ = ["NotificationProjection"]
