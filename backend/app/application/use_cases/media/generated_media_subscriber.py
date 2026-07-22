"""``GeneratedMediaIngestionSubscriber`` — the first real outbox consumer (α8.4a).

Registered on the in-process ``PublisherPort``, it listens for
``WorkflowRunSucceeded`` and triggers :class:`IngestGeneratedMedia`. This is the
seam that turns the orchestration layer into a *platform*: completely independent
consumers (analytics, notifications, billing, export, …) can attach to the same
event stream later without the runner ever knowing they exist.

Delivery is at-least-once (the relay redelivers on failure), so the subscriber is
**idempotent** — ingestion's deterministic storage key + the ``media_assets``
uniqueness make a redelivery a no-op. A raised exception propagates so the relay
records the failure and retries; a clean return (including the not-applicable
event types) lets the relay stamp the event published.

It builds a **fresh** ``IngestGeneratedMedia`` per event via an injected factory,
so each delivery runs in its own Unit of Work (no session reuse across events).
"""

from __future__ import annotations

from collections.abc import Callable
from uuid import UUID

import structlog

from app.application.interfaces.publisher import OutboxEvent
from app.application.use_cases.media.ingest_generated_media import IngestGeneratedMedia
from app.application.use_cases.workflow._events import EVENT_WORKFLOW_RUN_SUCCEEDED

_LOGGER = structlog.get_logger(__name__)


class GeneratedMediaIngestionSubscriber:
    """An ``EventHandler`` that ingests generated media when a run succeeds."""

    def __init__(self, ingest_factory: Callable[[], IngestGeneratedMedia]) -> None:
        # A factory (not an instance) so each event gets a fresh use case + UoW.
        self._ingest_factory = ingest_factory

    async def __call__(self, event: OutboxEvent) -> None:
        if event.event_type != EVENT_WORKFLOW_RUN_SUCCEEDED:
            return  # not applicable — clean return lets the relay mark it published

        payload = event.payload
        try:
            project_id = UUID(str(payload["project_id"]))
            workflow_run_id = UUID(str(payload["workflow_run_id"]))
        except (KeyError, ValueError):
            # A malformed WorkflowRunSucceeded payload is not retryable — log + skip
            # rather than parking the row forever.
            _LOGGER.error("media.ingest_bad_event_payload", event_id=str(event.id))
            return

        ingest = self._ingest_factory()
        result = await ingest.execute(project_id=project_id, workflow_run_id=workflow_run_id)
        _LOGGER.debug(
            "media.ingest_subscriber_handled",
            event_id=str(event.id),
            workflow_run_id=str(workflow_run_id),
            status=result.status,
            registered=len(result.registered_media_ids),
        )
