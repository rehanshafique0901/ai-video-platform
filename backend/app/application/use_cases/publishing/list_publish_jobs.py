"""``ListPublishJobs`` use case (Slice α8.6b).

Owner-scoped list of the caller's publish jobs, newest first (mirrors the owner-facing read
discipline of ``ListSocialAccounts``).
"""

from __future__ import annotations

from uuid import UUID

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.domain.publishing.publish_job import PublishJob


class ListPublishJobs:
    """List the caller's publish jobs (newest first)."""

    def __init__(self, uow: IUnitOfWork) -> None:
        self._uow = uow

    async def execute(self, *, tenant_id: UUID, owner_user_id: UUID) -> list[PublishJob]:
        async with self._uow:
            return await self._uow.publish_jobs.list_for_owner(
                tenant_id=tenant_id, owner_user_id=owner_user_id
            )
