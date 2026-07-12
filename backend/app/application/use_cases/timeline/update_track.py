"""``UpdateTrack`` use case (Slice α6.3a).

Contract (API_CONTRACT §3.2.4):

    PATCH /api/v1/projects/{project_id}/timeline/tracks/{track_id}
      body:  { version, kind?, z_index?, name?, locked?, muted? }
      → 200  { data: TrackPublic, meta: { timeline_version } }
      → 404  { error: { code: NOT_FOUND, ... } }        (project/timeline/track missing / not yours)
      → 409  { error: { code: CONFLICT, ... } }          (z_index already used)
      → 412  { error: { code: VERSION_CONFLICT, ... } } (stale timeline version)
      → 422  { error: { code: VALIDATION_FAILED, ... } }(via DTO)
      → 401  { error: { code: UNAUTHENTICATED, ... } }  (via CurrentUserDep)

The fence is the **timeline's** version (ADR-0038 / Q13 — tracks have no version
of their own): ``version`` is **required** in the body. Control flow is
404-before-412 (project → timeline → track visibility, then the aggregate fence).
A same-value patch is a ``200`` no-op (no write, no bump). A real change updates
the track and advances ``timelines.version`` once. A ``z_index`` collision with
another live track is a ``409`` (Q5). Does **not** touch ``projects.version``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

import structlog

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.application.use_cases.timeline.results import TrackResult
from app.core.errors import NotFoundError, VersionConflictError

_LOGGER = structlog.get_logger(__name__)


class UpdateTrack:
    """Version-fenced (on the timeline) partial update of a track."""

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
        changes: Mapping[str, Any],
        ip: str | None = None,
    ) -> TrackResult:
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

            track = await self._uow.timeline.get_track(timeline.id, track_id)
            if track is None:
                raise NotFoundError(
                    "track not found",
                    details={"track_id": str(track_id)},
                )

            # Aggregate fence (404-before-412): the token is the timeline's.
            if timeline.version != expected_version:
                _LOGGER.warning(
                    "track.update_rejected",
                    reason="version_mismatch",
                    project_id=str(project_id),
                    timeline_id=str(timeline.id),
                    track_id=str(track_id),
                    expected_version=expected_version,
                    ip=ip,
                )
                raise VersionConflictError("Resource has been modified.")

            effective = {k: v for k, v in changes.items() if getattr(track, k) != v}
            if not effective:
                _LOGGER.info(
                    "track.update_rejected",
                    reason="same_value_noop",
                    project_id=str(project_id),
                    timeline_id=str(timeline.id),
                    track_id=str(track_id),
                    ip=ip,
                )
                return TrackResult(track=track, timeline_version=timeline.version)

            updated = await self._uow.timeline.update_track(timeline.id, track_id, effective)
            if updated is None:
                # No track-level fence: a None here means the row was soft-deleted
                # between the visibility gate and the write → uniform 404.
                raise NotFoundError(
                    "track not found",
                    details={"track_id": str(track_id)},
                )

            new_version = await self._uow.timeline.bump_version(project_id, expected_version)
            if new_version is None:
                _LOGGER.warning(
                    "track.update_rejected",
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
            "track.updated",
            track_id=str(updated.id),
            timeline_id=str(timeline.id),
            project_id=str(project_id),
            changed_fields=sorted(effective.keys()),
            previous_version=expected_version,
            new_version=new_version,
            owner_user_id=str(owner_user_id),
            ip=ip,
        )
        return TrackResult(track=updated, timeline_version=new_version)
