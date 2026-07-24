"""Unit tests for ``ProcessExportJob`` (Slice α8.5a).

Drives the export use case against in-memory fakes: a queued ``ExportJob`` whose render's
master ``MediaAsset`` lives in a fake object storage, plus a fake ``IExporter`` (no real
FFmpeg). Asserts the master → delivery transform (W8.5.1/W8.5.2/W8.5.3), the
queued→running→succeeded lifecycle, deterministic-key idempotency, the failure path, and the
claim/lock guards.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from app.application.interfaces.exporter import (
    EXPORT_FORMAT_MIME,
    ExportError,
    ExportResult,
    ExportSpec,
    IExporter,
)
from app.application.use_cases.export.process_export_job import ProcessExportJob
from app.domain.export.export_status import ExportStatus
from tests.unit.application.use_cases.export._helpers import ExportFixture

pytestmark = pytest.mark.unit


class FakeExporter(IExporter):
    """Writes deterministic output bytes; records the specs + captured source bytes."""

    def __init__(self, *, output: bytes = b"DELIVERY", error: Exception | None = None) -> None:
        self._output = output
        self._error = error
        self.specs: list[ExportSpec] = []
        self.source_bytes: list[bytes] = []

    async def export(self, spec: ExportSpec) -> ExportResult:
        self.specs.append(spec)
        # Capture while the temp workspace still exists (torn down after export).
        self.source_bytes.append(Path(spec.source_path).read_bytes())
        if self._error is not None:
            raise self._error
        Path(spec.output_path).write_bytes(self._output)
        return ExportResult(
            output_path=spec.output_path,
            size_bytes=len(self._output),
            mime_type=EXPORT_FORMAT_MIME[spec.format],
            duration_seconds=5.0,
            width=1280,
            height=720,
        )


async def _queue_export(
    fx: ExportFixture,
    render_job_id: UUID,
    *,
    format: str = "mp4",
    quality: str = "hd_1080p",
    orientation: str = "horizontal",
) -> UUID:
    job = await fx.exports.add(
        render_job_id=render_job_id,
        requested_by_user_id=fx.owner,
        format=format,
        quality=quality,
        orientation=orientation,
        status=ExportStatus.QUEUED.value,
    )
    return job.id


async def _process(fx: ExportFixture, exporter: FakeExporter, export_job_id: UUID):
    uc = ProcessExportJob(uow=fx.uow, storage=fx.storage_resolver, exporter=exporter)
    return await uc.process(project_id=fx.project_id, export_job_id=export_job_id)


async def test_happy_path_transcodes_and_registers_delivery_asset() -> None:
    fx = ExportFixture()
    render_job_id, master_id = await fx.seed_ready()
    export_id = await _queue_export(fx, render_job_id)
    exporter = FakeExporter()

    result = await _process(fx, exporter, export_id)

    assert result.status == "exported"
    assert result.output_media_asset_id is not None
    # The exporter received the master's bytes as its source (W8.5.2).
    assert exporter.source_bytes == [b"MASTER-BYTES"]
    # Job settled succeeded with the delivery asset + size.
    job = await fx.exports.get_owned(fx.project_id, export_id)
    assert job is not None
    assert job.status == ExportStatus.SUCCEEDED.value
    assert job.output_media_asset_id == result.output_media_asset_id
    assert job.file_size_bytes == len(b"DELIVERY")
    # Delivery MediaAsset registered as a generated video with export lineage (W8.5.3).
    delivery = await fx.media.get_owned(result.output_media_asset_id, fx.tenant, fx.owner)
    assert delivery is not None
    assert delivery.kind == "video"
    assert delivery.source == "generated"
    assert delivery.mime_type == "video/mp4"
    assert delivery.source_metadata["origin"] == "export"
    assert delivery.source_metadata["master_media_asset_id"] == str(master_id)
    assert delivery.source_metadata["format"] == "mp4"
    # Deterministic output key.
    expected_key = f"exports/{fx.tenant}/{fx.project_id}/{export_id}/hd_1080p_horizontal.mp4"
    assert delivery.storage_key == expected_key
    assert expected_key in fx.storage.objects
    # An ExportJobSucceeded event carrying the delivery asset id.
    succeeded = [e for e in fx.uow.outbox.events if e["event_type"] == "ExportJobSucceeded"]
    assert len(succeeded) == 1
    assert succeeded[0]["payload"]["output_media_asset_id"] == str(result.output_media_asset_id)


async def test_gif_export_registers_image_kind() -> None:
    fx = ExportFixture()
    render_job_id, _ = await fx.seed_ready()
    export_id = await _queue_export(fx, render_job_id, format="gif")
    exporter = FakeExporter()

    result = await _process(fx, exporter, export_id)

    assert result.output_media_asset_id is not None
    delivery = await fx.media.get_owned(result.output_media_asset_id, fx.tenant, fx.owner)
    assert delivery is not None
    assert delivery.kind == "image"
    assert delivery.mime_type == "image/gif"


async def test_idempotent_reexport_recovers_existing_delivery_asset() -> None:
    fx = ExportFixture()
    render_job_id, _ = await fx.seed_ready()
    export_id = await _queue_export(fx, render_job_id)
    exporter = FakeExporter()

    first = await _process(fx, exporter, export_id)

    # Re-export (simulated by resetting the job to queued) hits the media storage-coords
    # uniqueness → the existing delivery asset is recovered, never duplicated (W8.5.3).
    fx.exports._jobs[export_id] = replace(
        fx.exports._jobs[export_id],
        status=ExportStatus.QUEUED.value,
        output_media_asset_id=None,
        file_size_bytes=None,
    )
    before = len(fx.media._media)
    second = await _process(fx, exporter, export_id)

    assert second.status == "exported"
    assert second.output_media_asset_id == first.output_media_asset_id
    assert len(fx.media._media) == before


async def test_export_failure_marks_job_failed_and_emits_event() -> None:
    fx = ExportFixture()
    render_job_id, _ = await fx.seed_ready()
    export_id = await _queue_export(fx, render_job_id)
    exporter = FakeExporter(error=ExportError("ffmpeg blew up"))

    result = await _process(fx, exporter, export_id)

    assert result.status == "failed"
    job = await fx.exports.get_owned(fx.project_id, export_id)
    assert job is not None
    assert job.status == ExportStatus.FAILED.value
    assert job.finished_at is not None
    types = [e["event_type"] for e in fx.uow.outbox.events]
    assert "ExportJobFailed" in types


async def test_missing_master_fails_the_job() -> None:
    fx = ExportFixture()
    render_job_id, master_id = await fx.seed_ready()
    export_id = await _queue_export(fx, render_job_id)
    # Master vanished between create and process.
    fx.media._media.pop(master_id, None)
    exporter = FakeExporter()

    result = await _process(fx, exporter, export_id)

    assert result.status == "failed"
    job = await fx.exports.get_owned(fx.project_id, export_id)
    assert job is not None and job.status == ExportStatus.FAILED.value


async def test_non_queued_job_is_noop() -> None:
    fx = ExportFixture()
    render_job_id, _ = await fx.seed_ready()
    export_id = await _queue_export(fx, render_job_id)
    # Drive it to succeeded first.
    await fx.exports.mark_running(export_id)
    await fx.exports.mark_succeeded(export_id, output_media_asset_id=uuid4(), file_size_bytes=10)
    exporter = FakeExporter()

    result = await _process(fx, exporter, export_id)

    assert result.status == "noop"
    assert result.reason == "not_queued"
    assert exporter.specs == []


async def test_locked_job_is_skipped() -> None:
    fx = ExportFixture()
    render_job_id, _ = await fx.seed_ready()
    export_id = await _queue_export(fx, render_job_id)
    async with fx.uow:
        await fx.uow.locks.acquire(
            key=f"export_job:{export_id}", owner="someone-else", lease=timedelta(seconds=60)
        )
        await fx.uow.commit()
    exporter = FakeExporter()

    result = await _process(fx, exporter, export_id)

    assert result.status == "skipped"
    assert result.reason == "locked"
    assert exporter.specs == []
