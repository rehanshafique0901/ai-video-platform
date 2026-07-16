"""``/api/v1/projects/{project_id}/workflow-runs/*`` HTTP router (α7.2).

Five **project-nested** endpoints (ownership derived through the project — a
workflow run has no owner columns of its own), all authenticated via
:data:`CurrentUserDep`:

* ``POST /projects/{id}/workflow-runs``                  → 201 (or 200 on idempotent
  replay), queue a run + seed its ``pending`` steps.
* ``GET  /projects/{id}/workflow-runs``                  → 200, list the project's
  runs (optional ``?status=`` filter) — summaries.
* ``GET  /projects/{id}/workflow-runs/{run_id}``         → 200, fetch one (run +
  steps + latest checkpoint).
* ``POST /projects/{id}/workflow-runs/{run_id}/advance`` → 200, run the deterministic
  runner to a terminal state (resumable, idempotent).
* ``POST /projects/{id}/workflow-runs/{run_id}/cancel``  → 200, cancel a
  ``queued``/``running``/``paused`` run (status-guarded).

Workflow runs are **status-guarded**, not version-fenced (ADR-0040 / D3.2): there
is **no ``version``** on the wire, cancel/advance carry no body, and there is no
``412``. The router stays thin — DTO projection + envelope + the 201/200 create
split; the project ownership gate, registry resolution, idempotency, the runner,
and the cancel state machine live in the use cases.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Query, Request, status
from fastapi.responses import JSONResponse

from app.api.v1.deps import (
    AdvanceWorkflowRunDep,
    CancelWorkflowRunDep,
    CreateWorkflowRunDep,
    CurrentUserDep,
    GetWorkflowRunDep,
    ListWorkflowRunsDep,
)
from app.api.v1.helpers import client_ip, envelope
from app.api.v1.schemas.workflow import (
    WorkflowCheckpointPublic,
    WorkflowRunCreateRequest,
    WorkflowRunPublic,
    WorkflowRunSummary,
    WorkflowStatusLiteral,
    WorkflowStepPublic,
)
from app.application.use_cases.workflow._view import WorkflowRunView
from app.domain.workflow.workflow_run import WorkflowRun, WorkflowStep

router = APIRouter(prefix="/projects/{project_id}/workflow-runs", tags=["workflow-runs"])


def _step_to_public(step: WorkflowStep) -> WorkflowStepPublic:
    return WorkflowStepPublic(
        id=step.id,
        step_index=step.step_index,
        step_name=step.step_name,
        status=step.status,
        started_at=step.started_at,
        finished_at=step.finished_at,
        retries=step.retries,
        output=step.output,
        error=step.error,
    )


def _run_to_summary(run: WorkflowRun) -> WorkflowRunSummary:
    return WorkflowRunSummary(
        id=run.id,
        project_id=run.project_id,
        workflow_key=run.workflow_key,
        workflow_version=run.workflow_version,
        status=run.status,
        started_at=run.started_at,
        finished_at=run.finished_at,
        triggered_by_user_id=run.triggered_by_user_id,
        idempotency_key=run.idempotency_key,
        output_summary=run.output_summary,
        error=run.error,
        created_at=run.created_at,
        updated_at=run.updated_at,
    )


def _view_to_public(view: WorkflowRunView) -> WorkflowRunPublic:
    run = view.run
    checkpoint = (
        WorkflowCheckpointPublic(
            id=view.latest_checkpoint.id,
            step_index=view.latest_checkpoint.step_index,
            state=view.latest_checkpoint.state,
            created_at=view.latest_checkpoint.created_at,
        )
        if view.latest_checkpoint is not None
        else None
    )
    return WorkflowRunPublic(
        id=run.id,
        project_id=run.project_id,
        workflow_key=run.workflow_key,
        workflow_version=run.workflow_version,
        status=run.status,
        started_at=run.started_at,
        finished_at=run.finished_at,
        triggered_by_user_id=run.triggered_by_user_id,
        idempotency_key=run.idempotency_key,
        output_summary=run.output_summary,
        error=run.error,
        created_at=run.created_at,
        updated_at=run.updated_at,
        input_snapshot=run.input_snapshot,
        steps=[_step_to_public(s) for s in view.steps],
        latest_checkpoint=checkpoint,
    )


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_workflow_run(
    project_id: UUID,
    body: WorkflowRunCreateRequest,
    request: Request,
    current_user: CurrentUserDep,
    use_case: CreateWorkflowRunDep,
) -> JSONResponse:
    """Queue a workflow run for the caller's project and seed its ``pending`` steps.

    Returns ``201`` with the queued ``WorkflowRunPublic``. When ``idempotency_key``
    matches an existing run for the project, returns that run with ``200`` instead
    (idempotent replay, Q7). ``404`` if the project is missing/not the caller's;
    ``422`` if ``workflow_key@workflow_version`` names no registered workflow.
    """
    result = await use_case.execute(
        project_id=project_id,
        owner_user_id=current_user.id,
        tenant_id=current_user.tenant_id,
        workflow_key=body.workflow_key,
        workflow_version=body.workflow_version,
        input_snapshot=body.input_snapshot,
        idempotency_key=body.idempotency_key,
        ip=client_ip(request),
    )
    code = status.HTTP_201_CREATED if result.created else status.HTTP_200_OK
    return JSONResponse(status_code=code, content=envelope(_view_to_public(result.view), request))


@router.get("")
async def list_workflow_runs(
    project_id: UUID,
    request: Request,
    current_user: CurrentUserDep,
    use_case: ListWorkflowRunsDep,
    status_filter: WorkflowStatusLiteral | None = Query(default=None, alias="status"),
) -> JSONResponse:
    """List the caller's project workflow runs, newest-first, optionally by status.

    ``?status=`` (a ``workflow_status`` value) narrows the result; a bad enum is a
    ``422``. ``404`` if the project is missing/not the caller's. Empty → ``200 []``.
    """
    runs = await use_case.execute(
        project_id=project_id,
        owner_user_id=current_user.id,
        tenant_id=current_user.tenant_id,
        status=status_filter,
    )
    return JSONResponse(content=envelope([_run_to_summary(r) for r in runs], request))


@router.get("/{workflow_run_id}")
async def get_workflow_run(
    project_id: UUID,
    workflow_run_id: UUID,
    request: Request,
    current_user: CurrentUserDep,
    use_case: GetWorkflowRunDep,
) -> JSONResponse:
    """Fetch one workflow run of the caller's project (with steps + latest checkpoint).

    A missing run — or one under another user's project — yields a uniform ``404``
    (D3.4).
    """
    view = await use_case.execute(
        project_id=project_id,
        workflow_run_id=workflow_run_id,
        owner_user_id=current_user.id,
        tenant_id=current_user.tenant_id,
    )
    return JSONResponse(content=envelope(_view_to_public(view), request))


@router.post("/{workflow_run_id}/advance")
async def advance_workflow_run(
    project_id: UUID,
    workflow_run_id: UUID,
    request: Request,
    current_user: CurrentUserDep,
    use_case: AdvanceWorkflowRunDep,
) -> JSONResponse:
    """Run the deterministic runner over a ``queued``/``running`` run to a terminal state.

    Returns ``200`` with the run after advancement (``succeeded`` or ``failed``),
    including its steps and latest checkpoint. ``404`` (project/run not visible),
    ``409`` (already terminal). No request body — advancement is status-guarded.
    """
    result = await use_case.execute(
        project_id=project_id,
        workflow_run_id=workflow_run_id,
        owner_user_id=current_user.id,
        tenant_id=current_user.tenant_id,
        ip=client_ip(request),
    )
    return JSONResponse(content=envelope(_view_to_public(result.view), request))


@router.post("/{workflow_run_id}/cancel")
async def cancel_workflow_run(
    project_id: UUID,
    workflow_run_id: UUID,
    request: Request,
    current_user: CurrentUserDep,
    use_case: CancelWorkflowRunDep,
) -> JSONResponse:
    """Cancel a ``queued``/``running``/``paused`` workflow run (status-guarded).

    Returns ``200`` with the canceled ``WorkflowRunPublic`` (a re-cancel of an
    already-canceled run is a ``200`` no-op). ``404`` (project/run not visible),
    ``409`` (already succeeded/failed). No request body and no ``412`` — there is no
    version token (D3.2).
    """
    result = await use_case.execute(
        project_id=project_id,
        workflow_run_id=workflow_run_id,
        owner_user_id=current_user.id,
        tenant_id=current_user.tenant_id,
        ip=client_ip(request),
    )
    return JSONResponse(content=envelope(_view_to_public(result.view), request))
