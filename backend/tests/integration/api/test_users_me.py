"""Integration tests for ``/api/v1/users/me`` (Slice α3.3 GET + Slice α4 PATCH).

End-to-end coverage of the users-me endpoints — through middleware,
exception handlers, DI, ``get_current_user``, the real
``SessionRepository`` / ``UserRepository``, and the real database.
Every test uses the SAVEPOINT-rolled-back ``client`` fixture from
``tests/integration/conftest.py``; nothing persists between tests.

Coverage map (α3.3 GET, pre-flight §3.2):

* H1  happy GET path                 → 200 + ``UserPublic`` shape (A1)
* H3  no ``Authorization``           → 401 (A2)
* H4  wrong scheme (Basic)           → 401 (A3)
* H5  empty ``Bearer``               → 401 (A3)
* H6  garbage JWT                    → 401 (A4)
* H7  tampered signature             → 401, no repo lookup reached (A4, A13)
* H8  expired access token           → 401 (A5)
* H9  session revoked mid-flight     → 401 (A7)
* H12 happy-path emits log           → ``auth.request.authenticated`` (A11)
* H13 JWT with unknown ``sid``       → 401 sid_missing_session (A6, A12)
* H14 refresh token in header        → 401 (A8, wrong ``kind`` claim)
* H2  password_hash never leaks      → folded into H1's assertions

Coverage map (α4 PATCH, pre-flight §5.3):

* H15 happy PATCH (change name)      → 200, ``display_name`` + ``version+1`` (A1, A3)
* H16 PATCH without auth             → 401 (A5)
* H17 PATCH with stale version       → 412 ``VERSION_CONFLICT``; DB unchanged (A4)
* H18 PATCH with extra field         → 422 (``extra="forbid"``) (A6)
* H19 PATCH with empty body ``{}``   → 422 (missing required fields) (A7)
* H20 PATCH with only ``version``    → 422 (missing ``display_name``) (A7)
* H21 PATCH with same value          → 200, version + updated_at unchanged (A8, A12)
* H22 PATCH → GET round-trip         → identical response bodies (A13)
* H23 sequential-CAS race            → v1 wins, v1-again gets 412 (A3)
* H24 PATCH after logout             → 401 (A5, ``session_revoked``)

Deliberately deferred to unit coverage (see
``tests/unit/api/test_deps_get_current_user.py``):

* Session-row TTL exceeded (``row.expires_at <= now``)
* User row hard-deleted between issuance and request

Both require mutating persisted state under the request's connection
and would introduce a DB-admin fixture that no other integration test
needs. The unit suite exercises both branches directly.

Also NOT asserted at the HTTP layer (A12 timestamp strictness): the
``touch_updated_at`` trigger on ``users`` uses ``now()`` =
``transaction_timestamp()``, which is constant within a transaction.
Because the integration fixture keeps one outer transaction open for the
whole test, register-then-PATCH cannot observe a strictly-greater
``updated_at`` for the "real change" path. The invariant is enforced by
the schema trigger (verified during migration review) and by the unit
tests (which use a fake that respects the semantics). H21 CAN still
assert unchanged ``updated_at`` because the same-value no-op short-
circuits before any UPDATE — the trigger never fires.
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

    # Sensitive-field non-leak (H2). ``password_hash`` and
    # ``last_login_at`` remain internal per ``schemas/users.py`` module
    # docstring — the former is a secret, the latter an internal audit
    # signal not part of the public identity projection.
    assert "password_hash" not in data
    assert "last_login_at" not in data

    # α4 additions to :class:`UserPublic`: ``version`` and
    # ``updated_at`` are now exposed so clients can round-trip the
    # optimistic-concurrency fence with ``PATCH /users/me`` (§D5, §Q1)
    # and drive "last modified" UX (§A12). Type checks — the exact
    # values are asserted in H15+ (happy-path PATCH) and by the
    # ``data == reg["user"]`` cross-endpoint equality above.
    assert isinstance(data["version"], int) and data["version"] >= 1
    assert isinstance(data["updated_at"], str) and data["updated_at"]


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


# =====================================================================
# α4 — PATCH /api/v1/users/me
# =====================================================================


# ---- H15 — happy PATCH -------------------------------------------------


@pytest.mark.integration
async def test_patch_me_happy_path_changes_display_name(client: AsyncClient) -> None:
    """A1 + A3: valid access + correct version + non-empty name →
    200 with new ``display_name`` and ``version = old + 1``.

    Note on ``updated_at``: strictly-greater cannot be asserted here
    (see module docstring). The trigger-driven timestamp uses
    ``transaction_timestamp()``, constant within the SAVEPOINT fixture's
    outer transaction, so register and PATCH observe the same value.
    Format/presence is asserted; strict monotonicity is enforced by the
    DB trigger schema (migration territory) and by unit tests.
    """
    reg = await _register(client)
    access = reg["access_token"]
    user_v1 = reg["user"]
    assert user_v1["version"] == 1

    r = await client.patch(
        "/api/v1/users/me",
        headers={"Authorization": f"Bearer {access}"},
        json={"display_name": "New Name", "version": 1},
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body.keys()) == {"data", "meta"}
    assert body["meta"]["request_id"]
    data = body["data"]
    assert data["id"] == user_v1["id"]
    assert data["display_name"] == "New Name"
    assert data["version"] == 2
    # A12 (weakened): updated_at present and well-formed. Strict `>` is
    # untestable in this fixture — see module docstring.
    assert isinstance(data["updated_at"], str) and data["updated_at"]
    # A2: no sensitive fields leak through the mutation surface either.
    assert "password_hash" not in data
    assert "last_login_at" not in data


# ---- H16 — no auth -----------------------------------------------------


@pytest.mark.integration
async def test_patch_me_without_auth_returns_401(client: AsyncClient) -> None:
    """A5: PATCH must run through ``CurrentUserDep`` — missing header
    rejects with the same generic 401 as ``GET /me`` H3."""
    r = await client.patch(
        "/api/v1/users/me",
        json={"display_name": "X", "version": 1},
    )
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "UNAUTHENTICATED"


# ---- H17 — wrong version → 412 VERSION_CONFLICT + DB unchanged ---------


@pytest.mark.integration
async def test_patch_me_with_stale_version_returns_412(client: AsyncClient) -> None:
    """A4: PATCH with a stale ``version`` fence returns 412 with
    ``error.code == "VERSION_CONFLICT"``. A follow-up ``GET`` proves the
    row is untouched (no side effects on the failure branch)."""
    reg = await _register(client)
    access = reg["access_token"]
    headers = {"Authorization": f"Bearer {access}"}
    original_name = reg["user"]["display_name"]

    r = await client.patch(
        "/api/v1/users/me",
        headers=headers,
        json={"display_name": "Would-be New", "version": 99},
    )
    assert r.status_code == 412, r.text
    err = r.json()["error"]
    assert err["code"] == "VERSION_CONFLICT"
    assert err["message"] == "Resource has been modified."

    # Round-trip: DB state after 412 is exactly the pre-PATCH state.
    r_get = await client.get("/api/v1/users/me", headers=headers)
    assert r_get.status_code == 200
    got = r_get.json()["data"]
    assert got["version"] == 1
    assert got["display_name"] == original_name


# ---- H18–H20 — body validation (422 surface) ---------------------------


@pytest.mark.integration
async def test_patch_me_with_extra_field_returns_422(client: AsyncClient) -> None:
    """A6: whitelist enforcement — ``email`` (or any non-``display_name`` /
    non-``version`` key) is rejected at Pydantic validation
    (``extra="forbid"``) with 422 before the handler is entered."""
    reg = await _register(client)
    access = reg["access_token"]

    r = await client.patch(
        "/api/v1/users/me",
        headers={"Authorization": f"Bearer {access}"},
        json={"display_name": "X", "version": 1, "email": "hack@example.com"},
    )
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "VALIDATION_FAILED"


@pytest.mark.integration
async def test_patch_me_with_empty_body_returns_422(client: AsyncClient) -> None:
    """A7: empty body ``{}`` — both required fields (``display_name``,
    ``version``) missing → 422. Fails fast at DTO validation."""
    reg = await _register(client)
    access = reg["access_token"]

    r = await client.patch(
        "/api/v1/users/me",
        headers={"Authorization": f"Bearer {access}"},
        json={},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_FAILED"


@pytest.mark.integration
async def test_patch_me_with_only_version_returns_422(client: AsyncClient) -> None:
    """A7: version present but no mutation fields → 422 (``display_name``
    is required in α4). Same 422 surface as the empty-body case."""
    reg = await _register(client)
    access = reg["access_token"]

    r = await client.patch(
        "/api/v1/users/me",
        headers={"Authorization": f"Bearer {access}"},
        json={"version": 1},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_FAILED"


# ---- H21 — same-value no-op --------------------------------------------


@pytest.mark.integration
async def test_patch_me_same_value_is_a_noop(client: AsyncClient) -> None:
    """A8 + A12: PATCH with ``display_name`` equal to the current value
    returns 200 with an identical representation — ``version`` and
    ``updated_at`` are unchanged. Because the repository short-circuits
    before running any UPDATE, the ``touch_updated_at`` trigger never
    fires, so this is the one branch where equality on ``updated_at`` is
    a legitimate wire-level assertion (unlike the ``> pre-value`` claim
    in H15, which the shared-transaction fixture cannot observe)."""
    reg = await _register(client)
    access = reg["access_token"]
    original_name = reg["user"]["display_name"]
    original_updated_at = reg["user"]["updated_at"]

    r = await client.patch(
        "/api/v1/users/me",
        headers={"Authorization": f"Bearer {access}"},
        json={"display_name": original_name, "version": 1},
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["display_name"] == original_name
    assert data["version"] == 1  # not incremented — no field changed
    assert data["updated_at"] == original_updated_at


# ---- H22 — PATCH → GET round-trip consistency --------------------------


@pytest.mark.integration
async def test_patch_me_response_matches_subsequent_get(client: AsyncClient) -> None:
    """A13: guards against "PATCH returns stale cache while DB has fresh
    data" bugs. The projection helper in the router
    (:func:`~app.api.v1.routers.users._to_public`) is the same for both
    endpoints, so their bodies must be byte-for-byte identical."""
    reg = await _register(client)
    access = reg["access_token"]
    headers = {"Authorization": f"Bearer {access}"}

    patch_r = await client.patch(
        "/api/v1/users/me",
        headers=headers,
        json={"display_name": "Roundtrip", "version": 1},
    )
    assert patch_r.status_code == 200

    get_r = await client.get("/api/v1/users/me", headers=headers)
    assert get_r.status_code == 200
    assert patch_r.json()["data"] == get_r.json()["data"]


# ---- H23 — sequential CAS race (concurrency proxy) ---------------------


@pytest.mark.integration
async def test_patch_me_second_write_with_stale_version_gets_412(client: AsyncClient) -> None:
    """A3: canonical concurrency race — two PATCHes with the same
    expected ``version``, one succeeds and one gets 412.

    Modeled as a sequential race rather than a truly-concurrent one:
    the SAVEPOINT fixture runs everything on one connection so real
    parallelism is impossible. Sequential replay still exercises the
    CAS invariant — the observable outcome (first PATCH succeeds and
    increments the version to 2; second PATCH with ``version=1`` fails
    with 412) is identical to the concurrent case at the wire level.
    True parallel-connection testing is a load-test concern, not a
    per-endpoint integration concern."""
    reg = await _register(client)
    access = reg["access_token"]
    headers = {"Authorization": f"Bearer {access}"}

    r1 = await client.patch(
        "/api/v1/users/me",
        headers=headers,
        json={"display_name": "First", "version": 1},
    )
    assert r1.status_code == 200
    assert r1.json()["data"]["version"] == 2

    r2 = await client.patch(
        "/api/v1/users/me",
        headers=headers,
        json={"display_name": "Second", "version": 1},  # stale — first already bumped to 2
    )
    assert r2.status_code == 412
    assert r2.json()["error"]["code"] == "VERSION_CONFLICT"

    # The winner's write is what remains.
    r_get = await client.get("/api/v1/users/me", headers=headers)
    got = r_get.json()["data"]
    assert got["display_name"] == "First"
    assert got["version"] == 2


# ---- H24 — PATCH after logout ------------------------------------------


@pytest.mark.integration
async def test_patch_me_after_logout_returns_401(client: AsyncClient) -> None:
    """A5: revoked-session enforcement is uniform across read and write
    surfaces — a PATCH with a still-cryptographically-valid access token
    whose session row has been revoked (via ``POST /auth/logout``) is
    rejected at ``get_current_user`` with the same generic 401 as the
    read-side ``session_revoked`` branch in H9."""
    reg = await _register(client)
    access = reg["access_token"]
    headers = {"Authorization": f"Bearer {access}"}

    logout = await client.post("/api/v1/auth/logout", headers=headers)
    assert logout.status_code == 204

    r = await client.patch(
        "/api/v1/users/me",
        headers=headers,
        json={"display_name": "AfterLogout", "version": 1},
    )
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "UNAUTHENTICATED"
