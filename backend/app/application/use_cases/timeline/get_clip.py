"""``GetClip`` use case (Slice α6.3b).

Contract (API_CONTRACT §3.2.4):

    GET /api/v1/projects/{project_id}/timeline/tracks/{track_id}/clips/{clip_id}
      → 200  { data: ClipPublic, meta: { timeline_version } }
      → 404  { error: { code: NOT_FOUND, ... } }        (project/timeline/track/clip missing / not yours)
      → 401  { error: { code: UNAUTHENTICATED, ... } }  (via CurrentUserDep)

Four-level gate (project → timeline → track → clip, all ``404`` — missing,
soft-deleted, or belonging to another parent are indistinguishable). Returns the
aggregate OCC token (``timelines.version``) alongside the clip. Read-only.
"""

from __future__ import annotations

from uuid import UUID

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.application.use_cases.timeline.results import ClipResult
from app.core.errors import NotFoundError


class GetClip:
    """Fetch a single clip of the caller's project timeline."""

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
        return ClipResult(clip=clip, timeline_version=timeline.version)
