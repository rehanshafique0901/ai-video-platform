"""Unit tests for the α8.3b webhook router (status mapping + raw-body capture).

The router is deliberately thin: read the **raw** body, call the ingress use case,
map the outcome to a status code. These tests mount only the webhooks router on a
bare FastAPI app and override the use-case dependency — **no DB, no container
init** — to pin the HTTP contract in isolation:

* 200 — accepted (``resumed`` / ``unknown_job`` ...); duplicates/unknowns are acked.
* 401 — ``WebhookVerificationError`` (bad/stale/missing signature).
* 400 — ``WebhookMalformedError``.
* 404 — ``unsupported`` provider.
* the exact request bytes reach the use case (the signature covers them).
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.v1.routers import webhooks
from app.application.interfaces.webhook_verifier import (
    WebhookMalformedError,
    WebhookVerificationError,
)
from app.application.use_cases.workflow.receive_provider_webhook import WebhookIngestResult
from app.core import container

pytestmark = pytest.mark.unit


class _StubUseCase:
    """Scripts the ingress outcome (a result, or an exception to raise)."""

    def __init__(
        self, *, result: WebhookIngestResult | None = None, raises: Exception | None = None
    ) -> None:
        self._result = result
        self._raises = raises
        self.seen_body: bytes | None = None
        self.seen_provider: str | None = None

    async def execute(
        self, *, provider: str, body: bytes, headers: Mapping[str, str]
    ) -> WebhookIngestResult:
        self.seen_body = body
        self.seen_provider = provider
        if self._raises is not None:
            raise self._raises
        assert self._result is not None
        return self._result


def _app(stub: _StubUseCase) -> FastAPI:
    app = FastAPI()
    app.include_router(webhooks.router, prefix="/api/v1")
    app.dependency_overrides[container.get_receive_provider_webhook_use_case] = lambda: stub
    return app


async def _post(app: FastAPI, path: str, body: bytes):  # type: ignore[no-untyped-def]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.post(path, content=body)


async def test_resumed_returns_200_and_passes_raw_body() -> None:
    stub = _StubUseCase(result=WebhookIngestResult(status="resumed", run_status="succeeded"))
    raw = b'{"status":"OK","request_id":"fal-req-1"}'

    response = await _post(_app(stub), "/api/v1/webhooks/providers/fal", raw)

    assert response.status_code == 200
    assert response.json()["data"]["status"] == "resumed"
    assert stub.seen_body == raw  # exact bytes, not a re-serialized dict
    assert stub.seen_provider == "fal"


async def test_unknown_job_still_returns_200() -> None:
    stub = _StubUseCase(result=WebhookIngestResult(status="unknown_job"))
    response = await _post(_app(stub), "/api/v1/webhooks/providers/fal", b"{}")
    assert response.status_code == 200
    assert response.json()["data"]["status"] == "unknown_job"


async def test_verification_error_returns_401() -> None:
    stub = _StubUseCase(raises=WebhookVerificationError("bad sig"))
    response = await _post(_app(stub), "/api/v1/webhooks/providers/fal", b"{}")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "WEBHOOK_UNVERIFIED"


async def test_malformed_error_returns_400() -> None:
    stub = _StubUseCase(raises=WebhookMalformedError("no request id"))
    response = await _post(_app(stub), "/api/v1/webhooks/providers/fal", b"{}")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "WEBHOOK_MALFORMED"


async def test_unsupported_provider_returns_404() -> None:
    stub = _StubUseCase(result=WebhookIngestResult(status="unsupported"))
    response = await _post(_app(stub), "/api/v1/webhooks/providers/stripe", b"{}")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "WEBHOOK_PROVIDER_UNKNOWN"
