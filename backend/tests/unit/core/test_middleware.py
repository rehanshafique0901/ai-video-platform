"""Unit tests for ``app.core.middleware.RequestIdMiddleware``.

Uses ``httpx.AsyncClient + ASGITransport`` instead of
``fastapi.testclient.TestClient`` — see ``test_errors.py`` for the
rationale.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from app.core.middleware import REQUEST_ID_HEADER, RequestIdMiddleware


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestIdMiddleware)

    @app.get("/echo")
    async def echo(request: Request) -> dict[str, str]:
        return {"request_id": getattr(request.state, "request_id", "")}

    return app


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=_build_app())
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.unit
async def test_middleware_generates_request_id_when_header_absent(
    client: AsyncClient,
) -> None:
    r = await client.get("/echo")
    assert r.status_code == 200
    body = r.json()
    assert body["request_id"]  # non-empty
    assert REQUEST_ID_HEADER in r.headers
    assert r.headers[REQUEST_ID_HEADER] == body["request_id"]


@pytest.mark.unit
async def test_middleware_preserves_incoming_request_id(client: AsyncClient) -> None:
    incoming = "12345678-1234-1234-1234-123456789abc"
    r = await client.get("/echo", headers={REQUEST_ID_HEADER: incoming})
    assert r.status_code == 200
    assert r.json()["request_id"] == incoming
    assert r.headers[REQUEST_ID_HEADER] == incoming


@pytest.mark.unit
async def test_middleware_generates_distinct_ids_across_requests(
    client: AsyncClient,
) -> None:
    r1 = await client.get("/echo")
    r2 = await client.get("/echo")
    assert r1.headers[REQUEST_ID_HEADER] != r2.headers[REQUEST_ID_HEADER]
