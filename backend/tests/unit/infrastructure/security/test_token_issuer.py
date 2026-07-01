"""Unit tests for ``AuthTokenIssuer`` (Slice α2a)."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from uuid import UUID, uuid4

import jwt
import pytest

from app.core.errors import UnauthorizedError
from app.domain.identity.user import User
from app.infrastructure.security.jwt import JWTService
from app.infrastructure.security.token_issuer import AuthTokenIssuer

_SECRET = "unit-test-secret-do-not-use-in-prod-32chars"


def _make_issuer(access_ttl: int = 900, refresh_ttl: int = 2_592_000) -> AuthTokenIssuer:
    jwt_svc = JWTService(
        secret=_SECRET,
        algorithm="HS256",
        access_ttl_seconds=access_ttl,
        refresh_ttl_seconds=refresh_ttl,
    )
    return AuthTokenIssuer(jwt_service=jwt_svc, refresh_ttl_seconds=refresh_ttl)


def _make_user() -> User:
    return User(
        id=uuid4(),
        tenant_id=uuid4(),
        email="user@example.com",
        password_hash="hash::pw",
        display_name="U",
        email_verified_at=None,
        last_login_at=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        version=1,
    )


@pytest.mark.unit
def test_issue_for_login_returns_populated_bundle() -> None:
    issuer = _make_issuer()
    user = _make_user()

    tokens = issuer.issue_for_login(user)

    assert tokens.access_token
    assert tokens.refresh_token
    assert tokens.access_token != tokens.refresh_token
    # Hash is real SHA-256 of the refresh JWT.
    assert tokens.refresh_token_hash == hashlib.sha256(tokens.refresh_token.encode()).hexdigest()
    assert isinstance(tokens.session_id, UUID)
    assert isinstance(tokens.family_id, UUID)


@pytest.mark.unit
def test_access_token_carries_sid_and_fam_claims() -> None:
    issuer = _make_issuer()
    user = _make_user()

    tokens = issuer.issue_for_login(user)
    payload = jwt.decode(tokens.access_token, _SECRET, algorithms=["HS256"])

    assert payload["kind"] == "access"
    assert payload["sub"] == str(user.id)
    assert payload["sid"] == str(tokens.session_id)
    assert payload["fam"] == str(tokens.family_id)


@pytest.mark.unit
def test_refresh_token_carries_sid_and_fam_claims() -> None:
    issuer = _make_issuer()
    user = _make_user()

    tokens = issuer.issue_for_login(user)
    payload = jwt.decode(tokens.refresh_token, _SECRET, algorithms=["HS256"])

    assert payload["kind"] == "refresh"
    assert payload["sub"] == str(user.id)
    assert payload["sid"] == str(tokens.session_id)
    assert payload["fam"] == str(tokens.family_id)


@pytest.mark.unit
def test_issue_for_rotation_preserves_family_but_mints_fresh_sid() -> None:
    issuer = _make_issuer()
    user = _make_user()

    first = issuer.issue_for_login(user)
    rotated = issuer.issue_for_rotation(user, family_id=first.family_id)

    assert rotated.family_id == first.family_id
    assert rotated.session_id != first.session_id
    assert rotated.refresh_token != first.refresh_token


@pytest.mark.unit
def test_verify_access_returns_typed_claims() -> None:
    issuer = _make_issuer()
    user = _make_user()

    tokens = issuer.issue_for_login(user)
    claims = issuer.verify_access(tokens.access_token)

    assert claims.subject == user.id
    assert claims.session_id == tokens.session_id
    assert claims.family_id == tokens.family_id


@pytest.mark.unit
def test_verify_refresh_returns_typed_claims() -> None:
    issuer = _make_issuer()
    user = _make_user()

    tokens = issuer.issue_for_login(user)
    claims = issuer.verify_refresh(tokens.refresh_token)

    assert claims.subject == user.id
    assert claims.session_id == tokens.session_id
    assert claims.family_id == tokens.family_id


@pytest.mark.unit
def test_verify_rejects_wrong_kind() -> None:
    issuer = _make_issuer()
    user = _make_user()

    tokens = issuer.issue_for_login(user)
    with pytest.raises(UnauthorizedError):
        issuer.verify_access(tokens.refresh_token)
    with pytest.raises(UnauthorizedError):
        issuer.verify_refresh(tokens.access_token)


@pytest.mark.unit
def test_verify_access_allow_expired_accepts_a_stale_token() -> None:
    """α2b LogoutSession contract: an expired but signature-valid
    access token is accepted when ``allow_expired=True``."""
    # ttl = -1 → the token is stale the instant it's minted.
    issuer = _make_issuer(access_ttl=-1)
    user = _make_user()
    tokens = issuer.issue_for_login(user)

    # Strict verification must reject the stale token.
    with pytest.raises(UnauthorizedError):
        issuer.verify_access(tokens.access_token)

    # Relaxed verification (logout path) must accept it and still
    # return typed claims with a real sid.
    claims = issuer.verify_access(tokens.access_token, allow_expired=True)
    assert claims.subject == user.id
    assert claims.session_id == tokens.session_id
    assert claims.family_id == tokens.family_id


@pytest.mark.unit
def test_verify_access_allow_expired_still_rejects_bad_signature() -> None:
    """``allow_expired=True`` relaxes ONLY the ``exp`` check —
    signature and kind must still verify."""
    issuer = _make_issuer(access_ttl=-1)
    user = _make_user()
    tokens = issuer.issue_for_login(user)

    tampered = tokens.access_token + "x"  # corrupt the signature

    with pytest.raises(UnauthorizedError):
        issuer.verify_access(tampered, allow_expired=True)

    # And a refresh token must never be accepted as an access token,
    # even when ``allow_expired`` relaxes ``exp``.
    with pytest.raises(UnauthorizedError):
        issuer.verify_access(tokens.refresh_token, allow_expired=True)


@pytest.mark.unit
def test_verify_rejects_token_missing_sid_claim() -> None:
    """A token minted without the sid/fam claims must be rejected as invalid."""
    # Craft one manually using JWTService without the AuthTokenIssuer's sid/fam.
    jwt_svc = JWTService(
        secret=_SECRET, algorithm="HS256", access_ttl_seconds=900, refresh_ttl_seconds=2_592_000
    )
    bare_access = jwt_svc.issue_access(uuid4())  # no claims dict

    issuer = _make_issuer()
    with pytest.raises(UnauthorizedError):
        issuer.verify_access(bare_access)
