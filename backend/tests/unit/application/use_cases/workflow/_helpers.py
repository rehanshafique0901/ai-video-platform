"""Shared scaffolding for the α7.2 WorkflowRun use-case unit tests.

A ``WorkflowRun`` is a **project-scoped orchestration** aggregate with **no owner
columns** — every use case runs the project ownership gate (→ 404) first, then
works over the ``workflow_runs`` fake (which owns steps + append-only
checkpoints). ``build_env`` wires a UoW with one owned live :class:`Project` plus
empty workflow-run + outbox fakes, returning the fakes so tests can assert repo
state, emitted events, and ``commit`` bookkeeping.

``seed_workflow_run`` inserts a run (bypassing the create use case) and seeds its
steps from the in-code definition; ``drive_to`` walks a run through the fake's CAS
transitions so cancel/advance tests can start from ``running`` / terminal states
without invoking the runner.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from app.domain.projects.project import Project
from app.domain.workflow.registry import NOOP_CHAIN, WORKFLOW_REGISTRY, WORKFLOW_VERSION_1
from app.domain.workflow.workflow_run import WorkflowRun
from app.domain.workflow.workflow_run_status import WorkflowRunStatus
from tests.unit.application.use_cases.auth._fakes import (
    FakeEventOutboxRepository,
    FakeProjectRepository,
    FakeUnitOfWork,
    FakeWorkflowRunRepository,
)


def _dt() -> datetime:
    return datetime.now(UTC)


def make_project(*, tenant_id: UUID, owner_user_id: UUID) -> Project:
    """A minimal live project owned by ``(tenant_id, owner_user_id)``."""
    return Project(
        id=uuid4(),
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        folder_id=None,
        current_version_id=None,
        name=f"Project {uuid4().hex[:8]}",
        description=None,
        aspect_ratio="horizontal",
        duration_seconds=None,
        language="en",
        style=None,
        settings={},
        created_at=_dt(),
        updated_at=_dt(),
        version=1,
    )


@dataclass
class Env:
    """The seeded fakes + the ids a test needs to address the owner + project."""

    uow: FakeUnitOfWork
    projects: FakeProjectRepository
    workflow_runs: FakeWorkflowRunRepository
    outbox: FakeEventOutboxRepository
    owner_user_id: UUID
    tenant_id: UUID
    project_id: UUID


def build_env() -> Env:
    """Wire a UoW with one owned live project + empty workflow-run/outbox fakes."""
    owner_user_id = uuid4()
    tenant_id = uuid4()
    project = make_project(tenant_id=tenant_id, owner_user_id=owner_user_id)
    projects = FakeProjectRepository(_rows={project.id: project})
    workflow_runs = FakeWorkflowRunRepository()
    outbox = FakeEventOutboxRepository()
    uow = FakeUnitOfWork(
        projects=projects,
        workflow_runs=workflow_runs,
        outbox=outbox,
    )
    return Env(
        uow=uow,
        projects=projects,
        workflow_runs=workflow_runs,
        outbox=outbox,
        owner_user_id=owner_user_id,
        tenant_id=tenant_id,
        project_id=project.id,
    )


async def seed_workflow_run(
    env: Env,
    *,
    project_id: UUID | None = None,
    workflow_key: str = NOOP_CHAIN,
    workflow_version: str = WORKFLOW_VERSION_1,
    status: str = WorkflowRunStatus.QUEUED.value,
    input_snapshot: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
    seed_steps: bool = True,
) -> WorkflowRun:
    """Insert one workflow run (+ its seeded steps) directly on the fake.

    Bypasses the create use case, so it can seed a run in any ``status`` (for
    cancel/advance classification tests). Steps are seeded ``pending`` from the
    in-code definition unless ``seed_steps=False``.
    """
    run = await env.workflow_runs.add(
        project_id=project_id if project_id is not None else env.project_id,
        workflow_key=workflow_key,
        workflow_version=workflow_version,
        status=status,
        input_snapshot=input_snapshot if input_snapshot is not None else {},
        triggered_by_user_id=env.owner_user_id,
        idempotency_key=idempotency_key,
    )
    if seed_steps:
        definition = WORKFLOW_REGISTRY.get(workflow_key, workflow_version)
        assert definition is not None, f"unknown workflow {workflow_key}@{workflow_version}"
        await env.workflow_runs.seed_steps(run.id, definition.step_specs)
    return run


async def mark_all_steps_succeeded(env: Env, run: WorkflowRun) -> None:
    """Drive every seeded step of ``run`` to ``succeeded`` via the fake's CAS methods.

    Used by resume tests to reach a state the runner should short-circuit past.
    """
    steps = await env.workflow_runs.list_steps(run.id)
    for step in steps:
        await env.workflow_runs.mark_step_running(run.id, step.step_index)
        await env.workflow_runs.mark_step_succeeded(run.id, step.step_index, {})
        await env.workflow_runs.append_checkpoint(
            run.id, step.step_index, {"completed_step": step.step_name}
        )
