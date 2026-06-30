"""Unit tests for ``app.infrastructure.security.jwt.JWTService``."""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.core.errors import UnauthorizedError
from app.infrastructure.security.jwt import JWTService

_SECRET = "test-secret-do-not-use-in-production-32chars"


@pytest.fixture
def jwt_service() -> JWTService:
    return JWTService(
        secret=_SECRET,
        algorithm="HS256",
        access_ttl_seconds=900,
        refresh_ttl_seconds=2_592_000,
    )


@pytest.mark.unit
def test_issue_and_verify_access_token(jwt_service: JWTService) -> None:
    user_id = uuid4()
    token = jwt_service.issue_access(user_id)
    payload = jwt_service.verify(token, "access")
    assert payload["sub"] == str(user_id)
    assert payload["kind"] == "access"


@pytest.mark.unit
def test_issue_and_verify_refresh_token(jwt_service: JWTService) -> None:
    user_id = uuid4()
    family_id = uuid4()
    token = jwt_service.issue_refresh(user_id, family_id)
    payload = jwt_service.verify(token, "refresh")
    assert payload["sub"] == str(user_id)
    assert payload["fam"] == str(family_id)
    assert payload["kind"] == "refresh"


@pytest.mark.unit
def test_verify_rejects_wrong_kind(jwt_service: JWTService) -> None:
    token = jwt_service.issue_access(uuid4())
    with pytest.raises(UnauthorizedError, match="expected refresh"):
        jwt_service.verify(token, "refresh")


@pytest.mark.unit
def test_verify_rejects_invalid_token(jwt_service: JWTService) -> None:
    with pytest.raises(UnauthorizedError, match="invalid token"):
        jwt_service.verify("not.a.jwt", "access")


@pytest.mark.unit
def test_verify_rejects_wrong_signature() -> None:
    issuer = JWTService(
        secret=_SECRET, algorithm="HS256", access_ttl_seconds=900, refresh_ttl_seconds=900
    )
    token = issuer.issue_access(uuid4())
    verifier = JWTService(
        secret="different-secret-also-32-characters-long",
        algorithm="HS256",
        access_ttl_seconds=900,
        refresh_ttl_seconds=900,
    )
    with pytest.raises(UnauthorizedError, match="invalid token"):
        verifier.verify(token, "access")


@pytest.mark.unit
def test_verify_rejects_expired_token() -> None:
    service = JWTService(
        secret=_SECRET,
        algorithm="HS256",
        access_ttl_seconds=-1,  # already expired
        refresh_ttl_seconds=900,
    )
    token = service.issue_access(uuid4())
    with pytest.raises(UnauthorizedError, match="expired"):
        service.verify(token, "access")


@pytest.mark.unit
def test_custom_claims_are_preserved(jwt_service: JWTService) -> None:
    user_id = uuid4()
    token = jwt_service.issue_access(user_id, claims={"role": "admin"})
    payload = jwt_service.verify(token, "access")
    assert payload["role"] == "admin"
