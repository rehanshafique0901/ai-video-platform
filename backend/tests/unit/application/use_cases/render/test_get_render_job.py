"""Unit tests for ``GetRenderJob`` (Slice α7.1).

Coverage map:

* U1 — happy path: returns the project-scoped job.
* U2 — unknown job id → ``NotFoundError`` (404).
* U3 — unknown project → ``NotFoundError`` (404).
* U4 — a job under a DIFFERENT project → uniform ``NotFoundError`` (404,
  anti-enumeration, D3.3).
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.application.use_cases.render.get_render_job import GetRenderJob
from app.core.errors import NotFoundError
from app.domain.render.render_status import RenderStatus
from tests.unit.application.use_cases.render._helpers import build_env, seed_render_job


@pytest.mark.unit
async def test_u1_happy_path() -> None:
    env = build_env()
    seeded = await seed_render_job(env)
    uc = GetRenderJob(uow=env.uow)

    job = await uc.execute(
        project_id=env.project_id,
        render_job_id=seeded.id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
    )
    assert job.id == seeded.id
    assert job.project_id == env.project_id


@pytest.mark.unit
async def test_u2_unknown_job_raises_404() -> None:
    env = build_env()
    uc = GetRenderJob(uow=env.uow)

    with pytest.raises(NotFoundError):
        await uc.execute(
            project_id=env.project_id,
            render_job_id=uuid4(),
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
        )


@pytest.mark.unit
async def test_u3_unknown_project_raises_404() -> None:
    env = build_env()
    seeded = await seed_render_job(env)
    uc = GetRenderJob(uow=env.uow)

    with pytest.raises(NotFoundError):
        await uc.execute(
            project_id=uuid4(),
            render_job_id=seeded.id,
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
        )


@pytest.mark.unit
async def test_u4_job_under_other_project_is_404() -> None:
    env = build_env()
    # A job that exists but belongs to a different project id.
    other = await env.render_jobs.add(
        project_id=uuid4(),
        timeline_id=uuid4(),
        pipeline="ffmpeg",
        pipeline_version="0.0.0",
        queue="normal",
        priority=0,
        status=RenderStatus.QUEUED.value,
        idempotency_key=None,
    )
    uc = GetRenderJob(uow=env.uow)

    with pytest.raises(NotFoundError):
        await uc.execute(
            project_id=env.project_id,
            render_job_id=other.id,
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
        )
