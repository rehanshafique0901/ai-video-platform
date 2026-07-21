"""Unit tests for the α7.6 pipeline composition inside ``AdvanceWorkflowRun``.

α7.6 extends the α7.2 runner: after a pure step handler succeeds, the runner now
interprets its ``StepResult.commands`` — minting a deterministic ``request_id``,
dispatching each **exactly once** (W7.6.2) via a ``ProviderDispatcherPort``,
recording terminal usage in the **same** transaction (Q5), and either pausing on
``IN_PROGRESS`` (Q2) or checkpointing the **opaque** provider envelope (W7.6.1).

These tests use a scripted fake dispatcher (so the outcome sequence is fully
controlled) + the in-memory usage/pricing fakes on the UoW. Coverage map:

* P1  — generate-image e2e: queued → succeeded; one dispatch; one priced usage row;
        the opaque provider envelope is checkpointed (no payload interpretation).
* P2  — deterministic request_id ``run_id:step_index:command_index`` (D5) is what is
        dispatched AND what the usage row dedupes on.
* P3  — generate-video pause seam (Q2): IN_PROGRESS → run ``paused`` + WorkflowRunPaused
        (carries provider_job_id) + a ``_paused`` checkpoint; step stays running; NO
        usage recorded (Q6); NO StepCompleted; commits once.
* P4  — provider FAILED (Q9): a terminal ``FAILED`` usage row IS recorded, then the
        run fails atomically (D4) with error code ``PROVIDER_FAILED``.
* P5  — transient ProviderError retries the whole (pure) step, re-emitting the SAME
        request_id (W7.6.2), then succeeds; exactly two dispatches, ``retries == 1``.
* P6  — model_id fail-fast (Q4): a command without a usable model_id fails the step
        terminally BEFORE any dispatch (dispatcher untouched, no usage row).
* P7  — the α7.2 deterministic workflows still run unchanged with a dispatcher wired
        (they emit no commands → the dispatcher is never called).

must_pass: P1, P2, P3, P4, P5, P6, P7
"""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest

from app.application.interfaces.provider_dispatcher import ProviderDispatcherPort
from app.application.interfaces.providers import (
    Capability,
    ProviderRateLimited,
    ProviderResponse,
    ProviderStatus,
    ProviderUsage,
)
from app.application.use_cases.workflow._events import (
    EVENT_WORKFLOW_RUN_FAILED,
    EVENT_WORKFLOW_RUN_PAUSED,
    EVENT_WORKFLOW_RUN_SUCCEEDED,
    EVENT_WORKFLOW_STEP_COMPLETED,
)
from app.application.use_cases.workflow.advance_workflow_run import AdvanceWorkflowRun
from app.domain.workflow.registry import (
    GENERATE_IMAGE,
    GENERATE_VIDEO,
    NOOP_CHAIN,
    WORKFLOW_VERSION_1,
    StepCommand,
    StepDefinition,
    StepResult,
    WorkflowDefinition,
    WorkflowRegistry,
)
from app.domain.workflow.workflow_run_status import WorkflowRunStatus
from app.domain.workflow.workflow_step_status import WorkflowStepStatus
from tests.unit.application.use_cases.workflow._helpers import build_env, seed_workflow_run


class _ScriptedDispatcher(ProviderDispatcherPort):
    """A fake ``ProviderDispatcherPort`` that returns/raises a scripted sequence.

    Each ``dispatch`` pops the next item from ``script``: an ``Exception`` is
    raised, anything else is returned as the response. Records every dispatched
    command in ``calls`` so tests can assert the exactly-once contract (W7.6.2) and
    the runner-minted ``request_id`` (D5).
    """

    def __init__(self, script: list[object]) -> None:
        self._script = list(script)
        self.calls: list[StepCommand] = []

    async def dispatch(self, command: StepCommand) -> ProviderResponse:
        self.calls.append(command)
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        assert isinstance(item, ProviderResponse)
        return item

    def supports(self, capability: Capability) -> bool:  # pragma: no cover - unused
        return True

    def list_capabilities(self) -> list[Capability]:  # pragma: no cover - unused
        return list(Capability)


def _image_response(request_id: str) -> ProviderResponse:
    return ProviderResponse(
        request_id=request_id,
        provider="mock-image",
        status=ProviderStatus.SUCCEEDED,
        output={"image_ref": f"mock://image/{request_id}", "size": "1024x1024"},
        usage=ProviderUsage(unit="images", quantity=1),
    )


async def _advance(uc, env, run_id, **overrides):  # type: ignore[no-untyped-def]
    kwargs = {
        "project_id": env.project_id,
        "workflow_run_id": run_id,
        "owner_user_id": env.owner_user_id,
        "tenant_id": env.tenant_id,
    }
    kwargs.update(overrides)
    return await uc.execute(**kwargs)


def _events_of(env, event_type):  # type: ignore[no-untyped-def]
    return [e for e in env.outbox.events if e["event_type"] == event_type]


# ANNOTATION: P1 proves the whole α7.6 loop composes — runner → dispatcher → mock →
# recorder → checkpoint → outbox — and that the runner checkpoints the provider
# response as an OPAQUE envelope (W7.6.1: it stores dict(output), never reads image_ref).
@pytest.mark.unit
async def test_p1_generate_image_pipeline_runs_to_succeeded() -> None:
    env = build_env()
    model_id = uuid4()
    env.uow._fake_model_pricing.set_price(model_id=model_id, unit="image", price_per_unit="0.04")
    run = await seed_workflow_run(
        env,
        workflow_key=GENERATE_IMAGE,
        input_snapshot={"subject": "a cat", "model_id": str(model_id)},
    )
    request_id = f"{run.id}:1:0"
    dispatcher = _ScriptedDispatcher([_image_response(request_id)])
    uc = AdvanceWorkflowRun(uow=env.uow, dispatcher=dispatcher)

    result = await _advance(uc, env, run.id)

    assert result.view.run.status == WorkflowRunStatus.SUCCEEDED.value
    # W7.6.2: exactly one dispatch for the single command.
    assert len(dispatcher.calls) == 1
    # One priced usage row written in the runner's transaction (Q5).
    assert len(env.uow._fake_usage.inserted) == 1
    usage = env.uow._fake_usage.inserted[0]
    assert usage.unit == "image"
    assert usage.estimated_cost == Decimal("0.04")
    assert usage.workflow_run_id == run.id
    # W7.6.1: the checkpoint carries the opaque envelope (dict of output), untouched.
    cp = result.view.latest_checkpoint
    assert cp is not None and cp.step_index == 1
    envelope = cp.state["provider_outputs"][0]
    assert envelope["provider"] == "mock-image"
    assert envelope["status"] == "succeeded"
    assert envelope["output"] == {"image_ref": f"mock://image/{request_id}", "size": "1024x1024"}
    assert env.uow.commits == 1
    assert len(_events_of(env, EVENT_WORKFLOW_STEP_COMPLETED)) == 2  # prepare + generate
    assert len(_events_of(env, EVENT_WORKFLOW_RUN_SUCCEEDED)) == 1


# ANNOTATION: P2 pins the D5 invariant request_id = run_id:step_index:command_index —
# the runner mints it (providers never do) and the usage row dedupes on that exact id.
@pytest.mark.unit
async def test_p2_request_id_is_runner_minted_and_deterministic() -> None:
    env = build_env()
    model_id = uuid4()
    run = await seed_workflow_run(
        env,
        workflow_key=GENERATE_IMAGE,
        input_snapshot={"subject": "x", "model_id": str(model_id)},
    )
    expected_rid = f"{run.id}:1:0"
    dispatcher = _ScriptedDispatcher([_image_response(expected_rid)])
    uc = AdvanceWorkflowRun(uow=env.uow, dispatcher=dispatcher)

    await _advance(uc, env, run.id)

    assert dispatcher.calls[0].args["request_id"] == expected_rid
    assert env.uow._fake_usage.inserted[0].request_id == expected_rid


# ANNOTATION: P3 is the async pause seam (Q2/Q8) — the entire reason the minimal video
# pipeline exists. IN_PROGRESS must persist paused + provider_job_id + checkpoint and stop,
# recording NO usage (Q6) and marking NO step complete. α8.3 owns resumption.
@pytest.mark.unit
async def test_p3_generate_video_pauses_on_in_progress() -> None:
    env = build_env()
    model_id = uuid4()
    run = await seed_workflow_run(
        env,
        workflow_key=GENERATE_VIDEO,
        input_snapshot={"subject": "a scene", "model_id": str(model_id)},
    )
    request_id = f"{run.id}:0:0"
    job_id = f"mock-video-job:{request_id}"
    dispatcher = _ScriptedDispatcher(
        [
            ProviderResponse(
                request_id=request_id,
                provider="mock-video",
                status=ProviderStatus.IN_PROGRESS,
                output={"provider_job_id": job_id},
                provider_job_id=job_id,
                usage=ProviderUsage(unit="seconds", quantity=5),
            )
        ]
    )
    uc = AdvanceWorkflowRun(uow=env.uow, dispatcher=dispatcher)

    result = await _advance(uc, env, run.id)

    assert result.advanced is True
    assert result.view.run.status == WorkflowRunStatus.PAUSED.value
    assert result.view.run.finished_at is None  # paused is not terminal
    # The step is left RUNNING (the provider job is still in flight → α8.3 completes it).
    step = result.view.steps[0]
    assert step.status == WorkflowStepStatus.RUNNING.value
    # No usage recorded for an async IN_PROGRESS call (Q6).
    assert env.uow._fake_usage.inserted == []
    # WorkflowRunPaused emitted, carrying the resume coordinates.
    paused = _events_of(env, EVENT_WORKFLOW_RUN_PAUSED)
    assert len(paused) == 1
    assert paused[0]["payload"]["provider_job_id"] == job_id
    assert paused[0]["payload"]["step_index"] == 0
    assert _events_of(env, EVENT_WORKFLOW_STEP_COMPLETED) == []
    # The checkpoint carries the resume coordinates.
    cp = result.view.latest_checkpoint
    assert cp is not None
    assert cp.state["_paused"]["provider_job_id"] == job_id
    assert env.uow.commits == 1


# ANNOTATION: P4 is the failure-as-a-unit contract (D4/Q9) — a provider FAILED still
# records a terminal usage row (accounting), then fails the workflow atomically.
@pytest.mark.unit
async def test_p4_provider_failed_records_usage_then_fails_run() -> None:
    env = build_env()
    model_id = uuid4()
    run = await seed_workflow_run(
        env,
        workflow_key=GENERATE_IMAGE,
        input_snapshot={"subject": "x", "model_id": str(model_id)},
    )
    request_id = f"{run.id}:1:0"
    dispatcher = _ScriptedDispatcher(
        [
            ProviderResponse(
                request_id=request_id,
                provider="mock-image",
                status=ProviderStatus.FAILED,
                error="content policy violation",
                usage=None,
            )
        ]
    )
    uc = AdvanceWorkflowRun(uow=env.uow, dispatcher=dispatcher)

    result = await _advance(uc, env, run.id)

    assert result.view.run.status == WorkflowRunStatus.FAILED.value
    assert result.view.run.error["error"]["code"] == "PROVIDER_FAILED"
    # A terminal FAILED usage row is still recorded (no media without accounting).
    assert len(env.uow._fake_usage.inserted) == 1
    assert env.uow._fake_usage.inserted[0].status == "failed"
    assert len(_events_of(env, EVENT_WORKFLOW_RUN_FAILED)) == 1


# ANNOTATION: P5 proves retries belong to the RUNNER, not the dispatcher (W7.6.2). A
# transient ProviderError re-runs the pure step, which re-emits the SAME deterministic
# request_id, and dispatch is called exactly once per attempt (no dispatcher-side retry).
@pytest.mark.unit
async def test_p5_transient_provider_error_retries_same_request_id() -> None:
    env = build_env()
    model_id = uuid4()

    def _handler(ctx):  # type: ignore[no-untyped-def]
        return StepResult.ok(
            commands=(
                StepCommand(kind="generate_image", args={"prompt": "p", "model_id": str(model_id)}),
            )
        )

    registry = WorkflowRegistry()
    registry.register(
        WorkflowDefinition(
            key="cmd-flow",
            version=WORKFLOW_VERSION_1,
            steps=(StepDefinition("do", _handler, max_retries=2),),
        )
    )
    run = await seed_workflow_run(
        env, workflow_key="cmd-flow", input_snapshot={"model_id": str(model_id)}, seed_steps=False
    )
    await env.workflow_runs.seed_steps(run.id, [(0, "do")])
    request_id = f"{run.id}:0:0"
    dispatcher = _ScriptedDispatcher(
        [ProviderRateLimited("slow down"), _image_response(request_id)]
    )
    uc = AdvanceWorkflowRun(uow=env.uow, registry=registry, dispatcher=dispatcher)

    result = await _advance(uc, env, run.id)

    assert result.view.run.status == WorkflowRunStatus.SUCCEEDED.value
    assert len(dispatcher.calls) == 2  # one per attempt — no dispatcher-side retry
    assert [c.args["request_id"] for c in dispatcher.calls] == [request_id, request_id]
    step = result.view.steps[0]
    assert step.retries == 1
    # The transient attempt recorded nothing; only the success recorded one row.
    assert len(env.uow._fake_usage.inserted) == 1


# ANNOTATION: P6 is the Q4 fail-fast — a generation command without a usable model_id
# is malformed; the step fails terminally BEFORE any provider dispatch or usage write.
@pytest.mark.unit
async def test_p6_missing_model_id_fails_fast_before_dispatch() -> None:
    env = build_env()
    run = await seed_workflow_run(
        env,
        workflow_key=GENERATE_IMAGE,
        input_snapshot={"subject": "x"},  # no model_id
    )
    dispatcher = _ScriptedDispatcher([])  # must never be called
    uc = AdvanceWorkflowRun(uow=env.uow, dispatcher=dispatcher)

    result = await _advance(uc, env, run.id)

    assert result.view.run.status == WorkflowRunStatus.FAILED.value
    assert result.view.run.error["error"]["code"] == "MODEL_ID_MISSING"
    assert dispatcher.calls == []
    assert env.uow._fake_usage.inserted == []


# ANNOTATION: P7 guards backward-compat — the α7.2 deterministic workflows (no commands)
# still run unchanged with a dispatcher wired; the dispatcher is simply never called.
@pytest.mark.unit
async def test_p7_deterministic_workflow_unchanged_with_dispatcher() -> None:
    env = build_env()
    run = await seed_workflow_run(env, workflow_key=NOOP_CHAIN)
    dispatcher = _ScriptedDispatcher([])
    uc = AdvanceWorkflowRun(uow=env.uow, dispatcher=dispatcher)

    result = await _advance(uc, env, run.id)

    assert result.view.run.status == WorkflowRunStatus.SUCCEEDED.value
    assert dispatcher.calls == []
    assert env.uow._fake_usage.inserted == []
