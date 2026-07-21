"""``ResumeWorkflowRun`` — the public resume seam for async completion (Slice α8.3).

The α7.6 pause seam stops a run at ``paused`` with an in-flight provider job; α8.3's
completion engine resolves that job and hands the terminal :class:`ProviderResponse`
here. This use case owns the **atomic resume transaction** — the single public
entrypoint every completion mechanism (polling now; webhook, manual resume, admin
replay later) converges on, so none of them reach into the runner's internals:

    CompletionEngine → ResumeWorkflowRun → AdvanceWorkflowRun

In one transaction it:

1. re-reads the run and no-ops idempotently if it is no longer ``paused``
   (a concurrent resume — poll racing a webhook — already handled it);
2. takes the run ``paused → running`` via a status-guarded CAS (:meth:`resume_run`)
   — the exactly-once gate: the loser writes nothing;
3. records the deferred **terminal usage** row under the *checkpointed* ``request_id``
   (Fork 1A coordinates from the ``_paused`` handoff — never a handler re-run);
4. completes the paused step (``mark_step_succeeded`` / ``mark_step_failed``);
5. emits ``WorkflowRunResumed``; then
6. on success **delegates continuation to the runner** via its public
   :meth:`AdvanceWorkflowRun.continue_paused_run_in_uow` (same open UoW → resume +
   continue + settle commit atomically, so a crash cannot strand a ``running`` run
   the paused-only poller would never revisit); on failure it settles the run failed
   itself (a pre-failed step must not be driven — the runner would skip it).

The runner's step-execution semantics are untouched; exactly one copy exists.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

import structlog

from app.application.interfaces.providers import Capability, ProviderResponse, ProviderStatus
from app.application.interfaces.unit_of_work import IUnitOfWork
from app.application.interfaces.usage_recorder import RecordUsageCommand
from app.application.use_cases.usage.usage_recorder_service import (
    DEFAULT_CURRENCY,
    record_usage_in_uow,
)
from app.application.use_cases.workflow._events import (
    emit_workflow_run_failed,
    emit_workflow_run_resumed,
)
from app.application.use_cases.workflow._view import WorkflowRunView
from app.application.use_cases.workflow.advance_workflow_run import AdvanceWorkflowRun
from app.core.errors import ConflictError
from app.domain.workflow.registry import WORKFLOW_REGISTRY, WorkflowRegistry
from app.domain.workflow.workflow_run_status import WorkflowRunStatus

_LOGGER = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ResumeWorkflowRunResult:
    """The run view after a resume attempt plus whether this call actually resumed.

    ``resumed`` is ``False`` for an idempotent no-op — the run was not ``paused`` (a
    concurrent completion already handled it, or it was never paused). The view still
    reflects the run's current state so the caller can report it.
    """

    view: WorkflowRunView
    resumed: bool


class ResumeWorkflowRun:
    """Atomically resume a paused run from a resolved provider result, then drive it."""

    def __init__(
        self,
        uow: IUnitOfWork,
        runner: AdvanceWorkflowRun,
        registry: WorkflowRegistry = WORKFLOW_REGISTRY,
        *,
        default_currency: str = DEFAULT_CURRENCY,
    ) -> None:
        # ``runner`` MUST share ``uow`` — the continuation participates in this
        # use case's open transaction (that atomicity is the whole point).
        self._uow = uow
        self._runner = runner
        self._registry = registry
        self._default_currency = default_currency

    async def execute(
        self,
        *,
        project_id: UUID,
        workflow_run_id: UUID,
        resolved: ProviderResponse,
    ) -> ResumeWorkflowRunResult:
        if resolved.status is ProviderStatus.IN_PROGRESS:
            # Defensive: the completion engine must only hand terminal results here.
            raise ValueError(
                "ResumeWorkflowRun requires a terminal provider result; got IN_PROGRESS "
                f"(workflow_run_id={workflow_run_id})"
            )

        async with self._uow:
            run = await self._uow.workflow_runs.get_owned(project_id, workflow_run_id)
            if run is None:
                raise ConflictError(
                    "workflow run not found for resume",
                    details={"workflow_run_id": str(workflow_run_id)},
                )

            if WorkflowRunStatus(run.status) is not WorkflowRunStatus.PAUSED:
                # Idempotent no-op: a concurrent completion already resumed/settled it
                # (or it was never paused). No writes; report the current state.
                final_steps = await self._uow.workflow_runs.list_steps(run.id)
                latest = await self._uow.workflow_runs.latest_checkpoint(run.id)
                _LOGGER.info(
                    "workflow_run.resume_noop_not_paused",
                    workflow_run_id=str(run.id),
                    status=run.status,
                )
                return ResumeWorkflowRunResult(
                    view=WorkflowRunView(run=run, steps=final_steps, latest_checkpoint=latest),
                    resumed=False,
                )

            handoff = await self._load_pause_handoff(run.id)
            definition = self._registry.get(run.workflow_key, run.workflow_version)
            if definition is None:  # pragma: no cover - registry drift (mirrors runner)
                raise ConflictError(
                    "workflow definition is no longer available",
                    details={
                        "workflow_key": run.workflow_key,
                        "workflow_version": run.workflow_version,
                    },
                )

            # Exactly-once gate (CAS paused → running). A concurrent resume that won
            # the flip leaves us with ``None`` → no-op replay, no writes.
            resumed_run = await self._uow.workflow_runs.resume_run(run.id)
            if resumed_run is None:
                final_steps = await self._uow.workflow_runs.list_steps(run.id)
                latest = await self._uow.workflow_runs.latest_checkpoint(run.id)
                current = await self._uow.workflow_runs.get_owned(project_id, workflow_run_id)
                _LOGGER.info(
                    "workflow_run.resume_lost_cas",
                    workflow_run_id=str(run.id),
                    status=current.status if current is not None else "unknown",
                )
                return ResumeWorkflowRunResult(
                    view=WorkflowRunView(
                        run=current if current is not None else run,
                        steps=final_steps,
                        latest_checkpoint=latest,
                    ),
                    resumed=False,
                )
            run = resumed_run

            await emit_workflow_run_resumed(
                self._uow,
                run,
                step_index=handoff.pending_step_index,
                provider_job_id=resolved.provider_job_id or handoff.provider_job_id,
                actor_user_id=None,  # completion is a system action (no user actor)
            )

            # Deferred terminal usage (Q6) — under the *checkpointed* request_id, not
            # the resolve() response's (which is empty). Idempotent on request_id.
            await record_usage_in_uow(
                self._uow,
                RecordUsageCommand(
                    tenant_id=handoff.tenant_id,
                    model_id=handoff.model_id,
                    status=resolved.status,
                    request_id=handoff.request_id,
                    capability=handoff.capability,
                    usage=resolved.usage,
                    project_id=run.project_id,
                    workflow_run_id=run.id,
                ),
                default_currency=self._default_currency,
            )

            if resolved.status is ProviderStatus.SUCCEEDED:
                # Complete the paused step with the opaque terminal envelope, then let
                # the runner drive the remaining steps + settle — all in this txn.
                output = {"provider_outputs": [self._completion_envelope(resolved, handoff)]}
                await self._uow.workflow_runs.mark_step_succeeded(
                    run.id, handoff.pending_step_index, output
                )
                result = await self._runner.continue_paused_run_in_uow(
                    run, definition, tenant_id=handoff.tenant_id, actor_user_id=None
                )
                await self._uow.commit()
                _LOGGER.info(
                    "workflow_run.resumed",
                    workflow_run_id=str(result.view.run.id),
                    status=result.view.run.status,
                    step_index=handoff.pending_step_index,
                )
                return ResumeWorkflowRunResult(view=result.view, resumed=True)

            # FAILED — fail the paused step + the run here. The runner is NOT delegated
            # to: a ``failed`` step is ``is_done`` and would be skipped, mis-settling.
            # The run-level error mirrors the runner's ``_drive_and_settle`` shape
            # (``{step_index, step_name, reason, error}``) so a failed run's observable
            # error is identical whether it failed inline (α7.6) or on completion (α8.3).
            step_error = {
                "code": "PROVIDER_FAILED",
                "message": resolved.error or "provider returned FAILED",
                "provider": resolved.provider,
                "request_id": handoff.request_id,
            }
            await self._uow.workflow_runs.mark_step_failed(
                run.id, handoff.pending_step_index, step_error
            )
            step_name = (
                definition.steps[handoff.pending_step_index].name
                if 0 <= handoff.pending_step_index < len(definition.steps)
                else str(handoff.pending_step_index)
            )
            run_error = {
                "step_index": handoff.pending_step_index,
                "step_name": step_name,
                "reason": "terminal",
                "error": step_error,
            }
            settled = await self._uow.workflow_runs.mark_run_failed(run.id, run_error)
            run = settled if settled is not None else run
            await emit_workflow_run_failed(self._uow, run, actor_user_id=None)
            final_steps = await self._uow.workflow_runs.list_steps(run.id)
            latest = await self._uow.workflow_runs.latest_checkpoint(run.id)
            await self._uow.commit()
            _LOGGER.info(
                "workflow_run.resumed_failed",
                workflow_run_id=str(run.id),
                status=run.status,
                step_index=handoff.pending_step_index,
            )
            return ResumeWorkflowRunResult(
                view=WorkflowRunView(run=run, steps=final_steps, latest_checkpoint=latest),
                resumed=True,
            )

    async def _load_pause_handoff(self, workflow_run_id: UUID) -> _PauseHandoff:
        """Read the enriched ``_paused`` block (Fork 1A) from the latest checkpoint."""
        checkpoint = await self._uow.workflow_runs.latest_checkpoint(workflow_run_id)
        state = checkpoint.state if checkpoint is not None else None
        paused = state.get("_paused") if isinstance(state, dict) else None
        if not isinstance(paused, dict):
            raise ConflictError(
                "paused run has no resume checkpoint",
                details={"workflow_run_id": str(workflow_run_id)},
            )
        try:
            return _PauseHandoff(
                pending_step_index=int(paused["pending_step_index"]),
                request_id=str(paused["request_id"]),
                provider_job_id=(
                    str(paused["provider_job_id"])
                    if paused.get("provider_job_id") is not None
                    else None
                ),
                command_index=int(paused["command_index"]),
                capability=Capability(str(paused["capability"])),
                model_id=UUID(str(paused["model_id"])),
                tenant_id=UUID(str(paused["tenant_id"])),
            )
        except (KeyError, ValueError, TypeError) as exc:
            raise ConflictError(
                "paused run checkpoint is malformed",
                details={"workflow_run_id": str(workflow_run_id), "reason": str(exc)},
            ) from exc

    @staticmethod
    def _completion_envelope(resolved: ProviderResponse, handoff: _PauseHandoff) -> dict[str, Any]:
        """Opaque terminal envelope stored on the completed step (mirrors the runner's)."""
        return {
            "provider": resolved.provider,
            "request_id": handoff.request_id,
            "status": resolved.status.value,
            "provider_job_id": resolved.provider_job_id or handoff.provider_job_id,
            "output": dict(resolved.output),
        }


@dataclass(frozen=True, slots=True)
class _PauseHandoff:
    """The immutable resume coordinates read from the ``_paused`` checkpoint (Fork 1A)."""

    pending_step_index: int
    request_id: str
    provider_job_id: str | None
    command_index: int
    capability: Capability
    model_id: UUID
    tenant_id: UUID
