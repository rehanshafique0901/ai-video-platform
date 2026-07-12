"""``GetProjectVersion`` use case (Slice α5d.1).

Contract (API_CONTRACT §3.3):

    GET /api/v1/projects/{project_id}/versions/{version_id}
      → 200  { data: ProjectVersionDetail (with snapshot), meta }
      → 404  { error: { code: NOT_FOUND, ... } }         (project or version missing / not yours)
      → 401  { error: { code: UNAUTHENTICATED, ... } }   (via CurrentUserDep)

Two-level visibility gate (mirrors α5c D6): the project must be owned by the
caller (``projects.get_owned`` → 404), then the version must belong to that
project (``versions.get_owned`` → 404, addressed by UUID ``id`` — α5d Q3).
Both 404s are indistinguishable from "never existed" (anti-enumeration).
Returns the FULL version including its immutable ``snapshot`` blob.
"""

from __future__ import annotations

from uuid import UUID

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.application.use_cases.versions.results import VersionResult
from app.core.errors import NotFoundError


class GetProjectVersion:
    """Fetch one version (with its snapshot) under the caller's own project."""

    def __init__(self, uow: IUnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self,
        *,
        project_id: UUID,
        version_id: UUID,
        owner_user_id: UUID,
        tenant_id: UUID,
    ) -> VersionResult:
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
            version = await self._uow.versions.get_owned(project_id, version_id)
            if version is None:
                raise NotFoundError(
                    "version not found",
                    details={"version_id": str(version_id)},
                )
            return VersionResult(version=version, current_version_id=project.current_version_id)
