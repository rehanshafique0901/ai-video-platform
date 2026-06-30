"""Unit tests for ``app.core.errors``: the error tree + handlers.

Uses ``httpx.AsyncClient + ASGITransport`` instead of
``fastapi.testclient.TestClient`` because Starlette 1.x deprecated the
sync TestClient's bundled httpx integration in favour of a separate
``httpx2`` package. ASGITransport is the modern async-native path and
needs no extra dependency.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.errors import (
    ConflictError,
    NotFoundError,
    UnauthorizedError,
    ValidationFailedError,
    register_exception_handlers,
)


def _build_app() -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/raise/{kind}")
    async def raise_endpoint(kind: str) -> dict[str, str]:
        if kind == "not_found":
            raise NotFoundError("nope", {"id": "x"})
        if kind == "conflict":
            raise ConflictError("dup", {"key": "y"})
        if kind == "unauthorized":
            raise UnauthorizedError("nope")
        if kind == "validation":
            raise ValidationFailedError("bad", {"field": "email"})
        if kind == "boom":
            raise RuntimeError("unhandled")
        return {"unreachable": kind}

    @app.get("/needs-query")
    async def needs_query(required: int) -> dict[str, int]:
        return {"got": required}

    return app


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=_build_app())
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.unit
async def test_not_found_envelope(client: AsyncClient) -> None:
    r = await client.get("/raise/not_found")
    assert r.status_code == 404
    body = r.json()
    assert body["error"]["code"] == "NOT_FOUND"
    assert body["error"]["message"] == "nope"
    assert body["error"]["details"] == {"id": "x"}
    assert "request_id" in body["error"]


@pytest.mark.unit
async def test_conflict_envelope(client: AsyncClient) -> None:
    r = await client.get("/raise/conflict")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "CONFLICT"


@pytest.mark.unit
async def test_unauthorized_envelope(client: AsyncClient) -> None:
    r = await client.get("/raise/unauthorized")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "UNAUTHENTICATED"


@pytest.mark.unit
async def test_validation_failed_envelope(client: AsyncClient) -> None:
    r = await client.get("/raise/validation")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_FAILED"


@pytest.mark.unit
async def test_unhandled_exception_becomes_500_internal() -> None:
    """``Exception``-level handler runs and writes a 500 envelope.

    Starlette 1.x's ``ServerErrorMiddleware`` re-raises after running
    an ``Exception``-level handler (so uvicorn / lifespan / error
    logging see the original error). In real production with uvicorn
    the client receives the response and the re-raise is internal; in
    httpx's ``ASGITransport`` the re-raise is surfaced to the test
    client by default. We opt out via ``raise_app_exceptions=False``
    so we can observe the handler's response shape.
    """
    transport = ASGITransport(app=_build_app(), raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/raise/boom")
    assert r.status_code == 500
    body = r.json()
    assert body["error"]["code"] == "INTERNAL_ERROR"
    assert body["error"]["details"] == {}


@pytest.mark.unit
async def test_request_validation_envelope(client: AsyncClient) -> None:
    r = await client.get("/needs-query")  # missing required query param
    assert r.status_code == 422
    body = r.json()
    assert body["error"]["code"] == "VALIDATION_FAILED"
    assert "errors" in body["error"]["details"]
