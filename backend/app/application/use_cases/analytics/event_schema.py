"""Deterministic analytics event schema (Slice α9.0 — AN3).

The single source of truth for which outbox events become analytics events, what their
stable ``event_name`` is, and which **neutral** payload keys are copied into
``analytics_events.properties``. Kept as frozen constants so tests assert the vocabulary
verbatim (a deterministic schema).

Only the **owner-attributable** publish + export lifecycle events are projected — they
carry ``requested_by_user_id`` in the payload (AN2). Render/workflow/generation events are
deferred (no owner id in payload). The property subset copies only already-neutral identity
fields — never a credential, bearer, URL, or byte (PUB-8 / ADR-0047 C8); the source events
carry none.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.application.use_cases.export._events import (
    EVENT_EXPORT_JOB_CREATED,
    EVENT_EXPORT_JOB_FAILED,
    EVENT_EXPORT_JOB_SUCCEEDED,
)
from app.application.use_cases.publishing._events import (
    EVENT_PUBLISH_JOB_CREATED,
    EVENT_PUBLISH_JOB_FAILED,
    EVENT_PUBLISH_JOB_SUCCEEDED,
)

# --- analytics event_name vocabulary (stable, deterministic) ----------------------------
EVENT_NAME_PUBLISH_CREATED = "publish.created"
EVENT_NAME_PUBLISH_SUCCEEDED = "publish.succeeded"
EVENT_NAME_PUBLISH_FAILED = "publish.failed"
EVENT_NAME_EXPORT_CREATED = "export.created"
EVENT_NAME_EXPORT_SUCCEEDED = "export.succeeded"
EVENT_NAME_EXPORT_FAILED = "export.failed"

# Ordered, exhaustive vocabulary — the read model zero-fills over exactly this set so the
# summary shape is stable regardless of what the caller has done.
ANALYTICS_EVENT_NAMES: tuple[str, ...] = (
    EVENT_NAME_EXPORT_CREATED,
    EVENT_NAME_EXPORT_SUCCEEDED,
    EVENT_NAME_EXPORT_FAILED,
    EVENT_NAME_PUBLISH_CREATED,
    EVENT_NAME_PUBLISH_SUCCEEDED,
    EVENT_NAME_PUBLISH_FAILED,
)

# outbox event_type → analytics event_name (the projected set; anything absent is ignored).
_EVENT_TYPE_TO_NAME: dict[str, str] = {
    EVENT_PUBLISH_JOB_CREATED: EVENT_NAME_PUBLISH_CREATED,
    EVENT_PUBLISH_JOB_SUCCEEDED: EVENT_NAME_PUBLISH_SUCCEEDED,
    EVENT_PUBLISH_JOB_FAILED: EVENT_NAME_PUBLISH_FAILED,
    EVENT_EXPORT_JOB_CREATED: EVENT_NAME_EXPORT_CREATED,
    EVENT_EXPORT_JOB_SUCCEEDED: EVENT_NAME_EXPORT_SUCCEEDED,
    EVENT_EXPORT_JOB_FAILED: EVENT_NAME_EXPORT_FAILED,
}

# The outbox event types this consumer handles (everything else → clean no-op).
HANDLED_EVENT_TYPES = frozenset(_EVENT_TYPE_TO_NAME)

# The neutral payload keys copied into ``properties`` per aggregate family (identity only;
# no secrets). Absent keys are simply omitted.
_PUBLISH_PROPERTY_KEYS = ("publish_job_id", "project_id", "platform", "status")
_EXPORT_PROPERTY_KEYS = ("export_job_id", "render_job_id", "format", "quality", "status")


def event_name_for(event_type: str) -> str | None:
    """Return the analytics ``event_name`` for an outbox ``event_type``, or ``None``."""
    return _EVENT_TYPE_TO_NAME.get(event_type)


def properties_for(event_type: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Extract the neutral identity subset for ``properties`` (never secrets).

    Always records ``source_event_type`` (the producing outbox type) plus the whitelisted
    identity keys for that aggregate family. Only keys actually present in ``payload`` are
    copied, so a slim event stays slim.
    """
    keys: tuple[str, ...]
    if event_type in (
        EVENT_PUBLISH_JOB_CREATED,
        EVENT_PUBLISH_JOB_SUCCEEDED,
        EVENT_PUBLISH_JOB_FAILED,
    ):
        keys = _PUBLISH_PROPERTY_KEYS
    else:
        keys = _EXPORT_PROPERTY_KEYS
    props: dict[str, Any] = {"source_event_type": event_type}
    for key in keys:
        if key in payload:
            props[key] = payload[key]
    return props


__all__ = [
    "ANALYTICS_EVENT_NAMES",
    "HANDLED_EVENT_TYPES",
    "EVENT_NAME_PUBLISH_CREATED",
    "EVENT_NAME_PUBLISH_SUCCEEDED",
    "EVENT_NAME_PUBLISH_FAILED",
    "EVENT_NAME_EXPORT_CREATED",
    "EVENT_NAME_EXPORT_SUCCEEDED",
    "EVENT_NAME_EXPORT_FAILED",
    "event_name_for",
    "properties_for",
]
