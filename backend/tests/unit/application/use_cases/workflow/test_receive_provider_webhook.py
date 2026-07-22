"""Unit tests for the α8.3b webhook ingress (``ReceiveProviderWebhook``).

The webhook is a *trigger*: verify → find the paused run by ``provider_job_id`` →
frozen ``CompletionEngine.complete()``. These tests reuse the α8.3 pause harness
(a real ``generate-video`` run driven to ``paused``) and a fake verifier, and pin:

* W1 — happy path: a verified webhook for a paused run drives it to SUCCEEDED
        ("resumed"), recording exactly one usage row under the checkpointed rid.
* W2 — unknown job id → "unknown_job" (HTTP-200 ack), no resume, no usage.
* W3 — duplicate delivery (Fal retry after resume) → "unknown_job" no-op; still
        exactly one usage row (exactly-once owned by complete()'s lease + CAS).
* W4 — unsupported provider → "unsupported" (router maps to 404); no lookup.
* W5 — bad signature: the verifier's WebhookVerificationError propagates (→ 401),
        and complete() is never triggered (no state change — W8.3b.1).
* W6 — poll racing the webhook: after a webhook resumes the run, poll_once finds
        nothing paused — no double resume/usage.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import uuid4

import pytest

from app.application.interfaces.providers import Capability, ProviderStatus
from app.application.interfaces.webhook_verifier import (
    IWebhookVerifier,
    VerifiedWebhook,
    WebhookVerificationError,
)
from app.application.use_cases.workflow._events import EVENT_WORKFLOW_RUN_RESUMED
from app.application.use_cases.workflow.receive_provider_webhook import ReceiveProviderWebhook
from app.domain.workflow.workflow_run_status import WorkflowRunStatus
from tests.unit.application.use_cases.workflow._helpers import Env, build_env
from tests.unit.application.use_cases.workflow.test_completion_engine import (
    _engine,
    _events_of,
    _pause_video_run,
    _price_video,
    _ResolvingDispatcher,
    _terminal_video,
)

pytestmark = pytest.mark.unit


class _StubVerifier(IWebhookVerifier):
    """Returns a fixed provider_job_id, or raises to simulate a bad signature."""

    def __init__(self, *, provider_job_id: str | None = None, raises: bool = False) -> None:
        self._provider_job_id = provider_job_id
        self._raises = raises
        self.calls = 0

    async def verify(self, *, body: bytes, headers: Mapping[str, str]) -> VerifiedWebhook:
        self.calls += 1
        if self._raises:
            raise WebhookVerificationError("bad signature")
        assert self._provider_job_id is not None
        return VerifiedWebhook(provider_job_id=self._provider_job_id)


def _ingress(
    env: Env, verifier: IWebhookVerifier, resolver: _ResolvingDispatcher
) -> ReceiveProviderWebhook:
    return ReceiveProviderWebhook(
        uow=env.uow,
        completion_engine=_engine(env, resolver),
        verifiers={"fal": verifier},
    )


def _echoing_resolver() -> _ResolvingDispatcher:
    """A resolver that echoes each run's own job id back as a SUCCEEDED result."""
    resolver = _ResolvingDispatcher(_terminal_video(ProviderStatus.SUCCEEDED, provider_job_id="x"))

    async def _resolve(capability: Capability, *, provider_job_id: str, envelope: Mapping[str, Any]):  # type: ignore[no-untyped-def]
        resolver.resolve_calls.append((capability, provider_job_id, dict(envelope)))
        return _terminal_video(ProviderStatus.SUCCEEDED, provider_job_id=provider_job_id)

    resolver.resolve_job = _resolve  # type: ignore[method-assign]
    return resolver


async def test_w1_verified_webhook_resumes_paused_run() -> None:
    env = build_env()
    model_id = uuid4()
    _price_video(env, model_id, "0.10")
    run_id, request_id, job_id = await _pause_video_run(env, model_id)
    ingress = _ingress(env, _StubVerifier(provider_job_id=job_id), _echoing_resolver())

    result = await ingress.execute(provider="fal", body=b"{}", headers={})

    assert result.status == "resumed"
    assert result.workflow_run_id == run_id
    assert result.run_status == WorkflowRunStatus.SUCCEEDED.value
    assert len(env.uow._fake_usage.inserted) == 1
    assert env.uow._fake_usage.inserted[0].request_id == request_id
    assert len(_events_of(env, EVENT_WORKFLOW_RUN_RESUMED)) == 1


async def test_w2_unknown_job_id_is_acked_noop() -> None:
    env = build_env()
    await _pause_video_run(env, uuid4())
    ingress = _ingress(env, _StubVerifier(provider_job_id="no-such-job"), _echoing_resolver())

    result = await ingress.execute(provider="fal", body=b"{}", headers={})

    assert result.status == "unknown_job"
    assert result.workflow_run_id is None
    assert env.uow._fake_usage.inserted == []


async def test_w3_duplicate_delivery_after_resume_is_noop() -> None:
    env = build_env()
    model_id = uuid4()
    _price_video(env, model_id, "0.10")
    run_id, request_id, job_id = await _pause_video_run(env, model_id)
    ingress = _ingress(env, _StubVerifier(provider_job_id=job_id), _echoing_resolver())

    first = await ingress.execute(provider="fal", body=b"{}", headers={})
    second = await ingress.execute(provider="fal", body=b"{}", headers={})

    assert first.status == "resumed"
    # The run is no longer paused → the retry finds nothing → safe no-op ack.
    assert second.status == "unknown_job"
    assert len(env.uow._fake_usage.inserted) == 1  # exactly-once (lease + CAS)


async def test_w4_unsupported_provider() -> None:
    env = build_env()
    verifier = _StubVerifier(provider_job_id="x")
    ingress = _ingress(env, verifier, _echoing_resolver())

    result = await ingress.execute(provider="stripe", body=b"{}", headers={})

    assert result.status == "unsupported"
    assert verifier.calls == 0  # never even attempted verification


async def test_w5_bad_signature_propagates_and_never_completes() -> None:
    env = build_env()
    run_id, _rid, _job = await _pause_video_run(env, uuid4())
    ingress = _ingress(env, _StubVerifier(raises=True), _echoing_resolver())

    with pytest.raises(WebhookVerificationError):
        await ingress.execute(provider="fal", body=b"{}", headers={})

    # W8.3b.1: no state changed — the run is still paused, no usage, no events.
    run = await env.workflow_runs.get_owned(env.project_id, run_id)
    assert run is not None and run.status == WorkflowRunStatus.PAUSED.value
    assert env.uow._fake_usage.inserted == []
    assert _events_of(env, EVENT_WORKFLOW_RUN_RESUMED) == []


async def test_w6_webhook_then_poll_does_not_double_resume() -> None:
    env = build_env()
    model_id = uuid4()
    _price_video(env, model_id, "0.10")
    run_id, request_id, job_id = await _pause_video_run(env, model_id)
    resolver = _echoing_resolver()
    engine = _engine(env, resolver)
    ingress = ReceiveProviderWebhook(
        uow=env.uow,
        completion_engine=engine,
        verifiers={"fal": _StubVerifier(provider_job_id=job_id)},
    )

    webhook_result = await ingress.execute(provider="fal", body=b"{}", headers={})
    poll = await engine.poll_once()

    assert webhook_result.status == "resumed"
    assert poll.scanned == 0  # already resumed — nothing left paused
    assert len(env.uow._fake_usage.inserted) == 1
