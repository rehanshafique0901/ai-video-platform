"""``JwtOAuthStateSigner`` — signed, stateless OAuth ``state`` tokens (α8.6a, OQ3).

A short-lived JWT carrying the acting principal + platform across the provider redirect.
CSRF-safe (the token is signed and single-purpose via a ``kind`` claim) and stateless (no
persistence table). ``verify`` is fail-closed: expiry, signature failure, wrong ``kind``,
or malformed claims all raise :class:`InvalidConnectionStateError`.

The signing key is injected at the composition root (configuration-blind adapter); the
adapter never reads settings itself.
"""

from __future__ import annotations

import secrets
from uuid import UUID

import jwt

from app.application.interfaces.clock import IClock
from app.application.interfaces.oauth_state_signer import (
    ConnectionState,
    InvalidConnectionStateError,
    IOAuthStateSigner,
)

_KIND = "oauth_state"


class JwtOAuthStateSigner(IOAuthStateSigner):
    """Sign/verify OAuth state as a short-lived HS256 JWT."""

    def __init__(
        self,
        *,
        secret: str,
        algorithm: str,
        ttl_seconds: int,
        clock: IClock,
    ) -> None:
        self._secret = secret
        self._algorithm = algorithm
        self._ttl_seconds = ttl_seconds
        self._clock = clock

    def sign(self, state: ConnectionState) -> str:
        now = self._clock.now()
        payload = {
            "kind": _KIND,
            "sub": str(state.user_id),
            "tid": str(state.tenant_id),
            "plat": state.platform,
            "iat": int(now.timestamp()),
            "exp": int(now.timestamp()) + self._ttl_seconds,
            "jti": secrets.token_urlsafe(16),
        }
        return jwt.encode(payload, self._secret, algorithm=self._algorithm)

    def verify(self, token: str) -> ConnectionState:
        # Signature is verified by PyJWT; expiry is checked against the injected clock so the
        # CSRF window is deterministic and unit-testable (the platform's IClock convention).
        try:
            payload = jwt.decode(
                token, self._secret, algorithms=[self._algorithm], options={"verify_exp": False}
            )
        except jwt.PyJWTError as e:
            raise InvalidConnectionStateError("invalid connection state") from e
        if payload.get("kind") != _KIND:
            raise InvalidConnectionStateError("state token is not a connection state")
        try:
            exp = int(payload["exp"])
            state = ConnectionState(
                user_id=UUID(payload["sub"]),
                tenant_id=UUID(payload["tid"]),
                platform=payload["plat"],
            )
        except (KeyError, ValueError) as e:
            raise InvalidConnectionStateError("malformed connection state claims") from e
        if exp <= int(self._clock.now().timestamp()):
            raise InvalidConnectionStateError("connection state has expired")
        return state


__all__ = ["JwtOAuthStateSigner"]
