"""Unit tests for ``LogoutSession`` (Slice α2b.2).

Covers the full happy path + every documented failure mode. Refresh
reuse detection is exercised through ``test_refresh_session`` — logout
is deliberately simpler and doesn't touch the family.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.application.use_cases.auth.errors import InvalidRefreshTokenError
from app.application.use_cases.auth.logout_session import LogoutSession
from app.domain.identity.session import Session
from app.domain.identity.user import User

from ._fakes import (
    FakeClock,
    FakeSessionRepository,
    FakeTokenIssuer,
    FakeUnitOfWork,
    FakeUserRepository,
)

_T0 = datetime(2026, 1, 1, tzinfo=UTC)
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _make_user() -> User:
    return User(
        id=uuid4(),
        tenant_id=uuid4(),
        email="u@example.com",
        password_hash="hash::pw",
        display_name="U",
        email_verified_at=None,
        last_login_at=None,
        created_at=_T0,
        updated_at=_T0,
        version=1,
    )


def _seed_session(user: User, issuer: FakeTokenIssuer) -> tuple[Session, str, FakeUnitOfWork]:
    """Mint a login token pair, seed a matching Session row, return everything."""
    tokens = issuer.issue_for_login(user)
    sessions = FakeSessionRepository()
    users = FakeUserRepository()
    uow = FakeUnitOfWork(users=users, sessions=sessions)
    row = Session(
        id=tokens.session_id,
        user_id=user.id,
        family_id=tokens.family_id,
        token_hash=tokens.refresh_token_hash,
        ip=None,
        user_agent=None,
        issued_at=tokens.issued_at,
        last_used_at=tokens.issued_at,
        expires_at=tokens.refresh_expires_at,
        revoked_at=None,
    )
    sessions._rows[row.id] = row
    return row, tokens.access_token, uow


def _make_use_case(uow: FakeUnitOfWork, issuer: FakeTokenIssuer, clock: FakeClock) -> LogoutSession:
    return LogoutSession(uow=uow, token_issuer=issuer, clock=clock)


# ---- happy path ------------------------------------------------------


@pytest.mark.unit
async def test_happy_path_revokes_the_session_row() -> None:
    issuer = FakeTokenIssuer()
    user = _make_user()
    row, access, uow = _seed_session(user, issuer)
    clock = FakeClock(fixed_at=_T0 + timedelta(minutes=5))

    await _make_use_case(uow, issuer, clock).execute(access)

    stored = uow._fake_sessions._rows[row.id]
    assert stored.revoked_at == _T0 + timedelta(minutes=5)
    assert uow.commits == 1


@pytest.mark.unit
async def test_happy_path_uses_injected_clock_for_revoked_at() -> None:
    issuer = FakeTokenIssuer()
    user = _make_user()
    row, access, uow = _seed_session(user, issuer)
    fixed = datetime(2027, 6, 15, 12, 30, tzinfo=UTC)
    clock = FakeClock(fixed_at=fixed)

    await _make_use_case(uow, issuer, clock).execute(access)

    assert uow._fake_sessions._rows[row.id].revoked_at == fixed


# ---- idempotency -----------------------------------------------------


@pytest.mark.unit
async def test_second_logout_is_a_204_noop_and_preserves_original_timestamp() -> None:
    """CAS semantics: only the first revoke wins; second is a silent no-op."""
    issuer = FakeTokenIssuer()
    user = _make_user()
    row, access, uow = _seed_session(user, issuer)

    first_clock = FakeClock(fixed_at=_T0 + timedelta(minutes=5))
    second_clock = FakeClock(fixed_at=_T0 + timedelta(hours=2))

    await _make_use_case(uow, issuer, first_clock).execute(access)
    original_ts = uow._fake_sessions._rows[row.id].revoked_at

    # Second call must NOT raise and must NOT overwrite the timestamp.
    await _make_use_case(uow, issuer, second_clock).execute(access)

    assert uow._fake_sessions._rows[row.id].revoked_at == original_ts


@pytest.mark.unit
async def test_logout_with_unknown_sid_returns_204_noop() -> None:
    """A valid-signature token whose sid isn't in the DB is silently accepted."""
    issuer = FakeTokenIssuer()
    user = _make_user()
    # Mint a token but DON'T seed the session row.
    tokens = issuer.issue_for_login(user)
    uow = FakeUnitOfWork()
    clock = FakeClock(fixed_at=_T0)

    # Must not raise.
    await _make_use_case(uow, issuer, clock).execute(tokens.access_token)
    # CAS attempt happened but returned False; no rows created.
    assert uow._fake_sessions.revoke_calls == [tokens.session_id]


# ---- verify failures → 401 -------------------------------------------


@pytest.mark.unit
async def test_unknown_token_raises_invalid_refresh_token_error() -> None:
    """Bad signature / tampered token → FakeTokenIssuer raises → wrapped as InvalidRefreshTokenError."""
    issuer = FakeTokenIssuer()
    user = _make_user()
    _seed_session(user, issuer)
    uow = FakeUnitOfWork()
    clock = FakeClock(fixed_at=_T0)

    with pytest.raises(InvalidRefreshTokenError):
        await _make_use_case(uow, issuer, clock).execute("garbage-token-not-registered")


@pytest.mark.unit
async def test_logout_error_message_is_generic() -> None:
    """Client-facing message must never leak the specific verify failure."""
    issuer = FakeTokenIssuer()
    uow = FakeUnitOfWork()
    clock = FakeClock(fixed_at=_T0)

    with pytest.raises(InvalidRefreshTokenError) as exc:
        await _make_use_case(uow, issuer, clock).execute("garbage")

    assert str(exc.value) == "invalid token"


# ---- structured logging ----------------------------------------------


@pytest.mark.unit
async def test_verify_failure_emits_auth_logout_rejected_log(
    capsys: pytest.CaptureFixture[str],
) -> None:
    issuer = FakeTokenIssuer()
    uow = FakeUnitOfWork()
    clock = FakeClock(fixed_at=_T0)

    with pytest.raises(InvalidRefreshTokenError):
        await _make_use_case(uow, issuer, clock).execute("garbage")

    combined = capsys.readouterr()
    output = _ANSI.sub("", combined.err + combined.out)
    assert "auth.logout.rejected" in output
    assert "verify_failed" in output


# ---- misc ------------------------------------------------------------


@pytest.mark.unit
async def test_logout_does_not_touch_the_user_row() -> None:
    """Logout revokes the session; the user account remains fully live."""
    issuer = FakeTokenIssuer()
    user = _make_user()
    _row, access, uow = _seed_session(user, issuer)
    uow._fake_users._rows[user.id] = user
    uow._fake_users._by_email[user.email] = user.id
    clock = FakeClock(fixed_at=_T0)

    await _make_use_case(uow, issuer, clock).execute(access)

    # User row untouched.
    assert uow._fake_users._rows[user.id] is user
