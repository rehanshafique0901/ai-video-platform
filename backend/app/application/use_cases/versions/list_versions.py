"""``ListProjectVersions`` use case (Slice α5d.1).

Contract (API_CONTRACT §3.3):

    GET /api/v1/projects/{project_id}/versions
      → 200  { data: [ProjectVersionPublic, ...] (newest first), meta }
      → 404  { error: { code: NOT_FOUND, ... } }         (project missing / not yours)
      → 401  { error: { code: UNAUTHENTICATED, ... } }   (via CurrentUserDep)

Returns the caller's project's version history as **metadata only** (no
snapshot bodies — α5d Q4), ordered newest-first by ``version_number``. Not
paginated (α5d.1): a project's version count is bounded editorial history.
Read-only: the project ownership gate runs first (404 if not the caller's).
"""

from __future__ import annotations

from uuid import UUID

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.application.use_cases.versions.results import VersionListResult
from app.core.errors import NotFoundError


class ListProjectVersions:
    """List the caller's project's version history (metadata, newest first)."""

    def __init__(self, uow: IUnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self,
        *,
        project_id: UUID,
        owner_user_id: UUID,
        tenant_id: UUID,
    ) -> VersionListResult:
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
            versions = await self._uow.versions.list_by_project(project_id)
            return VersionListResult(
                versions=versions,
                current_version_id=project.current_version_id,
            )
