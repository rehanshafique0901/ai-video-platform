"""Unit tests for ``ExportWorker`` (Slice α8.5a).

The poll ingress drains ``queued`` export jobs FIFO and delegates each to
``ProcessExportJob`` under its own lease. Asserts the scan → claim → settle loop and the
empty-scan no-op.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest

from app.application.interfaces.exporter import (
    EXPORT_FORMAT_MIME,
    ExportResult,
    ExportSpec,
    IExporter,
)
from app.application.use_cases.export.export_worker import ExportWorker
from app.application.use_cases.export.process_export_job import ProcessExportJob
from app.domain.export.export_status import ExportStatus
from tests.unit.application.use_cases.export._helpers import ExportFixture

pytestmark = pytest.mark.unit


class FakeExporter(IExporter):
    async def export(self, spec: ExportSpec) -> ExportResult:
        Path(spec.output_path).write_bytes(b"DELIVERY")
        return ExportResult(
            output_path=spec.output_path,
            size_bytes=8,
            mime_type=EXPORT_FORMAT_MIME[spec.format],
            duration_seconds=5.0,
            width=1280,
            height=720,
        )


async def _queue(fx: ExportFixture, render_job_id: UUID, *, format: str = "mp4") -> UUID:
    job = await fx.exports.add(
        render_job_id=render_job_id,
        requested_by_user_id=fx.owner,
        format=format,
        quality="hd_1080p",
        orientation="horizontal",
        status=ExportStatus.QUEUED.value,
    )
    return job.id


def _worker(fx: ExportFixture) -> ExportWorker:
    process = ProcessExportJob(uow=fx.uow, storage=fx.storage_resolver, exporter=FakeExporter())
    return ExportWorker(uow=fx.uow, process=process, batch_size=10)


async def test_run_once_drains_queued_exports() -> None:
    fx = ExportFixture()
    r1, _ = await fx.seed_ready()
    r2, _ = await fx.seed_ready()
    e1 = await _queue(fx, r1)
    e2 = await _queue(fx, r2, format="webm")

    result = await _worker(fx).run_once()

    assert result.scanned == 2
    assert {o.status for o in result.outcomes} == {"exported"}
    for export_id in (e1, e2):
        job = await fx.exports.get_owned(fx.project_id, export_id)
        assert job is not None and job.status == ExportStatus.SUCCEEDED.value


async def test_run_once_with_no_queued_jobs_is_noop() -> None:
    fx = ExportFixture()
    result = await _worker(fx).run_once()
    assert result.scanned == 0
    assert result.outcomes == []
