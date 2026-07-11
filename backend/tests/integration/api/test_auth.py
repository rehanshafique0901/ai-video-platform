"""Integration tests for ``/api/v1/auth/*`` (register, login, refresh, logout).

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
    body = {
        "email": _fresh_email(),
        "password": "correct horse battery staple",
        "name": "E2E",
    }
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
async def test_register_duplicate_email_returns_409_conflict(
    client: AsyncClient,
) -> None:
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
    body = {
        "email": _fresh_email(),
        "password": "correct horse battery staple",
        "name": "E2E",
    }
    r = await client.post("/api/v1/auth/register", json=body)
    assert r.status_code == 201, r.text

    access_token = r.json()["data"]["access_token"]
    payload = jwt.decode(
        access_token,
        settings.jwt_secret.get_secret_value(),
        algorithms=[settings.jwt_algorithm],
    )
    assert payload["kind"] == "access"
    assert payload["sid"]
    assert payload["fam"]
    assert payload["sub"] == r.json()["data"]["user"]["id"]


# ---- login -----------------------------------------------------------


@pytest.mark.integration
async def test_login_happy_path_returns_200_with_new_tokens(
    client: AsyncClient,
) -> None:
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
        json={
            "email": _fresh_email(),
            "password": "correct horse battery staple",
            "name": "R",
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["meta"]["request_id"]


# ---- α2b: refresh ----------------------------------------------------


async def _register(client: AsyncClient) -> tuple[str, dict]:
    """Register a fresh user; return (email, data payload)."""
    email = _fresh_email()
    r = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "correct horse battery staple", "name": "R"},
    )
    assert r.status_code == 201, r.text
    return email, r.json()["data"]


@pytest.mark.integration
async def test_refresh_happy_path_rotates_tokens_and_preserves_family(
    client: AsyncClient, settings
) -> None:
    _, reg = await _register(client)
    original_refresh = reg["refresh_token"]
    original_fam = jwt.decode(
        reg["access_token"],
        settings.jwt_secret.get_secret_value(),
        algorithms=[settings.jwt_algorithm],
    )["fam"]

    r = await client.post("/api/v1/auth/refresh", json={"refresh_token": original_refresh})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["access_token"] and data["refresh_token"]
    # Rotated tokens differ from the originals.
    assert data["refresh_token"] != original_refresh
    assert data["access_token"] != reg["access_token"]
    # Family preserved, sid rotated.
    new_access_claims = jwt.decode(
        data["access_token"],
        settings.jwt_secret.get_secret_value(),
        algorithms=[settings.jwt_algorithm],
    )
    assert new_access_claims["fam"] == original_fam
    old_sid = jwt.decode(
        reg["access_token"],
        settings.jwt_secret.get_secret_value(),
        algorithms=[settings.jwt_algorithm],
    )["sid"]
    assert new_access_claims["sid"] != old_sid


@pytest.mark.integration
async def test_refresh_reuse_detection_revokes_family(client: AsyncClient) -> None:
    """Replaying an already-rotated refresh token must invalidate every
    session in the family."""
    _, reg = await _register(client)
    original_refresh = reg["refresh_token"]

    # First refresh succeeds.
    ok = await client.post("/api/v1/auth/refresh", json={"refresh_token": original_refresh})
    assert ok.status_code == 200
    new_refresh = ok.json()["data"]["refresh_token"]

    # Replay: same original token → 401.
    replay = await client.post("/api/v1/auth/refresh", json={"refresh_token": original_refresh})
    assert replay.status_code == 401
    assert replay.json()["error"]["code"] == "UNAUTHENTICATED"

    # The freshly rotated token was also revoked by the family sweep.
    victim = await client.post("/api/v1/auth/refresh", json={"refresh_token": new_refresh})
    assert victim.status_code == 401


@pytest.mark.integration
async def test_refresh_with_garbage_token_returns_401(client: AsyncClient) -> None:
    r = await client.post("/api/v1/auth/refresh", json={"refresh_token": "not-a-jwt"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "UNAUTHENTICATED"
    assert r.json()["error"]["message"] == "invalid refresh token"


@pytest.mark.integration
async def test_refresh_with_access_token_returns_401(client: AsyncClient) -> None:
    """Sending an access token to /refresh must fail (wrong ``kind`` claim)."""
    _, reg = await _register(client)
    r = await client.post("/api/v1/auth/refresh", json={"refresh_token": reg["access_token"]})
    assert r.status_code == 401


@pytest.mark.integration
async def test_refresh_sid_mismatch_returns_401(client: AsyncClient, settings) -> None:
    """A12: refresh JWT whose ``sid`` doesn't match the row → 401."""
    from datetime import UTC, datetime, timedelta

    _, reg = await _register(client)
    # Decode the real refresh, tamper the ``sid`` to a random UUID,
    # re-sign with the same secret so the signature is valid.
    original = jwt.decode(
        reg["refresh_token"],
        settings.jwt_secret.get_secret_value(),
        algorithms=[settings.jwt_algorithm],
    )
    tampered_payload = {**original, "sid": str(uuid4())}
    tampered_payload["exp"] = int(
        (datetime.now(UTC) + timedelta(days=1)).timestamp()
    )  # ensure not expired
    tampered = jwt.encode(
        tampered_payload,
        settings.jwt_secret.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )
    # Note: hash lookup will miss because the tampered JWT string is
    # different from the stored hash's preimage. Either branch (hash
    # miss OR sid mismatch) yields the same 401 — both are correct.
    r = await client.post("/api/v1/auth/refresh", json={"refresh_token": tampered})
    assert r.status_code == 401
    assert r.json()["error"]["message"] == "invalid refresh token"


# ---- α2b: logout -----------------------------------------------------


@pytest.mark.integration
async def test_logout_happy_path_returns_204_and_revokes_session(
    client: AsyncClient,
) -> None:
    _, reg = await _register(client)
    r = await client.post(
        "/api/v1/auth/logout",
        headers={"Authorization": f"Bearer {reg['access_token']}"},
    )
    assert r.status_code == 204
    assert r.content == b""

    # The refresh token issued alongside the now-revoked session must
    # also be rejected (session row is revoked).
    followup = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": reg["refresh_token"]}
    )
    assert followup.status_code == 401


@pytest.mark.integration
async def test_logout_is_idempotent(client: AsyncClient) -> None:
    _, reg = await _register(client)
    headers = {"Authorization": f"Bearer {reg['access_token']}"}
    r1 = await client.post("/api/v1/auth/logout", headers=headers)
    r2 = await client.post("/api/v1/auth/logout", headers=headers)
    assert r1.status_code == r2.status_code == 204


@pytest.mark.integration
async def test_logout_missing_authorization_header_returns_401(
    client: AsyncClient,
) -> None:
    r = await client.post("/api/v1/auth/logout")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "UNAUTHENTICATED"


@pytest.mark.integration
async def test_logout_malformed_authorization_header_returns_401(
    client: AsyncClient,
) -> None:
    r = await client.post("/api/v1/auth/logout", headers={"Authorization": "NotBearer xyz"})
    assert r.status_code == 401


@pytest.mark.integration
async def test_logout_with_refresh_token_returns_401(client: AsyncClient) -> None:
    """Sending a refresh token to /logout must fail (wrong kind)."""
    _, reg = await _register(client)
    r = await client.post(
        "/api/v1/auth/logout",
        headers={"Authorization": f"Bearer {reg['refresh_token']}"},
    )
    assert r.status_code == 401
