"""``GetPublishJob`` use case (Slice α8.6b).

Owner-scoped read of one publish job (mirrors ``GetExportJob``). A missing job — or one
belonging to another principal — yields a uniform ``NotFoundError`` (→ 404, anti-enumeration).
"""

from __future__ import annotations

from uuid import UUID

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.core.errors import NotFoundError
from app.domain.publishing.publish_job import PublishJob


class GetPublishJob:
    """Fetch one of the caller's publish jobs."""

    def __init__(self, uow: IUnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self, *, tenant_id: UUID, owner_user_id: UUID, publish_job_id: UUID
    ) -> PublishJob:
        async with self._uow:
            job = await self._uow.publish_jobs.get_owned(
                tenant_id=tenant_id, owner_user_id=owner_user_id, publish_job_id=publish_job_id
            )
        if job is None:
            raise NotFoundError(
                "publish job not found", details={"publish_job_id": str(publish_job_id)}
            )
        return job
