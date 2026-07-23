"""``CreateExportJob`` use case (Slice α8.5a).

Contract:

    POST /api/v1/projects/{project_id}/render-jobs/{render_job_id}/exports
      body:  { format, quality, orientation }
      → 201  { data: ExportJobPublic, meta }              (new export queued)
      → 200  { data: ExportJobPublic, meta }              (idempotent replay — existing job)
      → 404  { error: NOT_FOUND }                         (project / render job not visible)
      → 422  { error: VALIDATION_FAILED }                 (master not ready / cross-orientation)
      → 401  { error: UNAUTHENTICATED }                   (via CurrentUserDep)

Creates the export job in ``queued`` (the α8.5a export worker drives execution). Export is
strictly downstream and delivery-only (W8.5.1/W8.5.2): the **only** legal source is the
completed render's master ``MediaAsset`` (Fork D). Steps:

1. project ownership gate (404-before-anything);
2. resolve the render job under the project; require it ``succeeded`` with a master output
   (else 422 — nothing to export yet);
3. **same-orientation guard (Fork F, tightened).** Compute the master's orientation from its
   dimensions and reject a mismatched request (422) — export preserves presentation and only
   changes delivery characteristics; cross-orientation reframe is out of α8.5a scope;
4. idempotency (Fork E): a repeat request for the same ``(render_job, format, quality,
   orientation)`` returns the existing active/fulfilled export (router → 200), backed by the
   partial-unique constraint as the race-safe backstop.

On create, an ``ExportJobCreated`` event is written to the ``event_outbox`` in the same
transaction (D9). This use case never mutates another aggregate (W8.5.1).
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

import structlog

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.application.use_cases.export._events import emit_export_job_created
from app.core.errors import ConflictError, NotFoundError, ValidationFailedError
from app.domain.export.export_job import ExportJob
from app.domain.export.export_status import ExportStatus
from app.domain.render.render_status import RenderStatus

_LOGGER = structlog.get_logger(__name__)

# Accepted delivery parameters (mirror the export_* DB enums). Validated here so direct
# use-case callers (and tests) fail cleanly with 422 rather than a DB DataError.
_VALID_FORMATS = frozenset({"mp4", "mov", "gif", "webm"})
_VALID_QUALITIES = frozenset({"sd", "hd_1080p", "qhd_2k", "uhd_4k"})
_VALID_ORIENTATIONS = frozenset({"horizontal", "vertical", "square"})


def orientation_of(width: int | None, height: int | None) -> str | None:
    """Map pixel dimensions → ``export_orientation`` value, or ``None`` if unknown."""
    if width is None or height is None or width <= 0 or height <= 0:
        return None
    if width > height:
        return "horizontal"
    if height > width:
        return "vertical"
    return "square"


@dataclass(frozen=True, slots=True)
class CreateExportJobResult:
    """The created (or idempotently-replayed) export plus whether it was newly made."""

    job: ExportJob
    created: bool


class CreateExportJob:
    """Queue a delivery export for a completed render (idempotent per encoding tuple)."""

    def __init__(self, uow: IUnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self,
        *,
        project_id: UUID,
        render_job_id: UUID,
        owner_user_id: UUID,
        tenant_id: UUID,
        format: str,
        quality: str,
        orientation: str,
        ip: str | None = None,
    ) -> CreateExportJobResult:
        if format not in _VALID_FORMATS:
            raise ValidationFailedError("unsupported export format", details={"format": format})
        if quality not in _VALID_QUALITIES:
            raise ValidationFailedError("unsupported export quality", details={"quality": quality})
        if orientation not in _VALID_ORIENTATIONS:
            raise ValidationFailedError(
                "unsupported export orientation", details={"orientation": orientation}
            )

        async with self._uow:
            project = await self._uow.projects.get_owned(
                project_id=project_id,
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
            )
            if project is None:
                raise NotFoundError("project not found", details={"project_id": str(project_id)})

            render_job = await self._uow.render_jobs.get_owned(project_id, render_job_id)
            if render_job is None:
                raise NotFoundError(
                    "render job not found", details={"render_job_id": str(render_job_id)}
                )
            if (
                render_job.status != RenderStatus.SUCCEEDED.value
                or render_job.output_media_asset_id is None
            ):
                # The render exists but has no completed master to export yet — the request
                # is well-formed but not fulfillable given current state (422, not 404).
                raise ValidationFailedError(
                    "render job has no completed master output to export",
                    details={"render_job_id": str(render_job_id), "status": render_job.status},
                )

            master = await self._uow.media.get_owned(
                render_job.output_media_asset_id, tenant_id, owner_user_id
            )
            if master is None:  # pragma: no cover — the succeeded render owns its master
                raise ValidationFailedError(
                    "render master media asset is unavailable",
                    details={"render_job_id": str(render_job_id)},
                )

            master_orientation = orientation_of(master.width, master.height)
            if master_orientation is None:
                raise ValidationFailedError(
                    "master dimensions are unknown; cannot verify export orientation",
                    details={"render_job_id": str(render_job_id)},
                )
            if orientation != master_orientation:
                # Cross-orientation export changes presentation, not delivery — out of α8.5a
                # scope (Fork F). A future reframe policy slice may lift this.
                raise ValidationFailedError(
                    "cross-orientation export is out of scope; export preserves the master's "
                    f"orientation ({master_orientation})",
                    details={
                        "requested_orientation": orientation,
                        "master_orientation": master_orientation,
                    },
                )

            # Idempotency pre-check (Fork E): replay the existing active/fulfilled export.
            existing = await self._uow.export_jobs.get_active(
                render_job_id, format=format, quality=quality, orientation=orientation
            )
            if existing is not None:
                _LOGGER.info(
                    "export_job.create_idempotent_replay",
                    export_job_id=str(existing.id),
                    render_job_id=str(render_job_id),
                    format=format,
                    quality=quality,
                    orientation=orientation,
                    owner_user_id=str(owner_user_id),
                    ip=ip,
                )
                return CreateExportJobResult(job=existing, created=False)

            try:
                job = await self._uow.export_jobs.add(
                    render_job_id=render_job_id,
                    requested_by_user_id=owner_user_id,
                    format=format,
                    quality=quality,
                    orientation=orientation,
                    status=ExportStatus.QUEUED.value,
                )
            except ConflictError:
                # Race: a concurrent request inserted the same tuple between our pre-check
                # and insert. Resolve idempotently by returning the winner (Fork E).
                winner = await self._uow.export_jobs.get_active(
                    render_job_id, format=format, quality=quality, orientation=orientation
                )
                if winner is None:  # pragma: no cover — constraint says it exists
                    raise
                _LOGGER.info(
                    "export_job.create_idempotent_race",
                    export_job_id=str(winner.id),
                    render_job_id=str(render_job_id),
                    owner_user_id=str(owner_user_id),
                    ip=ip,
                )
                return CreateExportJobResult(job=winner, created=False)

            await emit_export_job_created(self._uow, job, actor_user_id=owner_user_id)
            await self._uow.commit()

        _LOGGER.info(
            "export_job.created",
            export_job_id=str(job.id),
            render_job_id=str(render_job_id),
            project_id=str(project_id),
            format=format,
            quality=quality,
            orientation=orientation,
            owner_user_id=str(owner_user_id),
            ip=ip,
        )
        return CreateExportJobResult(job=job, created=True)
