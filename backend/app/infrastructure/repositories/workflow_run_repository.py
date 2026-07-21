"""SQLAlchemy implementation of ``IWorkflowRunRepository`` (Slice α7.2).

A **workflow run** is the record of one workflow execution plus its orchestration
graph — ordered steps and append-only checkpoints (ADR-0040). This adapter is the
first to use the **status-guarded CAS** concurrency model (D3.2): with no
``version`` column on ``workflow_runs`` / ``workflow_steps``, every lifecycle
transition is a status-predicated ``UPDATE … WHERE status IN (<allowed_from>)``
compare-and-swap that ``RETURNING``\\ s the row on success and yields ``None`` when
the guard did not match (the use case re-classifies). This is race-safe at the DB
without a numeric token.

* **Project-scoped, not owner-scoped.** ``workflow_runs`` carries no
  ``tenant_id`` / ``owner_user_id``; ownership is derived through the project. The
  create/read/cancel surface filters on ``project_id`` (the use case established
  ownership first). Runner transitions key on the run id alone (ownership already
  proven).
* **No soft-delete** — a run is a terminal audit record; "removal" is the
  ``canceled`` status.
* **Checkpoints are append-only (ADR-0014)** — :meth:`append_checkpoint` only ever
  INSERTs; the DB ``tg_workflow_checkpoints_bud_reject_mutation`` trigger blocks
  UPDATE/DELETE.
* **Idempotency backstop.** :meth:`add` maps the
  ``uq_workflow_runs_project_id_idempotency_key`` violation to ``ConflictError``;
  the use case resolves it by returning the existing run (Q7).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.interfaces.repositories import IWorkflowRunRepository
from app.core.errors import ConflictError
from app.domain.workflow.workflow_run import (
    WorkflowCheckpoint as WorkflowCheckpointEntity,
    WorkflowRun as WorkflowRunEntity,
    WorkflowStep as WorkflowStepEntity,
)
from app.domain.workflow.workflow_run_status import WorkflowRunStatus
from app.domain.workflow.workflow_step_status import WorkflowStepStatus
from app.infrastructure.db.models.workflows import (
    WorkflowCheckpoint as WorkflowCheckpointRow,
    WorkflowRun as WorkflowRunRow,
    WorkflowStep as WorkflowStepRow,
)

# The α7.2 cancel-eligible run statuses — kept in one place so the CAS predicate
# and the domain enum cannot drift (mirrors ``WorkflowRunStatus.is_cancelable``).
_CANCELABLE_RUN_STATUSES = (
    WorkflowRunStatus.QUEUED.value,
    WorkflowRunStatus.RUNNING.value,
    WorkflowRunStatus.PAUSED.value,
)
# Runnable step statuses (``WorkflowStepStatus.is_runnable``).
_RUNNABLE_STEP_STATUSES = (
    WorkflowStepStatus.PENDING.value,
    WorkflowStepStatus.RETRYING.value,
)


class WorkflowRunRepository(IWorkflowRunRepository):
    """Workflow-run persistence adapter (project-scoped, status-guarded, no soft-delete)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ---- create + seed -------------------------------------------------

    async def add(
        self,
        *,
        project_id: UUID,
        workflow_key: str,
        workflow_version: str,
        status: str,
        input_snapshot: dict[str, Any],
        triggered_by_user_id: UUID | None,
        idempotency_key: str | None,
    ) -> WorkflowRunEntity:
        row = WorkflowRunRow(
            project_id=project_id,
            workflow_key=workflow_key,
            workflow_version=workflow_version,
            status=status,
            input_snapshot=input_snapshot,
            triggered_by_user_id=triggered_by_user_id,
            idempotency_key=idempotency_key,
        )
        self._session.add(row)
        try:
            await self._session.flush()
        except IntegrityError as e:
            # 23505 on ``uq_workflow_runs_project_id_idempotency_key`` → a run with
            # this (project_id, idempotency_key) already exists. Surface as
            # ConflictError; the use case maps it to "return the existing run" (Q7).
            raise ConflictError(
                "workflow run already exists for this idempotency key",
                details={"constraint": _extract_constraint_name(e) or "unknown"},
            ) from e
        await self._session.refresh(row)
        return _run_to_entity(row)

    async def seed_steps(
        self, workflow_run_id: UUID, steps: list[tuple[int, str]]
    ) -> list[WorkflowStepEntity]:
        rows = [
            WorkflowStepRow(
                workflow_run_id=workflow_run_id,
                step_index=index,
                step_name=name,
                status=WorkflowStepStatus.PENDING.value,
            )
            for index, name in steps
        ]
        self._session.add_all(rows)
        await self._session.flush()
        for r in rows:
            await self._session.refresh(r)
        return [_step_to_entity(r) for r in sorted(rows, key=lambda r: r.step_index)]

    # ---- reads ---------------------------------------------------------

    async def get_by_project_and_key(
        self, project_id: UUID, idempotency_key: str
    ) -> WorkflowRunEntity | None:
        stmt = (
            select(WorkflowRunRow)
            .where(WorkflowRunRow.project_id == project_id)
            .where(WorkflowRunRow.idempotency_key == idempotency_key)
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _run_to_entity(row) if row is not None else None

    async def list_by_project(
        self, project_id: UUID, *, status: str | None = None
    ) -> list[WorkflowRunEntity]:
        stmt = select(WorkflowRunRow).where(WorkflowRunRow.project_id == project_id)
        if status is not None:
            stmt = stmt.where(WorkflowRunRow.status == status)
        # Total order (created_at, id) DESC → stable newest-first under ties.
        stmt = stmt.order_by(WorkflowRunRow.created_at.desc(), WorkflowRunRow.id.desc())
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_run_to_entity(r) for r in rows]

    async def get_owned(self, project_id: UUID, workflow_run_id: UUID) -> WorkflowRunEntity | None:
        stmt = (
            select(WorkflowRunRow)
            .where(WorkflowRunRow.id == workflow_run_id)
            .where(WorkflowRunRow.project_id == project_id)
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _run_to_entity(row) if row is not None else None

    async def list_steps(self, workflow_run_id: UUID) -> list[WorkflowStepEntity]:
        stmt = (
            select(WorkflowStepRow)
            .where(WorkflowStepRow.workflow_run_id == workflow_run_id)
            .order_by(WorkflowStepRow.step_index.asc())
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_step_to_entity(r) for r in rows]

    async def latest_checkpoint(
        self, workflow_run_id: UUID, step_index: int | None = None
    ) -> WorkflowCheckpointEntity | None:
        stmt = select(WorkflowCheckpointRow).where(
            WorkflowCheckpointRow.workflow_run_id == workflow_run_id
        )
        if step_index is not None:
            stmt = stmt.where(WorkflowCheckpointRow.step_index == step_index)
        stmt = stmt.order_by(
            WorkflowCheckpointRow.created_at.desc(), WorkflowCheckpointRow.id.desc()
        ).limit(1)
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _checkpoint_to_entity(row) if row is not None else None

    # ---- run transitions (status-guarded CAS) --------------------------

    async def mark_run_running(self, workflow_run_id: UUID) -> WorkflowRunEntity | None:
        upd = (
            update(WorkflowRunRow)
            .where(WorkflowRunRow.id == workflow_run_id)
            .where(WorkflowRunRow.status == WorkflowRunStatus.QUEUED.value)
            .values(
                status=WorkflowRunStatus.RUNNING.value,
                started_at=func.now(),
                updated_at=func.now(),
            )
            .returning(WorkflowRunRow)
        )
        row = (await self._session.execute(upd)).scalar_one_or_none()
        return _run_to_entity(row) if row is not None else None

    async def mark_run_succeeded(
        self, workflow_run_id: UUID, output_summary: dict[str, Any]
    ) -> WorkflowRunEntity | None:
        upd = (
            update(WorkflowRunRow)
            .where(WorkflowRunRow.id == workflow_run_id)
            .where(WorkflowRunRow.status == WorkflowRunStatus.RUNNING.value)
            .values(
                status=WorkflowRunStatus.SUCCEEDED.value,
                output_summary=output_summary,
                finished_at=func.now(),
                updated_at=func.now(),
            )
            .returning(WorkflowRunRow)
        )
        row = (await self._session.execute(upd)).scalar_one_or_none()
        return _run_to_entity(row) if row is not None else None

    async def mark_run_failed(
        self, workflow_run_id: UUID, error: dict[str, Any]
    ) -> WorkflowRunEntity | None:
        upd = (
            update(WorkflowRunRow)
            .where(WorkflowRunRow.id == workflow_run_id)
            .where(WorkflowRunRow.status == WorkflowRunStatus.RUNNING.value)
            .values(
                status=WorkflowRunStatus.FAILED.value,
                error=error,
                finished_at=func.now(),
                updated_at=func.now(),
            )
            .returning(WorkflowRunRow)
        )
        row = (await self._session.execute(upd)).scalar_one_or_none()
        return _run_to_entity(row) if row is not None else None

    async def mark_run_paused(self, workflow_run_id: UUID) -> WorkflowRunEntity | None:
        # CAS ``running → paused`` (α7.6, Q2). ``paused`` is not terminal, so
        # ``finished_at`` is left unset — the α8.3 completion service resumes the run.
        upd = (
            update(WorkflowRunRow)
            .where(WorkflowRunRow.id == workflow_run_id)
            .where(WorkflowRunRow.status == WorkflowRunStatus.RUNNING.value)
            .values(
                status=WorkflowRunStatus.PAUSED.value,
                updated_at=func.now(),
            )
            .returning(WorkflowRunRow)
        )
        row = (await self._session.execute(upd)).scalar_one_or_none()
        return _run_to_entity(row) if row is not None else None

    async def resume_run(self, workflow_run_id: UUID) -> WorkflowRunEntity | None:
        # CAS ``paused → running`` (α8.3 completion resume). Inverse of
        # ``mark_run_paused``; ``finished_at`` stays unset. Status-guarded so a
        # concurrent resume that already left ``paused`` yields no row (None → replay).
        upd = (
            update(WorkflowRunRow)
            .where(WorkflowRunRow.id == workflow_run_id)
            .where(WorkflowRunRow.status == WorkflowRunStatus.PAUSED.value)
            .values(
                status=WorkflowRunStatus.RUNNING.value,
                updated_at=func.now(),
            )
            .returning(WorkflowRunRow)
        )
        row = (await self._session.execute(upd)).scalar_one_or_none()
        return _run_to_entity(row) if row is not None else None

    async def list_paused(self) -> list[WorkflowRunEntity]:
        # Global paused-run scan for the completion poller (α8.3). Oldest first.
        stmt = (
            select(WorkflowRunRow)
            .where(WorkflowRunRow.status == WorkflowRunStatus.PAUSED.value)
            .order_by(WorkflowRunRow.created_at.asc(), WorkflowRunRow.id.asc())
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_run_to_entity(r) for r in rows]

    async def cancel(self, project_id: UUID, workflow_run_id: UUID) -> WorkflowRunEntity | None:
        # Status-guarded CAS (no version token — D3.2/D3.7). The ``status IN (...)``
        # predicate makes the terminal-state guard race-safe at the DB: a run the
        # runner moved to succeeded/failed between the use case's read and this
        # write is NOT overwritten (RETURNING yields no row → None → re-classify).
        upd = (
            update(WorkflowRunRow)
            .where(WorkflowRunRow.id == workflow_run_id)
            .where(WorkflowRunRow.project_id == project_id)
            .where(WorkflowRunRow.status.in_(_CANCELABLE_RUN_STATUSES))
            .values(
                status=WorkflowRunStatus.CANCELED.value,
                finished_at=func.now(),
                updated_at=func.now(),
            )
            .returning(WorkflowRunRow)
        )
        row = (await self._session.execute(upd)).scalar_one_or_none()
        return _run_to_entity(row) if row is not None else None

    # ---- step transitions (status-guarded CAS) -------------------------

    async def mark_step_running(
        self, workflow_run_id: UUID, step_index: int
    ) -> WorkflowStepEntity | None:
        upd = (
            update(WorkflowStepRow)
            .where(WorkflowStepRow.workflow_run_id == workflow_run_id)
            .where(WorkflowStepRow.step_index == step_index)
            .where(WorkflowStepRow.status.in_(_RUNNABLE_STEP_STATUSES))
            .values(
                status=WorkflowStepStatus.RUNNING.value,
                started_at=func.now(),
                updated_at=func.now(),
            )
            .returning(WorkflowStepRow)
        )
        row = (await self._session.execute(upd)).scalar_one_or_none()
        return _step_to_entity(row) if row is not None else None

    async def mark_step_succeeded(
        self, workflow_run_id: UUID, step_index: int, output: dict[str, Any]
    ) -> WorkflowStepEntity | None:
        upd = (
            update(WorkflowStepRow)
            .where(WorkflowStepRow.workflow_run_id == workflow_run_id)
            .where(WorkflowStepRow.step_index == step_index)
            .where(WorkflowStepRow.status == WorkflowStepStatus.RUNNING.value)
            .values(
                status=WorkflowStepStatus.SUCCEEDED.value,
                output=output,
                finished_at=func.now(),
                updated_at=func.now(),
            )
            .returning(WorkflowStepRow)
        )
        row = (await self._session.execute(upd)).scalar_one_or_none()
        return _step_to_entity(row) if row is not None else None

    async def mark_step_retrying(
        self, workflow_run_id: UUID, step_index: int, error: dict[str, Any]
    ) -> WorkflowStepEntity | None:
        upd = (
            update(WorkflowStepRow)
            .where(WorkflowStepRow.workflow_run_id == workflow_run_id)
            .where(WorkflowStepRow.step_index == step_index)
            .where(WorkflowStepRow.status == WorkflowStepStatus.RUNNING.value)
            .values(
                status=WorkflowStepStatus.RETRYING.value,
                retries=WorkflowStepRow.retries + 1,
                error=error,
                updated_at=func.now(),
            )
            .returning(WorkflowStepRow)
        )
        row = (await self._session.execute(upd)).scalar_one_or_none()
        return _step_to_entity(row) if row is not None else None

    async def mark_step_failed(
        self, workflow_run_id: UUID, step_index: int, error: dict[str, Any]
    ) -> WorkflowStepEntity | None:
        upd = (
            update(WorkflowStepRow)
            .where(WorkflowStepRow.workflow_run_id == workflow_run_id)
            .where(WorkflowStepRow.step_index == step_index)
            .where(WorkflowStepRow.status == WorkflowStepStatus.RUNNING.value)
            .values(
                status=WorkflowStepStatus.FAILED.value,
                error=error,
                finished_at=func.now(),
                updated_at=func.now(),
            )
            .returning(WorkflowStepRow)
        )
        row = (await self._session.execute(upd)).scalar_one_or_none()
        return _step_to_entity(row) if row is not None else None

    # ---- checkpoints (append-only) -------------------------------------

    async def append_checkpoint(
        self, workflow_run_id: UUID, step_index: int, state: dict[str, Any]
    ) -> WorkflowCheckpointEntity:
        row = WorkflowCheckpointRow(
            workflow_run_id=workflow_run_id,
            step_index=step_index,
            state=state,
        )
        self._session.add(row)
        # Flush so the INSERT participates in the caller's transaction. The
        # append-only trigger only rejects UPDATE/DELETE, so INSERT is fine.
        await self._session.flush()
        await self._session.refresh(row)
        return _checkpoint_to_entity(row)


def _run_to_entity(row: WorkflowRunRow) -> WorkflowRunEntity:
    return WorkflowRunEntity(
        id=row.id,
        project_id=row.project_id,
        workflow_key=row.workflow_key,
        workflow_version=row.workflow_version,
        status=row.status,
        started_at=row.started_at,
        finished_at=row.finished_at,
        triggered_by_user_id=row.triggered_by_user_id,
        idempotency_key=row.idempotency_key,
        input_snapshot=dict(row.input_snapshot),
        output_summary=dict(row.output_summary) if row.output_summary is not None else None,
        error=dict(row.error) if row.error is not None else None,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _step_to_entity(row: WorkflowStepRow) -> WorkflowStepEntity:
    return WorkflowStepEntity(
        id=row.id,
        workflow_run_id=row.workflow_run_id,
        step_index=row.step_index,
        step_name=row.step_name,
        status=row.status,
        started_at=row.started_at,
        finished_at=row.finished_at,
        retries=row.retries,
        input=dict(row.input) if row.input is not None else None,
        output=dict(row.output) if row.output is not None else None,
        error=dict(row.error) if row.error is not None else None,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _checkpoint_to_entity(row: WorkflowCheckpointRow) -> WorkflowCheckpointEntity:
    return WorkflowCheckpointEntity(
        id=row.id,
        workflow_run_id=row.workflow_run_id,
        step_index=row.step_index,
        state=dict(row.state),
        created_at=row.created_at,
    )


def _extract_constraint_name(exc: IntegrityError) -> str | None:
    """Best-effort extraction of the failed constraint name from psycopg.

    Mirrors the helper in ``render_job_repository.py`` / ``media_repository.py``.
    """
    orig = getattr(exc, "orig", None)
    diag = getattr(orig, "diag", None)
    name = getattr(diag, "constraint_name", None)
    return str(name) if name else None
