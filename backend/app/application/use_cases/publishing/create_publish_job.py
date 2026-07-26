"""``CreatePublishJob`` use case (Slice α8.6b).

Contract:

    POST /api/v1/publish-jobs
      body:  { export_job_id, social_account_id, title?, description?, tags?, visibility? }
      → 201  { data: PublishJobPublic, meta }              (new publish queued)
      → 200  { data: PublishJobPublic, meta }              (idempotent replay — existing job)
      → 404  { error: NOT_FOUND }                          (export/account not the caller's)
      → 422  { error: VALIDATION_FAILED }                  (export not ready / account not
                                                            connected / unsupported platform)
      → 401  { error: UNAUTHENTICATED }                    (via CurrentUserDep)

Creates the publish job in ``queued`` (the α8.6b publish worker drives execution). Publishing
is strictly downstream and consumes the **export delivery** ``MediaAsset`` only (PUB-1). It
requires **explicit user intent** (PUB-2) — there is no auto-publish. Steps:

1. destination account ownership + readiness gate (must be the caller's + ``connected``);
2. source resolution (PUB-1): resolve the export → its owning ``project_id`` (DQ1) + the
   delivery ``output_media_asset_id``; require the export ``succeeded`` with an artifact;
3. supported-platform gate (the destination registry must serve this platform);
4. build the deterministic :class:`ContentPackage` (PUB-9);
5. idempotency (DQ2): a repeat request for the same ``(source_media_asset, social_account)``
   returns the existing active/fulfilled job (router → 200), backed by the partial-unique
   constraint as the race-safe backstop.

On create, a ``PublishJobCreated`` event is written to the ``event_outbox`` in the same
transaction. This use case never mutates another aggregate (PUB-6).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from uuid import UUID

import structlog

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.application.use_cases.publishing._events import emit_publish_job_created
from app.core.errors import ConflictError, NotFoundError, ValidationFailedError
from app.domain.export.export_status import ExportStatus
from app.domain.publishing.content_package import Visibility, build_content_package
from app.domain.publishing.publish_job import PublishJob
from app.domain.publishing.publish_status import PublishStatus
from app.domain.publishing.social_account import AccountStatus

_LOGGER = structlog.get_logger(__name__)

_DEFAULT_MAX_ATTEMPTS = 5


@dataclass(frozen=True, slots=True)
class CreatePublishJobResult:
    """The created (or idempotently-replayed) publish plus whether it was newly made."""

    job: PublishJob
    created: bool


class CreatePublishJob:
    """Queue a publish of a finished export to a connected destination (idempotent)."""

    def __init__(self, uow: IUnitOfWork, *, supported_platforms: Iterable[str]) -> None:
        self._uow = uow
        self._supported_platforms = frozenset(supported_platforms)
        self._max_attempts = _DEFAULT_MAX_ATTEMPTS

    async def execute(
        self,
        *,
        owner_user_id: UUID,
        tenant_id: UUID,
        export_job_id: UUID,
        social_account_id: UUID,
        title: str | None = None,
        description: str | None = None,
        tags: tuple[str, ...] | None = None,
        visibility: Visibility | None = None,
        ip: str | None = None,
    ) -> CreatePublishJobResult:
        async with self._uow:
            account = await self._uow.social_accounts.get_owned(
                tenant_id=tenant_id, user_id=owner_user_id, social_account_id=social_account_id
            )
            if account is None:
                raise NotFoundError(
                    "social account not found",
                    details={"social_account_id": str(social_account_id)},
                )
            if account.status is not AccountStatus.CONNECTED:
                raise ValidationFailedError(
                    "social account is not connected",
                    details={
                        "social_account_id": str(social_account_id),
                        "status": account.status.value,
                    },
                )
            if account.platform not in self._supported_platforms:
                raise ValidationFailedError(
                    "no destination adapter is registered for this platform",
                    details={"platform": account.platform},
                )

            source = await self._uow.publish_jobs.resolve_source(
                export_job_id, tenant_id=tenant_id, owner_user_id=owner_user_id
            )
            if source is None:
                raise NotFoundError(
                    "export job not found", details={"export_job_id": str(export_job_id)}
                )
            if (
                source.export_status != ExportStatus.SUCCEEDED.value
                or source.source_media_asset_id is None
            ):
                raise ValidationFailedError(
                    "export job has no completed delivery artifact to publish",
                    details={
                        "export_job_id": str(export_job_id),
                        "status": source.export_status,
                    },
                )
            source_media_asset_id = source.source_media_asset_id

            # Idempotency pre-check (DQ2): replay the existing active/fulfilled publish.
            existing = await self._uow.publish_jobs.get_active(
                source_media_asset_id=source_media_asset_id, social_account_id=social_account_id
            )
            if existing is not None:
                _LOGGER.info(
                    "publish_job.create_idempotent_replay",
                    publish_job_id=str(existing.id),
                    export_job_id=str(export_job_id),
                    social_account_id=str(social_account_id),
                    owner_user_id=str(owner_user_id),
                    ip=ip,
                )
                return CreatePublishJobResult(job=existing, created=False)

            project = await self._uow.projects.get_owned(
                project_id=source.project_id, tenant_id=tenant_id, owner_user_id=owner_user_id
            )
            project_title = project.name if project is not None else None

            package = build_content_package(
                media_asset_id=source_media_asset_id,
                project_title=project_title,
                title=title,
                description=description,
                tags=tags,
                visibility=visibility,
            )

            try:
                job = await self._uow.publish_jobs.add(
                    tenant_id=tenant_id,
                    requested_by_user_id=owner_user_id,
                    project_id=source.project_id,
                    source_export_job_id=export_job_id,
                    source_media_asset_id=source_media_asset_id,
                    social_account_id=social_account_id,
                    platform=account.platform,
                    status=PublishStatus.QUEUED.value,
                    scheduled_at=None,
                    content_package=package,
                    max_attempts=self._max_attempts,
                )
            except ConflictError:
                # Race: a concurrent request inserted the same tuple between our pre-check
                # and insert. Resolve idempotently by returning the winner (DQ2).
                winner = await self._uow.publish_jobs.get_active(
                    source_media_asset_id=source_media_asset_id,
                    social_account_id=social_account_id,
                )
                if winner is None:  # pragma: no cover — constraint says it exists
                    raise
                _LOGGER.info(
                    "publish_job.create_idempotent_race",
                    publish_job_id=str(winner.id),
                    export_job_id=str(export_job_id),
                    owner_user_id=str(owner_user_id),
                    ip=ip,
                )
                return CreatePublishJobResult(job=winner, created=False)

            await emit_publish_job_created(self._uow, job, actor_user_id=owner_user_id)
            await self._uow.commit()

        _LOGGER.info(
            "publish_job.created",
            publish_job_id=str(job.id),
            export_job_id=str(export_job_id),
            project_id=str(job.project_id),
            social_account_id=str(social_account_id),
            platform=job.platform,
            owner_user_id=str(owner_user_id),
            ip=ip,
        )
        return CreatePublishJobResult(job=job, created=True)
