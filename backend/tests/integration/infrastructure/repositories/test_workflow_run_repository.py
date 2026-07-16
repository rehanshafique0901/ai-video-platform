"""Integration tests for ``WorkflowRunRepository`` (Slice α7.2).

Runs against the live database; each test is wrapped in a SAVEPOINT that rolls
back on teardown, so no rows persist. Covers the project-scoped, **status-guarded
CAS** workflow-run aggregate (ADR-0040): ``add`` (queued, DB defaults), the
``(project_id, idempotency_key)`` uniqueness backstop (→ ``ConflictError``),
newest-first listing with a ``status`` filter, project-scoped reads, the
status-predicated run/step transitions (which ``RETURNING`` the row on success and
yield ``None`` when the guard did not match — no ``version`` token, D3.2), step
retry accounting, and append-only checkpoints.

Coverage map:

* R1  — ``add`` creates ``status=queued`` with DB-defaulted timestamps.
* R2  — ``add`` duplicate ``(project_id, idempotency_key)`` → ``ConflictError``.
* R3  — ``add`` same key under a DIFFERENT project is allowed (scoping).
* R4  — ``get_by_project_and_key`` returns the row / ``None`` for a foreign project.
* R5  — ``seed_steps`` seeds ordered pending steps; ``list_steps`` returns them sorted.
* R6  — ``list_by_project`` newest-first; ``status`` filter narrows.
* R7  — ``get_owned`` project isolation → ``None``.
* R8  — ``mark_run_running`` CAS: ``queued → running`` once, then ``None`` (not queued).
* R9  — ``mark_run_succeeded`` requires ``running`` (from ``queued`` → ``None``).
* R10 — ``cancel`` happy: ``queued → canceled``; terminal ``succeeded`` → ``None``.
* R11 — step CAS chain: running → succeeded; ``mark_step_retrying`` bumps ``retries``.
* R12 — ``append_checkpoint`` + ``latest_checkpoint`` (overall + per ``step_index``).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import insert, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError
from app.domain.workflow.workflow_run_status import WorkflowRunStatus
from app.domain.workflow.workflow_step_status import WorkflowStepStatus
from app.infrastructure.db.models.identity import Tenant, User
from app.infrastructure.db.models.projects import Project as ProjectRow
from app.infrastructure.db.models.workflows import WorkflowRun as WorkflowRunRow
from app.infrastructure.repositories.workflow_run_repository import WorkflowRunRepository


async def _seed_project(session: AsyncSession) -> UUID:
    """Seed tenant + user + project; return the project id."""
    tenant_id = uuid4()
    await session.execute(
        insert(Tenant).values(id=tenant_id, name="WF Test", slug=f"wf-{tenant_id}")
    )
    user_id = uuid4()
    await session.execute(
        insert(User).values(
            id=user_id,
            tenant_id=tenant_id,
            email=f"wf-{user_id}@example.com",
            display_name="WF Owner",
        )
    )
    project_id = uuid4()
    await session.execute(
        insert(ProjectRow).values(
            id=project_id,
            tenant_id=tenant_id,
            owner_user_id=user_id,
            name=f"P {project_id}",
            aspect_ratio="horizontal",
        )
    )
    await session.flush()
    return project_id


async def _add_run(
    repo: WorkflowRunRepository,
    project_id: UUID,
    *,
    workflow_key: str = "noop-chain",
    workflow_version: str = "1.0.0",
    status: str = WorkflowRunStatus.QUEUED.value,
    input_snapshot: dict | None = None,
    idempotency_key: str | None = None,
):  # type: ignore[no-untyped-def]
    return await repo.add(
        project_id=project_id,
        workflow_key=workflow_key,
        workflow_version=workflow_version,
        status=status,
        input_snapshot=input_snapshot if input_snapshot is not None else {},
        triggered_by_user_id=None,
        idempotency_key=idempotency_key,
    )


# ---- R1 — add creates a queued run ------------------------------------


@pytest.mark.integration
async def test_r1_add_creates_queued_run(session: AsyncSession) -> None:
    project_id = await _seed_project(session)
    repo = WorkflowRunRepository(session)

    run = await _add_run(repo, project_id, input_snapshot={"topic": "cats"})
    assert run.status == WorkflowRunStatus.QUEUED.value
    assert run.project_id == project_id
    assert run.input_snapshot == {"topic": "cats"}
    assert run.started_at is None
    assert run.finished_at is None
    assert run.output_summary is None
    assert run.error is None
    assert run.created_at is not None


# ---- R2 — duplicate idempotency key → ConflictError -------------------


@pytest.mark.integration
async def test_r2_duplicate_idempotency_key_conflicts(session: AsyncSession) -> None:
    project_id = await _seed_project(session)
    repo = WorkflowRunRepository(session)
    await _add_run(repo, project_id, idempotency_key="dup")

    with pytest.raises(ConflictError):
        await _add_run(repo, project_id, idempotency_key="dup")


# ---- R3 — same key under a different project is allowed ----------------


@pytest.mark.integration
async def test_r3_same_key_distinct_projects_allowed(session: AsyncSession) -> None:
    p1 = await _seed_project(session)
    p2 = await _seed_project(session)
    repo = WorkflowRunRepository(session)

    r1 = await _add_run(repo, p1, idempotency_key="shared")
    r2 = await _add_run(repo, p2, idempotency_key="shared")
    assert r1.id != r2.id


# ---- R4 — get_by_project_and_key --------------------------------------


@pytest.mark.integration
async def test_r4_get_by_project_and_key(session: AsyncSession) -> None:
    project_id = await _seed_project(session)
    repo = WorkflowRunRepository(session)
    run = await _add_run(repo, project_id, idempotency_key="k")

    found = await repo.get_by_project_and_key(project_id, "k")
    assert found is not None and found.id == run.id

    foreign = await repo.get_by_project_and_key(uuid4(), "k")
    assert foreign is None


# ---- R5 — seed_steps + list_steps -------------------------------------


@pytest.mark.integration
async def test_r5_seed_steps_ordered_pending(session: AsyncSession) -> None:
    project_id = await _seed_project(session)
    repo = WorkflowRunRepository(session)
    run = await _add_run(repo, project_id)

    seeded = await repo.seed_steps(run.id, [(0, "extract"), (1, "transform"), (2, "summarize")])
    assert [s.step_index for s in seeded] == [0, 1, 2]
    assert all(s.status == WorkflowStepStatus.PENDING.value for s in seeded)
    assert all(s.retries == 0 for s in seeded)

    listed = await repo.list_steps(run.id)
    assert [s.step_name for s in listed] == ["extract", "transform", "summarize"]


# ---- R6 — list_by_project newest-first + status filter ----------------


@pytest.mark.integration
async def test_r6_list_newest_first_and_status_filter(session: AsyncSession) -> None:
    project_id = await _seed_project(session)
    repo = WorkflowRunRepository(session)
    first = await _add_run(repo, project_id)
    second = await _add_run(repo, project_id)

    # ``created_at`` is constant within the transaction; push ``first`` into the
    # past so newest-first deterministically reflects insertion order.
    await session.execute(
        update(WorkflowRunRow)
        .where(WorkflowRunRow.id == first.id)
        .values(created_at=datetime.now(UTC) - timedelta(hours=1))
    )
    await session.flush()

    all_runs = await repo.list_by_project(project_id)
    assert [r.id for r in all_runs] == [second.id, first.id]

    canceled = await repo.cancel(project_id, first.id)
    assert canceled is not None
    filtered = await repo.list_by_project(project_id, status=WorkflowRunStatus.CANCELED.value)
    assert [r.id for r in filtered] == [first.id]


# ---- R7 — get_owned project isolation → None --------------------------


@pytest.mark.integration
async def test_r7_get_owned_project_isolation(session: AsyncSession) -> None:
    project_id = await _seed_project(session)
    repo = WorkflowRunRepository(session)
    run = await _add_run(repo, project_id)

    assert (await repo.get_owned(project_id, run.id)) is not None
    assert (await repo.get_owned(uuid4(), run.id)) is None


# ---- R8 — mark_run_running CAS ----------------------------------------


@pytest.mark.integration
async def test_r8_mark_run_running_cas(session: AsyncSession) -> None:
    project_id = await _seed_project(session)
    repo = WorkflowRunRepository(session)
    run = await _add_run(repo, project_id)

    started = await repo.mark_run_running(run.id)
    assert started is not None
    assert started.status == WorkflowRunStatus.RUNNING.value
    assert started.started_at is not None

    # Second call: no longer queued → CAS guard yields None.
    assert (await repo.mark_run_running(run.id)) is None


# ---- R9 — mark_run_succeeded requires running -------------------------


@pytest.mark.integration
async def test_r9_mark_run_succeeded_requires_running(session: AsyncSession) -> None:
    project_id = await _seed_project(session)
    repo = WorkflowRunRepository(session)
    run = await _add_run(repo, project_id)

    # From queued (not running) → guard fails → None.
    assert (await repo.mark_run_succeeded(run.id, {"ok": True})) is None

    await repo.mark_run_running(run.id)
    settled = await repo.mark_run_succeeded(run.id, {"ok": True})
    assert settled is not None
    assert settled.status == WorkflowRunStatus.SUCCEEDED.value
    assert settled.output_summary == {"ok": True}
    assert settled.finished_at is not None


# ---- R10 — cancel happy + terminal guard ------------------------------


@pytest.mark.integration
async def test_r10_cancel_happy_and_terminal_guard(session: AsyncSession) -> None:
    project_id = await _seed_project(session)
    repo = WorkflowRunRepository(session)
    run = await _add_run(repo, project_id)

    canceled = await repo.cancel(project_id, run.id)
    assert canceled is not None
    assert canceled.status == WorkflowRunStatus.CANCELED.value
    assert canceled.finished_at is not None

    # A second run driven to a terminal (succeeded) state is not cancelable.
    other = await _add_run(repo, project_id)
    await repo.mark_run_running(other.id)
    await repo.mark_run_succeeded(other.id, {})
    assert (await repo.cancel(project_id, other.id)) is None


# ---- R11 — step CAS chain + retry accounting --------------------------


@pytest.mark.integration
async def test_r11_step_transitions_and_retries(session: AsyncSession) -> None:
    project_id = await _seed_project(session)
    repo = WorkflowRunRepository(session)
    run = await _add_run(repo, project_id)
    await repo.seed_steps(run.id, [(0, "step0")])

    running = await repo.mark_step_running(run.id, 0)
    assert running is not None and running.status == WorkflowStepStatus.RUNNING.value

    retrying = await repo.mark_step_retrying(run.id, 0, {"code": "TRANSIENT"})
    assert retrying is not None
    assert retrying.status == WorkflowStepStatus.RETRYING.value
    assert retrying.retries == 1

    # From retrying → running is runnable again; then succeed.
    running2 = await repo.mark_step_running(run.id, 0)
    assert running2 is not None and running2.retries == 1
    done = await repo.mark_step_succeeded(run.id, 0, {"out": 1})
    assert done is not None
    assert done.status == WorkflowStepStatus.SUCCEEDED.value
    assert done.output == {"out": 1}

    # Already succeeded (not running) → mark_step_succeeded guard yields None.
    assert (await repo.mark_step_succeeded(run.id, 0, {"out": 2})) is None


# ---- R12 — append_checkpoint + latest_checkpoint ----------------------


@pytest.mark.integration
async def test_r12_append_and_latest_checkpoint(session: AsyncSession) -> None:
    project_id = await _seed_project(session)
    repo = WorkflowRunRepository(session)
    run = await _add_run(repo, project_id)

    assert (await repo.latest_checkpoint(run.id)) is None

    await repo.append_checkpoint(run.id, 0, {"completed_step": "extract"})
    await repo.append_checkpoint(run.id, 1, {"completed_step": "transform"})

    # ``created_at`` is transaction-constant and checkpoint ids are random UUIDs,
    # so the overall (unfiltered) latest is an id-desc tiebreak among the two rows
    # — assert it resolves to one of the appended steps, not a specific one.
    latest = await repo.latest_checkpoint(run.id)
    assert latest is not None
    assert latest.step_index in (0, 1)

    # Per-step-index filter is deterministic (one checkpoint per step here).
    step0 = await repo.latest_checkpoint(run.id, 0)
    assert step0 is not None and step0.state == {"completed_step": "extract"}
    step1 = await repo.latest_checkpoint(run.id, 1)
    assert step1 is not None and step1.state == {"completed_step": "transform"}
