"""``ProvisionTimeline`` use case (Slice α6.3a).

Contract (API_CONTRACT §3.2.4):

    POST /api/v1/projects/{project_id}/timeline
      body:  { aspect_ratio?, frame_rate?, background_color? }
      → 201  { data: TimelinePublic (version=1, tracks=[]), meta }
      → 404  { error: { code: NOT_FOUND, ... } }        (project missing / not yours)
      → 409  { error: { code: CONFLICT, ... } }          (timeline already exists)
      → 422  { error: { code: VALIDATION_FAILED, ... } } (via DTO)
      → 401  { error: { code: UNAUTHENTICATED, ... } }   (via CurrentUserDep)

**Explicit, non-lazy creation (Q3).** One live timeline per project; a second
``POST`` is a ``409`` (surfaced from the ``uq_timelines_project_id`` partial-
unique index). ``aspect_ratio`` defaults from the project's ``aspect_ratio`` enum
when the body omits it (a project is authored at one of three ratios); the client
may override with an explicit string (e.g. a custom ``'21:9'``). The new timeline
carries ``version = 1`` and no tracks; it does **not** bump ``projects.version``
and is not captured in any snapshot (ADR-0035 / ADR-0038).
"""

from __future__ import annotations

from uuid import UUID

import structlog

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.application.use_cases.timeline.results import TimelineResult
from app.core.errors import NotFoundError

_LOGGER = structlog.get_logger(__name__)

# Derive a sensible default timeline aspect ratio from the project's authored
# orientation (``projects.aspect_ratio`` enum → a concrete ratio string). The
# client may override via the request body.
_PROJECT_RATIO_DEFAULTS = {
    "horizontal": "16:9",
    "vertical": "9:16",
    "square": "1:1",
}
_FALLBACK_RATIO = "16:9"


class ProvisionTimeline:
    """Create the single timeline for the caller's project."""

    def __init__(self, uow: IUnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self,
        *,
        project_id: UUID,
        owner_user_id: UUID,
        tenant_id: UUID,
        aspect_ratio: str | None = None,
        frame_rate: int = 30,
        background_color: str = "#000000",
        ip: str | None = None,
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

            resolved_ratio = aspect_ratio or _PROJECT_RATIO_DEFAULTS.get(
                project.aspect_ratio, _FALLBACK_RATIO
            )
            timeline = await self._uow.timeline.add(
                project_id=project_id,
                aspect_ratio=resolved_ratio,
                frame_rate=frame_rate,
                background_color=background_color,
            )
            await self._uow.commit()

        _LOGGER.info(
            "timeline.provisioned",
            timeline_id=str(timeline.id),
            project_id=str(project_id),
            aspect_ratio=resolved_ratio,
            frame_rate=frame_rate,
            owner_user_id=str(owner_user_id),
            ip=ip,
        )
        return TimelineResult(timeline=timeline, tracks=[])
