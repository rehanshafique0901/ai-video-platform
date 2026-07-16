"""Unit tests for ``CreateWorkflowRun`` (Slice α7.2).

Coverage map (α7.2 pre-flight / Q3, Q7, Q8 / D9):

* U1 — happy path: queues a run (status=queued), seeds the definition's steps
  (pending, in order), commits once, emits WorkflowRunCreated, created=True.
* U2 — unknown ``workflow_key@version`` → ``ValidationFailedError`` (422), no
  write / commit / event (definition resolved before any DB work).
* U3 — unknown project → ``NotFoundError`` (404), no write / commit / event.
* U4 — idempotent replay: repeat ``idempotency_key`` returns the existing run
  (created=False), mints no new run, no commit, no new event.
* U5 — distinct idempotency keys create distinct runs.
* U6 — WorkflowRunCreated event shape (aggregate_type/event_type/payload).
* U7 — ``input_snapshot`` is persisted on the run.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.application.use_cases.workflow._events import (
    AGGREGATE_TYPE,
    EVENT_WORKFLOW_RUN_CREATED,
)
from app.application.use_cases.workflow.create_workflow_run import CreateWorkflowRun
from app.core.errors import NotFoundError, ValidationFailedError
from app.domain.workflow.registry import NOOP_CHAIN, WORKFLOW_VERSION_1
from tests.unit.application.use_cases.workflow._helpers import build_env, seed_workflow_run


async def _create(uc: CreateWorkflowRun, env, **overrides):  # type: ignore[no-untyped-def]
    kwargs = {
        "project_id": env.project_id,
        "owner_user_id": env.owner_user_id,
        "tenant_id": env.tenant_id,
        "workflow_key": NOOP_CHAIN,
        "workflow_version": WORKFLOW_VERSION_1,
        "input_snapshot": {},
    }
    kwargs.update(overrides)
    return await uc.execute(**kwargs)


@pytest.mark.unit
async def test_u1_happy_path_queues_seeds_steps_and_emits_event() -> None:
    env = build_env()
    uc = CreateWorkflowRun(uow=env.uow)

    result = await _create(uc, env)

    assert result.created is True
    assert result.view.run.status == "queued"
    assert result.view.run.workflow_key == NOOP_CHAIN
    # noop-chain seeds three ordered, pending steps.
    assert [s.step_index for s in result.view.steps] == [0, 1, 2]
    assert [s.step_name for s in result.view.steps] == ["extract", "transform", "summarize"]
    assert all(s.status == "pending" for s in result.view.steps)
    assert result.view.latest_checkpoint is None
    assert env.uow.commits == 1
    assert len(env.outbox.events) == 1


@pytest.mark.unit
async def test_u2_unknown_workflow_raises_422() -> None:
    env = build_env()
    uc = CreateWorkflowRun(uow=env.uow)

    with pytest.raises(ValidationFailedError):
        await _create(uc, env, workflow_key="does-not-exist")

    assert env.uow.commits == 0
    assert env.workflow_runs._runs == {}
    assert env.outbox.events == []


@pytest.mark.unit
async def test_u3_unknown_project_raises_404() -> None:
    env = build_env()
    uc = CreateWorkflowRun(uow=env.uow)

    with pytest.raises(NotFoundError):
        await _create(uc, env, project_id=uuid4())

    assert env.uow.commits == 0
    assert env.workflow_runs._runs == {}
    assert env.outbox.events == []


@pytest.mark.unit
async def test_u4_idempotent_replay_returns_existing() -> None:
    env = build_env()
    existing = await seed_workflow_run(env, idempotency_key="abc123")
    uc = CreateWorkflowRun(uow=env.uow)

    result = await _create(uc, env, idempotency_key="abc123")

    assert result.created is False
    assert result.view.run.id == existing.id
    assert len(env.workflow_runs._runs) == 1  # no new run
    assert env.uow.commits == 0  # replay does not commit
    assert env.outbox.events == []  # no new event


@pytest.mark.unit
async def test_u5_distinct_keys_create_distinct_runs() -> None:
    env = build_env()
    uc = CreateWorkflowRun(uow=env.uow)

    r1 = await _create(uc, env, idempotency_key="k1")
    r2 = await _create(uc, env, idempotency_key="k2")

    assert r1.created is True and r2.created is True
    assert r1.view.run.id != r2.view.run.id
    assert len(env.workflow_runs._runs) == 2


@pytest.mark.unit
async def test_u6_created_event_shape() -> None:
    env = build_env()
    uc = CreateWorkflowRun(uow=env.uow)

    result = await _create(uc, env)

    ev = env.outbox.events[0]
    assert ev["aggregate_type"] == AGGREGATE_TYPE
    assert ev["event_type"] == EVENT_WORKFLOW_RUN_CREATED
    assert ev["aggregate_id"] == result.view.run.id
    assert ev["payload"]["workflow_run_id"] == str(result.view.run.id)
    assert ev["payload"]["project_id"] == str(env.project_id)
    assert ev["payload"]["workflow_key"] == NOOP_CHAIN
    assert ev["payload"]["status"] == "queued"
    assert ev["metadata"] == {"actor_user_id": str(env.owner_user_id)}


@pytest.mark.unit
async def test_u7_input_snapshot_persists() -> None:
    env = build_env()
    uc = CreateWorkflowRun(uow=env.uow)

    result = await _create(uc, env, input_snapshot={"topic": "cats", "seconds": 30})

    assert result.view.run.input_snapshot == {"topic": "cats", "seconds": 30}
