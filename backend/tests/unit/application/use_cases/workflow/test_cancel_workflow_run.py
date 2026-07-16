"""Unit tests for ``CancelWorkflowRun`` (Slice α7.2 — D3.7 status-guarded cancel).

Unlike α7.1's RenderJob cancel this is **not** version-fenced (no ``version``
column exists — D3.2), so there is no ``expected_version`` / ``412`` path.

Coverage map:

* U1 — ``queued`` → ``canceled``: state changes, commits once, emits
  WorkflowRunCanceled, canceled=True, ``finished_at`` set.
* U2 — ``running`` → ``canceled``: same as U1.
* U3 — ``canceled`` → cancel: idempotent 200 no-op (canceled=False, no event,
  no commit).
* U4 — ``succeeded`` → cancel: ``ConflictError`` (409), no commit / event.
* U5 — ``failed`` → cancel: ``ConflictError`` (409).
* U6 — unknown run → ``NotFoundError`` (404).
* U7 — unknown project → ``NotFoundError`` (404).
* U8 — WorkflowRunCanceled event shape (aggregate/event/payload=canceled state).
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.application.use_cases.workflow._events import (
    AGGREGATE_TYPE,
    EVENT_WORKFLOW_RUN_CANCELED,
)
from app.application.use_cases.workflow.cancel_workflow_run import CancelWorkflowRun
from app.core.errors import ConflictError, NotFoundError
from app.domain.workflow.workflow_run_status import WorkflowRunStatus
from tests.unit.application.use_cases.workflow._helpers import build_env, seed_workflow_run


async def _cancel(uc: CancelWorkflowRun, env, workflow_run_id, **overrides):  # type: ignore[no-untyped-def]
    kwargs = {
        "project_id": env.project_id,
        "workflow_run_id": workflow_run_id,
        "owner_user_id": env.owner_user_id,
        "tenant_id": env.tenant_id,
    }
    kwargs.update(overrides)
    return await uc.execute(**kwargs)


@pytest.mark.unit
async def test_u1_queued_cancels() -> None:
    env = build_env()
    run = await seed_workflow_run(env, status=WorkflowRunStatus.QUEUED.value)
    uc = CancelWorkflowRun(uow=env.uow)

    result = await _cancel(uc, env, run.id)

    assert result.canceled is True
    assert result.view.run.status == WorkflowRunStatus.CANCELED.value
    assert result.view.run.finished_at is not None
    assert env.uow.commits == 1
    assert len(env.outbox.events) == 1


@pytest.mark.unit
async def test_u2_running_cancels() -> None:
    env = build_env()
    run = await seed_workflow_run(env, status=WorkflowRunStatus.RUNNING.value)
    uc = CancelWorkflowRun(uow=env.uow)

    result = await _cancel(uc, env, run.id)

    assert result.canceled is True
    assert result.view.run.status == WorkflowRunStatus.CANCELED.value


@pytest.mark.unit
async def test_u3_already_canceled_is_noop() -> None:
    env = build_env()
    run = await seed_workflow_run(env, status=WorkflowRunStatus.CANCELED.value)
    uc = CancelWorkflowRun(uow=env.uow)

    result = await _cancel(uc, env, run.id)

    assert result.canceled is False
    assert result.view.run.status == WorkflowRunStatus.CANCELED.value
    assert env.uow.commits == 0
    assert env.outbox.events == []


@pytest.mark.unit
async def test_u4_succeeded_cancel_raises_409() -> None:
    env = build_env()
    run = await seed_workflow_run(env, status=WorkflowRunStatus.SUCCEEDED.value)
    uc = CancelWorkflowRun(uow=env.uow)

    with pytest.raises(ConflictError):
        await _cancel(uc, env, run.id)

    assert env.uow.commits == 0
    assert env.outbox.events == []


@pytest.mark.unit
async def test_u5_failed_cancel_raises_409() -> None:
    env = build_env()
    run = await seed_workflow_run(env, status=WorkflowRunStatus.FAILED.value)
    uc = CancelWorkflowRun(uow=env.uow)

    with pytest.raises(ConflictError):
        await _cancel(uc, env, run.id)


@pytest.mark.unit
async def test_u6_unknown_run_raises_404() -> None:
    env = build_env()
    uc = CancelWorkflowRun(uow=env.uow)

    with pytest.raises(NotFoundError):
        await _cancel(uc, env, uuid4())


@pytest.mark.unit
async def test_u7_unknown_project_raises_404() -> None:
    env = build_env()
    run = await seed_workflow_run(env)
    uc = CancelWorkflowRun(uow=env.uow)

    with pytest.raises(NotFoundError):
        await _cancel(uc, env, run.id, project_id=uuid4())


@pytest.mark.unit
async def test_u8_canceled_event_shape() -> None:
    env = build_env()
    run = await seed_workflow_run(env, status=WorkflowRunStatus.RUNNING.value)
    uc = CancelWorkflowRun(uow=env.uow)

    result = await _cancel(uc, env, run.id)

    ev = env.outbox.events[0]
    assert ev["aggregate_type"] == AGGREGATE_TYPE
    assert ev["event_type"] == EVENT_WORKFLOW_RUN_CANCELED
    assert ev["aggregate_id"] == result.view.run.id
    assert ev["payload"]["status"] == WorkflowRunStatus.CANCELED.value
    assert ev["metadata"] == {"actor_user_id": str(env.owner_user_id)}
