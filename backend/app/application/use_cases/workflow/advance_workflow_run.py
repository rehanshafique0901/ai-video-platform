"""``AdvanceWorkflowRun`` — the synchronous deterministic runner (Slice α7.2).

Contract (API_CONTRACT §3.2.6):

    POST /api/v1/projects/{project_id}/workflow-runs/{workflow_run_id}/advance
      → 200  { data: WorkflowRunPublic (ran to succeeded/failed), meta }
      → 404  { error: { code: NOT_FOUND, ... } }        (project/run missing / not yours)
      → 409  { error: { code: CONFLICT, ... } }         (already terminal / definition gone)
      → 401  { error: { code: UNAUTHENTICATED, ... } }  (via CurrentUserDep)

This is the **imperative shell** around the pure step handlers (D3.11). It is the
*only* place that touches the DB / outbox; the handlers it calls are pure
functions that return a :class:`~app.domain.workflow.registry.StepResult`
describing what should happen. There are **no external providers, no async worker,
no scheduler** — advance runs to a terminal state within one synchronous call, in
one UnitOfWork transaction (so the whole run either commits terminal or rolls
back).

Algorithm (D3.8):

1. Project gate (404) → run visibility (404) → resolve the definition.
2. Precondition: only ``queued`` (start) or ``running`` (resume) may advance;
   terminal/paused → ``409``. A ``queued`` run is taken ``queued → running`` via a
   status-guarded CAS and a ``WorkflowRunStarted`` event.
3. For each step in ``step_index`` order: skip already-``succeeded``/``skipped``
   steps (resume-safety, Q7); otherwise mark ``running`` and call the handler with
   ``(input_snapshot, prior checkpoint state, attempt)``. On success → persist
   ``output`` + append a checkpoint + emit ``WorkflowStepCompleted``. On a transient
   failure → bump ``retries`` / ``retrying`` and retry up to the definition bound
   (deterministic, no backoff — Q5); on exhaustion or a terminal failure → fail the
   step and the run.
4. All steps done → ``running → succeeded`` + ``output_summary`` +
   ``WorkflowRunSucceeded``; any terminal step failure → ``running → failed`` +
   ``error`` + ``WorkflowRunFailed``.

Concurrency is the status-guarded CAS (D3.2): a second concurrent advance finds no
runnable step in the expected state and is re-classified rather than double-running.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import structlog

from app.application.interfaces.provider_dispatcher import ProviderDispatcherPort
from app.application.interfaces.providers import (
    Capability,
    ProviderError,
    ProviderResponse,
    ProviderStatus,
)
from app.application.interfaces.unit_of_work import IUnitOfWork
from app.application.interfaces.usage_recorder import RecordUsageCommand
from app.application.use_cases.usage.usage_recorder_service import (
    DEFAULT_CURRENCY,
    record_usage_in_uow,
)
from app.application.use_cases.workflow._events import (
    emit_workflow_run_failed,
    emit_workflow_run_paused,
    emit_workflow_run_started,
    emit_workflow_run_succeeded,
    emit_workflow_step_completed,
)
from app.application.use_cases.workflow._view import WorkflowRunView
from app.core.errors import ConflictError, NotFoundError
from app.domain.workflow.registry import (
    WORKFLOW_REGISTRY,
    StepCommand,
    StepContext,
    StepDefinition,
    StepOutcome,
    StepResult,
    WorkflowDefinition,
    WorkflowRegistry,
)
from app.domain.workflow.workflow_run import WorkflowRun
from app.domain.workflow.workflow_run_status import WorkflowRunStatus
from app.domain.workflow.workflow_step_status import WorkflowStepStatus

_LOGGER = structlog.get_logger(__name__)

# The runner's own ``command.kind → Capability`` map (mirrors the dispatcher's closed
# table). Used only to tag the usage record; the runner never inspects the provider
# *payload* (W7.6.1) — capability is derived from the command it minted, not the
# response. A kind absent here is not a provider capability → the step fails terminally.
_KIND_TO_CAPABILITY: dict[str, Capability] = {
    "generate_text": Capability.LLM,
    "generate_image": Capability.IMAGE,
    "generate_video": Capability.VIDEO,
    "synthesize_voice": Capability.VOICE,
}


@dataclass(frozen=True, slots=True)
class _PauseInfo:
    """The async-pause coordinates the runner persists + emits (α7.6 Q2/Q8; α8.3 Fork 1A).

    α8.3 enriches the handoff with the ``command_index`` / ``capability`` / ``model_id``
    the completion engine needs to record the deferred **terminal usage** row under
    the same ``request_id`` — so completion is deterministic and never re-runs the
    pure handler to rediscover them.
    """

    step_index: int
    step_name: str
    provider: str
    request_id: str
    provider_job_id: str | None
    command_index: int
    capability: str
    model_id: str
    tenant_id: str
    # The opaque submit envelope (verbatim ``resp.output`` — W7.6.1, never inspected)
    # the completion engine hands back to the provider's ``resolve`` (Fal
    # ``status_url`` / ``response_url``; mock ``provider_job_id``).
    envelope: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _CommandsResult:
    """Outcome of dispatching a step's commands (α7.6).

    ``kind`` is one of ``"succeeded"`` / ``"transient"`` / ``"terminal"`` /
    ``"paused"``. ``provider_outputs`` carries the **opaque** response envelopes to
    checkpoint (W7.6.1); ``error`` is set on a failure; ``pause`` on ``IN_PROGRESS``.
    """

    kind: str
    provider_outputs: list[dict[str, Any]] = field(default_factory=list)
    error: dict[str, Any] | None = None
    pause: _PauseInfo | None = None


@dataclass(frozen=True, slots=True)
class _StepExec:
    """Outcome of executing one step (handler + its commands).

    ``status`` is ``"succeeded"`` / ``"failed"`` / ``"paused"``. On success,
    ``checkpoint_state`` threads into the next step; on failure, ``failure_error``
    settles the run; on pause, ``pause`` carries the resume coordinates.
    """

    status: str
    checkpoint_state: dict[str, Any] | None = None
    failure_error: dict[str, Any] | None = None
    pause: _PauseInfo | None = None


@dataclass(frozen=True, slots=True)
class _RunStepsOutcome:
    """Aggregate outcome of the step loop: failure / pause / clean, plus names done."""

    failure_error: dict[str, Any] | None
    completed: list[str]
    pause: _PauseInfo | None


def _response_view(resp: ProviderResponse) -> dict[str, Any]:
    """Project a :class:`ProviderResponse` to an **opaque** checkpoint envelope (W7.6.1).

    The runner passes the response through verbatim — provider key, request id,
    status, job id, and the whole ``output`` bag — **without inspecting any
    payload-specific key** (no ``image_ref`` / ``video_ref`` reads). Payload meaning
    belongs to the dispatcher/provider adapter, so the runner stays provider-agnostic.
    """
    return {
        "provider": resp.provider,
        "request_id": resp.request_id,
        "status": resp.status.value,
        "provider_job_id": resp.provider_job_id,
        "output": dict(resp.output),
    }


def _resolve_model_id(command: StepCommand) -> UUID | None:
    """Extract the command's ``model_id`` (Q4). ``None`` if absent/unparseable → fail fast."""
    raw = command.args.get("model_id")
    if raw is None:
        return None
    try:
        return UUID(str(raw))
    except (ValueError, TypeError):
        return None


@dataclass(frozen=True, slots=True)
class AdvanceWorkflowRunResult:
    """The run view after advancement plus whether this call did any work.

    ``advanced`` is ``True`` when the runner executed at least the start transition
    (queued → running) or a step; ``False`` is reserved for a resume that found
    nothing runnable (all steps already done) — still a ``200``.
    """

    view: WorkflowRunView
    advanced: bool


class AdvanceWorkflowRun:
    """Drive a workflow run to a terminal (or paused) state using its definition (D3.8).

    α7.6 extends the α7.2 shell: after a pure step handler succeeds, the runner now
    interprets its ``StepResult.commands`` — minting a deterministic ``request_id``
    (``run_id:step_index:command_index``, D5), dispatching each **exactly once**
    (W7.6.2) via the injected :class:`ProviderDispatcherPort`, recording terminal
    usage in the **same** transaction (Q5), and either pausing on ``IN_PROGRESS``
    (Q2) or checkpointing the opaque provider envelope (W7.6.1). When no dispatcher
    is injected (α7.2 deterministic workflows, which emit no commands) behaviour is
    unchanged.
    """

    def __init__(
        self,
        uow: IUnitOfWork,
        registry: WorkflowRegistry = WORKFLOW_REGISTRY,
        *,
        dispatcher: ProviderDispatcherPort | None = None,
        default_currency: str = DEFAULT_CURRENCY,
    ) -> None:
        self._uow = uow
        self._registry = registry
        self._dispatcher = dispatcher
        self._default_currency = default_currency

    async def execute(
        self,
        *,
        project_id: UUID,
        workflow_run_id: UUID,
        owner_user_id: UUID,
        tenant_id: UUID,
        ip: str | None = None,
    ) -> AdvanceWorkflowRunResult:
        async with self._uow:
            project = await self._uow.projects.get_owned(
                project_id=project_id,
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
            )
            if project is None:
                raise NotFoundError(
                    "workflow run not found",
                    details={"workflow_run_id": str(workflow_run_id)},
                )

            run = await self._uow.workflow_runs.get_owned(project_id, workflow_run_id)
            if run is None:
                raise NotFoundError(
                    "workflow run not found",
                    details={"workflow_run_id": str(workflow_run_id)},
                )

            definition = self._registry.get(run.workflow_key, run.workflow_version)
            if definition is None:
                # The run was created against a registered definition; if it's gone
                # now the run cannot be advanced (registry drift).
                raise ConflictError(
                    "workflow definition is no longer available",
                    details={
                        "workflow_key": run.workflow_key,
                        "workflow_version": run.workflow_version,
                    },
                )

            status = WorkflowRunStatus(run.status)
            if status.is_terminal:
                _LOGGER.info(
                    "workflow_run.advance_noop_terminal",
                    workflow_run_id=str(run.id),
                    project_id=str(project_id),
                    status=run.status,
                    owner_user_id=str(owner_user_id),
                    ip=ip,
                )
                raise ConflictError(
                    "workflow run is already in a terminal state",
                    details={"workflow_run_id": str(workflow_run_id), "status": run.status},
                )
            if not status.is_advanceable:
                # ``paused`` is not produced by the α7.2 runner and is not
                # advanceable here (pause/resume is deferred to α8.x, Q4).
                raise ConflictError(
                    "workflow run cannot be advanced from its current state",
                    details={"workflow_run_id": str(workflow_run_id), "status": run.status},
                )

            advanced = False

            # Start transition (queued → running) — status-guarded CAS + event.
            if status is WorkflowRunStatus.QUEUED:
                started = await self._uow.workflow_runs.mark_run_running(run.id)
                if started is None:
                    # Concurrent advance/cancel raced us out of ``queued``.
                    current = await self._uow.workflow_runs.get_owned(project_id, run.id)
                    raise ConflictError(
                        "workflow run is no longer queued",
                        details={
                            "workflow_run_id": str(workflow_run_id),
                            "status": current.status if current is not None else "unknown",
                        },
                    )
                run = started
                await emit_workflow_run_started(self._uow, run, actor_user_id=owner_user_id)
                advanced = True

            # Execute the step graph in order + settle — on this open UoW (Q5).
            result = await self._drive_and_settle(
                run,
                definition,
                tenant_id=tenant_id,
                actor_user_id=owner_user_id,
                advanced=advanced,
            )
            await self._uow.commit()

        _LOGGER.info(
            "workflow_run.advanced",
            workflow_run_id=str(result.view.run.id),
            project_id=str(project_id),
            status=result.view.run.status,
            owner_user_id=str(owner_user_id),
            ip=ip,
        )
        return result

    async def continue_paused_run_in_uow(
        self,
        run: WorkflowRun,
        definition: WorkflowDefinition,
        *,
        tenant_id: UUID,
        actor_user_id: UUID | None,
    ) -> AdvanceWorkflowRunResult:
        """Drive an already-``running`` run's remaining steps to settlement (α8.3).

        The **public** continuation seam: it runs the *same* step loop + settle as
        :meth:`execute`, but on the **caller's already-open UoW** — it does **not**
        open a transaction and does **not** commit. This lets :class:`ResumeWorkflowRun`
        commit *resume + terminal usage + step-succeeded + continuation + settle*
        atomically in one transaction, without any service reaching into the runner's
        private step-execution helpers. The caller must have already taken the run
        ``paused → running`` (and marked/rolled the resumed step). Already-``succeeded``
        steps are skipped (resume-safety, §3), so re-entry is safe.
        """
        return await self._drive_and_settle(
            run, definition, tenant_id=tenant_id, actor_user_id=actor_user_id, advanced=True
        )

    async def _drive_and_settle(
        self,
        run: WorkflowRun,
        definition: WorkflowDefinition,
        *,
        tenant_id: UUID,
        actor_user_id: UUID | None,
        advanced: bool,
    ) -> AdvanceWorkflowRunResult:
        """Run the step graph in order, settle the run, and build the view — no commit.

        The shared core of :meth:`execute` and :meth:`continue_paused_run_in_uow`
        (extracted α8.3). Participates in ``self._uow`` (already open); the caller
        owns the transaction boundary.
        """
        steps = await self._uow.workflow_runs.list_steps(run.id)
        outcome = await self._run_steps(
            run, definition, steps, tenant_id=tenant_id, actor_user_id=actor_user_id
        )
        advanced = (
            advanced
            or bool(outcome.completed)
            or outcome.failure_error is not None
            or outcome.pause is not None
        )

        if outcome.pause is not None:
            # Async pause seam (Q2): persist ``paused`` + the checkpointed
            # ``provider_job_id`` (already appended by the step) and stop. The step
            # stays ``running`` (the provider job is still in flight); the α8.3
            # completion service resolves it under the same ``request_id``.
            settled = await self._uow.workflow_runs.mark_run_paused(run.id)
            run = settled if settled is not None else run
            await emit_workflow_run_paused(
                self._uow,
                run,
                step_index=outcome.pause.step_index,
                provider_job_id=outcome.pause.provider_job_id,
                actor_user_id=actor_user_id,
            )
        elif outcome.failure_error is not None:
            settled = await self._uow.workflow_runs.mark_run_failed(run.id, outcome.failure_error)
            run = settled if settled is not None else run
            await emit_workflow_run_failed(self._uow, run, actor_user_id=actor_user_id)
        else:
            summary: dict[str, Any] = {
                "step_count": len(steps),
                "completed_steps": outcome.completed,
            }
            settled = await self._uow.workflow_runs.mark_run_succeeded(run.id, summary)
            run = settled if settled is not None else run
            await emit_workflow_run_succeeded(self._uow, run, actor_user_id=actor_user_id)

        final_steps = await self._uow.workflow_runs.list_steps(run.id)
        latest = await self._uow.workflow_runs.latest_checkpoint(run.id)
        return AdvanceWorkflowRunResult(
            view=WorkflowRunView(run=run, steps=final_steps, latest_checkpoint=latest),
            advanced=advanced,
        )

    async def _run_steps(
        self,
        run: WorkflowRun,
        definition: WorkflowDefinition,
        steps: list[Any],
        *,
        tenant_id: UUID,
        actor_user_id: UUID | None,
    ) -> _RunStepsOutcome:
        """Execute pending steps in order; return the aggregate outcome.

        ``prior_state`` threads the previous step's checkpoint state into the next
        step's context (the resume point). Already-``succeeded``/``skipped`` steps
        are skipped (resume-safety) but still contribute their checkpoint state. A
        step that pauses (``IN_PROGRESS`` command) stops the loop immediately.
        """
        prior_state: dict[str, Any] | None = None
        completed: list[str] = []

        for step in steps:
            step_status = WorkflowStepStatus(step.status)
            if step_status.is_done:
                cp = await self._uow.workflow_runs.latest_checkpoint(run.id, step.step_index)
                if cp is not None:
                    prior_state = cp.state
                if step_status is WorkflowStepStatus.SUCCEEDED:
                    completed.append(step.step_name)
                continue

            if step.step_index >= len(definition.steps):  # pragma: no cover - seeding guard
                return _RunStepsOutcome(
                    failure_error={
                        "code": "DEFINITION_MISMATCH",
                        "message": f"no definition for step_index {step.step_index}",
                        "step_index": step.step_index,
                    },
                    completed=completed,
                    pause=None,
                )
            step_def = definition.steps[step.step_index]

            exec_result = await self._run_single_step(
                run, step, step_def, prior_state, tenant_id=tenant_id, actor_user_id=actor_user_id
            )
            if exec_result.status == "failed":
                return _RunStepsOutcome(
                    failure_error=exec_result.failure_error, completed=completed, pause=None
                )
            if exec_result.status == "paused":
                return _RunStepsOutcome(
                    failure_error=None, completed=completed, pause=exec_result.pause
                )
            prior_state = exec_result.checkpoint_state
            completed.append(step.step_name)

        return _RunStepsOutcome(failure_error=None, completed=completed, pause=None)

    async def _run_single_step(
        self,
        run: WorkflowRun,
        step: Any,
        step_def: StepDefinition,
        prior_state: dict[str, Any] | None,
        *,
        tenant_id: UUID,
        actor_user_id: UUID | None,
    ) -> _StepExec:
        """Run one step (handler + its commands), with retries. Return its outcome."""
        while True:
            running = await self._uow.workflow_runs.mark_step_running(run.id, step.step_index)
            if running is None:  # pragma: no cover - unreachable in the sync single-txn model
                return _StepExec(
                    status="failed",
                    failure_error={
                        "code": "STEP_NOT_RUNNABLE",
                        "message": f"step {step.step_index} could not be started",
                        "step_index": step.step_index,
                    },
                )

            ctx = StepContext(
                run_input=run.input_snapshot,
                prior_state=prior_state,
                attempt=running.retries,
            )
            result = step_def.handler(ctx)

            if result.outcome is StepOutcome.SUCCEEDED:
                # α7.6: interpret the pure step's declarative commands (dispatch →
                # record usage → checkpoint the opaque envelope). A provider transient
                # error re-runs the whole (pure) step, re-emitting the same command
                # with the same deterministic request_id (W7.6.2) — so no layer
                # double-retries and the recorder dedupes the replay.
                cmd_result = await self._execute_commands(run, step, result, tenant_id=tenant_id)

                if cmd_result.kind == "transient":
                    error = cmd_result.error or {"code": "PROVIDER_TRANSIENT"}
                    if running.retries < step_def.max_retries:
                        await self._uow.workflow_runs.mark_step_retrying(
                            run.id, step.step_index, error
                        )
                        continue
                    await self._uow.workflow_runs.mark_step_failed(run.id, step.step_index, error)
                    return _StepExec(
                        status="failed",
                        failure_error={
                            "step_index": step.step_index,
                            "step_name": step.step_name,
                            "reason": "retries_exhausted",
                            "error": error,
                        },
                    )

                if cmd_result.kind == "terminal":
                    error = cmd_result.error or {"code": "PROVIDER_TERMINAL"}
                    await self._uow.workflow_runs.mark_step_failed(run.id, step.step_index, error)
                    return _StepExec(
                        status="failed",
                        failure_error={
                            "step_index": step.step_index,
                            "step_name": step.step_name,
                            "reason": "terminal",
                            "error": error,
                        },
                    )

                if cmd_result.kind == "paused":
                    # Pause seam (Q2): checkpoint the resume coordinates and stop.
                    # The step is left ``running`` (the provider job is still in
                    # flight); no usage is recorded (Q6 — terminal-only). No
                    # ``mark_step_succeeded``, no ``WorkflowStepCompleted``.
                    assert cmd_result.pause is not None
                    checkpoint_state = {
                        **result.checkpoint_state,
                        "provider_outputs": cmd_result.provider_outputs,
                        "_paused": {
                            "provider": cmd_result.pause.provider,
                            "request_id": cmd_result.pause.request_id,
                            "provider_job_id": cmd_result.pause.provider_job_id,
                            "pending_step_index": step.step_index,
                            # Fork 1A: immutable coordinates the α8.3 completion engine
                            # reads to record terminal usage without re-running handlers.
                            "command_index": cmd_result.pause.command_index,
                            "capability": cmd_result.pause.capability,
                            "model_id": cmd_result.pause.model_id,
                            "tenant_id": cmd_result.pause.tenant_id,
                            # Opaque submit envelope for the completion engine's resolve.
                            "envelope": cmd_result.pause.envelope,
                        },
                    }
                    await self._uow.workflow_runs.append_checkpoint(
                        run.id, step.step_index, checkpoint_state
                    )
                    return _StepExec(status="paused", pause=cmd_result.pause)

                # Clean success — persist output + the opaque provider envelopes.
                output = {**result.output}
                if cmd_result.provider_outputs:
                    output["provider_outputs"] = cmd_result.provider_outputs
                await self._uow.workflow_runs.mark_step_succeeded(run.id, step.step_index, output)
                checkpoint_state = {**result.checkpoint_state}
                if cmd_result.provider_outputs:
                    checkpoint_state["provider_outputs"] = cmd_result.provider_outputs
                cp = await self._uow.workflow_runs.append_checkpoint(
                    run.id, step.step_index, checkpoint_state
                )
                await emit_workflow_step_completed(
                    self._uow,
                    run,
                    step_index=step.step_index,
                    step_name=step.step_name,
                    actor_user_id=actor_user_id,
                )
                return _StepExec(status="succeeded", checkpoint_state=cp.state)

            error = result.error if result.error is not None else {"code": "UNKNOWN"}

            if result.outcome is StepOutcome.TRANSIENT_FAILURE:
                if running.retries < step_def.max_retries:
                    await self._uow.workflow_runs.mark_step_retrying(run.id, step.step_index, error)
                    continue  # retry with an incremented attempt counter
                await self._uow.workflow_runs.mark_step_failed(run.id, step.step_index, error)
                return _StepExec(
                    status="failed",
                    failure_error={
                        "step_index": step.step_index,
                        "step_name": step.step_name,
                        "reason": "retries_exhausted",
                        "error": error,
                    },
                )

            # TERMINAL_FAILURE
            await self._uow.workflow_runs.mark_step_failed(run.id, step.step_index, error)
            return _StepExec(
                status="failed",
                failure_error={
                    "step_index": step.step_index,
                    "step_name": step.step_name,
                    "reason": "terminal",
                    "error": error,
                },
            )

    async def _execute_commands(
        self,
        run: WorkflowRun,
        step: Any,
        result: StepResult,
        *,
        tenant_id: UUID,
    ) -> _CommandsResult:
        """Dispatch a step's commands once each (W7.6.2), recording terminal usage (D3/Q9).

        Ownership (D1/W7.6.1): runner → dispatcher → provider. The runner mints the
        deterministic ``request_id`` (D5), never inspects the provider payload, and
        forwards ``resp.usage`` / ``resp.status`` to the recorder. ``IN_PROGRESS``
        pauses (Q2); ``FAILED`` records a terminal failed usage row then fails the
        step (Q9); a transient :class:`ProviderError` bubbles up as ``"transient"``
        for the runner's retry; a terminal one (or a malformed command) fails.
        """
        if not result.commands:
            return _CommandsResult(kind="succeeded")

        if self._dispatcher is None:  # pragma: no cover - DI always injects it
            return _CommandsResult(
                kind="terminal",
                error={
                    "code": "NO_DISPATCHER",
                    "message": "step emitted commands but no provider dispatcher is configured",
                    "step_index": step.step_index,
                },
            )

        provider_outputs: list[dict[str, Any]] = []
        for command_index, command in enumerate(result.commands):
            capability = _KIND_TO_CAPABILITY.get(command.kind)
            if capability is None:
                return _CommandsResult(
                    kind="terminal",
                    error={
                        "code": "UNSUPPORTED_COMMAND",
                        "message": f"command kind {command.kind!r} is not a provider capability",
                        "command_kind": command.kind,
                    },
                )

            # Fail fast if the command carries no usable ``model_id`` (Q4): usage
            # cannot be priced without a real ``ai_models`` row, so a generation
            # command without one is malformed — a terminal failure, before dispatch.
            model_id = _resolve_model_id(command)
            if model_id is None:
                return _CommandsResult(
                    kind="terminal",
                    error={
                        "code": "MODEL_ID_MISSING",
                        "message": f"command {command.kind!r} is missing a valid 'model_id'",
                        "command_kind": command.kind,
                    },
                )

            # D5: runner-minted, deterministic request_id (replay-stable). Injected
            # into a fresh command; the pure handler never sees orchestration identity.
            request_id = f"{run.id}:{step.step_index}:{command_index}"
            dispatch_command = StepCommand(
                kind=command.kind, args={**command.args, "request_id": request_id}
            )

            try:
                # W7.6.2: exactly one dispatch per command (no dispatcher-side retry).
                resp = await self._dispatcher.dispatch(dispatch_command)
            except ProviderError as exc:
                return _CommandsResult(
                    kind="transient" if exc.transient else "terminal",
                    provider_outputs=provider_outputs,
                    error={
                        "code": type(exc).__name__,
                        "message": str(exc),
                        "command_kind": command.kind,
                        "transient": exc.transient,
                    },
                )

            if resp.status is ProviderStatus.IN_PROGRESS:
                return _CommandsResult(
                    kind="paused",
                    provider_outputs=provider_outputs,
                    pause=_PauseInfo(
                        step_index=step.step_index,
                        step_name=step.step_name,
                        provider=resp.provider,
                        request_id=resp.request_id,
                        provider_job_id=resp.provider_job_id,
                        # Fork 1A: the completion engine's terminal-usage coordinates.
                        # ``tenant_id`` is persisted too — the run derives ownership
                        # through its project (it carries no tenant/owner), so stashing
                        # it here avoids a completion-time project lookup that would
                        # itself need the tenant.
                        command_index=command_index,
                        capability=capability.value,
                        model_id=str(model_id),
                        tenant_id=str(tenant_id),
                        envelope=dict(resp.output),
                    ),
                )

            if resp.status is ProviderStatus.FAILED:
                # Q9: record the failed call (terminal usage), then fail the workflow —
                # no media without accounting, and the failure is atomic (D4).
                await self._record_usage(run, command, capability, model_id, resp, tenant_id)
                return _CommandsResult(
                    kind="terminal",
                    provider_outputs=provider_outputs,
                    error={
                        "code": "PROVIDER_FAILED",
                        "message": resp.error or "provider returned FAILED",
                        "command_kind": command.kind,
                        "provider": resp.provider,
                        "request_id": resp.request_id,
                    },
                )

            # SUCCEEDED — record terminal usage in the runner's transaction (D3/Q5).
            await self._record_usage(run, command, capability, model_id, resp, tenant_id)
            provider_outputs.append(_response_view(resp))

        return _CommandsResult(kind="succeeded", provider_outputs=provider_outputs)

    async def _record_usage(
        self,
        run: WorkflowRun,
        command: StepCommand,
        capability: Capability,
        model_id: UUID,
        resp: ProviderResponse,
        tenant_id: UUID,
    ) -> None:
        """Record one terminal provider call as a priced usage row on the runner's UoW.

        Uses :func:`record_usage_in_uow` (no commit — the runner owns the single
        transaction, Q5). Idempotent on the deterministic ``request_id`` (a replay
        returns the existing row). Capability + ``model_id`` come from the command
        the runner minted (W7.6.1 — never from the provider payload).
        """
        await record_usage_in_uow(
            self._uow,
            RecordUsageCommand(
                tenant_id=tenant_id,
                model_id=model_id,
                status=resp.status,
                request_id=resp.request_id,
                capability=capability,
                usage=resp.usage,
                project_id=run.project_id,
                workflow_run_id=run.id,
            ),
            default_currency=self._default_currency,
        )
