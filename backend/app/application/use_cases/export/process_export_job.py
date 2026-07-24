"""``ProcessExportJob`` — transcode one queued export into a delivery MediaAsset (α8.5a).

The worker body behind :class:`ExportWorker`. For a single ``queued`` export job it:

1. acquires the ``export_job:<id>`` lease (exactly-once, mirrors the render worker);
2. claims the job with a ``queued`` → ``running`` CAS;
3. resolves the **master** ``MediaAsset`` — the referenced render's ``output_media_asset_id``
   (Fork D: the *only* legal source; never Timeline / provider / intermediate artifacts);
4. **materializes** the master bytes from ``IObjectStorage`` to a temp workspace;
5. transcodes via the neutral ``IExporter`` (FFmpeg in prod, a fake in tests) into the
   requested ``(format, quality)`` delivery encoding — same orientation, no reframe;
6. stores the output under a **deterministic key** and registers a delivery ``MediaAsset``;
7. settles the job ``succeeded`` (with ``output_media_asset_id`` + ``file_size_bytes``) — or
   ``failed``.

Invariants:
* **W8.5.1** — export is downstream-only: it never recomposes, mutates, or re-renders the
  master. It touches only ``export_jobs`` lifecycle fields, reads the master ``MediaAsset``,
  creates a delivery ``MediaAsset``, storage, and export events.
* **W8.5.2** — consumes only a ``MediaAsset`` + the request params. Never Timeline, provider
  outputs, checkpoints, request/job IDs, or webhooks.
* **W8.5.3** — the rendered master is canonical; the delivery artifact is replaceable. The
  deterministic output key means a re-export writes identical coordinates and is recovered to
  the existing asset (idempotent, ConflictError == success).

Transcode + all file I/O happen **outside** any DB transaction (no lock held across CPU work).
"""

from __future__ import annotations

import asyncio
import hashlib
import tempfile
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import structlog

from app.application.interfaces.exporter import (
    EXPORT_FORMAT_KIND,
    EXPORT_FORMAT_MIME,
    ExportError,
    ExportResult,
    ExportSpec,
    IExporter,
)
from app.application.interfaces.object_storage import ObjectStorageError
from app.application.interfaces.storage_resolver import IStorageResolver
from app.application.interfaces.unit_of_work import IUnitOfWork
from app.application.use_cases.export._events import (
    emit_export_job_failed,
    emit_export_job_succeeded,
)
from app.core.errors import ConflictError
from app.domain.export.export_status import ExportStatus
from app.domain.render.render_status import RenderStatus

_LOGGER = structlog.get_logger(__name__)

_DEFAULT_LEASE = timedelta(seconds=900)


@dataclass(frozen=True, slots=True)
class ProcessExportJobResult:
    """Outcome of one export pass over a single job."""

    export_job_id: UUID
    status: str  # "exported" | "failed" | "skipped" | "noop"
    output_media_asset_id: UUID | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class _ResolvedMaster:
    """The master render's stored-object coordinates (the export source, Fork D)."""

    media_asset_id: UUID
    storage_backend: str
    storage_bucket: str
    storage_key: str


class ProcessExportJob:
    """Export a single ``queued`` job under its own lease (master → delivery MediaAsset)."""

    def __init__(
        self,
        uow: IUnitOfWork,
        storage: IStorageResolver,
        exporter: IExporter,
        *,
        workspace_dir: str | None = None,
        owner: str | None = None,
        lease: timedelta = _DEFAULT_LEASE,
    ) -> None:
        self._uow = uow
        self._storage = storage
        self._exporter = exporter
        self._workspace_dir = workspace_dir
        self._owner = owner or f"export-worker:{uuid4()}"
        self._lease = lease

    async def process(self, *, project_id: UUID, export_job_id: UUID) -> ProcessExportJobResult:
        lock_key = f"export_job:{export_job_id}"
        async with self._uow:
            lease = await self._uow.locks.acquire(
                key=lock_key, owner=self._owner, lease=self._lease
            )
            await self._uow.commit()
        if lease is None:
            _LOGGER.info("export.locked", export_job_id=str(export_job_id))
            return ProcessExportJobResult(
                export_job_id=export_job_id, status="skipped", reason="locked"
            )

        try:
            # Phase 1 — claim (queued → running CAS) + resolve ownership/master.
            async with self._uow:
                job = await self._uow.export_jobs.get_owned(project_id, export_job_id)
                if job is None or job.status != ExportStatus.QUEUED.value:
                    return ProcessExportJobResult(
                        export_job_id=export_job_id, status="noop", reason="not_queued"
                    )
                render_job = await self._uow.render_jobs.get_owned(project_id, job.render_job_id)
                if (
                    render_job is None
                    or render_job.status != RenderStatus.SUCCEEDED.value
                    or render_job.output_media_asset_id is None
                ):
                    # The master vanished / isn't complete — claim then fail deterministically.
                    claimed = await self._uow.export_jobs.mark_running(export_job_id)
                    await self._uow.commit()
                    if claimed is None:
                        return ProcessExportJobResult(
                            export_job_id=export_job_id, status="skipped", reason="claim_lost"
                        )
                    return await self._settle_failed(
                        export_job_id, ExportError("render master is not available to export")
                    )
                ownership = await self._uow.projects.get_ownership(project_id)
                if ownership is None:
                    return ProcessExportJobResult(
                        export_job_id=export_job_id, status="noop", reason="no_ownership"
                    )
                master_asset_id = render_job.output_media_asset_id
                claimed = await self._uow.export_jobs.mark_running(export_job_id)
                if claimed is None:
                    await self._uow.rollback()
                    return ProcessExportJobResult(
                        export_job_id=export_job_id, status="skipped", reason="claim_lost"
                    )
                await self._uow.commit()

            tenant_id, owner_user_id = ownership

            try:
                return await self._export_and_settle(
                    project_id=project_id,
                    export_job_id=export_job_id,
                    master_asset_id=master_asset_id,
                    tenant_id=tenant_id,
                    owner_user_id=owner_user_id,
                    format=job.format,
                    quality=job.quality,
                    orientation=job.orientation,
                )
            except (ExportError, ObjectStorageError) as exc:
                return await self._settle_failed(export_job_id, exc)
        finally:
            async with self._uow:
                await self._uow.locks.release(lease)
                await self._uow.commit()

    async def _export_and_settle(
        self,
        *,
        project_id: UUID,
        export_job_id: UUID,
        master_asset_id: UUID,
        tenant_id: UUID,
        owner_user_id: UUID,
        format: str,
        quality: str,
        orientation: str,
    ) -> ProcessExportJobResult:
        # Phase 2a — resolve the master's storage coordinates (Fork D: master only).
        master = await self._resolve_master(
            master_asset_id=master_asset_id, tenant_id=tenant_id, owner_user_id=owner_user_id
        )
        if master is None:
            raise ExportError("render master media asset is unavailable")

        # Phase 2b — materialize + transcode + store, ALL outside any DB transaction.
        with tempfile.TemporaryDirectory(dir=self._workspace_dir) as tmp:
            tmp_path = Path(tmp)
            src = await self._materialize(master, tmp_path / "master")
            out_path = tmp_path / f"out.{format}"
            result: ExportResult = await self._exporter.export(
                ExportSpec(
                    source_path=src,
                    output_path=str(out_path),
                    format=format,
                    quality=quality,
                    orientation=orientation,
                )
            )
            output_bytes = await asyncio.to_thread(Path(result.output_path).read_bytes)

        checksum = hashlib.sha256(output_bytes).digest()
        output_key = (
            f"exports/{tenant_id}/{project_id}/{export_job_id}/" f"{quality}_{orientation}.{format}"
        )
        stored = await self._storage.active().put(
            key=output_key, data=output_bytes, content_type=result.mime_type
        )

        # Phase 3 — register the delivery MediaAsset + settle the job succeeded.
        async with self._uow:
            output_media_asset_id = await self._register_output(
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                project_id=project_id,
                export_job_id=export_job_id,
                master_asset_id=master_asset_id,
                stored_backend=stored.backend,
                stored_bucket=stored.bucket,
                stored_key=stored.key,
                size_bytes=len(output_bytes),
                checksum=checksum,
                result=result,
                format=format,
                quality=quality,
                orientation=orientation,
            )
            settled = await self._uow.export_jobs.mark_succeeded(
                export_job_id,
                output_media_asset_id=output_media_asset_id,
                file_size_bytes=len(output_bytes),
            )
            if settled is None:
                # Canceled mid-export: the delivery asset is registered but the job is no
                # longer running. Do not resurrect it — commit the asset only.
                await self._uow.commit()
                _LOGGER.info(
                    "export.not_running_at_settle",
                    export_job_id=str(export_job_id),
                    output_media_asset_id=str(output_media_asset_id),
                )
                return ProcessExportJobResult(
                    export_job_id=export_job_id,
                    status="noop",
                    reason="not_running",
                    output_media_asset_id=output_media_asset_id,
                )
            await emit_export_job_succeeded(self._uow, settled)
            await self._uow.commit()

        _LOGGER.info(
            "export.succeeded",
            export_job_id=str(export_job_id),
            project_id=str(project_id),
            output_media_asset_id=str(output_media_asset_id),
            format=format,
            quality=quality,
            orientation=orientation,
        )
        return ProcessExportJobResult(
            export_job_id=export_job_id,
            status="exported",
            output_media_asset_id=output_media_asset_id,
        )

    async def _resolve_master(
        self, *, master_asset_id: UUID, tenant_id: UUID, owner_user_id: UUID
    ) -> _ResolvedMaster | None:
        async with self._uow:
            asset = await self._uow.media.get_owned(master_asset_id, tenant_id, owner_user_id)
            if asset is None:
                return None
            return _ResolvedMaster(
                media_asset_id=asset.id,
                storage_backend=asset.storage_backend,
                storage_bucket=asset.storage_bucket,
                storage_key=asset.storage_key,
            )

    async def _materialize(self, master: _ResolvedMaster, dest: Path) -> str:
        """Fetch the master's bytes from storage into ``dest``; return its path.

        Reads always resolve by the master's *persisted* backend (W8.5b.4 / W8.5b.5), never the
        active write backend — an existing master stays readable wherever it actually lives.
        Enforces that the source lives in the resolved storage location (W8.5.2: only the master
        ``MediaAsset`` coordinates are consumed, never provider URLs).
        """
        source = self._storage.resolve(master.storage_backend)
        if master.storage_bucket != source.bucket:
            raise ExportError(
                "master media is not in the export storage location "
                f"({master.storage_backend}/{master.storage_bucket})"
            )
        data = await source.get(key=master.storage_key)
        await asyncio.to_thread(dest.write_bytes, data)
        return str(dest)

    async def _register_output(
        self,
        *,
        tenant_id: UUID,
        owner_user_id: UUID,
        project_id: UUID,
        export_job_id: UUID,
        master_asset_id: UUID,
        stored_backend: str,
        stored_bucket: str,
        stored_key: str,
        size_bytes: int,
        checksum: bytes,
        result: ExportResult,
        format: str,
        quality: str,
        orientation: str,
    ) -> UUID:
        """Register the delivery ``MediaAsset``; recover the existing one on conflict (W8.5.3).

        ``source='generated'`` (no dedicated ``export`` ``media_source`` value — zero
        migration); ``source_metadata.origin='export'`` records the delivery lineage back to
        the canonical master.
        """
        try:
            asset = await self._uow.media.add(
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                kind=EXPORT_FORMAT_KIND[format],
                source="generated",
                storage_backend=stored_backend,
                storage_bucket=stored_bucket,
                storage_key=stored_key,
                mime_type=EXPORT_FORMAT_MIME[format],
                size_bytes=size_bytes,
                checksum_sha256=checksum,
                project_id=project_id,
                scene_id=None,
                prompt_id=None,
                model_id=None,
                provider=None,
                width=result.width,
                height=result.height,
                duration_seconds=result.duration_seconds,
                source_metadata={
                    "origin": "export",
                    "export_job_id": str(export_job_id),
                    "master_media_asset_id": str(master_asset_id),
                    "format": format,
                    "quality": quality,
                    "orientation": orientation,
                },
            )
            return asset.id
        except ConflictError:
            existing = await self._uow.media.get_by_storage_coords(
                storage_backend=stored_backend,
                storage_bucket=stored_bucket,
                storage_key=stored_key,
            )
            if existing is None:  # pragma: no cover - constraint says it must exist
                raise
            return existing.id

    async def _settle_failed(self, export_job_id: UUID, exc: Exception) -> ProcessExportJobResult:
        message = str(exc)[:500]
        error: dict[str, object] = {"code": "export_failed", "message": message}
        async with self._uow:
            failed = await self._uow.export_jobs.mark_failed(export_job_id)
            if failed is not None:
                await emit_export_job_failed(self._uow, failed, error=error)
                await self._uow.commit()
            else:
                await self._uow.rollback()
        _LOGGER.warning("export.failed", export_job_id=str(export_job_id), error=message)
        return ProcessExportJobResult(export_job_id=export_job_id, status="failed", reason=message)
