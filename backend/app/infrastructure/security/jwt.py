"""JWT issue + verify (ADR-0008).

Thin wrapper around ``PyJWT``. Slice α1 ships the service
unit-tested but unused; slice α2 wires it into the login + refresh
use cases.

Tokens carry a ``kind`` claim (``access`` or ``refresh``) so a
refresh token can never be accepted where an access token is
expected, and vice versa. Refresh tokens additionally carry a
``fam`` (family id) claim that backs the rotation chain on the
``sessions`` table per ADR-0008.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

import jwt

from app.core.errors import UnauthorizedError

TokenKind = Literal["access", "refresh"]


class JWTService:
    """Encode and decode signed JWTs."""

    def __init__(
        self,
        secret: str,
        algorithm: str,
        access_ttl_seconds: int,
        refresh_ttl_seconds: int,
    ) -> None:
        self._secret = secret
        self._algorithm = algorithm
        self._access_ttl = access_ttl_seconds
        self._refresh_ttl = refresh_ttl_seconds

    def issue_access(self, subject: UUID, claims: dict[str, Any] | None = None) -> str:
        return self._encode("access", subject, claims, self._access_ttl)

    def issue_refresh(
        self,
        subject: UUID,
        family_id: UUID,
        claims: dict[str, Any] | None = None,
    ) -> str:
        merged: dict[str, Any] = {**(claims or {}), "fam": str(family_id)}
        return self._encode("refresh", subject, merged, self._refresh_ttl)

    def verify(
        self,
        token: str,
        expected_kind: TokenKind,
        *,
        allow_expired: bool = False,
    ) -> dict[str, Any]:
        """Decode + validate a JWT.

        ``allow_expired`` (α2b): when True, PyJWT skips the ``exp``
        check. Signature + ``kind`` are still enforced. Used exclusively
        by ``LogoutSession`` so a user with an already-expired access
        token can still terminate their session (see
        ``AUTH_TOKEN_LIFECYCLE.md`` §Logout for rationale).
        """
        try:
            payload: dict[str, Any] = jwt.decode(
                token,
                self._secret,
                algorithms=[self._algorithm],
                options={"verify_exp": not allow_expired},
            )
        except jwt.ExpiredSignatureError as e:
            raise UnauthorizedError("token expired") from e
        except jwt.InvalidTokenError as e:
            raise UnauthorizedError("invalid token") from e
        if payload.get("kind") != expected_kind:
            raise UnauthorizedError(f"expected {expected_kind} token, got {payload.get('kind')!r}")
        return payload

    def _encode(
        self,
        kind: TokenKind,
        subject: UUID,
        claims: dict[str, Any] | None,
        ttl_seconds: int,
    ) -> str:
        now = datetime.now(UTC)
        payload: dict[str, Any] = {
            "sub": str(subject),
            "iat": now,
            "exp": now + timedelta(seconds=ttl_seconds),
            "kind": kind,
            **(claims or {}),
        }
        return jwt.encode(payload, self._secret, algorithm=self._algorithm)
