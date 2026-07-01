"""``AuthTokenIssuer`` — composes ``JWTService`` + SHA-256 + id minting.

Slice α2a-first. Implements the ``ITokenIssuer`` port. Given a domain
``User``, produces an ``IssuedTokens`` bundle containing:

* signed access + refresh JWTs, both carrying ``sid`` (session id) +
  ``fam`` (family id) claims,
* the SHA-256 hash of the refresh token (what gets persisted to the
  ``sessions.token_hash`` column — the raw JWT is never stored),
* the ``session_id`` + ``family_id`` (returned so the caller can
  build the corresponding ``sessions`` row),
* the ``issued_at`` timestamp and the absolute ``refresh_expires_at``
  (persisted to ``sessions.expires_at``).

Why ``session_id`` is generated up-front, not database-assigned:
because it is embedded in the ``sid`` claim of the JWT, which is
signed before the row is inserted. If it were DB-assigned we would
have to issue a placeholder, then rewrite the JWT, doubling
signature work.

The α1 ``JWTService`` is preserved unchanged. This class is a thin
adapter that adds ``sid`` to both tokens and computes the hash — its
existence keeps ``JWTService`` a general-purpose primitive that other
future callers (e.g. one-off email-verify tokens in α3) can reuse.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from app.application.interfaces.security import (
    IssuedTokens,
    ITokenIssuer,
    TokenClaims,
)
from app.core.errors import UnauthorizedError
from app.domain.identity.user import User
from app.infrastructure.security.jwt import JWTService


class AuthTokenIssuer(ITokenIssuer):
    """Composes ``JWTService`` + SHA-256 + id minting into one operation."""

    def __init__(
        self,
        jwt_service: JWTService,
        refresh_ttl_seconds: int,
    ) -> None:
        self._jwt = jwt_service
        # Access TTL is owned by JWTService internally; the issuer
        # only needs the refresh TTL to compute the ``sessions.expires_at``
        # value it returns to the caller.
        self._refresh_ttl_seconds = refresh_ttl_seconds

    def issue_for_login(self, user: User) -> IssuedTokens:
        return self._issue(user, family_id=uuid4())

    def issue_for_rotation(self, user: User, family_id: UUID) -> IssuedTokens:
        return self._issue(user, family_id=family_id)

    def verify_access(self, token: str, *, allow_expired: bool = False) -> TokenClaims:
        payload = self._jwt.verify(token, "access", allow_expired=allow_expired)
        return _payload_to_claims(payload)

    def verify_refresh(self, token: str) -> TokenClaims:
        payload = self._jwt.verify(token, "refresh")
        return _payload_to_claims(payload)

    # ------------------------------------------------------------------

    def _issue(self, user: User, family_id: UUID) -> IssuedTokens:
        session_id = uuid4()
        now = datetime.now(UTC)
        # ``sid`` on both tokens: access-side lets ``LogoutSession``
        # revoke the exact session row without a DB round-trip;
        # refresh-side lets ``RefreshSession`` locate the row directly.
        # ``fam`` on both: reuse-detection (α2b) can revoke a whole
        # family from either token type.
        common_claims: dict[str, Any] = {
            "sid": str(session_id),
            "fam": str(family_id),
        }
        access_token = self._jwt.issue_access(user.id, claims=common_claims)
        # JWTService.issue_refresh already sets ``fam`` internally, but
        # we also want ``sid`` on the refresh token, so we pass the
        # additional claim here.
        refresh_token = self._jwt.issue_refresh(
            user.id,
            family_id=family_id,
            claims={"sid": str(session_id)},
        )
        refresh_token_hash = hashlib.sha256(refresh_token.encode("utf-8")).hexdigest()
        return IssuedTokens(
            access_token=access_token,
            refresh_token=refresh_token,
            refresh_token_hash=refresh_token_hash,
            session_id=session_id,
            family_id=family_id,
            issued_at=now,
            refresh_expires_at=now + timedelta(seconds=self._refresh_ttl_seconds),
        )


def _payload_to_claims(payload: dict[str, Any]) -> TokenClaims:
    """Extract ``sub`` / ``sid`` / ``fam`` / ``exp`` from a verified payload.

    Any missing / malformed claim is treated as an invalid token —
    the α1 ``JWTService.verify`` already checked signature + kind + exp,
    but a token minted by a legacy client (or manually crafted) could
    still lack ``sid`` / ``fam``. Anti-tamper: fail closed.
    """
    try:
        return TokenClaims(
            subject=UUID(payload["sub"]),
            session_id=UUID(payload["sid"]),
            family_id=UUID(payload["fam"]),
            expires_at=datetime.fromtimestamp(int(payload["exp"]), UTC),
        )
    except (KeyError, ValueError, TypeError) as e:
        raise UnauthorizedError("invalid token") from e
