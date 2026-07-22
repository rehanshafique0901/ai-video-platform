"""Unit tests for ``GeneratedMediaIngestionSubscriber`` (Slice α8.4a)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.application.interfaces.publisher import OutboxEvent
from app.application.use_cases.media.generated_media_subscriber import (
    GeneratedMediaIngestionSubscriber,
)
from app.application.use_cases.media.ingest_generated_media import IngestGeneratedMediaResult

pytestmark = pytest.mark.unit


class _StubIngest:
    def __init__(self) -> None:
        self.calls: list[tuple[UUID, UUID]] = []

    async def execute(
        self, *, project_id: UUID, workflow_run_id: UUID
    ) -> IngestGeneratedMediaResult:
        self.calls.append((project_id, workflow_run_id))
        return IngestGeneratedMediaResult(status="ingested", registered_media_ids=[uuid4()])


def _event(event_type: str, payload: dict[str, Any]) -> OutboxEvent:
    return OutboxEvent(
        id=uuid4(),
        aggregate_type="workflow_run",
        aggregate_id=uuid4(),
        event_type=event_type,
        event_version="1",
        payload=payload,
        metadata={},
        occurred_at=datetime.now(UTC),
        attempts=0,
    )


def _subscriber() -> tuple[GeneratedMediaIngestionSubscriber, list[_StubIngest]]:
    built: list[_StubIngest] = []

    def factory() -> Any:
        stub = _StubIngest()
        built.append(stub)
        return stub

    return GeneratedMediaIngestionSubscriber(factory), built


async def test_triggers_ingestion_on_run_succeeded() -> None:
    subscriber, built = _subscriber()
    project_id = uuid4()
    run_id = uuid4()
    await subscriber(
        _event(
            "WorkflowRunSucceeded",
            {"project_id": str(project_id), "workflow_run_id": str(run_id)},
        )
    )
    assert len(built) == 1
    assert built[0].calls == [(project_id, run_id)]


async def test_ignores_other_event_types() -> None:
    subscriber, built = _subscriber()
    await subscriber(
        _event(
            "WorkflowRunFailed",
            {"project_id": str(uuid4()), "workflow_run_id": str(uuid4())},
        )
    )
    # No use case built for a non-applicable event.
    assert built == []


async def test_malformed_payload_does_not_raise_or_trigger() -> None:
    subscriber, built = _subscriber()
    # Missing workflow_run_id → not retryable; subscriber returns cleanly.
    await subscriber(_event("WorkflowRunSucceeded", {"project_id": str(uuid4())}))
    assert built == []
