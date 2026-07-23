"""``GetExportJob`` use case (Slice α8.5a).

Fetch one export job of the caller's project + render job. Ownership is layered: the project
gate first (the user must own the project), then the export must belong to that project's
render job and to the addressed ``render_job_id``. A missing / foreign export yields a
uniform ``404`` (anti-enumeration, mirror of α7.1 D3.3). Read-only.
"""

from __future__ import annotations

from uuid import UUID

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.core.errors import NotFoundError
from app.domain.export.export_job import ExportJob


class GetExportJob:
    """Return one export job of the caller's project (uniform 404 when not visible)."""

    def __init__(self, uow: IUnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self,
        *,
        project_id: UUID,
        render_job_id: UUID,
        export_job_id: UUID,
        owner_user_id: UUID,
        tenant_id: UUID,
    ) -> ExportJob:
        async with self._uow:
            project = await self._uow.projects.get_owned(
                project_id=project_id,
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
            )
            if project is None:
                raise NotFoundError(
                    "export job not found", details={"export_job_id": str(export_job_id)}
                )

            job = await self._uow.export_jobs.get_owned(project_id, export_job_id)
            if job is None or job.render_job_id != render_job_id:
                raise NotFoundError(
                    "export job not found", details={"export_job_id": str(export_job_id)}
                )
            return job
