"""``GetScene`` use case (Slice α5c).

Contract (API_CONTRACT §4):

    GET /api/v1/projects/{project_id}/scenes/{scene_id}
      → 200  { data: ScenePublic, meta }
      → 404  { error: { code: NOT_FOUND, ... } }        (project or scene missing / not yours)
      → 401  { error: { code: UNAUTHENTICATED, ... } }  (via CurrentUserDep)

Two-level visibility gate (α5c D6): the project must be owned by the caller
(``projects.get_owned`` → 404), then the scene must live under that
project's storyboard (``scenes.get_owned_scene`` → 404). Both 404s are
indistinguishable from "never existed" (anti-enumeration).
"""

from __future__ import annotations

from uuid import UUID

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.application.use_cases.scenes.results import SceneResult
from app.core.errors import NotFoundError


class GetScene:
    """Fetch one scene under the caller's own project."""

    def __init__(self, uow: IUnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self,
        *,
        project_id: UUID,
        scene_id: UUID,
        owner_user_id: UUID,
        tenant_id: UUID,
    ) -> SceneResult:
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
            scene = await self._uow.scenes.get_owned_scene(project_id, scene_id)
            if scene is None:
                raise NotFoundError(
                    "scene not found",
                    details={"scene_id": str(scene_id)},
                )
            position = await self._uow.scenes.position_of(
                storyboard_id=scene.storyboard_id, scene_number=scene.scene_number
            )
        return SceneResult(scene=scene, position=position)
