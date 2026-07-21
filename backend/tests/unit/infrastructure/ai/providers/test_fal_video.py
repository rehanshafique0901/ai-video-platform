"""Unit tests for the Fal.ai video adapter (Slice α8.2).

Every test drives :class:`FalVideoProvider` through an in-memory
``httpx.MockTransport`` — **no network**. They pin the signed-off behaviours:

* submit → the exact ``GenerateVideoResponse`` shape: ``IN_PROGRESS`` +
  ``provider_job_id`` + a *versioned* opaque ``output`` envelope + **no usage**
  (Q4 / Q5 / W8.2.1 / W8.2.2 / W8.2.3),
* **exactly one** HTTP request per call, no internal retry (W7.6.2),
* HTTP status → neutral ``ProviderError`` mapping, nothing HTTP leaks (Q8),
* observational equivalence with ``MockVideoProvider`` on the async path (W8.2.1).
"""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from app.application.interfaces.providers import (
    GenerateVideoRequest,
    GenerateVideoResponse,
    ProviderAuthenticationError,
    ProviderError,
    ProviderRateLimited,
    ProviderStatus,
    ProviderTimeout,
    ProviderUnavailable,
    ProviderValidationError,
)
from app.infrastructure.ai.providers.fal import FalVideoProvider
from app.infrastructure.ai.providers.mocks import MockVideoProvider

pytestmark = pytest.mark.unit

_Handler = Callable[[httpx.Request], httpx.Response]

_BASE_URL = "https://queue.fal.run"
_DEFAULT_MODEL = "fal-ai/ltx-video"


class _Recorder:
    """Wraps a responder, recording every request the adapter actually sends."""

    def __init__(self, responder: _Handler) -> None:
        self.requests: list[httpx.Request] = []
        self._responder = responder

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._responder(request)


def _provider(responder: _Handler) -> tuple[FalVideoProvider, _Recorder]:
    recorder = _Recorder(responder)
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(recorder),
        base_url=_BASE_URL,
    )
    return FalVideoProvider(client=client), recorder


def _accepted(
    request_id: str = "fal-req-123",
    *,
    status_url: str = "https://queue.fal.run/requests/fal-req-123/status",
    response_url: str = "https://queue.fal.run/requests/fal-req-123",
) -> _Handler:
    return lambda _req: httpx.Response(
        200,
        json={
            "request_id": request_id,
            "status_url": status_url,
            "response_url": response_url,
        },
    )


def _req(
    prompt: str = "a spaceship landing",
    *,
    model: str | None = None,
    duration_seconds: int | None = None,
) -> GenerateVideoRequest:
    return GenerateVideoRequest(
        request_id="run:0:0", prompt=prompt, model=model, duration_seconds=duration_seconds
    )


# --- submit shape (Q4 / Q5 / W8.2.1 / W8.2.2 / W8.2.3) -----------------------


async def test_submit_returns_in_progress_with_job_id_and_versioned_envelope() -> None:
    provider, _ = _provider(_accepted("fal-req-abc"))

    resp = await provider.submit(_req(duration_seconds=5))

    assert isinstance(resp, GenerateVideoResponse)
    # W8.2.2: submit-only — the adapter never drives a terminal state.
    assert resp.status is ProviderStatus.IN_PROGRESS
    assert resp.provider == "fal-video"
    assert resp.request_id == "run:0:0"
    # Q4: provider_job_id is the Fal request_id (the runner's resume coordinate).
    assert resp.provider_job_id == "fal-req-abc"
    # Q4: the versioned opaque envelope carries the completion coordinates for α8.3.
    assert resp.output == {
        "schema_version": 1,
        "provider": "fal",
        "provider_job_id": "fal-req-abc",
        "status_url": "https://queue.fal.run/requests/fal-req-123/status",
        "response_url": "https://queue.fal.run/requests/fal-req-123",
    }
    # Q5: no usage on submit — α8.3 records terminal usage on completion.
    assert resp.usage is None
    assert resp.video_ref is None  # populated only at completion (α8.4)
    assert resp.error is None


async def test_exactly_one_http_request_per_call() -> None:
    # W7.6.2: the adapter dispatches once; retries belong to the runner.
    provider, rec = _provider(_accepted())
    await provider.submit(_req())
    assert len(rec.requests) == 1
    sent = rec.requests[0]
    assert sent.method == "POST"
    assert sent.url.path == f"/{_DEFAULT_MODEL}"


async def test_request_payload_carries_prompt_and_duration_and_defaults_route() -> None:
    provider, rec = _provider(_accepted())
    await provider.submit(_req(prompt="a cat", duration_seconds=8))

    sent = rec.requests[0]
    assert sent.url.path == f"/{_DEFAULT_MODEL}"  # Q7 default route
    body = json.loads(sent.content)
    assert body["prompt"] == "a cat"
    assert body["duration"] == 8


async def test_explicit_supported_route_is_used() -> None:
    provider, rec = _provider(_accepted())
    await provider.submit(_req(model="fal-ai/minimax-video"))
    assert rec.requests[0].url.path == "/fal-ai/minimax-video"


async def test_duration_omitted_when_not_requested() -> None:
    provider, rec = _provider(_accepted())
    await provider.submit(_req(duration_seconds=None))
    assert "duration" not in json.loads(rec.requests[0].content)


# --- error mapping (Q8) ------------------------------------------------------


async def test_unsupported_model_is_terminal_and_makes_no_http_call() -> None:
    # A bad route is a malformed request — terminal, and must NOT hit the network.
    provider, rec = _provider(_accepted())
    with pytest.raises(ProviderValidationError):
        await provider.submit(_req(model="fal-ai/not-a-real-route"))
    assert rec.requests == []


@pytest.mark.parametrize("status", [401, 403])
async def test_auth_errors_are_terminal(status: int) -> None:
    provider, _ = _provider(lambda _r: httpx.Response(status, json={"detail": "bad key"}))
    with pytest.raises(ProviderAuthenticationError) as ei:
        await provider.submit(_req())
    assert ei.value.transient is False


@pytest.mark.parametrize("status", [400, 422])
async def test_4xx_is_terminal_validation_error(status: int) -> None:
    provider, _ = _provider(lambda _r: httpx.Response(status, json={"detail": "bad input"}))
    with pytest.raises(ProviderValidationError) as ei:
        await provider.submit(_req())
    assert ei.value.transient is False
    assert "bad input" in str(ei.value)  # Fal detail surfaced, not raw HTTP


async def test_429_is_transient_rate_limited() -> None:
    provider, _ = _provider(lambda _r: httpx.Response(429, json={"detail": "slow down"}))
    with pytest.raises(ProviderRateLimited) as ei:
        await provider.submit(_req())
    assert ei.value.transient is True


@pytest.mark.parametrize("status", [500, 502, 503, 504])
async def test_5xx_is_transient_unavailable(status: int) -> None:
    provider, _ = _provider(lambda _r: httpx.Response(status, text="upstream error"))
    with pytest.raises(ProviderUnavailable) as ei:
        await provider.submit(_req())
    assert ei.value.transient is True


async def test_timeout_is_transient_provider_timeout() -> None:
    def _boom(_req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=_req)

    provider, _ = _provider(_boom)
    with pytest.raises(ProviderTimeout) as ei:
        await provider.submit(_req())
    assert ei.value.transient is True


async def test_connection_error_is_transient_unavailable() -> None:
    def _boom(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=_req)

    provider, _ = _provider(_boom)
    with pytest.raises(ProviderUnavailable) as ei:
        await provider.submit(_req())
    assert ei.value.transient is True


async def test_2xx_without_request_id_is_transient_unavailable() -> None:
    provider, _ = _provider(lambda _r: httpx.Response(200, json={"status": "IN_QUEUE"}))
    with pytest.raises(ProviderUnavailable):
        await provider.submit(_req())


# --- metadata + health -------------------------------------------------------


async def test_metadata_advertises_async_video_capability() -> None:
    md = FalVideoProvider.metadata
    assert md.id == "fal-video"
    assert md.capability.value == "video"
    # Q9: truthful — α8.3's completion service branches on these flags.
    assert md.supports_polling is True
    assert md.supports_webhooks is True


async def test_health_is_static_healthy() -> None:
    provider, rec = _provider(_accepted())
    health = await provider.health()
    assert health.healthy is True
    assert rec.requests == []  # no live probe


# --- W8.2.1: observational equivalence with the mock -------------------------


async def test_observationally_equivalent_to_mock_video_provider() -> None:
    """On the async path the runner cannot tell the two apart by the fields it reads."""
    req = _req(duration_seconds=5)
    real, _ = _provider(_accepted("fal-req-xyz"))

    real_resp = await real.submit(req)
    mock_resp = await MockVideoProvider().submit(req)

    # Same DTO type.
    assert type(real_resp) is type(mock_resp) is GenerateVideoResponse
    # Same status the runner branches on → both pause (α7.6 IN_PROGRESS seam).
    assert real_resp.status is mock_resp.status is ProviderStatus.IN_PROGRESS
    # Both set a provider_job_id (the runner's checkpoint + WorkflowRunPaused coord).
    assert real_resp.provider_job_id is not None
    assert mock_resp.provider_job_id is not None
    # Both carry the job id in the opaque output envelope the runner checkpoints.
    assert real_resp.output.get("provider_job_id") == real_resp.provider_job_id
    assert mock_resp.output.get("provider_job_id") == mock_resp.provider_job_id
    # Only the values differ (real Fal ids/urls + provider id). Usage differs
    # (real None vs the mock's seconds), but the runner DISCARDS usage on pause —
    # it is never recorded until α8.3, so the pause behaviour is identical.
    assert real_resp.provider != mock_resp.provider
    assert real_resp.provider_job_id != mock_resp.provider_job_id


# --- resolve: async completion lifecycle (α8.3) ------------------------------

_STATUS_URL = "https://queue.fal.run/requests/fal-req-123/status"
_RESPONSE_URL = "https://queue.fal.run/requests/fal-req-123"
_ENVELOPE = {"status_url": _STATUS_URL, "response_url": _RESPONSE_URL}


def _router(*, status: httpx.Response, result: httpx.Response | None = None) -> _Handler:
    """Route a resolve's two GETs: ``…/status`` → ``status``, else → ``result``."""

    def _responder(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/status"):
            return status
        assert result is not None, f"unexpected result fetch: {request.url}"
        return result

    return _responder


async def test_resolve_in_progress_stays_in_progress() -> None:
    provider, rec = _provider(_router(status=httpx.Response(200, json={"status": "IN_PROGRESS"})))

    resp = await provider.resolve(provider_job_id="fal-req-123", envelope=_ENVELOPE)

    assert resp.status is ProviderStatus.IN_PROGRESS
    assert resp.provider_job_id == "fal-req-123"
    assert resp.usage is None
    # Only the status was polled — no result fetch while the job is still running.
    assert len(rec.requests) == 1
    assert rec.requests[0].url.path.endswith("/status")


async def test_resolve_completed_returns_succeeded_with_video_and_usage() -> None:
    provider, rec = _provider(
        _router(
            status=httpx.Response(200, json={"status": "COMPLETED"}),
            result=httpx.Response(
                200, json={"video": {"url": "https://cdn.fal/out.mp4", "duration": 8}}
            ),
        )
    )

    resp = await provider.resolve(provider_job_id="fal-req-123", envelope=_ENVELOPE)

    assert resp.status is ProviderStatus.SUCCEEDED
    assert resp.video_ref == "https://cdn.fal/out.mp4"
    assert resp.usage is not None
    assert resp.usage.unit == "seconds"
    assert resp.usage.quantity == 8
    # status then result — two GETs.
    assert len(rec.requests) == 2


async def test_resolve_error_status_returns_failed() -> None:
    provider, _ = _provider(_router(status=httpx.Response(200, json={"status": "ERROR"})))

    resp = await provider.resolve(provider_job_id="fal-req-123", envelope=_ENVELOPE)

    assert resp.status is ProviderStatus.FAILED
    assert resp.error is not None


async def test_resolve_transport_error_is_transient_provider_error() -> None:
    def _boom(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    provider, _ = _provider(_boom)

    with pytest.raises(ProviderUnavailable):
        await provider.resolve(provider_job_id="fal-req-123", envelope=_ENVELOPE)


async def test_resolve_without_status_url_raises() -> None:
    provider, _ = _provider(_accepted())

    with pytest.raises(ProviderUnavailable):
        await provider.resolve(provider_job_id="fal-req-123", envelope={})


async def test_typed_errors_are_provider_errors() -> None:
    # Sanity: every mapped error is a ProviderError so the runner's single
    # ``except ProviderError`` branch catches all of them.
    for exc in (
        ProviderAuthenticationError,
        ProviderValidationError,
        ProviderRateLimited,
        ProviderUnavailable,
        ProviderTimeout,
    ):
        assert issubclass(exc, ProviderError)
