"""``ListScenes`` use case (Slice α5c).

Contract (API_CONTRACT §4):

    GET /api/v1/projects/{project_id}/scenes
      → 200  { data: [ScenePublic, ...] (ordered by position), meta }
      → 404  { error: { code: NOT_FOUND, ... } }        (project missing / not yours)
      → 401  { error: { code: UNAUTHENTICATED, ... } }  (via CurrentUserDep)

Returns the caller's project's live scenes ordered by ``scene_number`` ASC
(the sparse ordering key). Not paginated (α5c Q2): a project's scene set is
a bounded editorial list. Read-only — if the project has no storyboard yet,
the result is an empty list and no storyboard is created (α5c D8). The
router derives each scene's dense 1-based ``position`` by enumerating this
already-sorted list, so no per-scene position query is needed here.
"""

from __future__ import annotations

from uuid import UUID

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.core.errors import NotFoundError
from app.domain.scenes.scene import Scene


class ListScenes:
    """List the caller's project's scenes, ordered, un-paginated."""

    def __init__(self, uow: IUnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self,
        *,
        project_id: UUID,
        owner_user_id: UUID,
        tenant_id: UUID,
    ) -> list[Scene]:
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
            return await self._uow.scenes.list_by_project(project_id)
