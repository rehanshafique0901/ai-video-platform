"""Unit tests for ``AdvanceWorkflowRun`` — the synchronous deterministic runner (α7.2).

The runner is the imperative shell around **pure** step handlers (D3.11): it runs
a ``queued`` run to a terminal state within one call, persisting step transitions +
append-only checkpoints and emitting the Q8 outbox events. These tests drive the
in-code deterministic workflows (``noop-chain`` / ``retry-succeed`` /
``terminal-fail`` / ``retry-exhaust``) so behaviour is reproducible with no I/O.

Coverage map:

* U1 — noop-chain: queued → succeeded, all steps succeeded + 3 checkpoints,
  output_summary lists completed steps, commits once, emits Started + 3×
  StepCompleted + Succeeded.
* U2 — retry-succeed: the flaky step retries then succeeds; its ``retries`` == 2
  (deterministic counter, Q5); run succeeds.
* U3 — terminal-fail: run → failed with reason ``terminal``; emits Started +
  StepCompleted (prepare) + Failed; the boom step is ``failed``.
* U4 — retry-exhaust: run → failed with reason ``retries_exhausted``; the doomed
  step's ``retries`` == the definition bound (2).
* U5 — advancing an already-``succeeded`` run → ``ConflictError`` (409).
* U6 — advancing a ``canceled`` run → ``ConflictError`` (409).
* U7 — unknown run / unknown project → ``NotFoundError`` (404).
* U8 — resume: advancing a ``running`` run whose steps are already ``succeeded``
  settles it ``succeeded`` without re-emitting StepCompleted events.
* U9 — event shapes: Started / StepCompleted (step coords) / Succeeded / Failed.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.application.use_cases.workflow._events import (
    AGGREGATE_TYPE,
    EVENT_WORKFLOW_RUN_FAILED,
    EVENT_WORKFLOW_RUN_STARTED,
    EVENT_WORKFLOW_RUN_SUCCEEDED,
    EVENT_WORKFLOW_STEP_COMPLETED,
)
from app.application.use_cases.workflow.advance_workflow_run import AdvanceWorkflowRun
from app.core.errors import ConflictError, NotFoundError
from app.domain.workflow.registry import (
    NOOP_CHAIN,
    RETRY_EXHAUST,
    RETRY_SUCCEED,
    TERMINAL_FAIL,
)
from app.domain.workflow.workflow_run_status import WorkflowRunStatus
from app.domain.workflow.workflow_step_status import WorkflowStepStatus
from tests.unit.application.use_cases.workflow._helpers import (
    build_env,
    mark_all_steps_succeeded,
    seed_workflow_run,
)


async def _advance(uc: AdvanceWorkflowRun, env, workflow_run_id, **overrides):  # type: ignore[no-untyped-def]
    kwargs = {
        "project_id": env.project_id,
        "workflow_run_id": workflow_run_id,
        "owner_user_id": env.owner_user_id,
        "tenant_id": env.tenant_id,
    }
    kwargs.update(overrides)
    return await uc.execute(**kwargs)


def _events_of(env, event_type):  # type: ignore[no-untyped-def]
    return [e for e in env.outbox.events if e["event_type"] == event_type]


@pytest.mark.unit
async def test_u1_noop_chain_runs_to_succeeded() -> None:
    env = build_env()
    run = await seed_workflow_run(env, workflow_key=NOOP_CHAIN)
    uc = AdvanceWorkflowRun(uow=env.uow)

    result = await _advance(uc, env, run.id)

    assert result.advanced is True
    assert result.view.run.status == WorkflowRunStatus.SUCCEEDED.value
    assert result.view.run.finished_at is not None
    assert all(s.status == WorkflowStepStatus.SUCCEEDED.value for s in result.view.steps)
    assert result.view.run.output_summary == {
        "step_count": 3,
        "completed_steps": ["extract", "transform", "summarize"],
    }
    # One checkpoint per step; latest belongs to the last step.
    assert result.view.latest_checkpoint is not None
    assert result.view.latest_checkpoint.step_index == 2
    assert len(env.workflow_runs._checkpoints) == 3
    assert env.uow.commits == 1
    # Started + 3×StepCompleted + Succeeded.
    assert len(_events_of(env, EVENT_WORKFLOW_RUN_STARTED)) == 1
    assert len(_events_of(env, EVENT_WORKFLOW_STEP_COMPLETED)) == 3
    assert len(_events_of(env, EVENT_WORKFLOW_RUN_SUCCEEDED)) == 1


@pytest.mark.unit
async def test_u2_retry_succeed_bumps_retry_counter() -> None:
    env = build_env()
    run = await seed_workflow_run(env, workflow_key=RETRY_SUCCEED)
    uc = AdvanceWorkflowRun(uow=env.uow)

    result = await _advance(uc, env, run.id)

    assert result.view.run.status == WorkflowRunStatus.SUCCEEDED.value
    flaky = next(s for s in result.view.steps if s.step_name == "flaky")
    # Fails on attempts 0 and 1, succeeds on attempt 2 → two retries recorded.
    assert flaky.retries == 2
    assert flaky.status == WorkflowStepStatus.SUCCEEDED.value


@pytest.mark.unit
async def test_u3_terminal_fail_settles_failed() -> None:
    env = build_env()
    run = await seed_workflow_run(env, workflow_key=TERMINAL_FAIL)
    uc = AdvanceWorkflowRun(uow=env.uow)

    result = await _advance(uc, env, run.id)

    assert result.view.run.status == WorkflowRunStatus.FAILED.value
    assert result.view.run.error is not None
    assert result.view.run.error["reason"] == "terminal"
    assert result.view.run.error["step_name"] == "boom"
    boom = next(s for s in result.view.steps if s.step_name == "boom")
    assert boom.status == WorkflowStepStatus.FAILED.value
    assert len(_events_of(env, EVENT_WORKFLOW_RUN_FAILED)) == 1
    # prepare succeeded before boom failed.
    assert len(_events_of(env, EVENT_WORKFLOW_STEP_COMPLETED)) == 1


@pytest.mark.unit
async def test_u4_retry_exhaust_settles_failed() -> None:
    env = build_env()
    run = await seed_workflow_run(env, workflow_key=RETRY_EXHAUST)
    uc = AdvanceWorkflowRun(uow=env.uow)

    result = await _advance(uc, env, run.id)

    assert result.view.run.status == WorkflowRunStatus.FAILED.value
    assert result.view.run.error["reason"] == "retries_exhausted"
    doomed = next(s for s in result.view.steps if s.step_name == "doomed")
    assert doomed.status == WorkflowStepStatus.FAILED.value
    # max_retries=2 → two retries recorded before exhaustion.
    assert doomed.retries == 2


@pytest.mark.unit
async def test_u5_advance_succeeded_raises_409() -> None:
    env = build_env()
    run = await seed_workflow_run(env, status=WorkflowRunStatus.SUCCEEDED.value)
    uc = AdvanceWorkflowRun(uow=env.uow)

    with pytest.raises(ConflictError):
        await _advance(uc, env, run.id)

    assert env.uow.commits == 0


@pytest.mark.unit
async def test_u6_advance_canceled_raises_409() -> None:
    env = build_env()
    run = await seed_workflow_run(env, status=WorkflowRunStatus.CANCELED.value)
    uc = AdvanceWorkflowRun(uow=env.uow)

    with pytest.raises(ConflictError):
        await _advance(uc, env, run.id)


@pytest.mark.unit
async def test_u7_unknown_run_and_project_raise_404() -> None:
    env = build_env()
    run = await seed_workflow_run(env)
    uc = AdvanceWorkflowRun(uow=env.uow)

    with pytest.raises(NotFoundError):
        await _advance(uc, env, uuid4())
    with pytest.raises(NotFoundError):
        await _advance(uc, env, run.id, project_id=uuid4())


@pytest.mark.unit
async def test_u8_resume_running_run_with_done_steps_settles_succeeded() -> None:
    env = build_env()
    run = await seed_workflow_run(env, status=WorkflowRunStatus.RUNNING.value)
    await mark_all_steps_succeeded(env, run)
    uc = AdvanceWorkflowRun(uow=env.uow)

    result = await _advance(uc, env, run.id)

    assert result.view.run.status == WorkflowRunStatus.SUCCEEDED.value
    # No start transition (already running) and no re-run of the done steps.
    assert _events_of(env, EVENT_WORKFLOW_RUN_STARTED) == []
    assert _events_of(env, EVENT_WORKFLOW_STEP_COMPLETED) == []
    assert len(_events_of(env, EVENT_WORKFLOW_RUN_SUCCEEDED)) == 1


@pytest.mark.unit
async def test_u9_event_shapes() -> None:
    env = build_env()
    run = await seed_workflow_run(env, workflow_key=NOOP_CHAIN)
    uc = AdvanceWorkflowRun(uow=env.uow)

    await _advance(uc, env, run.id)

    started = _events_of(env, EVENT_WORKFLOW_RUN_STARTED)[0]
    assert started["aggregate_type"] == AGGREGATE_TYPE
    assert started["aggregate_id"] == run.id
    assert started["payload"]["status"] == WorkflowRunStatus.RUNNING.value

    step_ev = _events_of(env, EVENT_WORKFLOW_STEP_COMPLETED)[0]
    assert step_ev["payload"]["step_index"] == 0
    assert step_ev["payload"]["step_name"] == "extract"

    succeeded = _events_of(env, EVENT_WORKFLOW_RUN_SUCCEEDED)[0]
    assert succeeded["payload"]["status"] == WorkflowRunStatus.SUCCEEDED.value
    assert succeeded["metadata"] == {"actor_user_id": str(env.owner_user_id)}
