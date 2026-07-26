"""Unit tests for the signed, stateless OAuth state signer (α8.6a, OQ3)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import jwt
import pytest

from app.application.interfaces.clock import IClock
from app.application.interfaces.oauth_state_signer import (
    ConnectionState,
    InvalidConnectionStateError,
)
from app.infrastructure.publishing.state_token_signer import JwtOAuthStateSigner

_SECRET = "unit-test-signing-secret-at-least-32-chars"


class _FixedClock(IClock):
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


def _signer(clock: IClock, ttl: int = 600) -> JwtOAuthStateSigner:
    return JwtOAuthStateSigner(secret=_SECRET, algorithm="HS256", ttl_seconds=ttl, clock=clock)


def test_sign_then_verify_round_trip() -> None:
    signer = _signer(_FixedClock(datetime(2026, 7, 26, tzinfo=UTC)))
    state = ConnectionState(user_id=uuid4(), tenant_id=uuid4(), platform="mock")
    recovered = signer.verify(signer.sign(state))
    assert recovered == state


def test_expired_token_is_rejected() -> None:
    issued = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
    token = _signer(_FixedClock(issued), ttl=60).sign(
        ConnectionState(user_id=uuid4(), tenant_id=uuid4(), platform="mock")
    )
    later = _signer(_FixedClock(issued + timedelta(minutes=5)))
    with pytest.raises(InvalidConnectionStateError):
        later.verify(token)


def test_tampered_token_is_rejected() -> None:
    signer = _signer(_FixedClock(datetime(2026, 7, 26, tzinfo=UTC)))
    token = signer.sign(ConnectionState(user_id=uuid4(), tenant_id=uuid4(), platform="mock"))
    with pytest.raises(InvalidConnectionStateError):
        signer.verify(token + "x")


def test_wrong_secret_is_rejected() -> None:
    clock = _FixedClock(datetime(2026, 7, 26, tzinfo=UTC))
    token = _signer(clock).sign(
        ConnectionState(user_id=uuid4(), tenant_id=uuid4(), platform="mock")
    )
    other = JwtOAuthStateSigner(
        secret="a-completely-different-secret-32bytes!!",
        algorithm="HS256",
        ttl_seconds=600,
        clock=clock,
    )
    with pytest.raises(InvalidConnectionStateError):
        other.verify(token)


def test_token_of_wrong_kind_is_rejected() -> None:
    # A validly-signed JWT that is not an oauth_state token must not be accepted.
    now = datetime(2026, 7, 26, tzinfo=UTC)
    forged = jwt.encode(
        {
            "kind": "access",
            "sub": str(uuid4()),
            "tid": str(uuid4()),
            "plat": "mock",
            "exp": int((now + timedelta(minutes=5)).timestamp()),
        },
        _SECRET,
        algorithm="HS256",
    )
    with pytest.raises(InvalidConnectionStateError):
        _signer(_FixedClock(now)).verify(forged)
