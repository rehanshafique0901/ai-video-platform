"""``DeleteScene`` use case (Slice α5c).

Contract (API_CONTRACT §4):

    DELETE /api/v1/projects/{project_id}/scenes/{scene_id}
      → 204  (no body)                                   (soft-deleted)
      → 404  { error: { code: NOT_FOUND, ... } }         (project/scene missing / not yours / already deleted)
      → 401  { error: { code: UNAUTHENTICATED, ... } }   (via CurrentUserDep)

Project-scoped **soft** delete (α5c D13, mirroring α5b ``DeleteProject``):
after the project ownership gate (→ 404), ``scenes.soft_delete_owned`` sets
``deleted_at`` on the caller's live scene and reports whether a live scene
was marked. ``False`` (missing, another project's scene, or already
soft-deleted) → 404, so a repeat delete — and any GET/PATCH/move after
delete — is a uniform 404 (idempotent-by-404). No version fence (α5c D13);
soft (not hard) delete preserves auditability and leaves a gap in
``scene_number`` (position is recomputed dynamically).
"""

from __future__ import annotations

from uuid import UUID

import structlog

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.core.errors import NotFoundError

_LOGGER = structlog.get_logger(__name__)


class DeleteScene:
    """Soft-delete a scene under the caller's own project; 404 otherwise."""

    def __init__(self, uow: IUnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self,
        *,
        project_id: UUID,
        scene_id: UUID,
        owner_user_id: UUID,
        tenant_id: UUID,
        ip: str | None = None,
    ) -> None:
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

            deleted = await self._uow.scenes.soft_delete_owned(project_id, scene_id)
            if not deleted:
                _LOGGER.warning(
                    "scene.delete_rejected",
                    reason="not_visible",
                    scene_id=str(scene_id),
                    project_id=str(project_id),
                    owner_user_id=str(owner_user_id),
                    ip=ip,
                )
                raise NotFoundError(
                    "scene not found",
                    details={"scene_id": str(scene_id)},
                )
            await self._uow.commit()

        _LOGGER.info(
            "scene.deleted",
            scene_id=str(scene_id),
            project_id=str(project_id),
            owner_user_id=str(owner_user_id),
            ip=ip,
        )
