"""``CreateTrack`` use case (Slice α6.3a).

Contract (API_CONTRACT §3.2.4):

    POST /api/v1/projects/{project_id}/timeline/tracks
      body:  { kind, z_index, name, locked?, muted?, version? }
      → 201  { data: TrackPublic, meta: { timeline_version } }
      → 404  { error: { code: NOT_FOUND, ... } }        (project/timeline missing / not yours)
      → 409  { error: { code: CONFLICT, ... } }          (z_index already used)
      → 412  { error: { code: VERSION_CONFLICT, ... } } (stale timeline version, if sent)
      → 422  { error: { code: VALIDATION_FAILED, ... } }(via DTO)
      → 401  { error: { code: UNAUTHENTICATED, ... } }  (via CurrentUserDep)

The track is a **child of the timeline aggregate**: creating it advances the
single OCC token ``timelines.version`` (ADR-0038 / Q13). ``version`` is **optional**
on this child ``POST`` (a create cannot be harmfully stale): when omitted the
aggregate token is bumped unconditionally; when supplied it is a fence (stale →
``412``). ``z_index`` is client-assigned and unique per live timeline — a
collision is a ``409`` (Q5). Does **not** touch ``projects.version``.
"""

from __future__ import annotations

from uuid import UUID

import structlog

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.application.use_cases.timeline.results import TrackResult
from app.core.errors import NotFoundError, VersionConflictError

_LOGGER = structlog.get_logger(__name__)


class CreateTrack:
    """Append a track to the caller's project timeline (aggregate-version bumping)."""

    def __init__(self, uow: IUnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self,
        *,
        project_id: UUID,
        owner_user_id: UUID,
        tenant_id: UUID,
        kind: str,
        z_index: int,
        name: str,
        locked: bool = False,
        muted: bool = False,
        expected_version: int | None = None,
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

            # Fast fence when the client sent a token (a create with a stale
            # timeline version is rejected before the insert). When omitted, the
            # aggregate bump below is unconditional (Q13).
            if expected_version is not None and timeline.version != expected_version:
                _LOGGER.warning(
                    "track.create_rejected",
                    reason="version_mismatch",
                    project_id=str(project_id),
                    timeline_id=str(timeline.id),
                    expected_version=expected_version,
                    ip=ip,
                )
                raise VersionConflictError("Resource has been modified.")

            track = await self._uow.timeline.add_track(
                timeline_id=timeline.id,
                kind=kind,
                z_index=z_index,
                name=name,
                locked=locked,
                muted=muted,
            )
            new_version = await self._uow.timeline.bump_version(project_id, expected_version)
            if new_version is None:
                # Fenced bump lost the race (concurrent aggregate mutation) → 412;
                # rollback undoes the track insert.
                _LOGGER.warning(
                    "track.create_rejected",
                    reason="version_mismatch",
                    project_id=str(project_id),
                    timeline_id=str(timeline.id),
                    expected_version=expected_version,
                    ip=ip,
                )
                raise VersionConflictError("Resource has been modified.")
            await self._uow.commit()

        _LOGGER.info(
            "track.created",
            track_id=str(track.id),
            timeline_id=str(timeline.id),
            project_id=str(project_id),
            kind=kind,
            z_index=z_index,
            owner_user_id=str(owner_user_id),
            new_version=new_version,
            ip=ip,
        )
        return TrackResult(track=track, timeline_version=new_version)
