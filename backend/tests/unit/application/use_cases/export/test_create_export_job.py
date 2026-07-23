"""Unit tests for ``CreateExportJob`` (Slice α8.5a).

Drives the create use case against in-memory fakes: an owned project with a **succeeded**
render job + master ``MediaAsset``. Asserts the ownership gate, the master-readiness check,
the same-orientation guard (Fork F), idempotent replay (Fork E), and that an
``ExportJobCreated`` event is emitted atomically (W8.5.1/W8.5.2).
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.application.use_cases.export._events import EVENT_EXPORT_JOB_CREATED
from app.application.use_cases.export.create_export_job import CreateExportJob
from app.core.errors import NotFoundError, ValidationFailedError
from app.domain.export.export_status import ExportStatus
from tests.unit.application.use_cases.export._helpers import ExportFixture

pytestmark = pytest.mark.unit


async def test_create_queues_export_and_emits_event() -> None:
    fx = ExportFixture()
    render_job_id, _ = await fx.seed_ready(width=1920, height=1080)
    uc = CreateExportJob(uow=fx.uow)

    result = await uc.execute(
        project_id=fx.project_id,
        render_job_id=render_job_id,
        owner_user_id=fx.owner,
        tenant_id=fx.tenant,
        format="mp4",
        quality="hd_1080p",
        orientation="horizontal",
    )

    assert result.created is True
    assert result.job.status == ExportStatus.QUEUED.value
    assert result.job.render_job_id == render_job_id
    assert result.job.requested_by_user_id == fx.owner
    assert result.job.format == "mp4"
    created = [e for e in fx.uow.outbox.events if e["event_type"] == EVENT_EXPORT_JOB_CREATED]
    assert len(created) == 1
    assert created[0]["payload"]["export_job_id"] == str(result.job.id)
    assert fx.uow.commits == 1


async def test_repeat_request_is_idempotent_replay() -> None:
    fx = ExportFixture()
    render_job_id, _ = await fx.seed_ready()
    uc = CreateExportJob(uow=fx.uow)
    kwargs = {
        "project_id": fx.project_id,
        "render_job_id": render_job_id,
        "owner_user_id": fx.owner,
        "tenant_id": fx.tenant,
        "format": "mp4",
        "quality": "hd_1080p",
        "orientation": "horizontal",
    }

    first = await uc.execute(**kwargs)
    second = await uc.execute(**kwargs)

    assert first.created is True
    assert second.created is False
    assert second.job.id == first.job.id
    # Only one create event / one insert.
    created = [e for e in fx.uow.outbox.events if e["event_type"] == EVENT_EXPORT_JOB_CREATED]
    assert len(created) == 1


async def test_distinct_encoding_is_a_new_export() -> None:
    fx = ExportFixture()
    render_job_id, _ = await fx.seed_ready()
    uc = CreateExportJob(uow=fx.uow)
    base = {
        "project_id": fx.project_id,
        "render_job_id": render_job_id,
        "owner_user_id": fx.owner,
        "tenant_id": fx.tenant,
        "orientation": "horizontal",
    }

    a = await uc.execute(format="mp4", quality="hd_1080p", **base)
    b = await uc.execute(format="webm", quality="hd_1080p", **base)

    assert a.created is True
    assert b.created is True
    assert a.job.id != b.job.id


async def test_unknown_project_is_not_found() -> None:
    fx = ExportFixture()
    render_job_id, _ = await fx.seed_ready()
    uc = CreateExportJob(uow=fx.uow)

    with pytest.raises(NotFoundError):
        await uc.execute(
            project_id=uuid4(),
            render_job_id=render_job_id,
            owner_user_id=fx.owner,
            tenant_id=fx.tenant,
            format="mp4",
            quality="hd_1080p",
            orientation="horizontal",
        )


async def test_unknown_render_job_is_not_found() -> None:
    fx = ExportFixture()
    uc = CreateExportJob(uow=fx.uow)

    with pytest.raises(NotFoundError):
        await uc.execute(
            project_id=fx.project_id,
            render_job_id=uuid4(),
            owner_user_id=fx.owner,
            tenant_id=fx.tenant,
            format="mp4",
            quality="hd_1080p",
            orientation="horizontal",
        )


async def test_render_not_succeeded_is_unprocessable() -> None:
    fx = ExportFixture()
    # A queued (not succeeded) render job has no master to export.
    job = await fx.render_jobs.add(
        project_id=fx.project_id,
        timeline_id=uuid4(),
        pipeline="ffmpeg",
        pipeline_version="0.0.0",
        queue="normal",
        priority=0,
        status="queued",
        idempotency_key=None,
    )
    uc = CreateExportJob(uow=fx.uow)

    with pytest.raises(ValidationFailedError):
        await uc.execute(
            project_id=fx.project_id,
            render_job_id=job.id,
            owner_user_id=fx.owner,
            tenant_id=fx.tenant,
            format="mp4",
            quality="hd_1080p",
            orientation="horizontal",
        )


async def test_cross_orientation_request_is_rejected() -> None:
    fx = ExportFixture()
    # Horizontal master (1920x1080); a vertical export changes presentation → 422 (Fork F).
    render_job_id, _ = await fx.seed_ready(width=1920, height=1080)
    uc = CreateExportJob(uow=fx.uow)

    with pytest.raises(ValidationFailedError):
        await uc.execute(
            project_id=fx.project_id,
            render_job_id=render_job_id,
            owner_user_id=fx.owner,
            tenant_id=fx.tenant,
            format="mp4",
            quality="hd_1080p",
            orientation="vertical",
        )


async def test_same_orientation_vertical_master_allows_vertical() -> None:
    fx = ExportFixture()
    render_job_id, _ = await fx.seed_ready(width=1080, height=1920)
    uc = CreateExportJob(uow=fx.uow)

    result = await uc.execute(
        project_id=fx.project_id,
        render_job_id=render_job_id,
        owner_user_id=fx.owner,
        tenant_id=fx.tenant,
        format="mp4",
        quality="hd_1080p",
        orientation="vertical",
    )
    assert result.created is True


async def test_unknown_master_dimensions_is_rejected() -> None:
    fx = ExportFixture()
    render_job_id, _ = await fx.seed_ready(width=None, height=None)
    uc = CreateExportJob(uow=fx.uow)

    with pytest.raises(ValidationFailedError):
        await uc.execute(
            project_id=fx.project_id,
            render_job_id=render_job_id,
            owner_user_id=fx.owner,
            tenant_id=fx.tenant,
            format="mp4",
            quality="hd_1080p",
            orientation="horizontal",
        )


@pytest.mark.parametrize(
    ("format", "quality", "orientation"),
    [("avi", "hd_1080p", "horizontal"), ("mp4", "8k", "horizontal"), ("mp4", "hd_1080p", "tall")],
)
async def test_invalid_enum_values_are_rejected(
    format: str, quality: str, orientation: str
) -> None:
    fx = ExportFixture()
    render_job_id, _ = await fx.seed_ready()
    uc = CreateExportJob(uow=fx.uow)

    with pytest.raises(ValidationFailedError):
        await uc.execute(
            project_id=fx.project_id,
            render_job_id=render_job_id,
            owner_user_id=fx.owner,
            tenant_id=fx.tenant,
            format=format,
            quality=quality,
            orientation=orientation,
        )
