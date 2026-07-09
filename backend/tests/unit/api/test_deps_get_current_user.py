"""Unit tests for ``get_current_user`` (Slice α3.2b).

Rationale — bottom-up testing philosophy (see α2b retro §2.4):

    repository integration → use-case-shaped unit tests → HTTP integration

Slice α3.2 added ``get_current_user`` and its integration tests will
land in the α3.3 HTTP suite (via ``GET /api/v1/users/me``). That leaves
one gap: the dep itself has seven rejection branches, and unit-only
coverage dropped 2.44pp when the dep landed because those branches are
only exercised through the integration route. Pre-flight §3.1
pre-authorised this file as the fallback; the α2b retro §5.4 documented
the trade-off.

These tests call ``get_current_user`` directly as an async function with
a Starlette :class:`~starlette.requests.Request` built from a scope dict,
a :class:`FakeTokenIssuer`, and a :class:`FakeUnitOfWork`. That mirrors
the α2a/α2b use-case unit-test pattern (small, hermetic, no FastAPI
router runtime) and asserts:

* Every rejection branch returns the anti-enumeration generic 401
  (``_GENERIC_401``) — the client cannot distinguish reasons.
* Structured log fires ``auth.request.rejected`` with the specific
  ``reason`` and — where applicable — ``security_event=True``.
* Happy path returns the ``User`` and fires
  ``auth.request.authenticated`` with the identity fields SIEM needs.

``structlog.testing.capture_logs()`` is used as a context manager per
test rather than a fixture that mutates the global structlog config —
that keeps this file hermetic and prevents leaking a bare ``LogCapture``
processor into any test that runs after it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
import structlog
from starlette.requests import Request

from app.api.v1.deps import _GENERIC_401, get_current_user
from app.core import container
from app.core.errors import UnauthorizedError
from app.domain.identity.session import Session
from app.domain.identity.user import User
from tests.unit.application.use_cases.auth._fakes import (
    FakeClock,
    FakeSessionRepository,
    FakeTokenIssuer,
    FakeUnitOfWork,
    FakeUserRepository,
)

# ---- Fixtures / helpers ----------------------------------------------

_FIXED_NOW = datetime(2026, 7, 9, 12, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _pin_container_clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    """Pin ``container.get_clock()`` to a fixed instant.

    ``get_current_user`` reads the clock via the container (matching the
    α2b use-case pattern). Unit tests therefore monkeypatch the container
    binding rather than instantiating one.
    """
    clock = FakeClock(fixed_at=_FIXED_NOW)
    monkeypatch.setattr(container, "get_clock", lambda: clock)
    return clock


def _make_request(*, authorization: str | None = None, ip: str = "203.0.113.7") -> Request:
    """Build a Starlette ``Request`` with only the fields ``get_current_user`` reads."""
    headers: list[tuple[bytes, bytes]] = [(b"user-agent", b"pytest-a3.2b/1.0")]
    if authorization is not None:
        headers.append((b"authorization", authorization.encode()))
    scope: dict[str, Any] = {
        "type": "http",
        "headers": headers,
        "client": (ip, 41000),
        "method": "GET",
        "path": "/",
    }
    return Request(scope)


def _make_user() -> User:
    now = datetime.now(UTC)
    return User(
        id=uuid4(),
        tenant_id=uuid4(),
        email="alice@example.com",
        password_hash="hash::pw",
        display_name="Alice",
        email_verified_at=None,
        last_login_at=None,
        created_at=now,
        updated_at=now,
        version=1,
    )


def _make_session(
    *,
    user_id: Any,
    revoked_at: datetime | None = None,
    expires_at: datetime | None = None,
) -> Session:
    return Session(
        id=uuid4(),
        user_id=user_id,
        family_id=uuid4(),
        token_hash="hash-does-not-matter-for-sid-lookup",
        ip=None,
        user_agent=None,
        issued_at=_FIXED_NOW - timedelta(minutes=5),
        last_used_at=_FIXED_NOW - timedelta(minutes=1),
        expires_at=expires_at or (_FIXED_NOW + timedelta(days=30)),
        revoked_at=revoked_at,
    )


def _pin_sid(row: Session, sid: Any) -> Session:
    """Return ``row`` with its ``id`` replaced by ``sid`` (rows are frozen)."""
    return Session(
        id=sid,
        user_id=row.user_id,
        family_id=row.family_id,
        token_hash=row.token_hash,
        ip=row.ip,
        user_agent=row.user_agent,
        issued_at=row.issued_at,
        last_used_at=row.last_used_at,
        expires_at=row.expires_at,
        revoked_at=row.revoked_at,
    )


def _seed_live(
    *,
    session_row: Session | None = None,
    user_in_repo: bool = True,
) -> tuple[User, Session, FakeTokenIssuer, FakeUnitOfWork, str]:
    """Wire up a fake UoW around one user, one issued token, and one session.

    Returns ``(user, session, issuer, uow, access_token)``. The session's
    id is pinned to the token's sid claim so the repo lookup round-trips.
    Toggle ``user_in_repo=False`` to model the ``sid_user_gone`` branch
    (session row remains but user is absent). Pass a custom ``session_row``
    to model ``session_revoked`` / ``session_expired``.
    """
    user = _make_user()
    users = FakeUserRepository()
    if user_in_repo:
        users._rows[user.id] = user
        users._by_email[user.email] = user.id

    issuer = FakeTokenIssuer()
    tokens = issuer.issue_for_login(user)

    base = session_row or _make_session(user_id=user.id)
    pinned = _pin_sid(base, tokens.session_id)
    sessions = FakeSessionRepository()
    sessions._rows[pinned.id] = pinned

    uow = FakeUnitOfWork(users=users, sessions=sessions)
    return user, pinned, issuer, uow, tokens.access_token


# ---- Happy path -------------------------------------------------------


@pytest.mark.unit
async def test_happy_path_returns_user_and_logs_authenticated() -> None:
    user, session, issuer, uow, access = _seed_live()
    request = _make_request(authorization=f"Bearer {access}")

    with structlog.testing.capture_logs() as entries:
        result = await get_current_user(request, issuer, uow)

    assert result.id == user.id
    assert any(
        e["event"] == "auth.request.authenticated"
        and e.get("user_id") == str(user.id)
        and e.get("session_id") == str(session.id)
        for e in entries
    )


# ---- Rejection branches ----------------------------------------------


@pytest.mark.unit
async def test_missing_header_rejects_with_generic_401() -> None:
    _, _, issuer, uow, _ = _seed_live()
    request = _make_request(authorization=None)

    with structlog.testing.capture_logs() as entries, pytest.raises(UnauthorizedError) as exc:
        await get_current_user(request, issuer, uow)

    assert str(exc.value) == _GENERIC_401
    (event,) = (e for e in entries if e["event"] == "auth.request.rejected")
    assert event["reason"] == "missing_header"
    assert event["security_event"] is False


@pytest.mark.unit
@pytest.mark.parametrize(
    "authorization",
    [
        "Basic dXNlcjpwYXNz",  # wrong scheme
        "Bearer ",  # empty token
        "Bearer",  # no space, no token
    ],
)
async def test_malformed_header_rejects_with_generic_401(authorization: str) -> None:
    _, _, issuer, uow, _ = _seed_live()
    request = _make_request(authorization=authorization)

    with structlog.testing.capture_logs() as entries, pytest.raises(UnauthorizedError) as exc:
        await get_current_user(request, issuer, uow)

    assert str(exc.value) == _GENERIC_401
    (event,) = (e for e in entries if e["event"] == "auth.request.rejected")
    assert event["reason"] == "malformed_header"
    assert event["security_event"] is False


@pytest.mark.unit
async def test_verify_failed_flags_security_event() -> None:
    _, _, issuer, uow, _ = _seed_live()
    # FakeTokenIssuer.verify raises UnauthorizedError for unknown tokens.
    request = _make_request(authorization="Bearer abc.def.tampered-signature")

    with structlog.testing.capture_logs() as entries, pytest.raises(UnauthorizedError) as exc:
        await get_current_user(request, issuer, uow)

    assert str(exc.value) == _GENERIC_401
    (event,) = (e for e in entries if e["event"] == "auth.request.rejected")
    assert event["reason"] == "verify_failed"
    assert event["security_event"] is True


@pytest.mark.unit
async def test_sid_missing_session_flags_security_event() -> None:
    _, _, issuer, uow, access = _seed_live()
    # Wipe the session row so the token's sid claim finds nothing.
    uow._fake_sessions._rows.clear()
    request = _make_request(authorization=f"Bearer {access}")

    with structlog.testing.capture_logs() as entries, pytest.raises(UnauthorizedError) as exc:
        await get_current_user(request, issuer, uow)

    assert str(exc.value) == _GENERIC_401
    (event,) = (e for e in entries if e["event"] == "auth.request.rejected")
    assert event["reason"] == "sid_missing_session"
    assert event["security_event"] is True


@pytest.mark.unit
async def test_session_revoked_rejects_without_security_flag() -> None:
    revoked_row = _make_session(
        user_id=uuid4(),
        revoked_at=_FIXED_NOW - timedelta(minutes=1),
    )
    _, _, issuer, uow, access = _seed_live(session_row=revoked_row)
    request = _make_request(authorization=f"Bearer {access}")

    with structlog.testing.capture_logs() as entries, pytest.raises(UnauthorizedError) as exc:
        await get_current_user(request, issuer, uow)

    assert str(exc.value) == _GENERIC_401
    (event,) = (e for e in entries if e["event"] == "auth.request.rejected")
    assert event["reason"] == "session_revoked"
    assert event["security_event"] is False


@pytest.mark.unit
async def test_session_expired_rejects_without_security_flag() -> None:
    expired_row = _make_session(
        user_id=uuid4(),
        expires_at=_FIXED_NOW - timedelta(seconds=1),
    )
    _, _, issuer, uow, access = _seed_live(session_row=expired_row)
    request = _make_request(authorization=f"Bearer {access}")

    with structlog.testing.capture_logs() as entries, pytest.raises(UnauthorizedError) as exc:
        await get_current_user(request, issuer, uow)

    assert str(exc.value) == _GENERIC_401
    (event,) = (e for e in entries if e["event"] == "auth.request.rejected")
    assert event["reason"] == "session_expired"
    assert event["security_event"] is False


@pytest.mark.unit
async def test_sid_user_gone_rejects_without_security_flag() -> None:
    # Session row exists and is live — but the user isn't in the repo.
    _, _, issuer, uow, access = _seed_live(user_in_repo=False)
    request = _make_request(authorization=f"Bearer {access}")

    with structlog.testing.capture_logs() as entries, pytest.raises(UnauthorizedError) as exc:
        await get_current_user(request, issuer, uow)

    assert str(exc.value) == _GENERIC_401
    (event,) = (e for e in entries if e["event"] == "auth.request.rejected")
    assert event["reason"] == "sid_user_gone"
    assert event["security_event"] is False


# ---- Anti-enumeration invariant --------------------------------------


@pytest.mark.unit
async def test_all_rejection_reason_ids_are_present_in_source() -> None:
    """Regression guard for the α3 anti-enumeration reason-id surface.

    Every rejection surface must emit one of the seven reason ids the
    pre-flight §2.D4 audit expects. A rename here (without updating this
    test) is a review-blocker signal that SIEM alerting depends on.
    """
    expected_reasons = {
        "missing_header",
        "malformed_header",
        "verify_failed",
        "sid_missing_session",
        "session_revoked",
        "session_expired",
        "sid_user_gone",
    }
    from app.api.v1 import deps as _deps

    with open(_deps.__file__, encoding="utf-8") as fh:
        text = fh.read()
    for reason in expected_reasons:
        assert f'"{reason}"' in text, f"reason id {reason!r} missing from deps.py"
