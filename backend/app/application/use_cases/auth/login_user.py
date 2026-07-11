"""``LoginUser`` use case (Slice α2a).

Contract (API_CONTRACT §3.1):

    POST /auth/login
      body: { email, password }
      → 200 { user, access_token, refresh_token }

Every successful login creates a **new** ``sessions`` row (new
``family_id`` + new ``session_id``) so distinct devices carry
distinct rotation families. Rotation and revocation are α2b concerns.

Anti-enumeration (approved improvement C, OWASP ASVS L2 §2.6.3): the
unknown-email path burns one ``hasher.verify`` call against a
constant dummy hash so its response time matches the wrong-password
path (Argon2id is ~300 ms per verify — unmatched, it would be a
timing side-channel that lets an attacker enumerate accounts). The
dummy hash is supplied by the DI container at process startup and
injected here, so the use case never touches Argon2 directly.

OCC-retry on ``users.last_login_at`` update was **deferred** per the
approved review (improvement H); the update runs once, and a
``StaleDataError`` (from concurrent logins) would surface to the
client as a 500. Add retry only if this becomes observable in
practice.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog

from app.application.interfaces.clock import IClock
from app.application.interfaces.security import (
    IPasswordHasher,
    IssuedTokens,
    ITokenIssuer,
)
from app.application.interfaces.unit_of_work import IUnitOfWork
from app.application.use_cases.auth.errors import InvalidCredentialsError
from app.domain.identity.session import Session
from app.domain.identity.user import User

_LOGGER = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class LoginUserResult:
    user: User
    session: Session
    tokens: IssuedTokens


class LoginUser:
    """Verify email + password, issue a fresh session, return tokens."""

    def __init__(
        self,
        uow: IUnitOfWork,
        hasher: IPasswordHasher,
        token_issuer: ITokenIssuer,
        dummy_password_hash: str,
        clock: IClock,
    ) -> None:
        self._uow = uow
        self._hasher = hasher
        self._token_issuer = token_issuer
        # Pre-computed at container init; used by the anti-enumeration
        # burn path. Must be a real Argon2id digest so ``verify`` takes
        # the same wall-time as the wrong-password branch.
        self._dummy_password_hash = dummy_password_hash
        self._clock = clock

    async def execute(
        self,
        email: str,
        password: str,
        ip: str | None = None,
        user_agent: str | None = None,
    ) -> LoginUserResult:
        async with self._uow:
            user = await self._uow.users.get_by_email(email)

            # ---------- Anti-enumeration branches ----------
            if user is None:
                # Burn one hash-verify to equalise wall-time with the
                # wrong-password path. Return value ignored.
                self._hasher.verify(password, self._dummy_password_hash)
                _LOGGER.warning(
                    "auth.login.failed",
                    reason="unknown_email",
                    email_domain=_email_domain(email),
                )
                raise InvalidCredentialsError("invalid email or password")

            if user.password_hash is None:
                # OAuth-only account (α5 territory). Same client-facing
                # error as wrong-password. Burn to keep timing consistent.
                self._hasher.verify(password, self._dummy_password_hash)
                _LOGGER.warning(
                    "auth.login.failed",
                    reason="oauth_only_account",
                    user_id=str(user.id),
                    email_domain=_email_domain(email),
                )
                raise InvalidCredentialsError("invalid email or password")

            if not self._hasher.verify(password, user.password_hash):
                _LOGGER.warning(
                    "auth.login.failed",
                    reason="wrong_password",
                    user_id=str(user.id),
                    email_domain=_email_domain(email),
                )
                raise InvalidCredentialsError("invalid email or password")

            # ---------- Happy path ----------
            now = self._clock.now()
            tokens = self._token_issuer.issue_for_login(user)

            session = await self._uow.sessions.add(
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

            await self._uow.users.update_last_login(user.id, now)
            await self._uow.commit()

        _LOGGER.info(
            "auth.login.succeeded",
            user_id=str(user.id),
            session_id=str(session.id),
            family_id=str(session.family_id),
        )
        return LoginUserResult(user=user, session=session, tokens=tokens)


def _email_domain(email: str) -> str:
    """Return the domain part of ``email`` for safe structured logging."""
    _, _, domain = email.rpartition("@")
    return domain
