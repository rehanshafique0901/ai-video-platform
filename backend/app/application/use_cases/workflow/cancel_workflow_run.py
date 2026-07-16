"""``CancelWorkflowRun`` use case (Slice α7.2).

Contract (API_CONTRACT §3.2.6):

    POST /api/v1/projects/{project_id}/workflow-runs/{workflow_run_id}/cancel
      → 200  { data: WorkflowRunPublic (status=canceled), meta }
      → 404  { error: { code: NOT_FOUND, ... } }        (project/run missing / not yours)
      → 409  { error: { code: CONFLICT, ... } }         (already succeeded/failed)
      → 401  { error: { code: UNAUTHENTICATED, ... } }  (via CurrentUserDep)

Cancel is a **status-guarded** transition (D3.7) — a POST verb that changes state,
returning the canceled run (``200``). Unlike ``RenderJob``'s cancel it is **not**
version-fenced: ``workflow_runs`` has no ``version`` column (D3.2), so there is no
``version`` body and no ``412``. The state machine:

    queued/running/paused ─▶ canceled   ✅
    canceled ─▶ canceled                ⇒ 200 no-op (idempotent)
    succeeded/failed ─▶ cancel          ⇒ 409 (completed work is not cancelable)

Control flow mirrors α7.1: project gate → run visibility (404) → terminal-state
classification → status-guarded CAS. The CAS carries a ``status IN
('queued','running','paused')`` predicate so a run the runner completes between
the read and the write is not silently overwritten — a ``None`` CAS result is
re-classified (canceled → 200 no-op; terminal → 409).

On a real cancel a ``WorkflowRunCanceled`` event is written to the ``event_outbox``
in the same transaction (D9).
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

import structlog

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.application.use_cases.workflow._events import emit_workflow_run_canceled
from app.application.use_cases.workflow._view import WorkflowRunView
from app.core.errors import ConflictError, NotFoundError
from app.domain.workflow.workflow_run_status import WorkflowRunStatus

_LOGGER = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class CancelWorkflowRunResult:
    """The (now-)canceled run view plus whether this call actually changed state.

    ``canceled`` is ``True`` when this call transitioned the run to ``canceled`` (a
    ``WorkflowRunCanceled`` event was emitted); ``False`` for an idempotent
    re-cancel of an already-``canceled`` run (no event, no state change). Both
    render ``200``.
    """

    view: WorkflowRunView
    canceled: bool


class CancelWorkflowRun:
    """Status-guarded cancel of the caller's project workflow run."""

    def __init__(self, uow: IUnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self,
        *,
        project_id: UUID,
        workflow_run_id: UUID,
        owner_user_id: UUID,
        tenant_id: UUID,
        ip: str | None = None,
    ) -> CancelWorkflowRunResult:
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

            status = WorkflowRunStatus(run.status)

            # Idempotent re-cancel: already canceled → 200 no-op, no event.
            if status is WorkflowRunStatus.CANCELED:
                steps = await self._uow.workflow_runs.list_steps(run.id)
                latest = await self._uow.workflow_runs.latest_checkpoint(run.id)
                _LOGGER.info(
                    "workflow_run.cancel_noop",
                    workflow_run_id=str(run.id),
                    project_id=str(project_id),
                    owner_user_id=str(owner_user_id),
                    ip=ip,
                )
                return CancelWorkflowRunResult(
                    view=WorkflowRunView(run=run, steps=steps, latest_checkpoint=latest),
                    canceled=False,
                )

            # Completed work is not cancelable → 409.
            if not status.is_cancelable:
                _LOGGER.warning(
                    "workflow_run.cancel_rejected",
                    reason="terminal_state",
                    workflow_run_id=str(run.id),
                    project_id=str(project_id),
                    status=run.status,
                    owner_user_id=str(owner_user_id),
                    ip=ip,
                )
                raise ConflictError(
                    "workflow run is already complete and cannot be canceled",
                    details={"workflow_run_id": str(workflow_run_id), "status": run.status},
                )

            canceled = await self._uow.workflow_runs.cancel(project_id, workflow_run_id)
            if canceled is None:
                # CAS lost — re-classify against the current row (the runner may have
                # raced the run to a terminal/canceled state). No version token exists,
                # so a lost CAS is purely a status race.
                current = await self._uow.workflow_runs.get_owned(project_id, workflow_run_id)
                if current is not None and WorkflowRunStatus(current.status) is (
                    WorkflowRunStatus.CANCELED
                ):
                    steps = await self._uow.workflow_runs.list_steps(current.id)
                    latest = await self._uow.workflow_runs.latest_checkpoint(current.id)
                    return CancelWorkflowRunResult(
                        view=WorkflowRunView(run=current, steps=steps, latest_checkpoint=latest),
                        canceled=False,
                    )
                current_status = current.status if current is not None else "unknown"
                raise ConflictError(
                    "workflow run is already complete and cannot be canceled",
                    details={
                        "workflow_run_id": str(workflow_run_id),
                        "status": current_status,
                    },
                )

            await emit_workflow_run_canceled(self._uow, canceled, actor_user_id=owner_user_id)
            steps = await self._uow.workflow_runs.list_steps(canceled.id)
            latest = await self._uow.workflow_runs.latest_checkpoint(canceled.id)
            await self._uow.commit()

        _LOGGER.info(
            "workflow_run.canceled",
            workflow_run_id=str(canceled.id),
            project_id=str(project_id),
            owner_user_id=str(owner_user_id),
            ip=ip,
        )
        return CancelWorkflowRunResult(
            view=WorkflowRunView(run=canceled, steps=steps, latest_checkpoint=latest),
            canceled=True,
        )
