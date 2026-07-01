"""Integration tests for ``/api/v1/auth/register`` and ``/api/v1/auth/login``.

End-to-end: through the middleware stack, exception handlers, DI
container, use cases, repositories, and the real database. Each test
is isolated via the SAVEPOINT-rolled-back client fixture in
``tests/integration/conftest.py``.
"""

from __future__ import annotations

from uuid import uuid4

import jwt
import pytest
from httpx import AsyncClient


def _fresh_email() -> str:
    return f"e2e-{uuid4()}@example.com"


# ---- register --------------------------------------------------------


@pytest.mark.integration
async def test_register_happy_path_returns_201_with_tokens(client: AsyncClient) -> None:
    body = {"email": _fresh_email(), "password": "correct horse battery staple", "name": "E2E"}
    r = await client.post("/api/v1/auth/register", json=body)

    assert r.status_code == 201, r.text
    data = r.json()["data"]
    assert data["user"]["email"] == body["email"]
    assert data["user"]["display_name"] == "E2E"
    assert data["user"]["tenant_id"]  # populated UUID string
    assert data["access_token"]
    assert data["refresh_token"]
    assert data["token_type"] == "Bearer"
    # ``password_hash`` never leaks into the response payload.
    assert "password_hash" not in data["user"]


@pytest.mark.integration
async def test_register_duplicate_email_returns_409_conflict(client: AsyncClient) -> None:
    email = _fresh_email()
    body = {"email": email, "password": "correct horse battery staple", "name": "First"}
    r1 = await client.post("/api/v1/auth/register", json=body)
    assert r1.status_code == 201, r1.text

    body["name"] = "Second"
    r2 = await client.post("/api/v1/auth/register", json=body)
    assert r2.status_code == 409, r2.text
    assert r2.json()["error"]["code"] == "CONFLICT"


@pytest.mark.integration
async def test_register_short_password_returns_422(client: AsyncClient) -> None:
    body = {"email": _fresh_email(), "password": "short", "name": "E2E"}
    r = await client.post("/api/v1/auth/register", json=body)
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "VALIDATION_FAILED"


@pytest.mark.integration
async def test_register_lowercases_email(client: AsyncClient) -> None:
    mixed = f"E2E-MIXED-{uuid4()}@Example.COM"
    body = {"email": mixed, "password": "correct horse battery staple", "name": "Mixed"}
    r = await client.post("/api/v1/auth/register", json=body)
    assert r.status_code == 201, r.text
    assert r.json()["data"]["user"]["email"] == mixed.lower()


@pytest.mark.integration
async def test_register_access_token_carries_sid_and_fam(client: AsyncClient, settings) -> None:
    """The access JWT must carry ``sid`` + ``fam`` claims (improvement A)."""
    body = {"email": _fresh_email(), "password": "correct horse battery staple", "name": "E2E"}
    r = await client.post("/api/v1/auth/register", json=body)
    assert r.status_code == 201, r.text

    access_token = r.json()["data"]["access_token"]
    payload = jwt.decode(
        access_token, settings.jwt_secret.get_secret_value(), algorithms=[settings.jwt_algorithm]
    )
    assert payload["kind"] == "access"
    assert payload["sid"]
    assert payload["fam"]
    assert payload["sub"] == r.json()["data"]["user"]["id"]


# ---- login -----------------------------------------------------------


@pytest.mark.integration
async def test_login_happy_path_returns_200_with_new_tokens(client: AsyncClient) -> None:
    email = _fresh_email()
    reg = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "correct horse battery staple", "name": "L"},
    )
    assert reg.status_code == 201, reg.text
    register_refresh = reg.json()["data"]["refresh_token"]

    r = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "correct horse battery staple"},
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["user"]["email"] == email
    assert data["access_token"]
    assert data["refresh_token"]
    # Login mints a NEW family — refresh token must differ from register's.
    assert data["refresh_token"] != register_refresh


@pytest.mark.integration
async def test_login_wrong_password_returns_401(client: AsyncClient) -> None:
    email = _fresh_email()
    reg = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "correct horse battery staple", "name": "W"},
    )
    assert reg.status_code == 201, reg.text

    r = await client.post("/api/v1/auth/login", json={"email": email, "password": "wrong password"})
    assert r.status_code == 401, r.text
    assert r.json()["error"]["code"] == "UNAUTHENTICATED"


@pytest.mark.integration
async def test_login_unknown_email_returns_401_same_message_as_wrong_password(
    client: AsyncClient,
) -> None:
    """Anti-enumeration end-to-end: the response body for unknown-email
    must be indistinguishable from wrong-password."""
    email = _fresh_email()
    await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "correct horse battery staple", "name": "M"},
    )
    wrong_pw = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": "wrong password"}
    )
    unknown = await client.post(
        "/api/v1/auth/login", json={"email": _fresh_email(), "password": "anything"}
    )

    assert wrong_pw.status_code == unknown.status_code == 401
    assert wrong_pw.json()["error"]["code"] == unknown.json()["error"]["code"]
    assert wrong_pw.json()["error"]["message"] == unknown.json()["error"]["message"]


@pytest.mark.integration
async def test_login_two_devices_creates_distinct_families(client: AsyncClient, settings) -> None:
    email = _fresh_email()
    await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "correct horse battery staple", "name": "D"},
    )
    r1 = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "correct horse battery staple"},
    )
    r2 = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "correct horse battery staple"},
    )
    fam1 = jwt.decode(
        r1.json()["data"]["access_token"],
        settings.jwt_secret.get_secret_value(),
        algorithms=[settings.jwt_algorithm],
    )["fam"]
    fam2 = jwt.decode(
        r2.json()["data"]["access_token"],
        settings.jwt_secret.get_secret_value(),
        algorithms=[settings.jwt_algorithm],
    )["fam"]
    assert fam1 != fam2


# ---- envelope --------------------------------------------------------


@pytest.mark.integration
async def test_responses_include_request_id_in_meta(client: AsyncClient) -> None:
    r = await client.post(
        "/api/v1/auth/register",
        json={"email": _fresh_email(), "password": "correct horse battery staple", "name": "R"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["meta"]["request_id"]
