"""Integration tests for the health + readiness endpoints.

End-to-end: through the middleware stack, the exception handlers, and
the real session factory bound to Supabase.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from app.core.middleware import REQUEST_ID_HEADER


@pytest.mark.integration
async def test_healthz_returns_ok_envelope(client: AsyncClient) -> None:
    r = await client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["data"]["status"] == "ok"
    assert "request_id" in body["meta"]
    assert body["meta"]["request_id"]  # non-empty


@pytest.mark.integration
async def test_healthz_echoes_request_id_header(client: AsyncClient) -> None:
    incoming = "12345678-1234-1234-1234-123456789abc"
    r = await client.get("/healthz", headers={REQUEST_ID_HEADER: incoming})
    assert r.status_code == 200
    assert r.headers[REQUEST_ID_HEADER] == incoming
    assert r.json()["meta"]["request_id"] == incoming


@pytest.mark.integration
async def test_readyz_returns_ready_when_db_up(client: AsyncClient) -> None:
    r = await client.get("/readyz")
    assert r.status_code == 200
    body = r.json()
    assert body["data"]["status"] == "ready"
    assert "request_id" in body["meta"]
