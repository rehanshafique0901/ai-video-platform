"""Port: ``ISocialCredentialStore`` — authorized access to a connected account (α8.6a).

The credential-ownership boundary (ADR-0047). Callers ask for **authorized access for a
``SocialAccount``** — never "decrypt this token" (C3/C4). The concrete implementation (the
credential service) is the *sole* module that decrypts stored OAuth tokens (C7); domain,
application, and destination-adapter code only ever hold an :class:`AuthorizedContext`,
which carries a short-lived bearer and **no** refresh token and **no** key material.

Fail-closed (approved): a revoked / expired-and-unrefreshable / missing credential raises
:class:`CredentialUnavailableError` — there is no plaintext fallback and no silent
degrade. A tampered ciphertext raises :class:`CredentialDecryptionError`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


class CredentialUnavailableError(Exception):
    """No usable credential for the account (revoked / expired-unrefreshable / missing).

    The publishing capability fails **explicitly** rather than degrading — a connected
    account is either securely authorized, or publishing is unavailable for it.
    """


class CredentialDecryptionError(Exception):
    """Stored ciphertext failed authenticated decryption (tamper or wrong/rotated key)."""


@dataclass(frozen=True, slots=True)
class GrantedTokens:
    """The tokens obtained from an OAuth exchange / refresh — the input to :meth:`store`.

    Plaintext lives only in transit through the credential service, which encrypts it
    before it ever touches the database (C1/C2). ``expires_at`` is a **non-secret**
    timestamp used to drive proactive refresh.
    """

    access_token: str
    refresh_token: str | None
    expires_at: datetime | None
    scopes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AuthorizedContext:
    """Ready-to-use, already-refreshed authorization for one destination call.

    Immutable and scoped to publishing infrastructure (OQ4). Carries the minimal bearer
    only — **never** the refresh token and **never** key material — so a destination
    adapter that receives it stays credential-blind (PUB-5 / C4).
    """

    access_token: str
    expires_at: datetime | None
    scopes: tuple[str, ...]


class ISocialCredentialStore(ABC):
    """Own the lifecycle of a connected account's OAuth credential (ADR-0047 C3)."""

    @abstractmethod
    async def store(self, social_account_id: UUID, tokens: GrantedTokens) -> None:
        """Encrypt and persist ``tokens`` for the account (insert or replace).

        The account row must already exist (the credential row references it). The tokens
        are envelope-encrypted before persistence; no plaintext is written (C1/C2).
        """
        ...

    @abstractmethod
    async def authorize(self, social_account_id: UUID) -> AuthorizedContext:
        """Return a fresh :class:`AuthorizedContext`, refreshing the token if near expiry.

        Raises :class:`CredentialUnavailableError` when the account is not ``connected``,
        has no stored credential, or is expired and cannot be refreshed (fail-closed).
        """
        ...

    @abstractmethod
    async def revoke(self, social_account_id: UUID) -> None:
        """Invalidate the credential at the provider (best-effort) and delete it locally (C6)."""
        ...


__all__ = [
    "AuthorizedContext",
    "CredentialDecryptionError",
    "CredentialUnavailableError",
    "GrantedTokens",
    "ISocialCredentialStore",
]
