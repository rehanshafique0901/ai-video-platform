"""End-to-end integration test for the First Pipeline (Slice α7.6).

Drives the **real** composition stack against the live database inside a SAVEPOINT
that rolls back on teardown: the α7.2 :class:`AdvanceWorkflowRun` runner, the α7.6
provider-backed workflow definitions (``default_registry``), the α7.4
:class:`StepCommandDispatcher` + mock provider registry, and the α7.5 usage
recorder — all on one session-bound Unit of Work, exercising the SQL the unit
suite cannot (partitioned ``usage_records`` insert, effective-at-time pricing,
status-guarded run/step CAS, append-only checkpoints, outbox writes).

This is the milestone check: the orchestration stack runs end-to-end with **no
external provider dependency** (the mocks stand in for real providers behind the
dispatcher). Two pipelines are proven:

* **E1 — image pipeline (fully executable):** ``generate-image@1.0.0`` runs
  prepare-prompt → generate-image (mock ``SUCCEEDED``) → priced usage row →
  checkpoint → ``succeeded``. Asserts the opaque provider envelope is checkpointed
  verbatim (W7.6.1), the usage row is priced off the seeded ``ai_model_pricing``
  under the deterministic ``request_id`` (D5/Q3), and the outbox carries the
  started → step-completed×2 → succeeded chain.
* **E2 — video pipeline (pause seam):** ``generate-video@1.0.0`` emits a
  ``generate_video`` command; the mock returns ``IN_PROGRESS`` + a
  ``provider_job_id``, so the runner settles ``running → paused``, emits
  ``WorkflowRunPaused``, checkpoints the resume coordinates, and records **no**
  usage (Q6). Nothing beyond pause (Q1).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import TracebackType
from typing import Self
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.use_cases.workflow.advance_workflow_run import AdvanceWorkflowRun
from app.domain.workflow.registry import (
    GENERATE_IMAGE,
    GENERATE_VIDEO,
    WORKFLOW_VERSION_1,
    default_registry,
)
from app.domain.workflow.workflow_run_status import WorkflowRunStatus
from app.domain.workflow.workflow_step_status import WorkflowStepStatus
from app.infrastructure.ai.dispatcher import StepCommandDispatcher
from app.infrastructure.db.models.ai_models import AIModel, AIModelPricing
from app.infrastructure.db.models.events import EventOutbox
from app.infrastructure.db.models.identity import Tenant, User
from app.infrastructure.db.models.projects import Project as ProjectRow
from app.infrastructure.db.models.usage import UsageRecord
from app.infrastructure.repositories.event_outbox_repository import EventOutboxRepository
from app.infrastructure.repositories.model_pricing_repository import ModelPricingRepository
from app.infrastructure.repositories.project_repository import ProjectRepository
from app.infrastructure.repositories.usage_record_repository import UsageRecordRepository
from app.infrastructure.repositories.workflow_run_repository import WorkflowRunRepository

pytestmark = pytest.mark.integration


# --------------------------------------------------------------------------- #
# Seed helpers                                                                #
# --------------------------------------------------------------------------- #
async def _seed_owner_project(session: AsyncSession) -> tuple[UUID, UUID, UUID]:
    """Seed tenant + user + project; return ``(tenant_id, user_id, project_id)``."""
    tenant_id = uuid4()
    await session.execute(
        insert(Tenant).values(id=tenant_id, name="Pipe Test", slug=f"pipe-{tenant_id}")
    )
    user_id = uuid4()
    await session.execute(
        insert(User).values(
            id=user_id,
            tenant_id=tenant_id,
            email=f"pipe-{user_id}@example.com",
            display_name="Pipe Owner",
        )
    )
    project_id = uuid4()
    await session.execute(
        insert(ProjectRow).values(
            id=project_id,
            tenant_id=tenant_id,
            owner_user_id=user_id,
            name=f"P {project_id}",
            aspect_ratio="horizontal",
        )
    )
    await session.flush()
    return tenant_id, user_id, project_id


async def _seed_model(session: AsyncSession, *, kind: str) -> UUID:
    model_id = uuid4()
    await session.execute(
        insert(AIModel).values(
            id=model_id,
            model_key=f"mk-{model_id}",
            provider="test",
            vendor_model_id="v1",
            kind=kind,
            status="available",
        )
    )
    await session.flush()
    return model_id


async def _seed_pricing(session: AsyncSession, *, model_id: UUID, unit: str, price: str) -> UUID:
    pricing_id = uuid4()
    await session.execute(
        insert(AIModelPricing).values(
            id=pricing_id,
            model_id=model_id,
            effective_from=datetime.now(UTC) - timedelta(days=1),
            effective_to=None,
            unit=unit,
            price_per_unit=Decimal(price),
            currency="USD",
        )
    )
    await session.flush()
    return pricing_id


# --------------------------------------------------------------------------- #
# Session-bound Unit of Work — the runner's single transaction on the SAVEPOINT #
# --------------------------------------------------------------------------- #
class _PipelineUnitOfWork:
    """Wires every repo the α7.6 runner touches onto the test session.

    ``commit`` flushes (not a real COMMIT) so writes stay inside the test's
    SAVEPOINT and vanish on teardown, while remaining visible to the post-run
    assertion SELECTs on the same session. ``__aenter__`` / ``__aexit__`` do not
    close the shared session — the fixture owns its lifecycle.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self.projects = ProjectRepository(session)
        self.workflow_runs = WorkflowRunRepository(session)
        self.outbox = EventOutboxRepository(session)
        self.usage = UsageRecordRepository(session)
        self.model_pricing = ModelPricingRepository(session)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    async def commit(self) -> None:
        await self._session.flush()

    async def rollback(self) -> None:
        await self._session.flush()


async def _add_run(
    repo: WorkflowRunRepository,
    project_id: UUID,
    user_id: UUID,
    *,
    workflow_key: str,
    input_snapshot: dict,
):  # type: ignore[no-untyped-def]
    return await repo.add(
        project_id=project_id,
        workflow_key=workflow_key,
        workflow_version=WORKFLOW_VERSION_1,
        status=WorkflowRunStatus.QUEUED.value,
        input_snapshot=input_snapshot,
        triggered_by_user_id=user_id,
        idempotency_key=None,
    )


async def _outbox_events(session: AsyncSession, run_id: UUID) -> list[str]:
    """Ordered ``event_type``s the runner wrote for ``run_id`` (by ``occurred_at``)."""
    stmt = (
        select(EventOutbox.event_type)
        .where(EventOutbox.aggregate_type == "workflow_run")
        .where(EventOutbox.aggregate_id == run_id)
        .order_by(EventOutbox.occurred_at, EventOutbox.id)
    )
    return [row[0] for row in (await session.execute(stmt)).all()]


# --------------------------------------------------------------------------- #
# E1 — image pipeline runs to succeeded, priced + checkpointed opaquely        #
# --------------------------------------------------------------------------- #
async def test_e1_image_pipeline_runs_to_succeeded(session: AsyncSession) -> None:
    tenant_id, user_id, project_id = await _seed_owner_project(session)
    model_id = await _seed_model(session, kind="image")
    await _seed_pricing(session, model_id=model_id, unit="image", price="0.04")

    registry = default_registry()
    definition = registry.get(GENERATE_IMAGE, WORKFLOW_VERSION_1)
    assert definition is not None

    uow = _PipelineUnitOfWork(session)
    run = await _add_run(
        uow.workflow_runs,
        project_id,
        user_id,
        workflow_key=GENERATE_IMAGE,
        input_snapshot={"subject": "a red fox", "model_id": str(model_id)},
    )
    await uow.workflow_runs.seed_steps(run.id, definition.step_specs)

    runner = AdvanceWorkflowRun(
        uow,  # type: ignore[arg-type]
        registry=registry,
        dispatcher=StepCommandDispatcher(),
    )
    result = await runner.execute(
        project_id=project_id,
        workflow_run_id=run.id,
        owner_user_id=user_id,
        tenant_id=tenant_id,
    )

    # Ran to a clean terminal success.
    assert result.advanced is True
    assert result.view.run.status == WorkflowRunStatus.SUCCEEDED.value
    steps = result.view.steps
    assert [s.status for s in steps] == [WorkflowStepStatus.SUCCEEDED.value] * 2

    # Deterministic request_id (D5): run_id:step_index:command_index. The image
    # command is command 0 of the second step (step_index 1).
    request_id = f"{run.id}:1:0"

    # Usage was recorded + priced off the seeded ai_model_pricing (1 image × $0.04).
    usage_row = await UsageRecordRepository(session).get_by_request_id(request_id)
    assert usage_row is not None
    assert usage_row.tenant_id == tenant_id
    assert usage_row.model_id == model_id
    assert usage_row.status == "success"
    assert usage_row.unit == "image"
    assert usage_row.unit_count == Decimal("1")
    assert usage_row.images_count == 1
    assert usage_row.estimated_cost == Decimal("0.04000000")
    assert usage_row.pricing_id is not None

    # The persisted row is linked back to the run + project (the view does not
    # project these, so read the ORM row directly). Exactly one row exists — the
    # command was dispatched exactly once (W7.6.2).
    orm_rows = (
        (await session.execute(select(UsageRecord).where(UsageRecord.request_id == request_id)))
        .scalars()
        .all()
    )
    assert len(orm_rows) == 1
    assert orm_rows[0].workflow_run_id == run.id
    assert orm_rows[0].project_id == project_id

    # The opaque provider envelope is checkpointed verbatim (W7.6.1) — the runner
    # stored the whole ``output`` bag without interpreting ``image_ref``.
    checkpoint = await uow.workflow_runs.latest_checkpoint(run.id, 1)
    assert checkpoint is not None
    outputs = checkpoint.state["provider_outputs"]
    assert isinstance(outputs, list) and len(outputs) == 1
    envelope = outputs[0]
    assert envelope["provider"] == "mock-image"
    assert envelope["request_id"] == request_id
    assert envelope["status"] == "succeeded"
    assert envelope["output"]["image_ref"] == f"mock://image/{request_id}"

    # Outbox: started → step completed ×2 → succeeded (no paused/failed).
    events = await _outbox_events(session, run.id)
    assert events == [
        "WorkflowRunStarted",
        "WorkflowStepCompleted",
        "WorkflowStepCompleted",
        "WorkflowRunSucceeded",
    ]


# --------------------------------------------------------------------------- #
# E2 — video pipeline pauses on IN_PROGRESS (the async seam), records no usage  #
# --------------------------------------------------------------------------- #
async def test_e2_video_pipeline_pauses_on_in_progress(session: AsyncSession) -> None:
    tenant_id, user_id, project_id = await _seed_owner_project(session)
    model_id = await _seed_model(session, kind="video")
    await _seed_pricing(session, model_id=model_id, unit="video_second", price="0.10")

    registry = default_registry()
    definition = registry.get(GENERATE_VIDEO, WORKFLOW_VERSION_1)
    assert definition is not None

    uow = _PipelineUnitOfWork(session)
    run = await _add_run(
        uow.workflow_runs,
        project_id,
        user_id,
        workflow_key=GENERATE_VIDEO,
        input_snapshot={"subject": "a city at dusk", "model_id": str(model_id)},
    )
    await uow.workflow_runs.seed_steps(run.id, definition.step_specs)

    runner = AdvanceWorkflowRun(
        uow,  # type: ignore[arg-type]
        registry=registry,
        dispatcher=StepCommandDispatcher(),
    )
    result = await runner.execute(
        project_id=project_id,
        workflow_run_id=run.id,
        owner_user_id=user_id,
        tenant_id=tenant_id,
    )

    # Settled running → paused (not terminal): finished_at stays unset (Q2).
    assert result.advanced is True
    assert result.view.run.status == WorkflowRunStatus.PAUSED.value
    assert result.view.run.finished_at is None
    # The paused step is left ``running`` (the provider job is still in flight).
    assert result.view.steps[0].status == WorkflowStepStatus.RUNNING.value

    # No usage recorded for an IN_PROGRESS call (Q6 — terminal-only).
    request_id = f"{run.id}:0:0"
    assert await UsageRecordRepository(session).get_by_request_id(request_id) is None
    usage_count = int(
        (
            await session.execute(
                select(func.count())
                .select_from(UsageRecord)
                .where(UsageRecord.workflow_run_id == run.id)
            )
        ).scalar_one()
    )
    assert usage_count == 0

    # The pause checkpoint carries the resume coordinates for α8.3.
    checkpoint = await uow.workflow_runs.latest_checkpoint(run.id, 0)
    assert checkpoint is not None
    paused = checkpoint.state["_paused"]
    assert paused["provider"] == "mock-video"
    assert paused["request_id"] == request_id
    assert paused["provider_job_id"] is not None
    assert paused["pending_step_index"] == 0

    # Outbox: started → paused (no step-completed, no succeeded/failed).
    events = await _outbox_events(session, run.id)
    assert events == ["WorkflowRunStarted", "WorkflowRunPaused"]
