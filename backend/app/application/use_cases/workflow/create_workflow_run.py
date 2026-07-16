"""``CreateWorkflowRun`` use case (Slice α7.2).

Contract (API_CONTRACT §3.2.6):

    POST /api/v1/projects/{project_id}/workflow-runs
      body:  { workflow_key, workflow_version, input_snapshot?, idempotency_key? }
      → 201  { data: WorkflowRunPublic, meta }              (new run queued, steps seeded)
      → 200  { data: WorkflowRunPublic, meta }              (idempotent replay — existing run)
      → 404  { error: { code: NOT_FOUND, ... } }            (project missing / not yours)
      → 422  { error: { code: VALIDATION_FAILED, ... } }    (unknown workflow_key@version / bad body)
      → 401  { error: { code: UNAUTHENTICATED, ... } }      (via CurrentUserDep)

Creates the run in ``queued`` and **seeds its steps** (``pending``) from the
in-code workflow definition (D3.9/Q3) — no execution yet (that is ``advance``).
Ownership is derived through the project (the ``workflow_runs`` row has no owner
columns), so step 1 is the project ownership gate (404-before-anything). An
unknown ``workflow_key@workflow_version`` is a ``422`` (the request is well-formed
but names no runnable workflow).

**Idempotency (Q7 — α7.1 parity).** When ``idempotency_key`` is supplied, a repeat
create with the same key for this project returns the **existing** run (router →
``200``); the DB unique constraint
(``uq_workflow_runs_project_id_idempotency_key``) is the race-safe backstop behind
the pre-check.

On create a ``WorkflowRunCreated`` event is written to the ``event_outbox`` in the
same transaction (D9). Run + steps + event commit atomically.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

import structlog

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.application.use_cases.workflow._events import emit_workflow_run_created
from app.application.use_cases.workflow._view import WorkflowRunView
from app.core.errors import ConflictError, NotFoundError, ValidationFailedError
from app.domain.workflow.registry import WORKFLOW_REGISTRY, WorkflowRegistry
from app.domain.workflow.workflow_run_status import WorkflowRunStatus

_LOGGER = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class CreateWorkflowRunResult:
    """The created (or idempotently-replayed) run view plus whether it was newly made.

    ``created`` is ``True`` for a fresh insert (router → ``201``) and ``False`` when
    an existing run was returned for a repeated ``idempotency_key`` (router →
    ``200``).
    """

    view: WorkflowRunView
    created: bool


class CreateWorkflowRun:
    """Queue a workflow run for the caller's project (idempotent on ``idempotency_key``)."""

    def __init__(self, uow: IUnitOfWork, registry: WorkflowRegistry = WORKFLOW_REGISTRY) -> None:
        self._uow = uow
        self._registry = registry

    async def execute(
        self,
        *,
        project_id: UUID,
        owner_user_id: UUID,
        tenant_id: UUID,
        workflow_key: str,
        workflow_version: str,
        input_snapshot: dict[str, Any],
        idempotency_key: str | None = None,
        ip: str | None = None,
    ) -> CreateWorkflowRunResult:
        # Resolve the workflow definition first (pure, no DB) — an unknown
        # key@version is a 422 regardless of ownership.
        definition = self._registry.get(workflow_key, workflow_version)
        if definition is None:
            raise ValidationFailedError(
                "unknown workflow",
                details={"workflow_key": workflow_key, "workflow_version": workflow_version},
            )

        async with self._uow:
            project = await self._uow.projects.get_owned(
                project_id=project_id,
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
            )
            if project is None:
                raise NotFoundError(
                    "project not found",
                    details={"project_id": str(project_id)},
                )

            # Idempotency pre-check (Q7): a repeat key returns the existing run.
            if idempotency_key is not None:
                existing = await self._uow.workflow_runs.get_by_project_and_key(
                    project_id, idempotency_key
                )
                if existing is not None:
                    steps = await self._uow.workflow_runs.list_steps(existing.id)
                    latest = await self._uow.workflow_runs.latest_checkpoint(existing.id)
                    _LOGGER.info(
                        "workflow_run.create_idempotent_replay",
                        workflow_run_id=str(existing.id),
                        project_id=str(project_id),
                        idempotency_key=idempotency_key,
                        owner_user_id=str(owner_user_id),
                        ip=ip,
                    )
                    return CreateWorkflowRunResult(
                        view=WorkflowRunView(run=existing, steps=steps, latest_checkpoint=latest),
                        created=False,
                    )

            try:
                run = await self._uow.workflow_runs.add(
                    project_id=project_id,
                    workflow_key=workflow_key,
                    workflow_version=workflow_version,
                    status=WorkflowRunStatus.QUEUED.value,
                    input_snapshot=input_snapshot,
                    triggered_by_user_id=owner_user_id,
                    idempotency_key=idempotency_key,
                )
            except ConflictError:
                # Race: a concurrent request inserted the same
                # (project_id, idempotency_key) between our pre-check and insert.
                # Resolve idempotently by returning the winner (Q7).
                assert idempotency_key is not None
                winner = await self._uow.workflow_runs.get_by_project_and_key(
                    project_id, idempotency_key
                )
                if winner is None:  # pragma: no cover — constraint says it exists
                    raise
                steps = await self._uow.workflow_runs.list_steps(winner.id)
                latest = await self._uow.workflow_runs.latest_checkpoint(winner.id)
                _LOGGER.info(
                    "workflow_run.create_idempotent_race",
                    workflow_run_id=str(winner.id),
                    project_id=str(project_id),
                    idempotency_key=idempotency_key,
                    owner_user_id=str(owner_user_id),
                    ip=ip,
                )
                return CreateWorkflowRunResult(
                    view=WorkflowRunView(run=winner, steps=steps, latest_checkpoint=latest),
                    created=False,
                )

            # Seed the ordered steps (pending) from the definition, then the event.
            seeded = await self._uow.workflow_runs.seed_steps(run.id, definition.step_specs)
            await emit_workflow_run_created(self._uow, run, actor_user_id=owner_user_id)
            await self._uow.commit()

        _LOGGER.info(
            "workflow_run.created",
            workflow_run_id=str(run.id),
            project_id=str(project_id),
            workflow_key=workflow_key,
            workflow_version=workflow_version,
            step_count=len(seeded),
            idempotency_key=idempotency_key,
            owner_user_id=str(owner_user_id),
            ip=ip,
        )
        return CreateWorkflowRunResult(
            view=WorkflowRunView(run=run, steps=seeded, latest_checkpoint=None),
            created=True,
        )
