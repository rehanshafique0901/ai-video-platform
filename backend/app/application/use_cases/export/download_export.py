"""``DownloadExport`` use case (Slice α8.5b.1).

The owner-facing read path that turns a finished ``export_jobs`` row into deliverable bytes.
It resolves + authorizes the export (owner-only, through the project → render job), enforces
that the artifact is ready (``succeeded`` with a live ``output_media_asset_id``, Fork C), and
asks the neutral :class:`IDownloadDelivery` seam how to deliver it (stream now; signed-URL
redirect later — Fork A). Download telemetry is updated **best-effort** and never blocks or
fails the download (Fork B / W8.5b.3).

Invariants:
* **W8.5b.1** — download is observational: it reads a finished delivery ``MediaAsset`` and
  transfers its bytes; its only write is the ``export_jobs`` download accounting.
* **W8.5b.2** — pure transfer: no encoding / transcoding / resize / re-composition happens on
  the download path (the export engine already owns those decisions; reinforces RC5 + W8.5.3).
* **W8.5b.3** — accounting is isolated: a counter failure is telemetry loss, not a user-visible
  failure, and delivery never depends on the counter being written.
"""

from __future__ import annotations

from uuid import UUID

import structlog

from app.application.interfaces.download_delivery import (
    DeliveryDecision,
    DownloadDeliveryError,
    DownloadRequest,
    IDownloadDelivery,
)
from app.application.interfaces.object_storage import ObjectStorageError
from app.application.interfaces.unit_of_work import IUnitOfWork
from app.core.errors import ConflictError, NotFoundError
from app.domain.export.export_status import ExportStatus

_LOGGER = structlog.get_logger(__name__)


class DownloadExport:
    """Deliver a completed export's bytes to its owner (uniform 404 when not visible)."""

    def __init__(self, uow: IUnitOfWork, delivery: IDownloadDelivery) -> None:
        self._uow = uow
        self._delivery = delivery

    async def execute(
        self,
        *,
        project_id: UUID,
        render_job_id: UUID,
        export_job_id: UUID,
        owner_user_id: UUID,
        tenant_id: UUID,
    ) -> DeliveryDecision:
        # Phase 1 — resolve + authorize + guard readiness (read-only, one txn).
        async with self._uow:
            project = await self._uow.projects.get_owned(
                project_id=project_id,
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
            )
            if project is None:
                raise NotFoundError(
                    "export job not found", details={"export_job_id": str(export_job_id)}
                )

            job = await self._uow.export_jobs.get_owned(project_id, export_job_id)
            if job is None or job.render_job_id != render_job_id:
                raise NotFoundError(
                    "export job not found", details={"export_job_id": str(export_job_id)}
                )

            # Fork C — only a succeeded export with a live delivery artifact is downloadable.
            if job.status != ExportStatus.SUCCEEDED.value or job.output_media_asset_id is None:
                raise ConflictError(
                    "export is not ready for download",
                    details={"export_job_id": str(export_job_id), "status": job.status},
                )

            asset = await self._uow.media.get_owned(
                job.output_media_asset_id, tenant_id, owner_user_id
            )
            if asset is None:
                # The row promises an artifact but it is gone/foreign — treat as not found.
                raise NotFoundError(
                    "export artifact not found", details={"export_job_id": str(export_job_id)}
                )

        # Phase 2 — prepare delivery (outside any txn; no byte transfer coupled to the DB).
        request = DownloadRequest(
            storage_backend=asset.storage_backend,
            storage_bucket=asset.storage_bucket,
            storage_key=asset.storage_key,
            media_type=asset.mime_type,
            filename=f"export_{job.quality}_{job.orientation}.{job.format}",
            content_length=asset.size_bytes,
        )
        try:
            decision = await self._delivery.deliver(request)
        except (ObjectStorageError, DownloadDeliveryError) as exc:
            # The artifact's bytes are unavailable (missing object / wrong backend). We have
            # NOT counted a download — a failed transfer is never counted (W8.5b.3).
            raise NotFoundError(
                "export artifact bytes are unavailable",
                details={"export_job_id": str(export_job_id)},
            ) from exc

        # Phase 3 — best-effort accounting (Fork B / W8.5b.3): never fail the download on it.
        await self._record_download_best_effort(export_job_id)

        return decision

    async def _record_download_best_effort(self, export_job_id: UUID) -> None:
        """Bump download telemetry in its own short txn; swallow any failure.

        Accounting is deliberately decoupled from byte transfer and from OCC: a failure here
        is telemetry loss, not a user-visible error, so it is logged and dropped rather than
        propagated (W8.5b.3). No retry — a metrics retry storm is worse than a lost count.
        """
        try:
            async with self._uow:
                await self._uow.export_jobs.record_download(export_job_id)
                await self._uow.commit()
        except Exception:
            _LOGGER.warning(
                "download.accounting_failed", export_job_id=str(export_job_id), exc_info=True
            )
