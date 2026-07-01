"""``LogoutSession`` use case (Slice α2b).

**Design choice worth calling out.** This use case accepts *expired*
access tokens as long as their signature, ``kind == 'access'``, and
``sid`` claim are still valid. This is deliberate: without it, a user
whose access token has already expired would have to refresh before
they could log out — which defeats the purpose of "log me out now,
I'm done." The relaxation applies **only** to ``exp``; every other
verification (signature, kind, presence of ``sid``) remains strict.
See ``docs/engineering/AUTH_TOKEN_LIFECYCLE.md`` §Logout for the
full rationale and threat-model analysis.

Contract (API_CONTRACT §3.1):

    POST /auth/logout
      headers: Authorization: Bearer <access_token>
      → 204 No Content         (session was revoked OR was already
                                 revoked OR the sid no longer exists;
                                 the endpoint is idempotent by design)
      → 401 UNAUTHENTICATED    (signature invalid / wrong kind /
                                 missing sid claim / malformed token)

Idempotency rules:

* Second logout of the same ``sid`` returns 204 no-op. The database
  row's ``revoked_at`` is **not** updated on the second call — the
  original logout timestamp stays authoritative for audit. Enforced
  by the ``ISessionRepository.revoke`` compare-and-swap semantics
  established in α2b.1.
* Logout with a valid-signature access token whose ``sid`` no longer
  exists (e.g. hard-deleted for GDPR, or DB restore predates the
  session) also returns 204. From the client's perspective the
  session is effectively logged out either way; surfacing a 4xx here
  would expose the DB state without giving the client any actionable
  information.

Structured logs shipped:

    auth.logout.succeeded    info   user_id, session_id
    auth.logout.rejected     warn   reason=verify_failed, detail
"""

from __future__ import annotations

import structlog

from app.application.interfaces.clock import IClock
from app.application.interfaces.security import ITokenIssuer
from app.application.interfaces.unit_of_work import IUnitOfWork
from app.application.use_cases.auth.errors import InvalidRefreshTokenError
from app.core.errors import UnauthorizedError

_LOGGER = structlog.get_logger(__name__)


class LogoutSession:
    """Revoke the session identified by an access token's ``sid`` claim."""

    def __init__(
        self,
        uow: IUnitOfWork,
        token_issuer: ITokenIssuer,
        clock: IClock,
    ) -> None:
        self._uow = uow
        self._token_issuer = token_issuer
        self._clock = clock

    async def execute(self, access_token: str) -> None:
        # ``allow_expired=True``: an expired access token is still an
        # acceptable logout credential. Signature + kind + sid must
        # still verify.
        try:
            claims = self._token_issuer.verify_access(access_token, allow_expired=True)
        except UnauthorizedError as e:
            _LOGGER.warning(
                "auth.logout.rejected",
                reason="verify_failed",
                detail=str(e),
            )
            # Reuse the α2b refresh-error type for consistent client-facing
            # envelopes on every non-happy auth path. The message is
            # deliberately generic; the server-side log carries the
            # specific verify failure reason.
            raise InvalidRefreshTokenError("invalid token") from e

        now = self._clock.now()
        async with self._uow:
            revoked = await self._uow.sessions.revoke(claims.session_id, at=now)
            await self._uow.commit()

        # Whether ``revoked`` was True (first-time logout) or False
        # (already revoked / unknown sid), the caller gets a 204 —
        # both cases satisfy the "log me out" intent. The distinction
        # is only meaningful for structured-log observability.
        _LOGGER.info(
            "auth.logout.succeeded",
            user_id=str(claims.subject),
            session_id=str(claims.session_id),
            was_already_revoked=not revoked,
        )
