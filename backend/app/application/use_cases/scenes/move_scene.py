"""``MoveScene`` use case (Slice α5c).

Contract (API_CONTRACT §4):

    POST /api/v1/projects/{project_id}/scenes/{scene_id}/move
      body:  { version, position }
      → 200  { data: ScenePublic (at new position, version incremented), meta }
      → 404  { error: { code: NOT_FOUND, ... } }         (project/scene missing / not yours)
      → 412  { error: { code: VERSION_CONFLICT, ... } }  (stale version / concurrent bump)
      → 422  { error: { code: VALIDATION_FAILED, ... } } (via DTO)
      → 401  { error: { code: UNAUTHENTICATED, ... } }   (via CurrentUserDep)

Reordering is a **dedicated domain action**, not a content ``PATCH`` (α5c
Q1) — it changes the scene's ordering key, so it earns its own endpoint and
its own version fence (α5c Q4/D12). Flow mirrors the 404-before-412 split:

1. Project gate → 404. 2. Scene visibility → 404. 3. Version fence → 412.
4. ``reorder_owned`` (project-row-locked gap/rebalance, α5c D9/D12) →
   ``None`` means a concurrent content-PATCH bumped the moved scene, or a
   concurrent delete, after step 2 → 412.

``position`` is 1-based and clamped into range by the repository; a move to
the scene's current slot is a no-op (200, version unchanged).
"""

from __future__ import annotations

from uuid import UUID

import structlog

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.application.use_cases.scenes.results import SceneResult
from app.core.errors import NotFoundError, VersionConflictError

_LOGGER = structlog.get_logger(__name__)


class MoveScene:
    """Version-fenced reorder of a scene within the caller's project."""

    def __init__(self, uow: IUnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self,
        *,
        project_id: UUID,
        scene_id: UUID,
        owner_user_id: UUID,
        tenant_id: UUID,
        expected_version: int,
        position: int,
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

            scene = await self._uow.scenes.get_owned_scene(project_id, scene_id)
            if scene is None:
                raise NotFoundError(
                    "scene not found",
                    details={"scene_id": str(scene_id)},
                )

            if scene.version != expected_version:
                _LOGGER.warning(
                    "scene.reorder_rejected",
                    reason="version_mismatch",
                    scene_id=str(scene_id),
                    project_id=str(project_id),
                    owner_user_id=str(owner_user_id),
                    expected_version=expected_version,
                    ip=ip,
                )
                raise VersionConflictError("Resource has been modified.")

            moved = await self._uow.scenes.reorder_owned(
                project_id=project_id,
                scene_id=scene_id,
                target_position=position,
                expected_version=expected_version,
            )
            if moved is None:
                _LOGGER.warning(
                    "scene.reorder_rejected",
                    reason="version_mismatch",
                    scene_id=str(scene_id),
                    project_id=str(project_id),
                    owner_user_id=str(owner_user_id),
                    expected_version=expected_version,
                    ip=ip,
                )
                raise VersionConflictError("Resource has been modified.")

            new_position = await self._uow.scenes.position_of(
                storyboard_id=moved.storyboard_id, scene_number=moved.scene_number
            )
            # Aggregate OCC Rule (α5d.2): a move to the scene's current slot is
            # a no-op (``reorder_owned`` returns the row with an unchanged
            # ``version``); only a real reorder advances the project-aggregate
            # OCC token.
            if moved.version != expected_version:
                await self._uow.projects.touch_version(
                    project_id=project_id, tenant_id=tenant_id, owner_user_id=owner_user_id
                )
            await self._uow.commit()

        _LOGGER.info(
            "scene.reordered",
            scene_id=str(moved.id),
            project_id=str(project_id),
            owner_user_id=str(owner_user_id),
            requested_position=position,
            new_position=new_position,
            ip=ip,
        )
        return SceneResult(scene=moved, position=new_position)
