"""``CreateProjectVersion`` use case (Slice α5d.1).

Contract (API_CONTRACT §3.3):

    POST /api/v1/projects/{project_id}/versions
      → 201  { data: ProjectVersionDetail (version_number = next), meta }
      → 404  { error: { code: NOT_FOUND, ... } }         (project missing / not yours)
      → 401  { error: { code: UNAUTHENTICATED, ... } }   (via CurrentUserDep)

Flow:

1. **Project gate** (mirrors α5c D6) — ``projects.get_owned`` → ``None`` → 404.
   ``owner_user_id`` / ``tenant_id`` come from ``CurrentUserDep``, never the
   body; a client cannot snapshot another owner's project.
2. **Capture** — ``versions.create_snapshot`` assembles the immutable snapshot
   under a project-row lock, assigns the next ``version_number``, links the
   lineage parent, and advances ``projects.current_version_id`` (α5d Q6). The
   ``reason`` is server-set to ``manual_save`` (α5d Q5 — the other
   ``version_reason`` values are later-slice concerns).
3. Commit and return the new version (with its snapshot).
"""

from __future__ import annotations

from uuid import UUID

import structlog

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.application.use_cases.versions.results import VersionResult
from app.core.errors import NotFoundError

_LOGGER = structlog.get_logger(__name__)

# α5d.1 captures are always operator-initiated manual saves. Later slices set
# other ``version_reason`` values (autosave / restore / branch / generated).
_REASON_MANUAL_SAVE = "manual_save"


class CreateProjectVersion:
    """Capture an immutable snapshot of the caller's own project."""

    def __init__(self, uow: IUnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self,
        *,
        project_id: UUID,
        owner_user_id: UUID,
        tenant_id: UUID,
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

            version = await self._uow.versions.create_snapshot(
                project_id=project_id,
                created_by_user_id=owner_user_id,
                reason=_REASON_MANUAL_SAVE,
            )
            await self._uow.commit()

        _LOGGER.info(
            "project_version.created",
            project_id=str(project_id),
            version_id=str(version.id),
            version_number=version.version_number,
            parent_version_id=(
                str(version.parent_version_id) if version.parent_version_id is not None else None
            ),
            scene_count=len(version.snapshot.get("scenes", [])),
            owner_user_id=str(owner_user_id),
            ip=ip,
        )
        # The just-captured version is, by construction, the project's new
        # current version (create_snapshot repointed current_version_id → it).
        return VersionResult(version=version, current_version_id=version.id)
