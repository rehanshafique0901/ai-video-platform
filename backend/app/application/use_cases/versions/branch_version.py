"""``BranchProjectVersion`` use case (Slice α5d.3).

Contract (API_CONTRACT §3.3):

    POST /api/v1/projects/{project_id}/versions/{version_id}/branch
      body:  { name }                                    (new project name)
      → 201  { data: ProjectPublic (the NEW project), meta.branched_from }
      → 404  { error: { code: NOT_FOUND, ... } }         (source project/version missing / not yours)
      → 409  { error: { code: CONFLICT, ... } }          (duplicate live project name for this owner)
      → 422  { error: { code: VALIDATION_FAILED, ... } } (via DTO)
      → 401  { error: { code: UNAUTHENTICATED, ... } }   (via CurrentUserDep)

Branch **forks** a historical snapshot into a brand-new, independently-editable
project (α5d.3 pre-flight Q1 Option A — "fork to a new project"). Unlike restore
(α5d.2), which rewinds *this* project onto an old snapshot, branch leaves the
source project untouched and creates a *new* aggregate seeded from the chosen
version's content. Flow (pre-flight §3, 404-before-anything):

1. **Source project gate** — ``projects.get_owned`` → ``None`` → 404.
2. **Source version gate** — ``versions.get_owned(project_id, version_id)`` →
   ``None`` → 404 (anti-enumeration; a missing/other-project version is a
   uniform 404, never a leak).
3. **Branch** — ``versions.branch`` creates the new project row (copying the
   mutable root from the source snapshot, owned by the caller), materializes
   the snapshot scenes with fresh ids, captures the new project's
   ``reason=branch`` v1 (with a ``branched_from`` provenance block + NULL
   parent), and advances the new project's current pointer — all in one
   transaction. A live-name collision raises ``ConflictError`` → 409.
4. Commit and return the new project + provenance breadcrumb.

No OCC fence: branch does not mutate the source (its snapshot is immutable), so
there is nothing to fence (pre-flight §4). The source project's ``version`` is
deliberately NOT bumped (Q8).
"""

from __future__ import annotations

from uuid import UUID

import structlog

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.application.use_cases.versions.results import VersionBranchResult
from app.core.errors import ConflictError, NotFoundError

_LOGGER = structlog.get_logger(__name__)


class BranchProjectVersion:
    """Fork a historical snapshot into a new independent project the caller owns."""

    def __init__(self, uow: IUnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self,
        *,
        project_id: UUID,
        version_id: UUID,
        owner_user_id: UUID,
        tenant_id: UUID,
        name: str,
        ip: str | None = None,
    ) -> VersionBranchResult:
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

            source = await self._uow.versions.get_owned(project_id, version_id)
            if source is None:
                raise NotFoundError(
                    "version not found",
                    details={"version_id": str(version_id)},
                )

            try:
                new_project, new_version = await self._uow.versions.branch(
                    source_project_id=project_id,
                    source_version_id=version_id,
                    source_version_number=source.version_number,
                    source_snapshot=source.snapshot,
                    new_project_name=name,
                    tenant_id=tenant_id,
                    owner_user_id=owner_user_id,
                )
            except ConflictError:
                # Duplicate live project name for this owner. Surface as-is
                # (handler → 409); ``name`` is user content, not logged.
                _LOGGER.warning(
                    "project_version.branch_rejected",
                    reason="duplicate_name",
                    source_project_id=str(project_id),
                    source_version_id=str(version_id),
                    owner_user_id=str(owner_user_id),
                    ip=ip,
                )
                raise
            await self._uow.commit()

        _LOGGER.info(
            "project_version.branched",
            source_project_id=str(project_id),
            source_version_id=str(version_id),
            source_version_number=source.version_number,
            new_project_id=str(new_project.id),
            new_version_id=str(new_version.id),
            scene_count=len(new_version.snapshot.get("scenes", [])),
            owner_user_id=str(owner_user_id),
            ip=ip,
        )
        return VersionBranchResult(
            project=new_project,
            source_project_id=project_id,
            source_version_id=version_id,
            source_version_number=source.version_number,
        )
