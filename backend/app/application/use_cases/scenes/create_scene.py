"""``CreateScene`` use case (Slice α5c).

Contract (API_CONTRACT §4):

    POST /api/v1/projects/{project_id}/scenes
      body:  { title, duration_seconds, narration?, subtitle? }
      → 201  { data: ScenePublic (version=1, position=last), meta }
      → 404  { error: { code: NOT_FOUND, ... } }         (project missing / not yours)
      → 422  { error: { code: VALIDATION_FAILED, ... } }  (via DTO)
      → 401  { error: { code: UNAUTHENTICATED, ... } }    (via CurrentUserDep)

Flow:

1. **Project gate** (α5c D6) — ``projects.get_owned`` → ``None`` → 404. The
   caller must own a live project; ``owner_user_id`` / ``tenant_id`` come
   from ``CurrentUserDep``, never the body.
2. **Ensure default storyboard** (α5c D1/D9) — resolve or create the one
   implicit storyboard under a project-row lock. ``created=True`` emits
   ``storyboard.default_created`` (the only place this lifecycle event is
   observable, since the repository does not log).
3. **Append** (α5c D10) — ``scenes.add`` places the scene at ``max + 1000``
   under the held lock (deterministic; repositioning is ``move``'s job).
4. Compute the dense display ``position`` and commit.
"""

from __future__ import annotations

from uuid import UUID

import structlog

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.application.use_cases.scenes.results import SceneResult
from app.core.errors import NotFoundError

_LOGGER = structlog.get_logger(__name__)


class CreateScene:
    """Append a scene to the caller's own project (auto-creating its storyboard)."""

    def __init__(self, uow: IUnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self,
        *,
        project_id: UUID,
        owner_user_id: UUID,
        tenant_id: UUID,
        title: str,
        duration_seconds: float,
        narration: str | None = None,
        subtitle: str | None = None,
        ip: str | None = None,
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

            storyboard_id, created = await self._uow.scenes.ensure_default_storyboard(project_id)
            if created:
                _LOGGER.info(
                    "storyboard.default_created",
                    project_id=str(project_id),
                    storyboard_id=str(storyboard_id),
                    owner_user_id=str(owner_user_id),
                    ip=ip,
                )

            scene = await self._uow.scenes.add(
                storyboard_id=storyboard_id,
                title=title,
                duration_seconds=duration_seconds,
                narration=narration,
                subtitle=subtitle,
            )
            position = await self._uow.scenes.position_of(
                storyboard_id=storyboard_id, scene_number=scene.scene_number
            )
            await self._uow.commit()

        _LOGGER.info(
            "scene.created",
            scene_id=str(scene.id),
            project_id=str(project_id),
            owner_user_id=str(owner_user_id),
            position=position,
            ip=ip,
        )
        return SceneResult(scene=scene, position=position)
