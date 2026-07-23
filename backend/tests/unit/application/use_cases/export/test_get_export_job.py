"""Unit tests for ``GetExportJob`` (Slice α8.5a).

Layered ownership: the project gate first, then the export must belong to that project's
addressed render job. Missing / foreign exports yield a uniform ``404``.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.application.use_cases.export.get_export_job import GetExportJob
from app.core.errors import NotFoundError
from app.domain.export.export_status import ExportStatus
from tests.unit.application.use_cases.export._helpers import ExportFixture

pytestmark = pytest.mark.unit


async def _queue(fx: ExportFixture, render_job_id):
    return await fx.exports.add(
        render_job_id=render_job_id,
        requested_by_user_id=fx.owner,
        format="mp4",
        quality="hd_1080p",
        orientation="horizontal",
        status=ExportStatus.QUEUED.value,
    )


async def test_get_returns_owned_export() -> None:
    fx = ExportFixture()
    render_job_id, _ = await fx.seed_ready()
    job = await _queue(fx, render_job_id)
    uc = GetExportJob(uow=fx.uow)

    got = await uc.execute(
        project_id=fx.project_id,
        render_job_id=render_job_id,
        export_job_id=job.id,
        owner_user_id=fx.owner,
        tenant_id=fx.tenant,
    )
    assert got.id == job.id


async def test_get_foreign_user_is_not_found() -> None:
    fx = ExportFixture()
    render_job_id, _ = await fx.seed_ready()
    job = await _queue(fx, render_job_id)
    uc = GetExportJob(uow=fx.uow)

    with pytest.raises(NotFoundError):
        await uc.execute(
            project_id=fx.project_id,
            render_job_id=render_job_id,
            export_job_id=job.id,
            owner_user_id=uuid4(),
            tenant_id=uuid4(),
        )


async def test_get_wrong_render_job_is_not_found() -> None:
    fx = ExportFixture()
    render_job_id, _ = await fx.seed_ready()
    job = await _queue(fx, render_job_id)
    uc = GetExportJob(uow=fx.uow)

    with pytest.raises(NotFoundError):
        await uc.execute(
            project_id=fx.project_id,
            render_job_id=uuid4(),
            export_job_id=job.id,
            owner_user_id=fx.owner,
            tenant_id=fx.tenant,
        )


async def test_get_missing_export_is_not_found() -> None:
    fx = ExportFixture()
    render_job_id, _ = await fx.seed_ready()
    uc = GetExportJob(uow=fx.uow)

    with pytest.raises(NotFoundError):
        await uc.execute(
            project_id=fx.project_id,
            render_job_id=render_job_id,
            export_job_id=uuid4(),
            owner_user_id=fx.owner,
            tenant_id=fx.tenant,
        )
