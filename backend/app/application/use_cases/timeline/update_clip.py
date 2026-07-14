"""``UpdateClip`` use case (Slice α6.3b).

Contract (API_CONTRACT §3.2.4):

    PATCH /api/v1/projects/{project_id}/timeline/tracks/{track_id}/clips/{clip_id}
      body:  { version, media_asset_id?, start_seconds?, end_seconds?,
               source_start_seconds?, source_end_seconds?, volume?, locked? }
      → 200  { data: ClipPublic, meta: { timeline_version } }
      → 404  { error: { code: NOT_FOUND, ... } }        (project/timeline/track/clip missing / not yours)
      → 412  { error: { code: VERSION_CONFLICT, ... } } (stale timeline version)
      → 422  { error: { code: VALIDATION_FAILED, ... } }(bad time range / bad media_asset_id, via DTO + link check)
      → 401  { error: { code: UNAUTHENTICATED, ... } }  (via CurrentUserDep)

The fence is the **timeline's** version (ADR-0038 / Q13 — clips have no version of
their own): ``version`` is **required** in the body. Control flow mirrors
``UpdateTrack``: 404-before-412 (project → timeline → track → clip visibility,
then the aggregate fence). A same-value patch is a ``200`` no-op (no write, no
bump). ``track_id`` is immutable (no cross-track move — a move is delete +
recreate, Q4). When the (effective) change (re)links a non-null
``media_asset_id`` it is validated (owned + live → ``422``, D4). A real change
updates the clip and advances ``timelines.version`` once. Does **not** touch
``projects.version``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

import structlog

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.application.use_cases.timeline._links import validate_clip_media_link
from app.application.use_cases.timeline.results import ClipResult
from app.core.errors import NotFoundError, ValidationFailedError, VersionConflictError

_LOGGER = structlog.get_logger(__name__)


class UpdateClip:
    """Version-fenced (on the timeline) partial update of a clip."""

    def __init__(self, uow: IUnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self,
        *,
        project_id: UUID,
        track_id: UUID,
        clip_id: UUID,
        owner_user_id: UUID,
        tenant_id: UUID,
        expected_version: int,
        changes: Mapping[str, Any],
        ip: str | None = None,
    ) -> ClipResult:
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

            clip = await self._uow.timeline.get_clip(track_id, clip_id)
            if clip is None:
                raise NotFoundError(
                    "clip not found",
                    details={"clip_id": str(clip_id)},
                )

            # Aggregate fence (404-before-412): the token is the timeline's.
            if timeline.version != expected_version:
                _LOGGER.warning(
                    "clip.update_rejected",
                    reason="version_mismatch",
                    project_id=str(project_id),
                    timeline_id=str(timeline.id),
                    track_id=str(track_id),
                    clip_id=str(clip_id),
                    expected_version=expected_version,
                    ip=ip,
                )
                raise VersionConflictError("Resource has been modified.")

            effective = {k: v for k, v in changes.items() if getattr(clip, k) != v}
            if not effective:
                _LOGGER.info(
                    "clip.update_rejected",
                    reason="same_value_noop",
                    project_id=str(project_id),
                    timeline_id=str(timeline.id),
                    track_id=str(track_id),
                    clip_id=str(clip_id),
                    ip=ip,
                )
                return ClipResult(clip=clip, timeline_version=timeline.version)

            # Merged-range re-check: a partial patch (e.g. only ``start_seconds``)
            # is validated against the stored clip so it cannot slip past the DB
            # CHECKs (``end > start``, ``source_end >= source_start``) into a 500.
            new_start = effective.get("start_seconds", clip.start_seconds)
            new_end = effective.get("end_seconds", clip.end_seconds)
            if new_end <= new_start:
                raise ValidationFailedError(
                    "end_seconds must be greater than start_seconds",
                    details={"field": "end_seconds"},
                )
            new_source_start = effective.get("source_start_seconds", clip.source_start_seconds)
            new_source_end = effective.get("source_end_seconds", clip.source_end_seconds)
            if new_source_end < new_source_start:
                raise ValidationFailedError(
                    "source_end_seconds must be greater than or equal to source_start_seconds",
                    details={"field": "source_end_seconds"},
                )

            # Re-validate the media link only when it is (re)linked to a non-null
            # asset (an explicit unlink to None is always valid).
            if "media_asset_id" in effective:
                await validate_clip_media_link(
                    self._uow,
                    tenant_id=tenant_id,
                    owner_user_id=owner_user_id,
                    media_asset_id=effective["media_asset_id"],
                )

            updated = await self._uow.timeline.update_clip(track_id, clip_id, effective)
            if updated is None:
                # No clip-level fence: a None here means the row was soft-deleted
                # between the visibility gate and the write → uniform 404.
                raise NotFoundError(
                    "clip not found",
                    details={"clip_id": str(clip_id)},
                )

            new_version = await self._uow.timeline.bump_version(project_id, expected_version)
            if new_version is None:
                _LOGGER.warning(
                    "clip.update_rejected",
                    reason="version_mismatch",
                    project_id=str(project_id),
                    timeline_id=str(timeline.id),
                    track_id=str(track_id),
                    clip_id=str(clip_id),
                    expected_version=expected_version,
                    ip=ip,
                )
                raise VersionConflictError("Resource has been modified.")
            await self._uow.commit()

        _LOGGER.info(
            "clip.updated",
            clip_id=str(updated.id),
            track_id=str(track_id),
            timeline_id=str(timeline.id),
            project_id=str(project_id),
            changed_fields=sorted(effective.keys()),
            previous_version=expected_version,
            new_version=new_version,
            owner_user_id=str(owner_user_id),
            ip=ip,
        )
        return ClipResult(clip=updated, timeline_version=new_version)
