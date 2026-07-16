"""``ListWorkflowRuns`` use case (Slice α7.2).

Contract (API_CONTRACT §3.2.6):

    GET /api/v1/projects/{project_id}/workflow-runs?status=
      → 200  { data: [WorkflowRunSummary], meta }
      → 404  { error: { code: NOT_FOUND, ... } }        (project missing / not yours)
      → 401  { error: { code: UNAUTHENTICATED, ... } }  (via CurrentUserDep)

Project-scoped listing, newest first, optional ``status`` filter. Returns the run
summaries only (no per-run steps — steps are on the single-run detail read). The
project gate (404) establishes ownership before the project-scoped read; an empty
project → ``200 []``.
"""

from __future__ import annotations

from uuid import UUID

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.core.errors import NotFoundError
from app.domain.workflow.workflow_run import WorkflowRun


class ListWorkflowRuns:
    """List the caller's project workflow runs, newest-first, optionally by status."""

    def __init__(self, uow: IUnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self,
        *,
        project_id: UUID,
        owner_user_id: UUID,
        tenant_id: UUID,
        status: str | None = None,
    ) -> list[WorkflowRun]:
        async with self._uow:
            project = await self._uow.projects.get_owned(
                project_id=project_id,
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
            )
            if project is None:
                raise NotFoundError(
                    "project not found",
                    details={"project_id": str(project_id)},
                )
            return await self._uow.workflow_runs.list_by_project(project_id, status=status)
