"""``ProcessPublishJob`` — publish one queued job to its destination (α8.6b).

The worker body behind :class:`PublishWorker`, a faithful adaptation of
:class:`app.application.use_cases.export.process_export_job.ProcessExportJob` (DQ8) with a
**dual lock** (DQ5) and **bounded retries** (DQ6). For a single ``queued`` publish job it:

1. acquires the ``publish_job:<id>`` lease, then the ``project_publish:<project_id>`` lease
   (job lock first — DQ5). If the project lock is unavailable, it releases the job lease
   cleanly and skips (the job stays ``queued`` and is retried next poll — no attempt burned);
2. claims the job with a ``queued`` → ``running`` CAS (which also bumps ``attempt``);
3. fetches a short-lived :class:`AuthorizedContext` from the credential service (PUB-5 — the
   worker/adapter stay credential-blind; the service is the sole decryptor, ADR-0047 C7);
4. **materializes** the export-delivery ``MediaAsset`` bytes to a temp workspace (PUB-1 — the
   only legal source is the finished export artifact);
5. uploads via the credential-blind :class:`IDestinationPublisher` (Mock in α8.6b);
6. settles ``succeeded`` (with the platform post identity) — or, on a **retryable**
   :class:`DestinationError` with attempts remaining, requeues with capped exponential
   backoff; otherwise fails permanently.

Invariants: authorize + materialize + upload happen **outside** any DB transaction (no lock
held across network/CPU work). Publishing never triggers rendering/export and never mutates
upstream state (PUB-6): it touches only ``publish_jobs`` lifecycle fields + publish events.
"""

from __future__ import annotations

import asyncio
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import structlog

from app.application.interfaces.destination_publisher import (
    DestinationError,
    IDestinationRegistry,
    PublishResult,
    UploadMedia,
    UploadThumbnail,
)
from app.application.interfaces.object_storage import ObjectStorageError
from app.application.interfaces.social_credential_store import (
    CredentialDecryptionError,
    CredentialUnavailableError,
    ISocialCredentialStore,
)
from app.application.interfaces.storage_resolver import IStorageResolver
from app.application.interfaces.unit_of_work import IUnitOfWork
from app.application.use_cases.publishing._events import (
    emit_publish_job_failed,
    emit_publish_job_succeeded,
)
from app.domain.publishing.publish_job import PublishJob

_LOGGER = structlog.get_logger(__name__)

_DEFAULT_LEASE = timedelta(seconds=900)
# Capped exponential backoff (DQ6): base * 2**(attempt-1), clamped to the cap.
_RETRY_BASE_SECONDS = 30
_RETRY_CAP_SECONDS = 3600


@dataclass(frozen=True, slots=True)
class ProcessPublishJobResult:
    """Outcome of one publish pass over a single job."""

    publish_job_id: UUID
    status: str  # "published" | "failed" | "retry" | "skipped" | "noop"
    platform_post_id: str | None = None
    reason: str | None = None


class ProcessPublishJob:
    """Publish a single ``queued`` job under a job + project lease (delivery → destination)."""

    def __init__(
        self,
        uow: IUnitOfWork,
        storage: IStorageResolver,
        credential_store: ISocialCredentialStore,
        destinations: IDestinationRegistry,
        *,
        workspace_dir: str | None = None,
        owner: str | None = None,
        lease: timedelta = _DEFAULT_LEASE,
    ) -> None:
        self._uow = uow
        self._storage = storage
        self._credential_store = credential_store
        self._destinations = destinations
        self._workspace_dir = workspace_dir
        self._owner = owner or f"publish-worker:{uuid4()}"
        self._lease = lease

    async def process(self, *, project_id: UUID, publish_job_id: UUID) -> ProcessPublishJobResult:
        # Lock 1 — the job lease (DQ5: acquire the job lock first).
        async with self._uow:
            job_lease = await self._uow.locks.acquire(
                key=f"publish_job:{publish_job_id}", owner=self._owner, lease=self._lease
            )
            await self._uow.commit()
        if job_lease is None:
            _LOGGER.info("publish.locked", publish_job_id=str(publish_job_id))
            return ProcessPublishJobResult(
                publish_job_id=publish_job_id, status="skipped", reason="locked"
            )

        try:
            # Lock 2 — the project serialisation lease. If unavailable, release the job lease
            # cleanly and skip (job stays queued, retried next poll; no attempt burned).
            async with self._uow:
                project_lease = await self._uow.locks.acquire(
                    key=f"project_publish:{project_id}", owner=self._owner, lease=self._lease
                )
                await self._uow.commit()
            if project_lease is None:
                _LOGGER.info(
                    "publish.project_locked",
                    publish_job_id=str(publish_job_id),
                    project_id=str(project_id),
                )
                return ProcessPublishJobResult(
                    publish_job_id=publish_job_id, status="skipped", reason="project_locked"
                )

            try:
                # Phase 1 — claim (queued → running CAS; also bumps attempt).
                async with self._uow:
                    claimed = await self._uow.publish_jobs.mark_running(publish_job_id)
                    if claimed is None:
                        await self._uow.rollback()
                        return ProcessPublishJobResult(
                            publish_job_id=publish_job_id, status="noop", reason="not_queued"
                        )
                    await self._uow.commit()

                # Phase 2 — authorize + materialize + upload, ALL outside any transaction.
                try:
                    return await self._publish_and_settle(claimed)
                except (CredentialUnavailableError, CredentialDecryptionError) as exc:
                    # Fail-closed: no usable credential is permanent for this attempt set.
                    return await self._settle_failed(
                        claimed,
                        {"code": "credential_unavailable", "message": str(exc)[:500]},
                    )
                except ObjectStorageError as exc:
                    return await self._settle_retry_or_fail(
                        claimed, retryable=True, code="storage_error", message=str(exc)
                    )
                except DestinationError as exc:
                    return await self._settle_retry_or_fail(
                        claimed, retryable=exc.retryable, code=exc.code, message=str(exc)
                    )
            finally:
                async with self._uow:
                    await self._uow.locks.release(project_lease)
                    await self._uow.commit()
        finally:
            async with self._uow:
                await self._uow.locks.release(job_lease)
                await self._uow.commit()

    async def _publish_and_settle(self, job: PublishJob) -> ProcessPublishJobResult:
        # Phase 2a — a fresh, already-refreshed bearer (credential-blind boundary, PUB-5).
        auth = await self._credential_store.authorize(job.social_account_id)

        # Phase 2b — resolve + materialize the export-delivery artifact (PUB-1).
        async with self._uow:
            asset = await self._uow.media.get_owned(
                job.source_media_asset_id, job.tenant_id, job.requested_by_user_id
            )
        if asset is None:
            return await self._settle_failed(
                job,
                {"code": "artifact_unavailable", "message": "delivery media asset is unavailable"},
            )

        adapter = self._destinations.for_platform(job.platform)

        # Phase 2c (α9.3) — resolve the optional creator-supplied thumbnail (owner-scoped).
        # Best-effort: a missing/soft-deleted thumbnail never blocks the video publish (ADR-0050
        # Invariants 2/7). Resolution happens in the worker; the adapter never reads media_assets.
        thumb_asset: Any = None
        thumb_id = job.content_package.thumbnail_media_asset_id
        if thumb_id is not None:
            async with self._uow:
                thumb_asset = await self._uow.media.get_owned(
                    thumb_id, job.tenant_id, job.requested_by_user_id
                )
            if thumb_asset is None:
                _LOGGER.info(
                    "publish.thumbnail_unavailable",
                    publish_job_id=str(job.id),
                    thumbnail_media_asset_id=str(thumb_id),
                )

        with tempfile.TemporaryDirectory(dir=self._workspace_dir) as tmp:
            artifact_path = await self._materialize(asset, Path(tmp) / "artifact")
            thumbnail: UploadThumbnail | None = None
            if thumb_asset is not None:
                try:
                    thumb_path = await self._materialize(thumb_asset, Path(tmp) / "thumbnail")
                    thumbnail = UploadThumbnail(
                        path=thumb_path,
                        mime_type=thumb_asset.mime_type,
                        size_bytes=thumb_asset.size_bytes,
                    )
                except ObjectStorageError as exc:
                    # Best-effort: proceed without a thumbnail rather than fail the publish.
                    _LOGGER.warning(
                        "publish.thumbnail_materialize_failed",
                        publish_job_id=str(job.id),
                        error=str(exc)[:500],
                    )
            upload = UploadMedia(
                path=artifact_path,
                mime_type=asset.mime_type,
                size_bytes=asset.size_bytes,
                thumbnail=thumbnail,
            )
            result: PublishResult = await adapter.publish(
                package=job.content_package, auth=auth, media=upload
            )

        # Phase 3 — settle succeeded + emit the terminal event, atomically.
        async with self._uow:
            settled = await self._uow.publish_jobs.mark_succeeded(
                job.id,
                platform_post_id=result.external_post_id,
                platform_post_url=result.post_url,
            )
            if settled is None:
                # Canceled mid-publish: do not resurrect it.
                await self._uow.rollback()
                _LOGGER.info("publish.not_running_at_settle", publish_job_id=str(job.id))
                return ProcessPublishJobResult(
                    publish_job_id=job.id, status="noop", reason="not_running"
                )
            await emit_publish_job_succeeded(self._uow, settled)
            await self._uow.commit()

        _LOGGER.info(
            "publish.succeeded",
            publish_job_id=str(job.id),
            project_id=str(job.project_id),
            platform=job.platform,
            platform_post_id=result.external_post_id,
        )
        return ProcessPublishJobResult(
            publish_job_id=job.id,
            status="published",
            platform_post_id=result.external_post_id,
        )

    async def _materialize(self, asset: Any, dest: Path) -> str:
        """Fetch the delivery artifact's bytes into ``dest`` (by its persisted backend)."""
        source = self._storage.resolve(asset.storage_backend)
        if asset.storage_bucket != source.bucket:
            raise ObjectStorageError(
                "delivery artifact is not in the resolved storage location "
                f"({asset.storage_backend}/{asset.storage_bucket})"
            )
        data = await source.get(key=asset.storage_key)
        await asyncio.to_thread(dest.write_bytes, data)
        return str(dest)

    async def _settle_retry_or_fail(
        self, job: PublishJob, *, retryable: bool, code: str, message: str
    ) -> ProcessPublishJobResult:
        error: dict[str, Any] = {"code": code, "message": message[:500]}
        # ``job.attempt`` already reflects this attempt (mark_running bumped it). Retry while
        # attempts remain; the max-th transient failure becomes a permanent failure (DQ6).
        if retryable and job.attempt < job.max_attempts:
            when = datetime.now(UTC) + self._backoff(job.attempt)
            async with self._uow:
                requeued = await self._uow.publish_jobs.reschedule_for_retry(
                    job.id, scheduled_at=when, error=error
                )
                if requeued is not None:
                    # A retry is not terminal — no PublishJobFailed event (PUB-8).
                    await self._uow.commit()
                else:
                    await self._uow.rollback()
            _LOGGER.info(
                "publish.retry_scheduled",
                publish_job_id=str(job.id),
                attempt=job.attempt,
                max_attempts=job.max_attempts,
                scheduled_at=when.isoformat(),
                code=code,
            )
            return ProcessPublishJobResult(publish_job_id=job.id, status="retry", reason=message)
        return await self._settle_failed(job, error)

    async def _settle_failed(
        self, job: PublishJob, error: dict[str, Any]
    ) -> ProcessPublishJobResult:
        async with self._uow:
            failed = await self._uow.publish_jobs.mark_failed(job.id, error=error)
            if failed is not None:
                await emit_publish_job_failed(self._uow, failed, error=error)
                await self._uow.commit()
            else:
                await self._uow.rollback()
        _LOGGER.warning("publish.failed", publish_job_id=str(job.id), error=error.get("message"))
        return ProcessPublishJobResult(
            publish_job_id=job.id, status="failed", reason=str(error.get("message"))
        )

    @staticmethod
    def _backoff(attempt: int) -> timedelta:
        seconds = min(_RETRY_CAP_SECONDS, _RETRY_BASE_SECONDS * (2 ** max(0, attempt - 1)))
        return timedelta(seconds=seconds)
