"""Unit tests for ``CreateRenderJob`` (Slice α7.1).

Coverage map (α7.1 pre-flight / Q1, Q2, Q4, Q6 / D9):

* U1 — happy path: queues a job (status=queued, version=1, progress='0.00'),
  binds the project's timeline server-side, commits once, emits RenderJobCreated.
* U2 — passed pipeline/queue/priority persist on the job.
* U3 — unknown project → ``NotFoundError`` (404), no write / commit / event.
* U4 — project with no timeline → ``ValidationFailedError`` (422).
* U5 — idempotent replay: repeat ``idempotency_key`` returns the existing job
  (created=False), mints no new job, no commit, no new event.
* U6 — distinct idempotency keys create distinct jobs.
* U7 — RenderJobCreated event shape (aggregate_type/event_type/payload).
* U8 — ``render_job.created`` INFO log emitted with the field set.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
import structlog

from app.application.use_cases.render._events import (
    AGGREGATE_TYPE,
    EVENT_RENDER_JOB_CREATED,
)
from app.application.use_cases.render.create_render_job import CreateRenderJob
from app.core.errors import NotFoundError, ValidationFailedError
from tests.unit.application.use_cases.render._helpers import build_env, seed_render_job


async def _create(uc: CreateRenderJob, env, **overrides):  # type: ignore[no-untyped-def]
    kwargs = {
        "project_id": env.project_id,
        "owner_user_id": env.owner_user_id,
        "tenant_id": env.tenant_id,
        "pipeline": "ffmpeg",
        "pipeline_version": "0.0.0",
        "queue": "normal",
        "priority": 0,
    }
    kwargs.update(overrides)
    return await uc.execute(**kwargs)


@pytest.mark.unit
async def test_u1_happy_path_queues_and_emits_event() -> None:
    env = build_env()
    uc = CreateRenderJob(uow=env.uow)

    result = await _create(uc, env)

    assert result.created is True
    assert result.job.status == "queued"
    assert result.job.version == 1
    assert result.job.progress == "0.00"
    assert result.job.timeline_id == env.timeline_id
    assert env.uow.commits == 1
    assert len(env.outbox.events) == 1


@pytest.mark.unit
async def test_u2_passed_fields_persist() -> None:
    env = build_env()
    uc = CreateRenderJob(uow=env.uow)

    result = await _create(
        uc, env, pipeline="remotion", pipeline_version="1.2.3", queue="high", priority=7
    )

    assert result.job.pipeline == "remotion"
    assert result.job.pipeline_version == "1.2.3"
    assert result.job.queue == "high"
    assert result.job.priority == 7


@pytest.mark.unit
async def test_u3_unknown_project_raises_404() -> None:
    env = build_env()
    uc = CreateRenderJob(uow=env.uow)

    with pytest.raises(NotFoundError):
        await _create(uc, env, project_id=uuid4())

    assert env.uow.commits == 0
    assert env.render_jobs._jobs == {}
    assert env.outbox.events == []


@pytest.mark.unit
async def test_u4_no_timeline_raises_422() -> None:
    env = build_env(with_timeline=False)
    uc = CreateRenderJob(uow=env.uow)

    with pytest.raises(ValidationFailedError):
        await _create(uc, env)

    assert env.uow.commits == 0
    assert env.render_jobs._jobs == {}


@pytest.mark.unit
async def test_u5_idempotent_replay_returns_existing() -> None:
    env = build_env()
    existing = await seed_render_job(env, idempotency_key="abc123")
    uc = CreateRenderJob(uow=env.uow)

    result = await _create(uc, env, idempotency_key="abc123")

    assert result.created is False
    assert result.job.id == existing.id
    assert len(env.render_jobs._jobs) == 1  # no new job
    assert env.uow.commits == 0  # replay does not commit
    assert env.outbox.events == []  # no new event


@pytest.mark.unit
async def test_u6_distinct_keys_create_distinct_jobs() -> None:
    env = build_env()
    uc = CreateRenderJob(uow=env.uow)

    r1 = await _create(uc, env, idempotency_key="k1")
    r2 = await _create(uc, env, idempotency_key="k2")

    assert r1.created is True and r2.created is True
    assert r1.job.id != r2.job.id
    assert len(env.render_jobs._jobs) == 2


@pytest.mark.unit
async def test_u7_created_event_shape() -> None:
    env = build_env()
    uc = CreateRenderJob(uow=env.uow)

    result = await _create(uc, env)

    ev = env.outbox.events[0]
    assert ev["aggregate_type"] == AGGREGATE_TYPE
    assert ev["event_type"] == EVENT_RENDER_JOB_CREATED
    assert ev["aggregate_id"] == result.job.id
    assert ev["payload"]["render_job_id"] == str(result.job.id)
    assert ev["payload"]["project_id"] == str(env.project_id)
    assert ev["payload"]["status"] == "queued"
    assert ev["metadata"] == {"actor_user_id": str(env.owner_user_id)}


@pytest.mark.unit
async def test_u8_created_log_emitted() -> None:
    env = build_env()
    uc = CreateRenderJob(uow=env.uow)

    with structlog.testing.capture_logs() as logs:
        result = await _create(uc, env, ip="203.0.113.7")

    events = [e for e in logs if e.get("event") == "render_job.created"]
    assert len(events) == 1
    ev = events[0]
    assert ev["render_job_id"] == str(result.job.id)
    assert ev["project_id"] == str(env.project_id)
    assert ev["queue"] == "normal"
