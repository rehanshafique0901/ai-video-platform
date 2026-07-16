"""``AdvanceWorkflowRun`` — the synchronous deterministic runner (Slice α7.2).

Contract (API_CONTRACT §3.2.6):

    POST /api/v1/projects/{project_id}/workflow-runs/{workflow_run_id}/advance
      → 200  { data: WorkflowRunPublic (ran to succeeded/failed), meta }
      → 404  { error: { code: NOT_FOUND, ... } }        (project/run missing / not yours)
      → 409  { error: { code: CONFLICT, ... } }         (already terminal / definition gone)
      → 401  { error: { code: UNAUTHENTICATED, ... } }  (via CurrentUserDep)

This is the **imperative shell** around the pure step handlers (D3.11). It is the
*only* place that touches the DB / outbox; the handlers it calls are pure
functions that return a :class:`~app.domain.workflow.registry.StepResult`
describing what should happen. There are **no external providers, no async worker,
no scheduler** — advance runs to a terminal state within one synchronous call, in
one UnitOfWork transaction (so the whole run either commits terminal or rolls
back).

Algorithm (D3.8):

1. Project gate (404) → run visibility (404) → resolve the definition.
2. Precondition: only ``queued`` (start) or ``running`` (resume) may advance;
   terminal/paused → ``409``. A ``queued`` run is taken ``queued → running`` via a
   status-guarded CAS and a ``WorkflowRunStarted`` event.
3. For each step in ``step_index`` order: skip already-``succeeded``/``skipped``
   steps (resume-safety, Q7); otherwise mark ``running`` and call the handler with
   ``(input_snapshot, prior checkpoint state, attempt)``. On success → persist
   ``output`` + append a checkpoint + emit ``WorkflowStepCompleted``. On a transient
   failure → bump ``retries`` / ``retrying`` and retry up to the definition bound
   (deterministic, no backoff — Q5); on exhaustion or a terminal failure → fail the
   step and the run.
4. All steps done → ``running → succeeded`` + ``output_summary`` +
   ``WorkflowRunSucceeded``; any terminal step failure → ``running → failed`` +
   ``error`` + ``WorkflowRunFailed``.

Concurrency is the status-guarded CAS (D3.2): a second concurrent advance finds no
runnable step in the expected state and is re-classified rather than double-running.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

import structlog

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.application.use_cases.workflow._events import (
    emit_workflow_run_failed,
    emit_workflow_run_started,
    emit_workflow_run_succeeded,
    emit_workflow_step_completed,
)
from app.application.use_cases.workflow._view import WorkflowRunView
from app.core.errors import ConflictError, NotFoundError
from app.domain.workflow.registry import (
    WORKFLOW_REGISTRY,
    StepContext,
    StepDefinition,
    StepOutcome,
    WorkflowDefinition,
    WorkflowRegistry,
)
from app.domain.workflow.workflow_run import WorkflowRun
from app.domain.workflow.workflow_run_status import WorkflowRunStatus
from app.domain.workflow.workflow_step_status import WorkflowStepStatus

_LOGGER = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class AdvanceWorkflowRunResult:
    """The run view after advancement plus whether this call did any work.

    ``advanced`` is ``True`` when the runner executed at least the start transition
    (queued → running) or a step; ``False`` is reserved for a resume that found
    nothing runnable (all steps already done) — still a ``200``.
    """

    view: WorkflowRunView
    advanced: bool


class AdvanceWorkflowRun:
    """Drive a workflow run to a terminal state using its in-code definition (D3.8)."""

    def __init__(self, uow: IUnitOfWork, registry: WorkflowRegistry = WORKFLOW_REGISTRY) -> None:
        self._uow = uow
        self._registry = registry

    async def execute(
        self,
        *,
        project_id: UUID,
        workflow_run_id: UUID,
        owner_user_id: UUID,
        tenant_id: UUID,
        ip: str | None = None,
    ) -> AdvanceWorkflowRunResult:
        async with self._uow:
            project = await self._uow.projects.get_owned(
                project_id=project_id,
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
            )
            if project is None:
                raise NotFoundError(
                    "workflow run not found",
                    details={"workflow_run_id": str(workflow_run_id)},
                )

            run = await self._uow.workflow_runs.get_owned(project_id, workflow_run_id)
            if run is None:
                raise NotFoundError(
                    "workflow run not found",
                    details={"workflow_run_id": str(workflow_run_id)},
                )

            definition = self._registry.get(run.workflow_key, run.workflow_version)
            if definition is None:
                # The run was created against a registered definition; if it's gone
                # now the run cannot be advanced (registry drift).
                raise ConflictError(
                    "workflow definition is no longer available",
                    details={
                        "workflow_key": run.workflow_key,
                        "workflow_version": run.workflow_version,
                    },
                )

            status = WorkflowRunStatus(run.status)
            if status.is_terminal:
                _LOGGER.info(
                    "workflow_run.advance_noop_terminal",
                    workflow_run_id=str(run.id),
                    project_id=str(project_id),
                    status=run.status,
                    owner_user_id=str(owner_user_id),
                    ip=ip,
                )
                raise ConflictError(
                    "workflow run is already in a terminal state",
                    details={"workflow_run_id": str(workflow_run_id), "status": run.status},
                )
            if not status.is_advanceable:
                # ``paused`` is not produced by the α7.2 runner and is not
                # advanceable here (pause/resume is deferred to α8.x, Q4).
                raise ConflictError(
                    "workflow run cannot be advanced from its current state",
                    details={"workflow_run_id": str(workflow_run_id), "status": run.status},
                )

            advanced = False

            # Start transition (queued → running) — status-guarded CAS + event.
            if status is WorkflowRunStatus.QUEUED:
                started = await self._uow.workflow_runs.mark_run_running(run.id)
                if started is None:
                    # Concurrent advance/cancel raced us out of ``queued``.
                    current = await self._uow.workflow_runs.get_owned(project_id, run.id)
                    raise ConflictError(
                        "workflow run is no longer queued",
                        details={
                            "workflow_run_id": str(workflow_run_id),
                            "status": current.status if current is not None else "unknown",
                        },
                    )
                run = started
                await emit_workflow_run_started(self._uow, run, actor_user_id=owner_user_id)
                advanced = True

            # Execute the step graph in order.
            steps = await self._uow.workflow_runs.list_steps(run.id)
            failure_error, completed = await self._run_steps(
                run, definition, steps, actor_user_id=owner_user_id
            )
            advanced = advanced or bool(completed) or failure_error is not None

            if failure_error is not None:
                settled = await self._uow.workflow_runs.mark_run_failed(run.id, failure_error)
                run = settled if settled is not None else run
                await emit_workflow_run_failed(self._uow, run, actor_user_id=owner_user_id)
            else:
                summary: dict[str, Any] = {
                    "step_count": len(steps),
                    "completed_steps": completed,
                }
                settled = await self._uow.workflow_runs.mark_run_succeeded(run.id, summary)
                run = settled if settled is not None else run
                await emit_workflow_run_succeeded(self._uow, run, actor_user_id=owner_user_id)

            final_steps = await self._uow.workflow_runs.list_steps(run.id)
            latest = await self._uow.workflow_runs.latest_checkpoint(run.id)
            await self._uow.commit()

        _LOGGER.info(
            "workflow_run.advanced",
            workflow_run_id=str(run.id),
            project_id=str(project_id),
            status=run.status,
            owner_user_id=str(owner_user_id),
            ip=ip,
        )
        return AdvanceWorkflowRunResult(
            view=WorkflowRunView(run=run, steps=final_steps, latest_checkpoint=latest),
            advanced=advanced,
        )

    async def _run_steps(
        self,
        run: WorkflowRun,
        definition: WorkflowDefinition,
        steps: list[Any],
        *,
        actor_user_id: UUID,
    ) -> tuple[dict[str, Any] | None, list[str]]:
        """Execute pending steps in order; return (failure_error | None, completed_names).

        ``prior_state`` threads the previous step's checkpoint state into the next
        step's context (the resume point). Already-``succeeded``/``skipped`` steps
        are skipped (resume-safety) but still contribute their checkpoint state.
        """
        prior_state: dict[str, Any] | None = None
        completed: list[str] = []

        for step in steps:
            step_status = WorkflowStepStatus(step.status)
            if step_status.is_done:
                cp = await self._uow.workflow_runs.latest_checkpoint(run.id, step.step_index)
                if cp is not None:
                    prior_state = cp.state
                if step_status is WorkflowStepStatus.SUCCEEDED:
                    completed.append(step.step_name)
                continue

            if step.step_index >= len(definition.steps):  # pragma: no cover - seeding guard
                return (
                    {
                        "code": "DEFINITION_MISMATCH",
                        "message": f"no definition for step_index {step.step_index}",
                        "step_index": step.step_index,
                    },
                    completed,
                )
            step_def = definition.steps[step.step_index]

            outcome_error, checkpoint_state = await self._run_single_step(
                run, step, step_def, prior_state, actor_user_id=actor_user_id
            )
            if outcome_error is not None:
                return outcome_error, completed
            prior_state = checkpoint_state
            completed.append(step.step_name)

        return None, completed

    async def _run_single_step(
        self,
        run: WorkflowRun,
        step: Any,
        step_def: StepDefinition,
        prior_state: dict[str, Any] | None,
        *,
        actor_user_id: UUID,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Run one step (with retries). Return (failure_error | None, checkpoint_state | None)."""
        while True:
            running = await self._uow.workflow_runs.mark_step_running(run.id, step.step_index)
            if running is None:  # pragma: no cover - unreachable in the sync single-txn model
                return (
                    {
                        "code": "STEP_NOT_RUNNABLE",
                        "message": f"step {step.step_index} could not be started",
                        "step_index": step.step_index,
                    },
                    None,
                )

            ctx = StepContext(
                run_input=run.input_snapshot,
                prior_state=prior_state,
                attempt=running.retries,
            )
            result = step_def.handler(ctx)

            if result.outcome is StepOutcome.SUCCEEDED:
                await self._uow.workflow_runs.mark_step_succeeded(
                    run.id, step.step_index, result.output
                )
                cp = await self._uow.workflow_runs.append_checkpoint(
                    run.id, step.step_index, result.checkpoint_state
                )
                await emit_workflow_step_completed(
                    self._uow,
                    run,
                    step_index=step.step_index,
                    step_name=step.step_name,
                    actor_user_id=actor_user_id,
                )
                return None, cp.state

            error = result.error if result.error is not None else {"code": "UNKNOWN"}

            if result.outcome is StepOutcome.TRANSIENT_FAILURE:
                if running.retries < step_def.max_retries:
                    await self._uow.workflow_runs.mark_step_retrying(run.id, step.step_index, error)
                    continue  # retry with an incremented attempt counter
                await self._uow.workflow_runs.mark_step_failed(run.id, step.step_index, error)
                return (
                    {
                        "step_index": step.step_index,
                        "step_name": step.step_name,
                        "reason": "retries_exhausted",
                        "error": error,
                    },
                    None,
                )

            # TERMINAL_FAILURE
            await self._uow.workflow_runs.mark_step_failed(run.id, step.step_index, error)
            return (
                {
                    "step_index": step.step_index,
                    "step_name": step.step_name,
                    "reason": "terminal",
                    "error": error,
                },
                None,
            )
