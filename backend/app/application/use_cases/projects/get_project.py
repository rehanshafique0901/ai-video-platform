"""``GetProject`` use case (Slice α5a).

Contract (API_CONTRACT §3.3):

    GET /api/v1/projects/{project_id}
      → 200  { data: ProjectPublic, meta }
      → 404  { error: { code: NOT_FOUND, ... } }        (missing / not yours)
      → 401  { error: { code: UNAUTHENTICATED, ... } }  (via CurrentUserDep)

The lookup is owner-and-tenant scoped (α5a D5): a project that exists
but belongs to another owner/tenant returns the SAME ``404 NOT_FOUND``
as one that never existed. The repository collapses "absent",
"soft-deleted", and "not yours" into a single ``None`` so this use case
cannot leak cross-owner existence — the anti-enumeration posture from
α3 carried onto the resource surface.
"""

from __future__ import annotations

from uuid import UUID

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.core.errors import NotFoundError
from app.domain.projects.project import Project


class GetProject:
    """Fetch one project owned by the authenticated caller."""

    def __init__(self, uow: IUnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self,
        *,
        project_id: UUID,
        owner_user_id: UUID,
        tenant_id: UUID,
    ) -> Project:
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
        return project
