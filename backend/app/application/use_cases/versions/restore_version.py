"""``RestoreProjectVersion`` use case (Slice α5d.2).

Contract (API_CONTRACT §3.3):

    POST /api/v1/projects/{project_id}/versions/{version_id}/restore
      body:  { version }                                   (aggregate OCC token)
      → 200  { data: ProjectVersionDetail (new reason=restore head), meta }
      → 404  { error: { code: NOT_FOUND, ... } }         (project/version missing / not yours)
      → 412  { error: { code: VERSION_CONFLICT, ... } }  (stale aggregate version)
      → 422  { error: { code: VALIDATION_FAILED, ... } } (via DTO)
      → 401  { error: { code: UNAUTHENTICATED, ... } }   (via CurrentUserDep)

Restore makes a historical snapshot the project's live content again **without
rewriting history** (ADR-0035 D2): it appends a new ``reason=restore`` version
and repoints ``current_version_id``. Flow (pre-flight §3, 404-before-412):

1. **Project gate** — ``projects.get_owned`` → ``None`` → 404.
2. **Source version gate** — ``versions.get_owned(project_id, version_id)`` →
   ``None`` → 404 (anti-enumeration; runs *before* the fence, so a caller can
   never learn a version exists via a 412).
3. **Restore** — ``versions.restore`` locks the project, fences on the
   **aggregate** ``projects.version`` (§4 Aggregate OCC Rule), rewrites the
   root + reconciles scenes by id, and captures the trailing ``reason=restore``
   version — all in one transaction. ``None`` means the fence failed (stale
   aggregate token) → 412.
4. Commit and return the new head (with its snapshot).
"""

from __future__ import annotations

from uuid import UUID

import structlog

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.application.use_cases.versions.results import VersionResult
from app.core.errors import NotFoundError, VersionConflictError

_LOGGER = structlog.get_logger(__name__)


class RestoreProjectVersion:
    """Restore a historical snapshot into the caller's own project's live state."""

    def __init__(self, uow: IUnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self,
        *,
        project_id: UUID,
        version_id: UUID,
        owner_user_id: UUID,
        tenant_id: UUID,
        expected_version: int,
        ip: str | None = None,
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

            # Source-version visibility gate BEFORE the fence (404-before-412):
            # a missing/other-project version is a uniform 404.
            source = await self._uow.versions.get_owned(project_id, version_id)
            if source is None:
                raise NotFoundError(
                    "version not found",
                    details={"version_id": str(version_id)},
                )

            restored = await self._uow.versions.restore(
                project_id=project_id,
                source_version_id=version_id,
                restored_by_user_id=owner_user_id,
                expected_project_version=expected_version,
            )
            if restored is None:
                _LOGGER.warning(
                    "project_version.restore_rejected",
                    reason="version_mismatch",
                    project_id=str(project_id),
                    source_version_id=str(version_id),
                    owner_user_id=str(owner_user_id),
                    expected_version=expected_version,
                    ip=ip,
                )
                raise VersionConflictError("Resource has been modified.")

            await self._uow.commit()

        _LOGGER.info(
            "project_version.restored",
            project_id=str(project_id),
            source_version_id=str(version_id),
            new_version_id=str(restored.id),
            new_version_number=restored.version_number,
            scene_count=len(restored.snapshot.get("scenes", [])),
            owner_user_id=str(owner_user_id),
            ip=ip,
        )
        # The restore version is, by construction, the project's new current
        # version (restore repointed current_version_id → it).
        return VersionResult(version=restored, current_version_id=restored.id)
