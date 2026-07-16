"""``GetRenderJob`` use case (Slice α7.1).

Contract (API_CONTRACT §3.2.5):

    GET /api/v1/projects/{project_id}/render-jobs/{render_job_id}
      → 200  { data: RenderJobPublic, meta }
      → 404  { error: { code: NOT_FOUND, ... } }        (project/job missing / not yours)
      → 401  { error: { code: UNAUTHENTICATED, ... } }  (via CurrentUserDep)

Project ownership gate (404) then a project-scoped single read; a job under
another user's project — or an unknown id — is the same uniform ``404``
(anti-enumeration, α7.1 D3.3).
"""

from __future__ import annotations

from uuid import UUID

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.core.errors import NotFoundError
from app.domain.render.render_job import RenderJob


class GetRenderJob:
    """Fetch one render job owned (via project) by the caller."""

    def __init__(self, uow: IUnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self,
        *,
        project_id: UUID,
        render_job_id: UUID,
        owner_user_id: UUID,
        tenant_id: UUID,
    ) -> RenderJob:
        async with self._uow:
            project = await self._uow.projects.get_owned(
                project_id=project_id,
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
            )
            if project is None:
                raise NotFoundError(
                    "render job not found",
                    details={"render_job_id": str(render_job_id)},
                )

            job = await self._uow.render_jobs.get_owned(project_id, render_job_id)
            if job is None:
                raise NotFoundError(
                    "render job not found",
                    details={"render_job_id": str(render_job_id)},
                )
            return job
