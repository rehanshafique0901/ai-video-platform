"""``UpdateScene`` use case (Slice α5c).

Contract (API_CONTRACT §4):

    PATCH /api/v1/projects/{project_id}/scenes/{scene_id}
      body:  { version, title?, duration_seconds?, narration?, subtitle? }
      → 200  { data: ScenePublic (version incremented on real change), meta }
      → 404  { error: { code: NOT_FOUND, ... } }         (project/scene missing / not yours)
      → 412  { error: { code: VERSION_CONFLICT, ... } }  (stale version)
      → 422  { error: { code: VALIDATION_FAILED, ... } } (via DTO)
      → 401  { error: { code: UNAUTHENTICATED, ... } }   (via CurrentUserDep)

Identical control flow to α5b ``UpdateProject`` (404-before-412, α5c D6/D7):

1. Project gate → 404. 2. Scene visibility → 404. 3. Version fence → 412.
4. Same-value no-op → 200, no write. 5. Real change → version-fenced CAS; a
``None`` return means a concurrent bump/delete after step 2 → 412.

Only **content** fields are patchable here (``title`` /
``duration_seconds`` / ``narration`` / ``subtitle``). Reordering is a
separate domain action (``POST …/move``, α5c Q1/D11) and never appears in
this ``changes`` mapping — ``scene_number`` is not a wire field.
``changes`` is the tri-state mapping built by the router from
``model_fields_set``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

import structlog

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.application.use_cases.scenes.results import SceneResult
from app.core.errors import NotFoundError, VersionConflictError

_LOGGER = structlog.get_logger(__name__)


class UpdateScene:
    """Version-fenced partial update of a scene under the caller's project."""

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
        changes: Mapping[str, Any],
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
                    "scene.update_rejected",
                    reason="version_mismatch",
                    scene_id=str(scene_id),
                    project_id=str(project_id),
                    owner_user_id=str(owner_user_id),
                    expected_version=expected_version,
                    ip=ip,
                )
                raise VersionConflictError("Resource has been modified.")

            effective = {k: v for k, v in changes.items() if getattr(scene, k) != v}
            if not effective:
                _LOGGER.info(
                    "scene.update_rejected",
                    reason="same_value_noop",
                    scene_id=str(scene.id),
                    project_id=str(project_id),
                    owner_user_id=str(owner_user_id),
                    ip=ip,
                )
                position = await self._uow.scenes.position_of(
                    storyboard_id=scene.storyboard_id, scene_number=scene.scene_number
                )
                return SceneResult(scene=scene, position=position)

            updated = await self._uow.scenes.update_owned(
                project_id=project_id,
                scene_id=scene_id,
                expected_version=expected_version,
                changes=effective,
            )
            if updated is None:
                _LOGGER.warning(
                    "scene.update_rejected",
                    reason="version_mismatch",
                    scene_id=str(scene_id),
                    project_id=str(project_id),
                    owner_user_id=str(owner_user_id),
                    expected_version=expected_version,
                    ip=ip,
                )
                raise VersionConflictError("Resource has been modified.")

            position = await self._uow.scenes.position_of(
                storyboard_id=updated.storyboard_id, scene_number=updated.scene_number
            )
            await self._uow.commit()

        _LOGGER.info(
            "scene.updated",
            scene_id=str(updated.id),
            project_id=str(project_id),
            owner_user_id=str(owner_user_id),
            changed_fields=sorted(effective.keys()),
            previous_version=expected_version,
            new_version=updated.version,
            ip=ip,
        )
        return SceneResult(scene=updated, position=position)
