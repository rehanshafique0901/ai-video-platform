"""``CreateClip`` use case (Slice α6.3b).

Contract (API_CONTRACT §3.2.4):

    POST /api/v1/projects/{project_id}/timeline/tracks/{track_id}/clips
      body:  { media_asset_id?, start_seconds, end_seconds,
               source_start_seconds?, source_end_seconds?, volume?, locked?, version? }
      → 201  { data: ClipPublic, meta: { timeline_version } }
      → 404  { error: { code: NOT_FOUND, ... } }        (project/timeline/track missing / not yours)
      → 412  { error: { code: VERSION_CONFLICT, ... } } (stale timeline version, if sent)
      → 422  { error: { code: VALIDATION_FAILED, ... } }(bad time range / bad media_asset_id, via DTO + link check)
      → 401  { error: { code: UNAUTHENTICATED, ... } }  (via CurrentUserDep)

The clip is a **child of the timeline aggregate** (via its track): creating it
advances the single OCC token ``timelines.version`` (ADR-0038 / Q13). ``version``
is **optional** on this child ``POST`` (a create cannot be harmfully stale): when
omitted the aggregate token is bumped unconditionally; when supplied it is a
fence (stale → ``412``). Control flow is 404-before-{412,422}: project → timeline
→ track visibility first, then the aggregate fence, then the ``media_asset_id``
link check (D4). Clips may overlap and need not fit the timeline duration (Q6).
Does **not** touch ``projects.version``.
"""

from __future__ import annotations

from uuid import UUID

import structlog

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.application.use_cases.timeline._links import validate_clip_media_link
from app.application.use_cases.timeline.results import ClipResult
from app.core.errors import NotFoundError, VersionConflictError

_LOGGER = structlog.get_logger(__name__)


class CreateClip:
    """Append a clip to a track of the caller's project timeline (aggregate-bumping)."""

    def __init__(self, uow: IUnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self,
        *,
        project_id: UUID,
        track_id: UUID,
        owner_user_id: UUID,
        tenant_id: UUID,
        media_asset_id: UUID | None,
        start_seconds: float,
        end_seconds: float,
        source_start_seconds: float,
        source_end_seconds: float,
        volume: float,
        locked: bool,
        expected_version: int | None = None,
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

            # Fast fence when a token was sent (a create with a stale timeline
            # version is rejected before the insert); unconditional bump otherwise.
            if expected_version is not None and timeline.version != expected_version:
                _LOGGER.warning(
                    "clip.create_rejected",
                    reason="version_mismatch",
                    project_id=str(project_id),
                    timeline_id=str(timeline.id),
                    track_id=str(track_id),
                    expected_version=expected_version,
                    ip=ip,
                )
                raise VersionConflictError("Resource has been modified.")

            # Body semantics: the linked asset must be one the caller owns (→ 422).
            await validate_clip_media_link(
                self._uow,
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                media_asset_id=media_asset_id,
            )

            clip = await self._uow.timeline.add_clip(
                track_id=track_id,
                media_asset_id=media_asset_id,
                start_seconds=start_seconds,
                end_seconds=end_seconds,
                source_start_seconds=source_start_seconds,
                source_end_seconds=source_end_seconds,
                volume=volume,
                locked=locked,
            )
            new_version = await self._uow.timeline.bump_version(project_id, expected_version)
            if new_version is None:
                # Fenced bump lost the race (concurrent aggregate mutation) → 412;
                # rollback undoes the clip insert.
                _LOGGER.warning(
                    "clip.create_rejected",
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
            "clip.created",
            clip_id=str(clip.id),
            track_id=str(track_id),
            timeline_id=str(timeline.id),
            project_id=str(project_id),
            media_asset_id=str(media_asset_id) if media_asset_id else None,
            new_version=new_version,
            owner_user_id=str(owner_user_id),
            ip=ip,
        )
        return ClipResult(clip=clip, timeline_version=new_version)
