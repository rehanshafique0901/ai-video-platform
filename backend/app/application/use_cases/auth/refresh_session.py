"""``RefreshSession`` use case (Slice α2b).

Contract (API_CONTRACT §3.1):

    POST /auth/refresh
      body: { refresh_token }
      → 200 { user, access_token, refresh_token }
      → 401 { code: "UNAUTHENTICATED", message: "invalid refresh token" }
            for EVERY failure mode (see below)

Every 401 response looks identical to the client. Server-side logs
(``auth.refresh.rejected`` / ``auth.refresh.reuse_detected``) carry
the actual reason so operators + SIEM tooling can distinguish
signature failures, replays, sid mismatches, unknown tokens, and
soft-deleted users — the client cannot. This is anti-enumeration for
the refresh surface (OWASP ASVS L2 §3.5).

Flow:

1.  **Verify JWT** — signature, ``kind == "refresh"``, ``exp`` are
    checked by ``token_issuer.verify_refresh``. Any failure is logged
    with a specific reason then re-raised as
    ``InvalidRefreshTokenError``.

2.  **Hash + lookup** — ``sha256(refresh_jwt)`` → ``sessions.get_by_hash``.
    Missing row = unknown token (401).

3.  **Consistency check (A12)** — ``row.id == claims.session_id``.
    This is defence in depth: given HMAC-signed JWTs, a hash-collision
    on a valid token is astronomically unlikely, but if the check ever
    trips it means either a tampered claim or a bug. Fail loud (log
    with ``security_event=True``), fail closed (401).

4.  **Reuse detection (A5)** — if the looked-up row has
    ``revoked_at != NULL``, someone is replaying a rotated token. Enumerate
    the whole family (``sessions.list_family``) and revoke every
    still-live sibling. Emit ``auth.refresh.reuse_detected`` with
    ``security_event=True`` for SIEM. Client still gets the generic 401.

5.  **User liveness (A13)** — ``users.get_by_id`` filters
    soft-deleted rows (repository contract). A valid-JWT-plus-live-
    session-row-plus-missing-user combination means the account was
    deleted between issuance and this refresh; revoke the session (no
    family nuke — this isn't a compromise signal) and return the same
    401.

6.  **Rotate** — CAS-revoke the old row, mint fresh tokens via
    ``issue_for_rotation`` (preserves ``family_id``, fresh ``sid``),
    insert the new row, commit. If the CAS returns False, a concurrent
    refresh already claimed the token; the loser bails with the same
    401 (see Q9 in the α2b pre-flight — extremely rare in practice).

Structured logs shipped:

    auth.refresh.rejected           warn    reason=…, security_event=<bool>
    auth.refresh.reuse_detected     warn    security_event=True
    auth.refresh.rotated            info    old_sid, new_sid, family_id

``security_event=True`` is set only on events an ops team would want
to alert on: reuse detection, sid mismatch, hash-miss. Signature /
expiry rejections are noisy under normal client behaviour and don't
carry the flag.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from uuid import UUID

import structlog

from app.application.interfaces.clock import IClock
from app.application.interfaces.security import IssuedTokens, ITokenIssuer
from app.application.interfaces.unit_of_work import IUnitOfWork
from app.application.use_cases.auth.errors import InvalidRefreshTokenError
from app.core.errors import UnauthorizedError
from app.domain.identity.session import Session
from app.domain.identity.user import User

_LOGGER = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class RefreshSessionResult:
    user: User
    session: Session
    tokens: IssuedTokens


class RefreshSession:
    """Rotate a refresh token into a fresh (access, refresh) pair."""

    def __init__(
        self,
        uow: IUnitOfWork,
        token_issuer: ITokenIssuer,
        clock: IClock,
    ) -> None:
        self._uow = uow
        self._token_issuer = token_issuer
        self._clock = clock

    async def execute(
        self,
        refresh_token: str,
        ip: str | None = None,
        user_agent: str | None = None,
    ) -> RefreshSessionResult:
        # Step 1: verify the JWT surface (signature / kind / exp).
        try:
            claims = self._token_issuer.verify_refresh(refresh_token)
        except UnauthorizedError as e:
            _LOGGER.warning(
                "auth.refresh.rejected",
                reason="verify_failed",
                detail=str(e),
            )
            raise InvalidRefreshTokenError("invalid refresh token") from e

        h = hashlib.sha256(refresh_token.encode("utf-8")).hexdigest()

        async with self._uow:
            # Step 2: hash lookup.
            row = await self._uow.sessions.get_by_hash(h)
            if row is None:
                _LOGGER.warning(
                    "auth.refresh.rejected",
                    reason="hash_miss",
                    security_event=True,
                    user_id=str(claims.subject),
                    claimed_sid=str(claims.session_id),
                )
                raise InvalidRefreshTokenError("invalid refresh token")

            # Step 3: sid consistency (defence in depth — A12).
            if row.id != claims.session_id:
                _LOGGER.warning(
                    "auth.refresh.rejected",
                    reason="sid_mismatch",
                    security_event=True,
                    loaded_sid=str(row.id),
                    claimed_sid=str(claims.session_id),
                )
                raise InvalidRefreshTokenError("invalid refresh token")

            # Step 4: reuse detection.
            if row.revoked_at is not None:
                await self._revoke_family(row.family_id)
                await self._uow.commit()
                _LOGGER.warning(
                    "auth.refresh.reuse_detected",
                    security_event=True,
                    user_id=str(row.user_id),
                    family_id=str(row.family_id),
                    replayed_sid=str(claims.session_id),
                )
                raise InvalidRefreshTokenError("invalid refresh token")

            # Step 5: user liveness (A13).
            user = await self._uow.users.get_by_id(row.user_id)
            if user is None:
                # Legitimate token but the account is gone. Revoke this
                # session only (not the family — a deleted user is not
                # a compromise signal).
                now = self._clock.now()
                await self._uow.sessions.revoke(row.id, at=now)
                await self._uow.commit()
                _LOGGER.warning(
                    "auth.refresh.rejected",
                    reason="user_gone",
                    user_id=str(row.user_id),
                    session_id=str(row.id),
                )
                raise InvalidRefreshTokenError("invalid refresh token")

            # Step 6: rotate. CAS-revoke first so a concurrent refresh
            # loses cleanly rather than double-issuing.
            now = self._clock.now()
            revoked = await self._uow.sessions.revoke(row.id, at=now)
            if not revoked:
                _LOGGER.warning(
                    "auth.refresh.rejected",
                    reason="cas_race_lost",
                    user_id=str(user.id),
                    session_id=str(row.id),
                )
                raise InvalidRefreshTokenError("invalid refresh token")

            tokens = self._token_issuer.issue_for_rotation(user, family_id=row.family_id)

            new_session = await self._uow.sessions.add(
                Session(
                    id=tokens.session_id,
                    user_id=user.id,
                    family_id=tokens.family_id,
                    token_hash=tokens.refresh_token_hash,
                    ip=ip,
                    user_agent=user_agent,
                    issued_at=tokens.issued_at,
                    last_used_at=tokens.issued_at,
                    expires_at=tokens.refresh_expires_at,
                    revoked_at=None,
                )
            )

            await self._uow.commit()

        _LOGGER.info(
            "auth.refresh.rotated",
            user_id=str(user.id),
            family_id=str(new_session.family_id),
            old_sid=str(row.id),
            new_sid=str(new_session.id),
        )
        return RefreshSessionResult(user=user, session=new_session, tokens=tokens)

    async def _revoke_family(self, family_id: UUID) -> None:
        """Revoke every still-live row in the family. Order unspecified."""
        now = self._clock.now()
        family = await self._uow.sessions.list_family(family_id)
        for member in family:
            if member.revoked_at is None:
                await self._uow.sessions.revoke(member.id, at=now)
