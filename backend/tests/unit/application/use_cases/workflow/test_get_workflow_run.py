"""Unit tests for ``GetWorkflowRun`` (Slice α7.2).

Coverage map:

* U1 — returns the run plus its ordered steps and the latest checkpoint.
* U2 — unknown run → ``NotFoundError`` (404).
* U3 — unknown project → ``NotFoundError`` (404).
* U4 — cross-project id → ``NotFoundError`` (404) (uniform not-found, no leak).
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.application.use_cases.workflow.get_workflow_run import GetWorkflowRun
from app.core.errors import NotFoundError
from tests.unit.application.use_cases.workflow._helpers import (
    build_env,
    mark_all_steps_succeeded,
    seed_workflow_run,
)


async def _get(uc: GetWorkflowRun, env, workflow_run_id, **overrides):  # type: ignore[no-untyped-def]
    kwargs = {
        "project_id": env.project_id,
        "workflow_run_id": workflow_run_id,
        "owner_user_id": env.owner_user_id,
        "tenant_id": env.tenant_id,
    }
    kwargs.update(overrides)
    return await uc.execute(**kwargs)


@pytest.mark.unit
async def test_u1_returns_run_steps_and_latest_checkpoint() -> None:
    env = build_env()
    run = await seed_workflow_run(env)
    await mark_all_steps_succeeded(env, run)
    uc = GetWorkflowRun(uow=env.uow)

    view = await _get(uc, env, run.id)

    assert view.run.id == run.id
    assert [s.step_index for s in view.steps] == [0, 1, 2]
    assert view.latest_checkpoint is not None
    # Latest checkpoint belongs to the last (highest-index) completed step.
    assert view.latest_checkpoint.step_index == 2


@pytest.mark.unit
async def test_u2_unknown_run_raises_404() -> None:
    env = build_env()
    uc = GetWorkflowRun(uow=env.uow)

    with pytest.raises(NotFoundError):
        await _get(uc, env, uuid4())


@pytest.mark.unit
async def test_u3_unknown_project_raises_404() -> None:
    env = build_env()
    run = await seed_workflow_run(env)
    uc = GetWorkflowRun(uow=env.uow)

    with pytest.raises(NotFoundError):
        await _get(uc, env, run.id, project_id=uuid4())


@pytest.mark.unit
async def test_u4_cross_project_run_raises_404() -> None:
    env = build_env()
    # A run owned by another project is invisible through this project's gate.
    other = await seed_workflow_run(env, project_id=uuid4())
    uc = GetWorkflowRun(uow=env.uow)

    with pytest.raises(NotFoundError):
        await _get(uc, env, other.id)
