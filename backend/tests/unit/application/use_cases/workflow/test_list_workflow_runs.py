"""Unit tests for ``ListWorkflowRuns`` (Slice α7.2).

Coverage map:

* U1 — empty project → ``200 []``.
* U2 — newest-first ordering (most recently created run leads).
* U3 — ``status`` filter returns only matching runs.
* U4 — unknown project → ``NotFoundError`` (404).
* U5 — project-scoping: another project's runs are not visible.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.application.use_cases.workflow.list_workflow_runs import ListWorkflowRuns
from app.core.errors import NotFoundError
from app.domain.workflow.registry import RETRY_SUCCEED, WORKFLOW_VERSION_1
from app.domain.workflow.workflow_run_status import WorkflowRunStatus
from tests.unit.application.use_cases.workflow._helpers import build_env, seed_workflow_run


async def _list(uc: ListWorkflowRuns, env, **overrides):  # type: ignore[no-untyped-def]
    kwargs = {
        "project_id": env.project_id,
        "owner_user_id": env.owner_user_id,
        "tenant_id": env.tenant_id,
    }
    kwargs.update(overrides)
    return await uc.execute(**kwargs)


@pytest.mark.unit
async def test_u1_empty_project_returns_empty() -> None:
    env = build_env()
    uc = ListWorkflowRuns(uow=env.uow)

    assert await _list(uc, env) == []


@pytest.mark.unit
async def test_u2_newest_first_ordering() -> None:
    env = build_env()
    first = await seed_workflow_run(env)
    second = await seed_workflow_run(env)
    third = await seed_workflow_run(env)
    uc = ListWorkflowRuns(uow=env.uow)

    runs = await _list(uc, env)

    assert [r.id for r in runs] == [third.id, second.id, first.id]


@pytest.mark.unit
async def test_u3_status_filter() -> None:
    env = build_env()
    await seed_workflow_run(env, status=WorkflowRunStatus.QUEUED.value)
    succeeded = await seed_workflow_run(env, status=WorkflowRunStatus.SUCCEEDED.value)
    uc = ListWorkflowRuns(uow=env.uow)

    runs = await _list(uc, env, status=WorkflowRunStatus.SUCCEEDED.value)

    assert [r.id for r in runs] == [succeeded.id]


@pytest.mark.unit
async def test_u4_unknown_project_raises_404() -> None:
    env = build_env()
    uc = ListWorkflowRuns(uow=env.uow)

    with pytest.raises(NotFoundError):
        await _list(uc, env, project_id=uuid4())


@pytest.mark.unit
async def test_u5_project_scoping_hides_other_projects_runs() -> None:
    env = build_env()
    # A run under a different project must not surface for this project.
    await seed_workflow_run(
        env, project_id=uuid4(), workflow_key=RETRY_SUCCEED, workflow_version=WORKFLOW_VERSION_1
    )
    mine = await seed_workflow_run(env)
    uc = ListWorkflowRuns(uow=env.uow)

    runs = await _list(uc, env)

    assert [r.id for r in runs] == [mine.id]
