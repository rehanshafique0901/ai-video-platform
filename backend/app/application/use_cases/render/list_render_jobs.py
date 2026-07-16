"""``ListRenderJobs`` use case (Slice α7.1).

Contract (API_CONTRACT §3.2.5):

    GET /api/v1/projects/{project_id}/render-jobs?status=
      → 200  { data: [RenderJobPublic], meta }
      → 404  { error: { code: NOT_FOUND, ... } }        (project missing / not yours)
      → 401  { error: { code: UNAUTHENTICATED, ... } }  (via CurrentUserDep)

Project-scoped listing, newest first, optional ``status`` filter. Ownership is
established via the project gate (404) before the project-scoped read; an empty
project → ``200 []``. Not paginated in α7.1 (a project's render history is
bounded operational history — pagination is a later concern if needed).
"""

from __future__ import annotations

from uuid import UUID

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.core.errors import NotFoundError
from app.domain.render.render_job import RenderJob


class ListRenderJobs:
    """List the caller's project render jobs, newest-first, optionally by status."""

    def __init__(self, uow: IUnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self,
        *,
        project_id: UUID,
        owner_user_id: UUID,
        tenant_id: UUID,
        status: str | None = None,
    ) -> list[RenderJob]:
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
            return await self._uow.render_jobs.list_by_project(project_id, status=status)
