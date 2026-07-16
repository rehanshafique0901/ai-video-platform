"""Unit tests for ``ListRenderJobs`` (Slice α7.1).

Coverage map:

* U1 — empty project → ``[]``.
* U2 — newest-first ordering (insertion-ordinal DESC, mirroring created_at DESC).
* U3 — ``status`` filter narrows the result.
* U4 — unknown project → ``NotFoundError`` (404).
* U5 — a job under a DIFFERENT project is not listed (project scoping).
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.application.use_cases.render.list_render_jobs import ListRenderJobs
from app.core.errors import NotFoundError
from app.domain.render.render_status import RenderStatus
from tests.unit.application.use_cases.render._helpers import build_env, seed_render_job


@pytest.mark.unit
async def test_u1_empty_returns_empty_list() -> None:
    env = build_env()
    uc = ListRenderJobs(uow=env.uow)

    jobs = await uc.execute(
        project_id=env.project_id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
    )
    assert jobs == []


@pytest.mark.unit
async def test_u2_newest_first() -> None:
    env = build_env()
    first = await seed_render_job(env)
    second = await seed_render_job(env)
    uc = ListRenderJobs(uow=env.uow)

    jobs = await uc.execute(
        project_id=env.project_id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
    )
    assert [j.id for j in jobs] == [second.id, first.id]


@pytest.mark.unit
async def test_u3_status_filter() -> None:
    env = build_env()
    await seed_render_job(env, status=RenderStatus.QUEUED.value)
    canceled = await seed_render_job(env, status=RenderStatus.CANCELED.value)
    uc = ListRenderJobs(uow=env.uow)

    jobs = await uc.execute(
        project_id=env.project_id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
        status=RenderStatus.CANCELED.value,
    )
    assert [j.id for j in jobs] == [canceled.id]


@pytest.mark.unit
async def test_u4_unknown_project_raises_404() -> None:
    env = build_env()
    uc = ListRenderJobs(uow=env.uow)

    with pytest.raises(NotFoundError):
        await uc.execute(
            project_id=uuid4(),
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
        )


@pytest.mark.unit
async def test_u5_other_project_job_not_listed() -> None:
    env = build_env()
    await seed_render_job(env)
    # A job inserted directly under a different project id is invisible here.
    await env.render_jobs.add(
        project_id=uuid4(),
        timeline_id=uuid4(),
        pipeline="ffmpeg",
        pipeline_version="0.0.0",
        queue="normal",
        priority=0,
        status=RenderStatus.QUEUED.value,
        idempotency_key=None,
    )
    uc = ListRenderJobs(uow=env.uow)

    jobs = await uc.execute(
        project_id=env.project_id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
    )
    assert len(jobs) == 1
    assert all(j.project_id == env.project_id for j in jobs)
