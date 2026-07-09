"""Integration tests for ``GET /api/v1/users/me`` (Slice α3.3).

End-to-end coverage of the first authenticated business endpoint —
through middleware, exception handlers, DI, ``get_current_user``, the
real ``SessionRepository``/``UserRepository``, and the real database.
Every test uses the SAVEPOINT-rolled-back ``client`` fixture from
``tests/integration/conftest.py``; nothing persists between tests.

Coverage map vs. pre-flight §3.2 (acceptance criteria in parentheses):

* H1  happy path                 → 200 + ``UserPublic`` shape (A1)
* H3  no ``Authorization``       → 401 (A2)
* H4  wrong scheme (Basic)       → 401 (A3)
* H5  empty ``Bearer``           → 401 (A3)
* H6  garbage JWT                → 401 (A4)
* H7  tampered signature         → 401, no repo lookup reached (A4, A13)
* H8  expired access token       → 401 (A5)
* H9  session revoked mid-flight → 401 (A7)
* H12 happy-path emits log       → ``auth.request.authenticated`` (A11)
* H13 JWT with unknown ``sid``   → 401 sid_missing_session (A6, A12)
* H14 refresh token in header    → 401 (A8, wrong ``kind`` claim)
* H2  password_hash never leaks  → folded into H1's assertions

Deliberately deferred to unit coverage (see
``tests/unit/api/test_deps_get_current_user.py``):

* Session-row TTL exceeded (``row.expires_at <= now``)
* User row hard-deleted between issuance and request

Both require mutating persisted state under the request's connection
and would introduce a DB-admin fixture that no other integration test
needs. The unit suite exercises both branches directly.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import jwt
import pytest
from httpx import AsyncClient

# ``configure_logging`` uses ``structlog.dev.ConsoleRenderer`` in the
# ``local`` env, which wraps every field name and value in ANSI colour
# escapes. Substring assertions must strip these to match the underlying
# text (``reason=verify_failed``, ``user_id=<uuid>``, etc.).
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _fresh_email() -> str:
    return f"me-{uuid4()}@example.com"


async def _register(client: AsyncClient) -> dict:
    """Register a fresh user and return the ``data`` payload."""
    r = await client.post(
        "/api/v1/auth/register",
        json={"email": _fresh_email(), "password": "correct horse battery staple", "name": "M"},
    )
    assert r.status_code == 201, r.text
    return r.json()["data"]


def _decode(token: str, settings) -> dict:
    return jwt.decode(
        token,
        settings.jwt_secret.get_secret_value(),
        algorithms=[settings.jwt_algorithm],
    )


def _reencode(claims: dict, settings) -> str:
    return jwt.encode(
        claims,
        settings.jwt_secret.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )


def _deps_messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Return every rendered log message emitted from ``app.api.v1.deps``.

    ``configure_logging`` routes structlog through ``stdlib.LoggerFactory``
    with ``cache_logger_on_first_use=True``, which pins the module-level
    ``_LOGGER`` at import time. ``structlog.testing.capture_logs`` cannot
    reconfigure a cached logger post hoc; pytest's ``caplog`` (which
    subscribes to stdlib records at the root handler level) is the
    correct capture seam here. Substring assertions on the rendered
    console line verify both event name and structured fields (e.g.
    ``reason=verify_failed``) without coupling to structlog internals.
    """
    return [
        _ANSI_RE.sub("", record.getMessage())
        for record in caplog.records
        if record.name == "app.api.v1.deps"
    ]


# ---- H1 / H2 — happy path & sensitive-field non-leak ------------------


@pytest.mark.integration
async def test_me_happy_path_returns_public_projection(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]

    r = await client.get("/api/v1/users/me", headers={"Authorization": f"Bearer {access}"})

    assert r.status_code == 200, r.text
    body = r.json()
    # Envelope shape (API_CONTRACT §1.1).
    assert set(body.keys()) == {"data", "meta"}
    assert body["meta"]["request_id"]

    # ``data`` is the exact ``UserPublic`` projection issued by the register
    # response, byte-for-byte — proves the schema move preserved semantics.
    data = body["data"]
    assert data == reg["user"]

    # Sensitive-field non-leak (H2).
    assert "password_hash" not in data
    assert "last_login_at" not in data
    assert "updated_at" not in data
    assert "version" not in data


# ---- H3–H6 — malformed / missing bearer -------------------------------


@pytest.mark.integration
async def test_me_missing_authorization_header_returns_401(client: AsyncClient) -> None:
    r = await client.get("/api/v1/users/me")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "UNAUTHENTICATED"


@pytest.mark.integration
async def test_me_wrong_scheme_basic_returns_401(client: AsyncClient) -> None:
    r = await client.get("/api/v1/users/me", headers={"Authorization": "Basic dXNlcjpwYXNz"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "UNAUTHENTICATED"


@pytest.mark.integration
async def test_me_empty_bearer_token_returns_401(client: AsyncClient) -> None:
    r = await client.get("/api/v1/users/me", headers={"Authorization": "Bearer "})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "UNAUTHENTICATED"


@pytest.mark.integration
async def test_me_garbage_jwt_returns_401(client: AsyncClient) -> None:
    r = await client.get("/api/v1/users/me", headers={"Authorization": "Bearer not.a.real.jwt"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "UNAUTHENTICATED"


# ---- H7 — tampered signature must fail before any DB lookup (A13) -----


@pytest.mark.integration
async def test_me_tampered_signature_rejects_before_session_lookup(
    client: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    """A13: an invalid-signature JWT short-circuits at the verify step;
    no ``SessionRepository`` call ever runs. We prove this via the
    structured-log trace: rejection reason is ``verify_failed``, and
    the session-liveness reasons (``sid_missing_session`` /
    ``session_revoked`` / ``session_expired``) are absent."""
    caplog.set_level(logging.WARNING, logger="app.api.v1.deps")
    reg = await _register(client)
    access = reg["access_token"]
    # Flip the final character of the signature segment. Ensures a
    # syntactically valid three-part JWT that fails cryptographic
    # verification specifically (not shape validation).
    head, payload, sig = access.split(".")
    tampered_sig = sig[:-1] + ("A" if sig[-1] != "A" else "B")
    tampered = f"{head}.{payload}.{tampered_sig}"

    r = await client.get("/api/v1/users/me", headers={"Authorization": f"Bearer {tampered}"})

    assert r.status_code == 401
    messages = _deps_messages(caplog)
    rejection_msgs = [m for m in messages if "auth.request.rejected" in m]
    assert any(
        "reason=verify_failed" in m for m in rejection_msgs
    ), f"expected reason=verify_failed in dep logs; saw: {rejection_msgs}"
    session_branch_reasons = ("sid_missing_session", "session_revoked", "session_expired")
    for m in rejection_msgs:
        for reason in session_branch_reasons:
            assert (
                f"reason={reason}" not in m
            ), f"invalid-signature path must not reach session lookup; saw: {m}"


# ---- H8 — expired access token ----------------------------------------


@pytest.mark.integration
async def test_me_expired_access_token_returns_401(client: AsyncClient, settings) -> None:
    reg = await _register(client)
    claims = _decode(reg["access_token"], settings)
    claims["exp"] = int((datetime.now(UTC) - timedelta(hours=1)).timestamp())
    expired = _reencode(claims, settings)

    r = await client.get("/api/v1/users/me", headers={"Authorization": f"Bearer {expired}"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "UNAUTHENTICATED"


# ---- H9 — session revoked mid-flight ----------------------------------


@pytest.mark.integration
async def test_me_after_logout_returns_401_session_revoked(
    client: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    headers = {"Authorization": f"Bearer {access}"}

    # First call succeeds — session is live.
    first = await client.get("/api/v1/users/me", headers=headers)
    assert first.status_code == 200

    # Revoke the session via logout.
    logout = await client.post("/api/v1/auth/logout", headers=headers)
    assert logout.status_code == 204

    # Reuse the same (still-cryptographically-valid) access token — the
    # session row is now revoked, so ``get_current_user`` rejects it.
    caplog.set_level(logging.WARNING, logger="app.api.v1.deps")
    caplog.clear()  # drop logs from the pre-logout happy-path call.
    second = await client.get("/api/v1/users/me", headers=headers)
    assert second.status_code == 401
    messages = _deps_messages(caplog)
    assert any(
        "auth.request.rejected" in m and "reason=session_revoked" in m for m in messages
    ), f"expected session_revoked rejection log; saw: {messages}"


# ---- H12 — happy path emits authenticated_request log (contract-only) --


@pytest.mark.integration
async def test_me_happy_path_emits_authenticated_log(
    client: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    """Contract-only assertion per pre-flight §3.2 Rec 2:
    verify the event name and that ``user_id`` / ``session_id`` are
    populated. Timestamps, ordering, and incidental metadata are NOT
    asserted — that would couple the test to logging plumbing."""
    caplog.set_level(logging.INFO, logger="app.api.v1.deps")
    reg = await _register(client)
    access = reg["access_token"]

    r = await client.get("/api/v1/users/me", headers={"Authorization": f"Bearer {access}"})
    assert r.status_code == 200

    messages = _deps_messages(caplog)
    authed = [m for m in messages if "auth.request.authenticated" in m]
    assert authed, f"expected auth.request.authenticated log; saw: {messages}"
    entry = authed[0]
    assert "user_id=" in entry, f"user_id must be populated on the happy-path log; saw: {entry}"
    assert (
        "session_id=" in entry
    ), f"session_id must be populated on the happy-path log; saw: {entry}"


# ---- H13 — JWT with unknown session id → sid_missing_session ----------


@pytest.mark.integration
async def test_me_with_unknown_sid_returns_401_missing_session(
    client: AsyncClient, settings, caplog: pytest.LogCaptureFixture
) -> None:
    """A12: a JWT bearing a valid signature but a ``sid`` that references
    no session (attacker-forged or deleted-since-issuance) → 401 via
    the ``sid_missing_session`` branch."""
    caplog.set_level(logging.WARNING, logger="app.api.v1.deps")
    reg = await _register(client)
    claims = _decode(reg["access_token"], settings)
    claims["sid"] = str(uuid4())  # random UUID — no session has this id.
    # Keep exp valid so we're specifically exercising the session
    # existence check, not the JWT expiry branch.
    claims["exp"] = int((datetime.now(UTC) + timedelta(hours=1)).timestamp())
    forged = _reencode(claims, settings)

    r = await client.get("/api/v1/users/me", headers={"Authorization": f"Bearer {forged}"})
    assert r.status_code == 401
    messages = _deps_messages(caplog)
    assert any(
        "auth.request.rejected" in m and "reason=sid_missing_session" in m for m in messages
    ), f"expected sid_missing_session rejection log; saw: {messages}"


# ---- H14 — refresh token used as bearer -------------------------------


@pytest.mark.integration
async def test_me_with_refresh_token_returns_401(client: AsyncClient) -> None:
    """Wrong ``kind`` claim: refresh tokens must not authenticate
    resource requests. ``AuthTokenIssuer.verify_access`` rejects them at
    the JWT-verify step."""
    reg = await _register(client)
    r = await client.get(
        "/api/v1/users/me",
        headers={"Authorization": f"Bearer {reg['refresh_token']}"},
    )
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "UNAUTHENTICATED"
