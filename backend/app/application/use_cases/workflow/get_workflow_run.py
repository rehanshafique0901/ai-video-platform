"""``GetWorkflowRun`` use case (Slice α7.2).

Contract (API_CONTRACT §3.2.6):

    GET /api/v1/projects/{project_id}/workflow-runs/{workflow_run_id}
      → 200  { data: WorkflowRunPublic (with steps[] + latest checkpoint), meta }
      → 404  { error: { code: NOT_FOUND, ... } }        (project/run missing / not yours)
      → 401  { error: { code: UNAUTHENTICATED, ... } }  (via CurrentUserDep)

Project ownership gate (404) then a project-scoped single read; a run under
another user's project — or an unknown id — is the same uniform ``404``
(anti-enumeration, D3.4). Returns the run plus its ordered steps and latest
checkpoint (the detail read-model).
"""

from __future__ import annotations

from uuid import UUID

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.application.use_cases.workflow._view import WorkflowRunView
from app.core.errors import NotFoundError


class GetWorkflowRun:
    """Fetch one workflow run (with steps + latest checkpoint) owned via the caller's project."""

    def __init__(self, uow: IUnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self,
        *,
        project_id: UUID,
        workflow_run_id: UUID,
        owner_user_id: UUID,
        tenant_id: UUID,
    ) -> WorkflowRunView:
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
            steps = await self._uow.workflow_runs.list_steps(run.id)
            latest = await self._uow.workflow_runs.latest_checkpoint(run.id)
            return WorkflowRunView(run=run, steps=steps, latest_checkpoint=latest)
