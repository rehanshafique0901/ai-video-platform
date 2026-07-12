"""``UpdateTimeline`` use case (Slice α6.3a).

Contract (API_CONTRACT §3.2.4):

    PATCH /api/v1/projects/{project_id}/timeline
      body:  { version, aspect_ratio?, frame_rate?, background_color?, duration_seconds? }
      → 200  { data: TimelinePublic (version incremented on real change), meta }
      → 404  { error: { code: NOT_FOUND, ... } }        (project/timeline missing / not yours)
      → 412  { error: { code: VERSION_CONFLICT, ... } } (stale version)
      → 422  { error: { code: VALIDATION_FAILED, ... } }(via DTO)
      → 401  { error: { code: UNAUTHENTICATED, ... } }  (via CurrentUserDep)

Version-fenced root update — the same 404-before-412 control flow as
``UpdateScene`` / ``UpdateProject`` (ADR-0038: ``timelines.version`` is the
aggregate OCC token). Only root columns (``aspect_ratio`` / ``frame_rate`` /
``background_color`` / ``duration_seconds``) are patchable; ``project_version_id``
is server-owned and deferred (ADR-0035). A same-value patch is a ``200`` no-op
(no write, no version bump). Does **not** touch ``projects.version``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

import structlog

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.application.use_cases.timeline.results import TimelineResult
from app.core.errors import NotFoundError, VersionConflictError

_LOGGER = structlog.get_logger(__name__)


class UpdateTimeline:
    """Version-fenced partial update of the caller's project timeline root."""

    def __init__(self, uow: IUnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self,
        *,
        project_id: UUID,
        owner_user_id: UUID,
        tenant_id: UUID,
        expected_version: int,
        changes: Mapping[str, Any],
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

            timeline = await self._uow.timeline.get_by_project(project_id)
            if timeline is None:
                raise NotFoundError(
                    "timeline not found",
                    details={"project_id": str(project_id)},
                )

            if timeline.version != expected_version:
                _LOGGER.warning(
                    "timeline.update_rejected",
                    reason="version_mismatch",
                    project_id=str(project_id),
                    timeline_id=str(timeline.id),
                    owner_user_id=str(owner_user_id),
                    expected_version=expected_version,
                    ip=ip,
                )
                raise VersionConflictError("Resource has been modified.")

            effective = {k: v for k, v in changes.items() if getattr(timeline, k) != v}
            if not effective:
                _LOGGER.info(
                    "timeline.update_rejected",
                    reason="same_value_noop",
                    project_id=str(project_id),
                    timeline_id=str(timeline.id),
                    owner_user_id=str(owner_user_id),
                    ip=ip,
                )
                tracks = await self._uow.timeline.list_tracks(timeline.id)
                return TimelineResult(timeline=timeline, tracks=tracks)

            updated = await self._uow.timeline.update_owned(
                project_id=project_id,
                expected_version=expected_version,
                changes=effective,
            )
            if updated is None:
                _LOGGER.warning(
                    "timeline.update_rejected",
                    reason="version_mismatch",
                    project_id=str(project_id),
                    timeline_id=str(timeline.id),
                    owner_user_id=str(owner_user_id),
                    expected_version=expected_version,
                    ip=ip,
                )
                raise VersionConflictError("Resource has been modified.")

            tracks = await self._uow.timeline.list_tracks(updated.id)
            await self._uow.commit()

        _LOGGER.info(
            "timeline.updated",
            timeline_id=str(updated.id),
            project_id=str(project_id),
            owner_user_id=str(owner_user_id),
            changed_fields=sorted(effective.keys()),
            previous_version=expected_version,
            new_version=updated.version,
            ip=ip,
        )
        return TimelineResult(timeline=updated, tracks=tracks)
