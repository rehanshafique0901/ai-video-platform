"""``/api/v1/publish-jobs/*`` router (α8.6b Publish Runtime).

Top-level (not project-nested — ruled 2026-07-27): publish jobs carry explicit ownership +
``project_id``. Three endpoints, all authenticated via :data:`CurrentUserDep`:

* ``POST /publish-jobs``        → 201 (or 200 on idempotent replay), queue a publish of a
  finished export delivery to a connected destination.
* ``POST /publish-jobs/batch`` → 201, fan out one export publish to N connected accounts (α9.4);
  ``data`` is a per-account outcome array. Additive — the single-create endpoint is unchanged.
* ``GET  /publish-jobs/{id}``  → 200, fetch one publish job.
* ``GET  /publish-jobs``       → 200, list the caller's publish jobs (newest first).

The router stays thin: DTO projection + envelope + the 201/200 create split. Ownership,
readiness, and idempotency live in the use cases; execution is the α8.6b publish worker
(jobs stay ``queued`` until it claims them). No credential material crosses this boundary.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse

from app.api.v1.deps import (
    CreatePublishJobDep,
    CreatePublishJobsDep,
    CurrentUserDep,
    GetPublishJobDep,
    ListPublishJobsDep,
)
from app.api.v1.helpers import client_ip, envelope
from app.api.v1.schemas.publish_jobs import (
    ContentPackagePublic,
    PublishJobBatchCreateRequest,
    PublishJobBatchItemPublic,
    PublishJobCreateRequest,
    PublishJobPublic,
)
from app.application.use_cases.publishing.create_publish_jobs import PublishFanOutItem
from app.domain.publishing.publish_job import PublishJob

router = APIRouter(prefix="/publish-jobs", tags=["publish-jobs"])


def _to_public(job: PublishJob) -> PublishJobPublic:
    """Project a domain ``PublishJob`` into the wire DTO (no credential material, C8)."""
    return PublishJobPublic(
        id=job.id,
        requested_by_user_id=job.requested_by_user_id,
        project_id=job.project_id,
        source_export_job_id=job.source_export_job_id,
        source_media_asset_id=job.source_media_asset_id,
        social_account_id=job.social_account_id,
        platform=job.platform,
        status=job.status,
        scheduled_at=job.scheduled_at,
        attempt=job.attempt,
        max_attempts=job.max_attempts,
        content_package=ContentPackagePublic.from_domain(job.content_package),
        platform_post_id=job.platform_post_id,
        platform_post_url=job.platform_post_url,
        error=job.error,
        published_at=job.published_at,
        finished_at=job.finished_at,
        version=job.version,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_publish_job(
    body: PublishJobCreateRequest,
    request: Request,
    current_user: CurrentUserDep,
    use_case: CreatePublishJobDep,
) -> JSONResponse:
    """Queue a publish of a finished export delivery to a connected destination.

    Returns ``201`` with the queued ``PublishJobPublic``. When an active/fulfilled publish
    for the same ``(source media asset, social account)`` already exists, returns it with
    ``200`` (idempotent replay). ``404`` if the export/account is not the caller's; ``422``
    if the export has no completed delivery artifact, the account is not connected, or the
    platform has no registered destination adapter. An optional ``publish_at`` (timezone-aware,
    future) schedules the platform-native go-live (α8.9b) — the upload still runs immediately.
    An optional ``thumbnail_media_asset_id`` (α9.3) names an owned image asset to set as the
    video's thumbnail (best-effort, after the primary upload succeeds); a non-owned id is ``404``
    and a non-image asset is ``422``.
    """
    result = await use_case.execute(
        owner_user_id=current_user.id,
        tenant_id=current_user.tenant_id,
        export_job_id=body.export_job_id,
        social_account_id=body.social_account_id,
        title=body.title,
        description=body.description,
        tags=tuple(body.tags) if body.tags is not None else None,
        visibility=body.visibility,
        publish_at=body.publish_at,
        thumbnail_media_asset_id=body.thumbnail_media_asset_id,
        ip=client_ip(request),
    )
    code = status.HTTP_201_CREATED if result.created else status.HTTP_200_OK
    return JSONResponse(status_code=code, content=envelope(_to_public(result.job), request))


def _to_batch_item(item: PublishFanOutItem) -> PublishJobBatchItemPublic:
    """Project one fan-out outcome into its wire DTO (unambiguous per-account result)."""
    return PublishJobBatchItemPublic(
        social_account_id=item.social_account_id,
        created=item.created,
        publish_job=_to_public(item.job) if item.job is not None else None,
        error=(
            {"code": item.error.code, "message": item.error.message}
            if item.error is not None
            else None
        ),
    )


@router.post("/batch", status_code=status.HTTP_201_CREATED)
async def create_publish_jobs_batch(
    body: PublishJobBatchCreateRequest,
    request: Request,
    current_user: CurrentUserDep,
    use_case: CreatePublishJobsDep,
) -> JSONResponse:
    """Fan out a publish of one finished export to N of the caller's connected accounts (α9.4).

    An **additive** entry point that composes the single-create use case — ``POST /publish-jobs``
    is unchanged. Shared prerequisites are validated once and fail the whole request: a
    non-owned export/thumbnail is ``404``; a not-ready export or a non-image thumbnail is ``422``.
    Otherwise returns ``201`` with a ``data`` array of per-account outcomes (input order): each
    item is ``created`` (freshly queued), an idempotent replay (``created=false`` + ``publish_job``),
    or a per-account ``error`` (account not owned / not connected / unsupported platform) — so one
    bad account never blocks the rest. Every created job is an ordinary ``PublishJob``: scheduling,
    captions, thumbnails, notifications, and analytics all apply with no batch-specific logic.
    """
    result = await use_case.execute(
        owner_user_id=current_user.id,
        tenant_id=current_user.tenant_id,
        export_job_id=body.export_job_id,
        social_account_ids=body.social_account_ids,
        title=body.title,
        description=body.description,
        tags=tuple(body.tags) if body.tags is not None else None,
        visibility=body.visibility,
        publish_at=body.publish_at,
        thumbnail_media_asset_id=body.thumbnail_media_asset_id,
        ip=client_ip(request),
    )
    data = [_to_batch_item(item) for item in result.items]
    return JSONResponse(status_code=status.HTTP_201_CREATED, content=envelope(data, request))


@router.get("/{publish_job_id}")
async def get_publish_job(
    publish_job_id: UUID,
    request: Request,
    current_user: CurrentUserDep,
    use_case: GetPublishJobDep,
) -> JSONResponse:
    """Fetch one of the caller's publish jobs (uniform ``404`` if missing / not owned)."""
    job = await use_case.execute(
        tenant_id=current_user.tenant_id,
        owner_user_id=current_user.id,
        publish_job_id=publish_job_id,
    )
    return JSONResponse(content=envelope(_to_public(job), request))


@router.get("")
async def list_publish_jobs(
    request: Request,
    current_user: CurrentUserDep,
    use_case: ListPublishJobsDep,
) -> JSONResponse:
    """List the caller's publish jobs (newest first)."""
    jobs = await use_case.execute(tenant_id=current_user.tenant_id, owner_user_id=current_user.id)
    return JSONResponse(content=envelope([_to_public(j) for j in jobs], request))
