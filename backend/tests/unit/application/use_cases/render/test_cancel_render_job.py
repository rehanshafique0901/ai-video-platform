"""Unit tests for ``CancelRenderJob`` (Slice α7.1 — Q3/D3.5–D3.6 state machine).

Coverage map:

* U1 — ``queued`` → ``canceled``: state changes, version bumps, commits once,
  emits RenderJobCanceled, canceled=True.
* U2 — ``running`` → ``canceled`` (best-effort): same as U1.
* U3 — ``canceled`` → cancel: idempotent 200 no-op (canceled=False, no event,
  no commit, no version bump).
* U4 — ``succeeded`` → cancel: ``ConflictError`` (409), no commit / event.
* U5 — ``failed`` → cancel: ``ConflictError`` (409).
* U6 — stale version on a cancelable job → ``VersionConflictError`` (412), no
  state change / commit / event.
* U7 — unknown job → ``NotFoundError`` (404).
* U8 — unknown project → ``NotFoundError`` (404).
* U9 — canceled event shape (aggregate/event/payload=canceled state).
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.application.use_cases.render._events import (
    AGGREGATE_TYPE,
    EVENT_RENDER_JOB_CANCELED,
)
from app.application.use_cases.render.cancel_render_job import CancelRenderJob
from app.core.errors import ConflictError, NotFoundError, VersionConflictError
from app.domain.render.render_status import RenderStatus
from tests.unit.application.use_cases.render._helpers import build_env, seed_render_job


@pytest.mark.unit
async def test_u1_queued_cancels() -> None:
    env = build_env()
    job = await seed_render_job(env, status=RenderStatus.QUEUED.value)
    uc = CancelRenderJob(uow=env.uow)

    result = await uc.execute(
        project_id=env.project_id,
        render_job_id=job.id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
        expected_version=job.version,
    )
    assert result.canceled is True
    assert result.job.status == RenderStatus.CANCELED.value
    assert result.job.version == job.version + 1
    assert env.uow.commits == 1
    assert len(env.outbox.events) == 1


@pytest.mark.unit
async def test_u2_running_cancels() -> None:
    env = build_env()
    job = await seed_render_job(env, status=RenderStatus.RUNNING.value)
    uc = CancelRenderJob(uow=env.uow)

    result = await uc.execute(
        project_id=env.project_id,
        render_job_id=job.id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
        expected_version=job.version,
    )
    assert result.canceled is True
    assert result.job.status == RenderStatus.CANCELED.value


@pytest.mark.unit
async def test_u3_already_canceled_is_noop() -> None:
    env = build_env()
    job = await seed_render_job(env, status=RenderStatus.CANCELED.value)
    uc = CancelRenderJob(uow=env.uow)

    result = await uc.execute(
        project_id=env.project_id,
        render_job_id=job.id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
        expected_version=job.version,
    )
    assert result.canceled is False
    assert result.job.status == RenderStatus.CANCELED.value
    assert result.job.version == job.version  # no bump
    assert env.uow.commits == 0
    assert env.outbox.events == []


@pytest.mark.unit
@pytest.mark.parametrize("terminal", [RenderStatus.SUCCEEDED, RenderStatus.FAILED])
async def test_u4_u5_terminal_states_conflict(terminal: RenderStatus) -> None:
    env = build_env()
    job = await seed_render_job(env, status=terminal.value)
    uc = CancelRenderJob(uow=env.uow)

    with pytest.raises(ConflictError):
        await uc.execute(
            project_id=env.project_id,
            render_job_id=job.id,
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
            expected_version=job.version,
        )
    assert env.uow.commits == 0
    assert env.outbox.events == []


@pytest.mark.unit
async def test_u6_stale_version_on_cancelable_raises_412() -> None:
    env = build_env()
    job = await seed_render_job(env, status=RenderStatus.QUEUED.value)
    uc = CancelRenderJob(uow=env.uow)

    with pytest.raises(VersionConflictError):
        await uc.execute(
            project_id=env.project_id,
            render_job_id=job.id,
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
            expected_version=job.version + 99,
        )
    assert env.render_jobs._jobs[job.id].status == RenderStatus.QUEUED.value
    assert env.uow.commits == 0
    assert env.outbox.events == []


@pytest.mark.unit
async def test_u7_unknown_job_raises_404() -> None:
    env = build_env()
    uc = CancelRenderJob(uow=env.uow)

    with pytest.raises(NotFoundError):
        await uc.execute(
            project_id=env.project_id,
            render_job_id=uuid4(),
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
            expected_version=1,
        )


@pytest.mark.unit
async def test_u8_unknown_project_raises_404() -> None:
    env = build_env()
    job = await seed_render_job(env)
    uc = CancelRenderJob(uow=env.uow)

    with pytest.raises(NotFoundError):
        await uc.execute(
            project_id=uuid4(),
            render_job_id=job.id,
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
            expected_version=job.version,
        )


@pytest.mark.unit
async def test_u9_canceled_event_shape() -> None:
    env = build_env()
    job = await seed_render_job(env, status=RenderStatus.QUEUED.value)
    uc = CancelRenderJob(uow=env.uow)

    result = await uc.execute(
        project_id=env.project_id,
        render_job_id=job.id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
        expected_version=job.version,
    )
    ev = env.outbox.events[0]
    assert ev["aggregate_type"] == AGGREGATE_TYPE
    assert ev["event_type"] == EVENT_RENDER_JOB_CANCELED
    assert ev["aggregate_id"] == result.job.id
    assert ev["payload"]["render_job_id"] == str(result.job.id)
    assert ev["payload"]["status"] == RenderStatus.CANCELED.value
    assert ev["metadata"] == {"actor_user_id": str(env.owner_user_id)}
