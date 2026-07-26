"""Integration tests for ``/api/v1/social-accounts`` (α8.6a Account Connections).

End-to-end coverage of wiring, auth, envelope, validation, and the uniform 404 path through
middleware, exception handlers, DI, ``get_current_user``, and the live database (all via the
SAVEPOINT-rolled-back ``client`` fixture).

Per the platform's layered proof cadence: the credential-encryption boundary + connection
happy path (exchange → encrypt → store → authorize → revoke) is proven against a real DB by
``test_social_credential_service.py`` and the use-case unit suite; these tests prove the
HTTP surface. The callback happy path is intentionally not exercised here — the credential
service commits on its own connection, which the shared-DB client fixture cannot roll back.

Coverage:
* connect / list / revoke require auth                                → 401
* connect returns a provider authorization URL                       → 200
* connect for an unsupported platform                                 → 422
* callback with a forged/invalid state                               → 422
* callback missing query params                                       → 422
* list is empty for a fresh user                                     → 200 [] + meta
* revoke unknown id / non-uuid                                        → 404 / 422
"""

from __future__ import annotations

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
            "email": f"sa-{uuid4()}@example.com",
            "password": "correct horse battery staple",
            "name": "SA",
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["data"]["access_token"]


async def test_endpoints_require_auth(client: AsyncClient) -> None:
    assert (
        await client.post("/api/v1/social-accounts/connect", json={"platform": "mock"})
    ).status_code == 401
    assert (await client.get("/api/v1/social-accounts")).status_code == 401
    assert (await client.post(f"/api/v1/social-accounts/{uuid4()}/revoke")).status_code == 401


async def test_connect_returns_authorization_url(client: AsyncClient) -> None:
    access = await _register(client)
    r = await client.post(
        "/api/v1/social-accounts/connect",
        headers=_auth(access),
        json={"platform": "mock"},
    )
    assert r.status_code == 200, r.text
    url = r.json()["data"]["authorization_url"]
    assert url.startswith("https://mock.oauth.local/authorize")
    assert "state=" in url


async def test_connect_unsupported_platform_is_422(client: AsyncClient) -> None:
    access = await _register(client)
    r = await client.post(
        "/api/v1/social-accounts/connect",
        headers=_auth(access),
        json={"platform": "youtube"},
    )
    assert r.status_code == 422, r.text


async def test_callback_with_invalid_state_is_422(client: AsyncClient) -> None:
    r = await client.get(
        "/api/v1/social-accounts/callback",
        params={"state": "forged.state.token", "code": "abc"},
    )
    assert r.status_code == 422, r.text


async def test_callback_missing_params_is_422(client: AsyncClient) -> None:
    assert (await client.get("/api/v1/social-accounts/callback")).status_code == 422


async def test_list_empty_for_fresh_user(client: AsyncClient) -> None:
    access = await _register(client)
    r = await client.get("/api/v1/social-accounts", headers=_auth(access))
    assert r.status_code == 200, r.text
    assert r.json()["data"] == []


async def test_revoke_unknown_is_404(client: AsyncClient) -> None:
    access = await _register(client)
    r = await client.post(f"/api/v1/social-accounts/{uuid4()}/revoke", headers=_auth(access))
    assert r.status_code == 404, r.text


async def test_revoke_non_uuid_is_422(client: AsyncClient) -> None:
    access = await _register(client)
    r = await client.post("/api/v1/social-accounts/not-a-uuid/revoke", headers=_auth(access))
    assert r.status_code == 422, r.text
