"""``DeleteTrack`` use case (Slice α6.3a).

Contract (API_CONTRACT §3.2.4):

    DELETE /api/v1/projects/{project_id}/timeline/tracks/{track_id}?version=<n>
      → 204  (no body)                                  (soft-deleted; aggregate bumped)
      → 404  { error: { code: NOT_FOUND, ... } }        (project/timeline/track missing / not yours / already deleted)
      → 412  { error: { code: VERSION_CONFLICT, ... } } (stale timeline version)
      → 401  { error: { code: UNAUTHENTICATED, ... } }  (via CurrentUserDep)

Deleting a track changes the aggregate, so it is fenced on — and advances —
``timelines.version`` (ADR-0038 / Q13); the expected version is **required** (a
``version`` query parameter). Control flow is **404-before-412**: a missing /
already-deleted track is a uniform ``404`` (idempotent-by-404) *before* the fence
is consulted, so a repeat delete is ``404`` (not ``412``). Only a live track with
a stale token yields ``412``. Soft delete frees the ``z_index`` slot. Does **not**
touch ``projects.version``.
"""

from __future__ import annotations

from uuid import UUID

import structlog

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.core.errors import NotFoundError, VersionConflictError

_LOGGER = structlog.get_logger(__name__)


class DeleteTrack:
    """Version-fenced (on the timeline) soft-delete of a track; 404 otherwise."""

    def __init__(self, uow: IUnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self,
        *,
        project_id: UUID,
        track_id: UUID,
        owner_user_id: UUID,
        tenant_id: UUID,
        expected_version: int,
        ip: str | None = None,
    ) -> int:
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

            timeline = await self._uow.timeline.get_by_project(project_id)
            if timeline is None:
                raise NotFoundError(
                    "timeline not found",
                    details={"project_id": str(project_id)},
                )

            # 404-before-412: an absent/already-deleted track is a 404 regardless
            # of the token (idempotent-by-404), decided before the fence.
            track = await self._uow.timeline.get_track(timeline.id, track_id)
            if track is None:
                raise NotFoundError(
                    "track not found",
                    details={"track_id": str(track_id)},
                )

            if timeline.version != expected_version:
                _LOGGER.warning(
                    "track.delete_rejected",
                    reason="version_mismatch",
                    project_id=str(project_id),
                    timeline_id=str(timeline.id),
                    track_id=str(track_id),
                    expected_version=expected_version,
                    ip=ip,
                )
                raise VersionConflictError("Resource has been modified.")

            deleted = await self._uow.timeline.soft_delete_track(timeline.id, track_id)
            if not deleted:
                # Lost a race to a concurrent delete after the visibility gate.
                raise NotFoundError(
                    "track not found",
                    details={"track_id": str(track_id)},
                )

            new_version = await self._uow.timeline.bump_version(project_id, expected_version)
            if new_version is None:
                _LOGGER.warning(
                    "track.delete_rejected",
                    reason="version_mismatch",
                    project_id=str(project_id),
                    timeline_id=str(timeline.id),
                    track_id=str(track_id),
                    expected_version=expected_version,
                    ip=ip,
                )
                raise VersionConflictError("Resource has been modified.")
            await self._uow.commit()

        _LOGGER.info(
            "track.deleted",
            track_id=str(track_id),
            timeline_id=str(timeline.id),
            project_id=str(project_id),
            previous_version=expected_version,
            new_version=new_version,
            owner_user_id=str(owner_user_id),
            ip=ip,
        )
        return new_version
