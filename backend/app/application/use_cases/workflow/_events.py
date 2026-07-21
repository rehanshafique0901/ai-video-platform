"""Domain-event emitters for the WorkflowRun aggregate (Slice α7.2).

The one place that knows the *shape* of WorkflowRun outbox events, so the use
cases stay focused on control flow. Events are written to the ``event_outbox``
**inside the caller's UnitOfWork transaction** (before ``commit()``), so the
aggregate mutation and its event commit atomically — the transactional-outbox
guarantee (blueprint §6 / D9). α7.2 produces the rows; publication is a later
slice (α7.3 relay).

The signed-off Q8 event set:

* ``WorkflowRunCreated``   — a run was queued (steps seeded ``pending``).
* ``WorkflowRunStarted``   — the runner took the run ``queued → running``.
* ``WorkflowStepCompleted``— a step succeeded (carries ``step_index``/``step_name``).
* ``WorkflowRunPaused``    — an async command returned ``IN_PROGRESS``; the run
  settled ``running → paused`` (carries ``step_index``/``provider_job_id`` so the
  α8.3 completion service can resume — α7.6 sign-off Q8).
* ``WorkflowRunSucceeded`` — all steps done; run settled ``running → succeeded``.
* ``WorkflowRunFailed``    — a step failed terminally; run settled ``→ failed``.
* ``WorkflowRunCanceled``  — the run was canceled.

Payloads carry **orchestration fields only** (D3.10): identity + workflow key +
status (+ the step coordinates for the step event). A consumer that needs richer
state resolves it from the referenced aggregates. ``event_version`` starts at
``"1.0"``; any breaking payload change MUST bump it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.domain.workflow.workflow_run import WorkflowRun

AGGREGATE_TYPE = "workflow_run"
EVENT_WORKFLOW_RUN_CREATED = "WorkflowRunCreated"
EVENT_WORKFLOW_RUN_STARTED = "WorkflowRunStarted"
EVENT_WORKFLOW_STEP_COMPLETED = "WorkflowStepCompleted"
EVENT_WORKFLOW_RUN_PAUSED = "WorkflowRunPaused"
EVENT_WORKFLOW_RUN_SUCCEEDED = "WorkflowRunSucceeded"
EVENT_WORKFLOW_RUN_FAILED = "WorkflowRunFailed"
EVENT_WORKFLOW_RUN_CANCELED = "WorkflowRunCanceled"


def _base_payload(run: WorkflowRun) -> dict[str, object]:
    """The common WorkflowRun event body (identity + workflow key + status)."""
    return {
        "workflow_run_id": str(run.id),
        "project_id": str(run.project_id),
        "workflow_key": run.workflow_key,
        "workflow_version": run.workflow_version,
        "status": run.status,
    }


async def _emit(
    uow: IUnitOfWork,
    run: WorkflowRun,
    event_type: str,
    *,
    actor_user_id: UUID | None,
    **extra: object,
) -> None:
    payload = _base_payload(run)
    payload.update(extra)
    await uow.outbox.add(
        aggregate_type=AGGREGATE_TYPE,
        aggregate_id=run.id,
        event_type=event_type,
        payload=payload,
        occurred_at=datetime.now(UTC),
        metadata={"actor_user_id": str(actor_user_id)} if actor_user_id is not None else {},
    )


async def emit_workflow_run_created(
    uow: IUnitOfWork, run: WorkflowRun, *, actor_user_id: UUID | None
) -> None:
    """Append a ``WorkflowRunCreated`` event for a freshly-queued run."""
    await _emit(uow, run, EVENT_WORKFLOW_RUN_CREATED, actor_user_id=actor_user_id)


async def emit_workflow_run_started(
    uow: IUnitOfWork, run: WorkflowRun, *, actor_user_id: UUID | None
) -> None:
    """Append a ``WorkflowRunStarted`` event for the first ``queued → running`` transition."""
    await _emit(uow, run, EVENT_WORKFLOW_RUN_STARTED, actor_user_id=actor_user_id)


async def emit_workflow_step_completed(
    uow: IUnitOfWork,
    run: WorkflowRun,
    *,
    step_index: int,
    step_name: str,
    actor_user_id: UUID | None,
) -> None:
    """Append a ``WorkflowStepCompleted`` event for a step that succeeded."""
    await _emit(
        uow,
        run,
        EVENT_WORKFLOW_STEP_COMPLETED,
        actor_user_id=actor_user_id,
        step_index=step_index,
        step_name=step_name,
    )


async def emit_workflow_run_paused(
    uow: IUnitOfWork,
    run: WorkflowRun,
    *,
    step_index: int,
    provider_job_id: str | None,
    actor_user_id: UUID | None,
) -> None:
    """Append a ``WorkflowRunPaused`` event for an async ``IN_PROGRESS`` command (α7.6 Q8).

    Carries the paused ``step_index`` and the provider's ``provider_job_id`` so the
    α8.3 completion service can resolve the job and resume under the same
    ``request_id``. No usage is recorded for the pause (Q6 — terminal-only).
    """
    await _emit(
        uow,
        run,
        EVENT_WORKFLOW_RUN_PAUSED,
        actor_user_id=actor_user_id,
        step_index=step_index,
        provider_job_id=provider_job_id,
    )


async def emit_workflow_run_succeeded(
    uow: IUnitOfWork, run: WorkflowRun, *, actor_user_id: UUID | None
) -> None:
    """Append a ``WorkflowRunSucceeded`` event once all steps have completed."""
    await _emit(uow, run, EVENT_WORKFLOW_RUN_SUCCEEDED, actor_user_id=actor_user_id)


async def emit_workflow_run_failed(
    uow: IUnitOfWork, run: WorkflowRun, *, actor_user_id: UUID | None
) -> None:
    """Append a ``WorkflowRunFailed`` event when a step fails terminally."""
    await _emit(uow, run, EVENT_WORKFLOW_RUN_FAILED, actor_user_id=actor_user_id)


async def emit_workflow_run_canceled(
    uow: IUnitOfWork, run: WorkflowRun, *, actor_user_id: UUID | None
) -> None:
    """Append a ``WorkflowRunCanceled`` event for a just-canceled run."""
    await _emit(uow, run, EVENT_WORKFLOW_RUN_CANCELED, actor_user_id=actor_user_id)
