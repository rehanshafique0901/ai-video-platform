"""Integration tests for ``/api/v1/publish-jobs`` (α8.6b Publish Runtime).

Proves the HTTP surface end-to-end through middleware, exception handlers, DI,
``get_current_user``, and the live DB (via the SAVEPOINT-rolled-back ``client`` fixture):
auth, validation, the uniform ``404`` for unknown/foreign resources, and the empty list.

The create happy path (queue → worker claim → destination upload) requires a CONNECTED
social account, which the credential service commits on its own connection (the shared-DB
client fixture cannot roll that back). It is proven against a real DB by
``test_publish_runtime_end_to_end.py`` and the use-case unit suite; these tests prove the
HTTP contract only.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.integration


def _auth(access: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access}"}


async def _register(client: AsyncClient) -> str:
    r = await client.post(
        "/api/v1/auth/register",
        json={
            "email": f"pj-{uuid4()}@example.com",
            "password": "correct horse battery staple",
            "name": "PJ",
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["data"]["access_token"]


async def test_endpoints_require_auth(client: AsyncClient) -> None:
    assert (
        await client.post(
            "/api/v1/publish-jobs",
            json={"export_job_id": str(uuid4()), "social_account_id": str(uuid4())},
        )
    ).status_code == 401
    assert (await client.get("/api/v1/publish-jobs")).status_code == 401
    assert (await client.get(f"/api/v1/publish-jobs/{uuid4()}")).status_code == 401


async def test_list_empty_for_fresh_user(client: AsyncClient) -> None:
    access = await _register(client)
    r = await client.get("/api/v1/publish-jobs", headers=_auth(access))
    assert r.status_code == 200, r.text
    assert r.json()["data"] == []


async def test_create_unknown_account_is_404(client: AsyncClient) -> None:
    access = await _register(client)
    r = await client.post(
        "/api/v1/publish-jobs",
        headers=_auth(access),
        json={"export_job_id": str(uuid4()), "social_account_id": str(uuid4())},
    )
    assert r.status_code == 404, r.text


async def test_create_missing_body_is_422(client: AsyncClient) -> None:
    access = await _register(client)
    r = await client.post("/api/v1/publish-jobs", headers=_auth(access), json={})
    assert r.status_code == 422, r.text


async def test_create_non_uuid_body_is_422(client: AsyncClient) -> None:
    access = await _register(client)
    r = await client.post(
        "/api/v1/publish-jobs",
        headers=_auth(access),
        json={"export_job_id": "not-a-uuid", "social_account_id": str(uuid4())},
    )
    assert r.status_code == 422, r.text


async def test_create_naive_publish_at_is_422(client: AsyncClient) -> None:
    # α8.9b — a naive (tz-less) schedule is rejected at the ingress before any account lookup.
    access = await _register(client)
    naive = (datetime.now(UTC) + timedelta(hours=2)).replace(tzinfo=None)
    r = await client.post(
        "/api/v1/publish-jobs",
        headers=_auth(access),
        json={
            "export_job_id": str(uuid4()),
            "social_account_id": str(uuid4()),
            "publish_at": naive.isoformat(),
        },
    )
    assert r.status_code == 422, r.text


async def test_create_past_publish_at_is_422(client: AsyncClient) -> None:
    access = await _register(client)
    past = datetime.now(UTC) - timedelta(hours=1)
    r = await client.post(
        "/api/v1/publish-jobs",
        headers=_auth(access),
        json={
            "export_job_id": str(uuid4()),
            "social_account_id": str(uuid4()),
            "publish_at": past.isoformat(),
        },
    )
    assert r.status_code == 422, r.text


async def test_create_future_publish_at_parses_then_404_on_unknown_account(
    client: AsyncClient,
) -> None:
    # α8.9b — a valid future tz-aware schedule passes ingress validation, so the request
    # proceeds to the account gate and 404s on an unknown account (proving the field parses).
    access = await _register(client)
    future = datetime.now(UTC) + timedelta(days=1)
    r = await client.post(
        "/api/v1/publish-jobs",
        headers=_auth(access),
        json={
            "export_job_id": str(uuid4()),
            "social_account_id": str(uuid4()),
            "publish_at": future.isoformat(),
        },
    )
    assert r.status_code == 404, r.text


async def test_get_unknown_is_404(client: AsyncClient) -> None:
    access = await _register(client)
    r = await client.get(f"/api/v1/publish-jobs/{uuid4()}", headers=_auth(access))
    assert r.status_code == 404, r.text


async def test_get_non_uuid_is_422(client: AsyncClient) -> None:
    access = await _register(client)
    r = await client.get("/api/v1/publish-jobs/not-a-uuid", headers=_auth(access))
    assert r.status_code == 422, r.text
