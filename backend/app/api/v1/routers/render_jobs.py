"""``/api/v1/projects/{project_id}/render-jobs/*`` HTTP router (α7.1).

Four **project-nested** endpoints (ownership derived through the project — a
render job has no owner columns of its own), all authenticated via
:data:`CurrentUserDep`:

* ``POST   /projects/{project_id}/render-jobs``                  → 201 (or 200 on
  idempotent replay), queue a render job.
* ``GET    /projects/{project_id}/render-jobs``                  → 200, list the
  project's render jobs (optional ``?status=`` filter).
* ``GET    /projects/{project_id}/render-jobs/{render_job_id}``  → 200, fetch one.
* ``POST   /projects/{project_id}/render-jobs/{id}/cancel``      → 200, cancel a
  ``queued``/``running`` job (version-fenced).

Render jobs are **self-versioned** (ADR-0039): a ``version`` is on the wire and
cancel is fenced on it (``412`` on a stale token; ``409`` if already complete;
``200`` no-op re-cancel). The router stays thin — DTO projection + envelope + the
201/200 create split; the project ownership gate, timeline resolution, idempotency,
and the cancel state machine live in the use cases. There is **no** background
execution in α7.1 (jobs stay ``queued`` until the α8.x worker).
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Query, Request, status
from fastapi.responses import JSONResponse

from app.api.v1.deps import (
    CancelRenderJobDep,
    CreateRenderJobDep,
    CurrentUserDep,
    GetRenderJobDep,
    ListRenderJobsDep,
)
from app.api.v1.helpers import client_ip, envelope
from app.api.v1.schemas.render import (
    RenderJobCancelRequest,
    RenderJobCreateRequest,
    RenderJobPublic,
    RenderStatusLiteral,
)
from app.domain.render.render_job import RenderJob

router = APIRouter(prefix="/projects/{project_id}/render-jobs", tags=["render-jobs"])


def _to_public(job: RenderJob) -> RenderJobPublic:
    """Project a domain ``RenderJob`` into the wire DTO."""
    return RenderJobPublic(
        id=job.id,
        project_id=job.project_id,
        timeline_id=job.timeline_id,
        workflow_run_id=job.workflow_run_id,
        pipeline=job.pipeline,
        pipeline_version=job.pipeline_version,
        queue=job.queue,
        priority=job.priority,
        status=job.status,
        started_at=job.started_at,
        finished_at=job.finished_at,
        progress=job.progress,
        error=job.error,
        output_media_asset_id=job.output_media_asset_id,
        idempotency_key=job.idempotency_key,
        version=job.version,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_render_job(
    project_id: UUID,
    body: RenderJobCreateRequest,
    request: Request,
    current_user: CurrentUserDep,
    use_case: CreateRenderJobDep,
) -> JSONResponse:
    """Queue a render job for the caller's project.

    Returns ``201`` with the queued ``RenderJobPublic``. When ``idempotency_key``
    matches an existing job for the project, returns that job with ``200`` instead
    (idempotent replay, α7.1 Q4). ``404`` if the project is missing/not the
    caller's; ``422`` if the project has no timeline to render.
    """
    result = await use_case.execute(
        project_id=project_id,
        owner_user_id=current_user.id,
        tenant_id=current_user.tenant_id,
        pipeline=body.pipeline,
        pipeline_version=body.pipeline_version,
        queue=body.queue,
        priority=body.priority,
        idempotency_key=body.idempotency_key,
        ip=client_ip(request),
    )
    code = status.HTTP_201_CREATED if result.created else status.HTTP_200_OK
    return JSONResponse(status_code=code, content=envelope(_to_public(result.job), request))


@router.get("")
async def list_render_jobs(
    project_id: UUID,
    request: Request,
    current_user: CurrentUserDep,
    use_case: ListRenderJobsDep,
    status_filter: RenderStatusLiteral | None = Query(default=None, alias="status"),
) -> JSONResponse:
    """List the caller's project render jobs, newest-first, optionally by status.

    ``?status=`` (a ``render_status`` value) narrows the result; a bad enum is a
    ``422``. ``404`` if the project is missing/not the caller's. Empty → ``200 []``.
    """
    jobs = await use_case.execute(
        project_id=project_id,
        owner_user_id=current_user.id,
        tenant_id=current_user.tenant_id,
        status=status_filter,
    )
    return JSONResponse(content=envelope([_to_public(j) for j in jobs], request))


@router.get("/{render_job_id}")
async def get_render_job(
    project_id: UUID,
    render_job_id: UUID,
    request: Request,
    current_user: CurrentUserDep,
    use_case: GetRenderJobDep,
) -> JSONResponse:
    """Fetch one render job of the caller's project.

    A missing job — or one under another user's project — yields a uniform
    ``404`` (α7.1 D3.3).
    """
    job = await use_case.execute(
        project_id=project_id,
        render_job_id=render_job_id,
        owner_user_id=current_user.id,
        tenant_id=current_user.tenant_id,
    )
    return JSONResponse(content=envelope(_to_public(job), request))


@router.post("/{render_job_id}/cancel")
async def cancel_render_job(
    project_id: UUID,
    render_job_id: UUID,
    body: RenderJobCancelRequest,
    request: Request,
    current_user: CurrentUserDep,
    use_case: CancelRenderJobDep,
) -> JSONResponse:
    """Cancel a ``queued``/``running`` render job (version-fenced).

    The body carries the aggregate ``version``. Returns ``200`` with the
    canceled ``RenderJobPublic`` (a re-cancel of an already-canceled job is a
    ``200`` no-op). ``404`` (project/job not visible), ``409`` (already
    succeeded/failed), ``412`` (stale version on a still-cancelable job).
    """
    result = await use_case.execute(
        project_id=project_id,
        render_job_id=render_job_id,
        owner_user_id=current_user.id,
        tenant_id=current_user.tenant_id,
        expected_version=body.version,
        ip=client_ip(request),
    )
    return JSONResponse(content=envelope(_to_public(result.job), request))
