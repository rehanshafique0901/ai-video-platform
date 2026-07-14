"""``ListClips`` use case (Slice α6.3b).

Contract (API_CONTRACT §3.2.4):

    GET /api/v1/projects/{project_id}/timeline/tracks/{track_id}/clips
      → 200  { data: [ClipPublic, ...] (start_seconds ASC), meta: { timeline_version } }
      → 404  { error: { code: NOT_FOUND, ... } }        (project/timeline/track missing / not yours)
      → 401  { error: { code: UNAUTHENTICATED, ... } }  (via CurrentUserDep)

Three-level gate (project → timeline → track, all ``404``), then the track's live
clips ordered by ``start_seconds`` ASC, ``id`` ASC (a total order, D7). Returns
the aggregate OCC token (``timelines.version``) so the caller can carry it into a
subsequent fenced clip write. Read-only.
"""

from __future__ import annotations

from uuid import UUID

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.application.use_cases.timeline.results import ClipListResult
from app.core.errors import NotFoundError


class ListClips:
    """List a track's clips, ordered by ``start_seconds``."""

    def __init__(self, uow: IUnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self,
        *,
        project_id: UUID,
        track_id: UUID,
        owner_user_id: UUID,
        tenant_id: UUID,
    ) -> ClipListResult:
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

            clips = await self._uow.timeline.list_clips(track_id)
        return ClipListResult(clips=clips, timeline_version=timeline.version)
