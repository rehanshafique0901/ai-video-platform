"""Unit tests for the α8.3 async completion path.

α7.6 leaves an async run ``paused`` with an in-flight provider job + an enriched
``_paused`` checkpoint (Fork 1A: ``model_id`` / ``capability`` / ``command_index`` /
``tenant_id`` / opaque ``envelope``). α8.3 closes the loop through two public seams:

    CompletionEngine.complete() ─► ResumeWorkflowRun.execute() ─► AdvanceWorkflowRun

These tests drive a real ``generate-video`` run to ``paused`` (the authentic Fork 1A
checkpoint the runner writes), then exercise resolve → resume without ever reaching
into runner internals or re-running a handler.

Coverage map:

ResumeWorkflowRun (the public atomic-resume use case)
* R1 — SUCCEEDED: CAS paused→running, terminal usage under the CHECKPOINTED
        request_id, step succeeded, WorkflowRunResumed emitted, runner drives the
        (single-step) run to SUCCEEDED — all in one commit; no re-dispatch.
* R2 — FAILED: terminal FAILED usage recorded, step + run failed, WorkflowRunFailed
        emitted; the runner is NOT delegated to (a failed step must not be driven).
* R3 — idempotent no-op when the run is not paused (resumed=False, no writes).
* R4 — a non-terminal (IN_PROGRESS) result handed to resume is rejected (defensive).

CompletionEngine (the single idempotent entrypoint every ingress converges on)
* C1 — IN_PROGRESS resolve leaves the run paused ("in_progress"); no resume, no
        usage, no re-dispatch.
* C2 — SUCCEEDED resolve resumes + drives the run ("resumed" → succeeded).
* C3 — exactly-once under the per-run lease: a held foreign lease makes complete()
        skip ("locked") — no resolve, no resume.
* C4 — idempotent complete() on a non-paused run is a "noop".
* C5 — poll_once scans every paused run oldest-first and completes each.

must_pass: R1, R2, R3, R4, C1, C2, C3, C4, C5
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.application.interfaces.provider_dispatcher import ProviderDispatcherPort
from app.application.interfaces.providers import (
    Capability,
    ProviderResponse,
    ProviderStatus,
    ProviderUsage,
)
from app.application.use_cases.workflow._events import (
    EVENT_WORKFLOW_RUN_FAILED,
    EVENT_WORKFLOW_RUN_RESUMED,
)
from app.application.use_cases.workflow.advance_workflow_run import AdvanceWorkflowRun
from app.application.use_cases.workflow.completion_engine import CompletionEngine
from app.application.use_cases.workflow.resume_workflow_run import ResumeWorkflowRun
from app.domain.workflow.registry import GENERATE_VIDEO
from app.domain.workflow.workflow_run_status import WorkflowRunStatus
from app.domain.workflow.workflow_step_status import WorkflowStepStatus
from tests.unit.application.use_cases.workflow._helpers import Env, build_env, seed_workflow_run

_OWNER = "completion-engine-test"


class _SubmitOnlyDispatcher(ProviderDispatcherPort):
    """Returns a fixed submit response; ``resolve_job`` is never reached (pause setup)."""

    def __init__(self, response: ProviderResponse) -> None:
        self._response = response

    async def dispatch(self, command: Any) -> ProviderResponse:
        return self._response

    async def resolve_job(  # pragma: no cover - the submit path never resolves
        self, capability: Capability, *, provider_job_id: str, envelope: Mapping[str, Any]
    ) -> ProviderResponse:
        raise NotImplementedError

    def supports(self, capability: Capability) -> bool:  # pragma: no cover - unused
        return True

    def list_capabilities(self) -> list[Capability]:  # pragma: no cover - unused
        return list(Capability)


class _ResolvingDispatcher(ProviderDispatcherPort):
    """Resolves to a scripted terminal/in-progress result; ``dispatch`` must never fire.

    Records every ``resolve_job`` call so tests can assert the resolve coordinates,
    and raises on ``dispatch`` so any accidental re-run of the pure handler / re-submit
    during resumption fails loudly (W8.3: completion never re-dispatches).
    """

    def __init__(self, response: ProviderResponse) -> None:
        self._response = response
        self.resolve_calls: list[tuple[Capability, str, dict[str, Any]]] = []

    async def dispatch(self, command: Any) -> ProviderResponse:  # pragma: no cover - guard
        raise AssertionError("completion must not re-dispatch (submit) — only resolve")

    async def resolve_job(
        self, capability: Capability, *, provider_job_id: str, envelope: Mapping[str, Any]
    ) -> ProviderResponse:
        self.resolve_calls.append((capability, provider_job_id, dict(envelope)))
        return self._response

    def supports(self, capability: Capability) -> bool:  # pragma: no cover - unused
        return True

    def list_capabilities(self) -> list[Capability]:  # pragma: no cover - unused
        return list(Capability)


def _terminal_video(
    status: ProviderStatus,
    *,
    provider_job_id: str,
    seconds: int = 8,
    video_ref: str | None = "mock-video://out.mp4",
    error: str | None = None,
) -> ProviderResponse:
    """A terminal (or IN_PROGRESS) video resolve result — request_id deliberately empty.

    The resolve response carries NO request_id: the completion path records usage under
    the *checkpointed* request_id (Fork 1A), never the resolve response's.
    """
    return ProviderResponse(
        request_id="",
        provider="mock-video",
        status=status,
        output={"provider_job_id": provider_job_id, "video_ref": video_ref},
        provider_job_id=provider_job_id,
        error=error,
        usage=(
            ProviderUsage(unit="seconds", quantity=seconds)
            if status is not ProviderStatus.IN_PROGRESS
            else None
        ),
    )


async def _pause_video_run(env: Env, model_id: UUID) -> tuple[UUID, str, str]:
    """Drive a real ``generate-video`` run to ``paused`` and return (run_id, request_id, job_id).

    Uses the α7.6 runner so the ``_paused`` checkpoint is the authentic enriched Fork 1A
    handoff (not a hand-rolled dict), exercising the real pause→resume seam end to end.
    """
    run = await seed_workflow_run(
        env,
        workflow_key=GENERATE_VIDEO,
        input_snapshot={"subject": "a scene", "model_id": str(model_id)},
    )
    request_id = f"{run.id}:0:0"
    job_id = f"mock-video-job:{request_id}"
    submit = _SubmitOnlyDispatcher(
        ProviderResponse(
            request_id=request_id,
            provider="mock-video",
            status=ProviderStatus.IN_PROGRESS,
            output={"provider_job_id": job_id, "status_url": "https://q/req/status"},
            provider_job_id=job_id,
            usage=ProviderUsage(unit="seconds", quantity=5),
        )
    )
    runner = AdvanceWorkflowRun(uow=env.uow, dispatcher=submit)
    result = await runner.execute(
        project_id=env.project_id,
        workflow_run_id=run.id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
    )
    assert result.view.run.status == WorkflowRunStatus.PAUSED.value
    assert env.uow._fake_usage.inserted == []  # Q6: no usage on pause
    return run.id, request_id, job_id


def _events_of(env: Env, event_type: str) -> list[dict[str, Any]]:
    return [e for e in env.outbox.events if e["event_type"] == event_type]


def _price_video(env: Env, model_id: UUID, per_second: str = "0.10") -> None:
    env.uow._fake_model_pricing.set_price(
        model_id=model_id, unit="video_second", price_per_unit=per_second
    )


def _resume(env: Env, resolver: _ResolvingDispatcher) -> ResumeWorkflowRun:
    """Wire a ResumeWorkflowRun whose runner shares the SAME uow (atomic continuation)."""
    runner = AdvanceWorkflowRun(uow=env.uow, dispatcher=resolver)
    return ResumeWorkflowRun(uow=env.uow, runner=runner)


# --- ResumeWorkflowRun -------------------------------------------------------


@pytest.mark.unit
async def test_r1_succeeded_resumes_records_usage_and_drives_to_succeeded() -> None:
    env = build_env()
    model_id = uuid4()
    _price_video(env, model_id, "0.10")
    run_id, request_id, job_id = await _pause_video_run(env, model_id)
    commits_after_pause = env.uow.commits

    resolver = _ResolvingDispatcher(
        _terminal_video(ProviderStatus.SUCCEEDED, provider_job_id=job_id)
    )
    resume = _resume(env, resolver)

    result = await resume.execute(
        project_id=env.project_id,
        workflow_run_id=run_id,
        resolved=_terminal_video(ProviderStatus.SUCCEEDED, provider_job_id=job_id, seconds=8),
    )

    assert result.resumed is True
    # Single-step video workflow: resumed step done → runner settles the run SUCCEEDED.
    assert result.view.run.status == WorkflowRunStatus.SUCCEEDED.value
    # Terminal usage recorded under the CHECKPOINTED request_id (not the resolve's empty one).
    assert len(env.uow._fake_usage.inserted) == 1
    usage = env.uow._fake_usage.inserted[0]
    assert usage.request_id == request_id
    assert usage.status == "success"
    assert usage.unit == "video_second"
    assert usage.estimated_cost == Decimal("0.80")  # 8s × $0.10
    assert usage.workflow_run_id == run_id
    # The paused step is completed with the opaque terminal envelope.
    step = result.view.steps[0]
    assert step.status == WorkflowStepStatus.SUCCEEDED.value
    # WorkflowRunResumed emitted with the resume coordinates.
    resumed_events = _events_of(env, EVENT_WORKFLOW_RUN_RESUMED)
    assert len(resumed_events) == 1
    assert resumed_events[0]["payload"]["provider_job_id"] == job_id
    # Resume + usage + step + continuation + settle committed once (atomic).
    assert env.uow.commits == commits_after_pause + 1


@pytest.mark.unit
async def test_r2_failed_records_terminal_usage_then_fails_run() -> None:
    env = build_env()
    model_id = uuid4()
    _price_video(env, model_id, "0.10")
    run_id, request_id, job_id = await _pause_video_run(env, model_id)

    resolver = _ResolvingDispatcher(_terminal_video(ProviderStatus.FAILED, provider_job_id=job_id))
    resume = _resume(env, resolver)

    result = await resume.execute(
        project_id=env.project_id,
        workflow_run_id=run_id,
        resolved=_terminal_video(
            ProviderStatus.FAILED, provider_job_id=job_id, error="provider exploded", seconds=0
        ),
    )

    assert result.resumed is True
    assert result.view.run.status == WorkflowRunStatus.FAILED.value
    # Same failed-run error shape as the α7.6 inline path (runner-consistent).
    assert result.view.run.error["error"]["code"] == "PROVIDER_FAILED"
    assert result.view.run.error["step_index"] == 0
    # A terminal FAILED usage row is still recorded (accounting), under the checkpoint rid.
    assert len(env.uow._fake_usage.inserted) == 1
    assert env.uow._fake_usage.inserted[0].status == "failed"
    assert env.uow._fake_usage.inserted[0].request_id == request_id
    assert result.view.steps[0].status == WorkflowStepStatus.FAILED.value
    assert len(_events_of(env, EVENT_WORKFLOW_RUN_FAILED)) == 1


@pytest.mark.unit
async def test_r3_resume_is_noop_when_run_not_paused() -> None:
    env = build_env()
    # A plain queued run (never paused) — resume must not touch it.
    run = await seed_workflow_run(
        env, workflow_key=GENERATE_VIDEO, input_snapshot={"model_id": str(uuid4())}
    )
    resolver = _ResolvingDispatcher(_terminal_video(ProviderStatus.SUCCEEDED, provider_job_id="x"))
    resume = _resume(env, resolver)

    result = await resume.execute(
        project_id=env.project_id,
        workflow_run_id=run.id,
        resolved=_terminal_video(ProviderStatus.SUCCEEDED, provider_job_id="x"),
    )

    assert result.resumed is False
    assert result.view.run.status == WorkflowRunStatus.QUEUED.value
    assert env.uow._fake_usage.inserted == []
    assert _events_of(env, EVENT_WORKFLOW_RUN_RESUMED) == []


@pytest.mark.unit
async def test_r4_in_progress_result_is_rejected() -> None:
    env = build_env()
    model_id = uuid4()
    run_id, _rid, job_id = await _pause_video_run(env, model_id)
    resolver = _ResolvingDispatcher(
        _terminal_video(ProviderStatus.IN_PROGRESS, provider_job_id=job_id)
    )
    resume = _resume(env, resolver)

    with pytest.raises(ValueError):
        await resume.execute(
            project_id=env.project_id,
            workflow_run_id=run_id,
            resolved=_terminal_video(ProviderStatus.IN_PROGRESS, provider_job_id=job_id),
        )


# --- CompletionEngine --------------------------------------------------------


def _engine(env: Env, resolver: _ResolvingDispatcher) -> CompletionEngine:
    resume = _resume(env, resolver)
    return CompletionEngine(env.uow, resume, resolver, owner=_OWNER, lease=timedelta(seconds=60))


@pytest.mark.unit
async def test_c1_complete_leaves_run_paused_when_still_in_progress() -> None:
    env = build_env()
    model_id = uuid4()
    run_id, _rid, job_id = await _pause_video_run(env, model_id)
    resolver = _ResolvingDispatcher(
        _terminal_video(ProviderStatus.IN_PROGRESS, provider_job_id=job_id)
    )
    engine = _engine(env, resolver)

    outcome = await engine.complete(project_id=env.project_id, workflow_run_id=run_id)

    assert outcome.status == "in_progress"
    assert outcome.run_status == "paused"
    # Resolve was queried; the run stays paused, no usage, no resume.
    assert len(resolver.resolve_calls) == 1
    cap, resolved_job, envelope = resolver.resolve_calls[0]
    assert cap is Capability.VIDEO
    assert resolved_job == job_id
    assert envelope["status_url"] == "https://q/req/status"  # opaque envelope threaded back
    assert env.uow._fake_usage.inserted == []
    run = await env.workflow_runs.get_owned(env.project_id, run_id)
    assert run is not None and run.status == WorkflowRunStatus.PAUSED.value


@pytest.mark.unit
async def test_c2_complete_resumes_and_drives_terminal_run() -> None:
    env = build_env()
    model_id = uuid4()
    _price_video(env, model_id, "0.10")
    run_id, request_id, job_id = await _pause_video_run(env, model_id)
    resolver = _ResolvingDispatcher(
        _terminal_video(ProviderStatus.SUCCEEDED, provider_job_id=job_id, seconds=8)
    )
    engine = _engine(env, resolver)

    outcome = await engine.complete(project_id=env.project_id, workflow_run_id=run_id)

    assert outcome.status == "resumed"
    assert outcome.run_status == WorkflowRunStatus.SUCCEEDED.value
    assert len(env.uow._fake_usage.inserted) == 1
    assert env.uow._fake_usage.inserted[0].request_id == request_id
    assert len(_events_of(env, EVENT_WORKFLOW_RUN_RESUMED)) == 1


@pytest.mark.unit
async def test_c3_complete_is_skipped_when_lease_is_held() -> None:
    env = build_env()
    model_id = uuid4()
    run_id, _rid, job_id = await _pause_video_run(env, model_id)
    # A foreign holder owns the per-run lease → complete() must not resolve/resume.
    async with env.uow:
        held = await env.uow.locks.acquire(
            key=f"workflow_run:{run_id}", owner="other-ingress", lease=timedelta(seconds=60)
        )
        await env.uow.commit()
    assert held is not None

    resolver = _ResolvingDispatcher(
        _terminal_video(ProviderStatus.SUCCEEDED, provider_job_id=job_id)
    )
    engine = _engine(env, resolver)

    outcome = await engine.complete(project_id=env.project_id, workflow_run_id=run_id)

    assert outcome.status == "locked"
    assert resolver.resolve_calls == []  # never resolved under a foreign lease
    assert env.uow._fake_usage.inserted == []
    run = await env.workflow_runs.get_owned(env.project_id, run_id)
    assert run is not None and run.status == WorkflowRunStatus.PAUSED.value


@pytest.mark.unit
async def test_c4_complete_is_noop_when_run_not_paused() -> None:
    env = build_env()
    run = await seed_workflow_run(
        env, workflow_key=GENERATE_VIDEO, input_snapshot={"model_id": str(uuid4())}
    )
    resolver = _ResolvingDispatcher(_terminal_video(ProviderStatus.SUCCEEDED, provider_job_id="x"))
    engine = _engine(env, resolver)

    outcome = await engine.complete(project_id=env.project_id, workflow_run_id=run.id)

    assert outcome.status == "noop"
    assert resolver.resolve_calls == []  # no handoff → nothing to resolve
    assert env.uow._fake_usage.inserted == []


@pytest.mark.unit
async def test_c5_poll_once_scans_and_completes_every_paused_run() -> None:
    env = build_env()
    m1, m2 = uuid4(), uuid4()
    _price_video(env, m1, "0.10")
    _price_video(env, m2, "0.10")
    run1, rid1, job1 = await _pause_video_run(env, m1)
    run2, rid2, job2 = await _pause_video_run(env, m2)

    # Both resolve SUCCEEDED. One resolver instance handles both jobs (job id echoed back).
    resolver = _ResolvingDispatcher(
        _terminal_video(ProviderStatus.SUCCEEDED, provider_job_id="ignored")
    )

    # The resolver must echo the per-run job id; wrap resolve to return a matching result.
    async def _resolve(capability, *, provider_job_id, envelope):  # type: ignore[no-untyped-def]
        resolver.resolve_calls.append((capability, provider_job_id, dict(envelope)))
        return _terminal_video(ProviderStatus.SUCCEEDED, provider_job_id=provider_job_id)

    resolver.resolve_job = _resolve  # type: ignore[method-assign]
    engine = _engine(env, resolver)

    poll = await engine.poll_once()

    assert poll.scanned == 2
    assert {o.status for o in poll.outcomes} == {"resumed"}
    # Both runs settled terminal, one usage row each under their own checkpoint rids.
    r1 = await env.workflow_runs.get_owned(env.project_id, run1)
    r2 = await env.workflow_runs.get_owned(env.project_id, run2)
    assert r1 is not None and r1.status == WorkflowRunStatus.SUCCEEDED.value
    assert r2 is not None and r2.status == WorkflowRunStatus.SUCCEEDED.value
    recorded_rids = {u.request_id for u in env.uow._fake_usage.inserted}
    assert recorded_rids == {rid1, rid2}
