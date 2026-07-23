"""``/api/v1/projects/{project_id}/render-jobs/{render_job_id}/exports/*`` router (α8.5a).

Two **render-job-nested** endpoints (ownership derived through the project → render job),
both authenticated via :data:`CurrentUserDep`:

* ``POST /projects/{pid}/render-jobs/{rid}/exports``               → 201 (or 200 on idempotent
  replay), queue a delivery export of the render's master.
* ``GET  /projects/{pid}/render-jobs/{rid}/exports/{export_id}``   → 200, fetch one export.

Export is downstream, delivery-only (W8.5.1/W8.5.2): the router stays thin — DTO projection +
envelope + the 201/200 create split; the ownership gate, master-readiness check, the
same-orientation guard (Fork F), and idempotency live in the use cases. Execution is the
α8.5a export worker (jobs stay ``queued`` until it claims them). Download-serving is deferred
to α8.5b.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse

from app.api.v1.deps import CreateExportJobDep, CurrentUserDep, GetExportJobDep
from app.api.v1.helpers import client_ip, envelope
from app.api.v1.schemas.export import ExportJobCreateRequest, ExportJobPublic
from app.domain.export.export_job import ExportJob

router = APIRouter(
    prefix="/projects/{project_id}/render-jobs/{render_job_id}/exports",
    tags=["export-jobs"],
)


def _to_public(job: ExportJob) -> ExportJobPublic:
    """Project a domain ``ExportJob`` into the wire DTO."""
    return ExportJobPublic(
        id=job.id,
        render_job_id=job.render_job_id,
        requested_by_user_id=job.requested_by_user_id,
        format=job.format,
        quality=job.quality,
        orientation=job.orientation,
        status=job.status,
        output_media_asset_id=job.output_media_asset_id,
        download_count=job.download_count,
        last_downloaded_at=job.last_downloaded_at,
        file_size_bytes=job.file_size_bytes,
        finished_at=job.finished_at,
        version=job.version,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_export_job(
    project_id: UUID,
    render_job_id: UUID,
    body: ExportJobCreateRequest,
    request: Request,
    current_user: CurrentUserDep,
    use_case: CreateExportJobDep,
) -> JSONResponse:
    """Queue a delivery export of a completed render's master.

    Returns ``201`` with the queued ``ExportJobPublic``. When an active/fulfilled export for
    the same ``(render_job, format, quality, orientation)`` already exists, returns it with
    ``200`` (idempotent replay, Fork E). ``404`` if the project/render job is not the
    caller's; ``422`` if the render has no completed master or the requested orientation
    differs from the master's (cross-orientation is out of α8.5a scope).
    """
    result = await use_case.execute(
        project_id=project_id,
        render_job_id=render_job_id,
        owner_user_id=current_user.id,
        tenant_id=current_user.tenant_id,
        format=body.format,
        quality=body.quality,
        orientation=body.orientation,
        ip=client_ip(request),
    )
    code = status.HTTP_201_CREATED if result.created else status.HTTP_200_OK
    return JSONResponse(status_code=code, content=envelope(_to_public(result.job), request))


@router.get("/{export_job_id}")
async def get_export_job(
    project_id: UUID,
    render_job_id: UUID,
    export_job_id: UUID,
    request: Request,
    current_user: CurrentUserDep,
    use_case: GetExportJobDep,
) -> JSONResponse:
    """Fetch one export job of the caller's render job.

    A missing export — or one under another user's project / a different render job — yields
    a uniform ``404`` (anti-enumeration).
    """
    job = await use_case.execute(
        project_id=project_id,
        render_job_id=render_job_id,
        export_job_id=export_job_id,
        owner_user_id=current_user.id,
        tenant_id=current_user.tenant_id,
    )
    return JSONResponse(content=envelope(_to_public(job), request))
