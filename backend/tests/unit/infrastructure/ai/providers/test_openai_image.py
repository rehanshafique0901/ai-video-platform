"""Unit tests for the OpenAI Images adapter (Slice α8.1).

Every test drives :class:`OpenAIImageProvider` through an in-memory
``httpx.MockTransport`` — **no network**. They pin the four signed-off
behaviours:

* success → the exact ``GenerateImageResponse`` shape (W8.1.3),
* **exactly one** HTTP request per call, no internal retry (W7.6.2),
* HTTP status → neutral ``ProviderError`` mapping, nothing HTTP leaks (Q7),
* observational equivalence with ``MockImageProvider`` (W8.1.3).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import fields

import httpx
import pytest

from app.application.interfaces.providers import (
    GenerateImageRequest,
    GenerateImageResponse,
    ProviderAuthenticationError,
    ProviderError,
    ProviderRateLimited,
    ProviderStatus,
    ProviderTimeout,
    ProviderUnavailable,
    ProviderValidationError,
)
from app.infrastructure.ai.providers.mocks import MockImageProvider
from app.infrastructure.ai.providers.openai import OpenAIImageProvider

pytestmark = pytest.mark.unit

_Handler = Callable[[httpx.Request], httpx.Response]


class _Recorder:
    """Wraps a responder, recording every request the adapter actually sends."""

    def __init__(self, responder: _Handler) -> None:
        self.requests: list[httpx.Request] = []
        self._responder = responder

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._responder(request)


def _provider(responder: _Handler) -> tuple[OpenAIImageProvider, _Recorder]:
    recorder = _Recorder(responder)
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(recorder),
        base_url="https://api.openai.com/v1",
    )
    return OpenAIImageProvider(client=client), recorder


def _ok(url: str = "https://cdn.openai.example/generated.png") -> _Handler:
    return lambda _req: httpx.Response(200, json={"data": [{"url": url}]})


def _req(
    prompt: str = "a red bicycle", *, model: str | None = None, size: str | None = None
) -> GenerateImageRequest:
    return GenerateImageRequest(request_id="run:0:0", prompt=prompt, model=model, size=size)


# --- success shape (W8.1.3) --------------------------------------------------


async def test_success_returns_succeeded_with_image_ref_and_usage() -> None:
    provider, rec = _provider(_ok("https://cdn.openai.example/x.png"))

    resp = await provider.generate_image(_req(size="1024x1024"))

    assert isinstance(resp, GenerateImageResponse)
    assert resp.status is ProviderStatus.SUCCEEDED
    assert resp.provider == "openai-image"
    assert resp.request_id == "run:0:0"
    assert resp.image_ref == "https://cdn.openai.example/x.png"
    assert resp.output == {"image_ref": "https://cdn.openai.example/x.png", "size": "1024x1024"}
    assert resp.usage is not None
    assert resp.usage.unit == "images"
    assert resp.usage.quantity == 1
    assert resp.provider_job_id is None  # synchronous — never async
    assert resp.error is None


async def test_exactly_one_http_request_per_call() -> None:
    # W7.6.2: the adapter dispatches once; retries belong to the runner.
    provider, rec = _provider(_ok())
    await provider.generate_image(_req())
    assert len(rec.requests) == 1
    sent = rec.requests[0]
    assert sent.method == "POST"
    assert sent.url.path == "/v1/images/generations"


async def test_request_payload_uses_url_format_and_defaults_to_dalle3() -> None:
    import json

    provider, rec = _provider(_ok())
    await provider.generate_image(_req(prompt="a cat", size="512x512"))

    body = json.loads(rec.requests[0].content)
    assert body["model"] == "dall-e-3"  # Q3 default
    assert body["prompt"] == "a cat"
    assert body["response_format"] == "url"  # Q3: compact ref, no storage
    assert body["n"] == 1
    assert body["size"] == "512x512"


async def test_explicit_supported_model_is_passed_through() -> None:
    import json

    provider, rec = _provider(_ok())
    await provider.generate_image(_req(model="dall-e-2"))
    assert json.loads(rec.requests[0].content)["model"] == "dall-e-2"


async def test_size_omitted_when_not_requested() -> None:
    import json

    provider, rec = _provider(_ok())
    await provider.generate_image(_req(size=None))
    assert "size" not in json.loads(rec.requests[0].content)


# --- error mapping (Q7) ------------------------------------------------------


async def test_unsupported_model_is_terminal_and_makes_no_http_call() -> None:
    # A bad model is a malformed request — terminal, and must NOT hit the network.
    provider, rec = _provider(_ok())
    with pytest.raises(ProviderValidationError):
        await provider.generate_image(_req(model="gpt-image-1"))
    assert rec.requests == []


@pytest.mark.parametrize("status", [401, 403])
async def test_auth_errors_are_terminal(status: int) -> None:
    provider, _ = _provider(lambda _r: httpx.Response(status, json={"error": {"message": "nope"}}))
    with pytest.raises(ProviderAuthenticationError) as ei:
        await provider.generate_image(_req())
    assert ei.value.transient is False


async def test_400_is_terminal_validation_error() -> None:
    provider, _ = _provider(
        lambda _r: httpx.Response(400, json={"error": {"message": "bad prompt"}})
    )
    with pytest.raises(ProviderValidationError) as ei:
        await provider.generate_image(_req())
    assert ei.value.transient is False
    assert "bad prompt" in str(ei.value)  # OpenAI detail surfaced, not raw HTTP


async def test_429_is_transient_rate_limited() -> None:
    provider, _ = _provider(
        lambda _r: httpx.Response(429, json={"error": {"message": "slow down"}})
    )
    with pytest.raises(ProviderRateLimited) as ei:
        await provider.generate_image(_req())
    assert ei.value.transient is True


@pytest.mark.parametrize("status", [500, 502, 503, 504])
async def test_5xx_is_transient_unavailable(status: int) -> None:
    provider, _ = _provider(lambda _r: httpx.Response(status, text="upstream error"))
    with pytest.raises(ProviderUnavailable) as ei:
        await provider.generate_image(_req())
    assert ei.value.transient is True


async def test_timeout_is_transient_provider_timeout() -> None:
    def _boom(_req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=_req)

    provider, _ = _provider(_boom)
    with pytest.raises(ProviderTimeout) as ei:
        await provider.generate_image(_req())
    assert ei.value.transient is True


async def test_connection_error_is_transient_unavailable() -> None:
    def _boom(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=_req)

    provider, _ = _provider(_boom)
    with pytest.raises(ProviderUnavailable) as ei:
        await provider.generate_image(_req())
    assert ei.value.transient is True


async def test_200_with_empty_data_is_transient_unavailable() -> None:
    provider, _ = _provider(lambda _r: httpx.Response(200, json={"data": []}))
    with pytest.raises(ProviderUnavailable):
        await provider.generate_image(_req())


async def test_200_item_without_url_is_transient_unavailable() -> None:
    provider, _ = _provider(lambda _r: httpx.Response(200, json={"data": [{"b64_json": "…"}]}))
    with pytest.raises(ProviderUnavailable):
        await provider.generate_image(_req())


# --- metadata + health -------------------------------------------------------


async def test_metadata_advertises_synchronous_image_capability() -> None:
    md = OpenAIImageProvider.metadata
    assert md.id == "openai-image"
    assert md.capability.value == "image"
    assert md.supports_polling is False
    assert md.supports_webhooks is False


async def test_health_is_static_healthy() -> None:
    provider, rec = _provider(_ok())
    health = await provider.health()
    assert health.healthy is True
    assert rec.requests == []  # Q10: no live probe


# --- W8.1.3: observational equivalence with the mock -------------------------


async def test_observationally_equivalent_to_mock_image_provider() -> None:
    """The runner cannot tell the two apart by type, fields, status, or shape."""
    req = _req(size="1024x1024")
    real, _ = _provider(_ok("https://cdn.openai.example/real.png"))

    real_resp = await real.generate_image(req)
    mock_resp = await MockImageProvider().generate_image(req)

    # Same DTO type.
    assert type(real_resp) is type(mock_resp) is GenerateImageResponse

    # Same populated field-set (which fields are None vs set is identical).
    def _populated(resp: GenerateImageResponse) -> set[str]:
        return {f.name for f in fields(resp) if getattr(resp, f.name) is not None}

    assert _populated(real_resp) == _populated(mock_resp)
    # Same status semantics + same output/usage shape.
    assert real_resp.status is mock_resp.status is ProviderStatus.SUCCEEDED
    assert real_resp.output.keys() == mock_resp.output.keys() == {"image_ref", "size"}
    assert real_resp.usage is not None and mock_resp.usage is not None
    assert real_resp.usage.unit == mock_resp.usage.unit == "images"
    assert real_resp.usage.quantity == mock_resp.usage.quantity == 1
    # Only the *values* differ (the image ref + provider id).
    assert real_resp.image_ref != mock_resp.image_ref
    assert real_resp.provider != mock_resp.provider


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
