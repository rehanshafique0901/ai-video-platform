"""``GetTimeline`` use case (Slice α6.3a).

Contract (API_CONTRACT §3.2.4):

    GET /api/v1/projects/{project_id}/timeline
      → 200  { data: TimelinePublic (with ordered tracks), meta }
      → 404  { error: { code: NOT_FOUND, ... } }        (project/timeline missing / not yours)
      → 401  { error: { code: UNAUTHENTICATED, ... } }  (via CurrentUserDep)

Two-level gate: project ownership (→ ``404``) then timeline resolution (a project
without a provisioned timeline is a ``404`` — the timeline is created explicitly,
Q3). Returns the composition tree: the timeline root plus its live tracks ordered
by ``z_index`` (α6.3b extends each track with its clips). Read-only.
"""

from __future__ import annotations

from uuid import UUID

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.application.use_cases.timeline.results import TimelineResult
from app.core.errors import NotFoundError


class GetTimeline:
    """Fetch the caller's project timeline with its ordered tracks."""

    def __init__(self, uow: IUnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self,
        *,
        project_id: UUID,
        owner_user_id: UUID,
        tenant_id: UUID,
    ) -> TimelineResult:
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

            tracks = await self._uow.timeline.list_tracks(timeline.id)
        return TimelineResult(timeline=timeline, tracks=tracks)
