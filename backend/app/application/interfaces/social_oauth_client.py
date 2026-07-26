"""Port: ``ISocialOAuthClient`` — a destination's OAuth connection mechanism (α8.6a).

Connecting an account inherently needs authorization-URL construction, authorization-code
exchange, token refresh, and revocation. That is a **credential-acquisition** concern,
distinct from the destination *upload* API (which is α8.6c). α8.6a defines this port and
ships a deterministic **Mock** implementation; the real YouTube OAuth client lands with its
upload adapter in α8.6c (OQ1).

Configuration-blind (W8.1.1): concrete adapters receive their client secret / endpoints by
injection at the composition root and never read configuration themselves.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from app.application.interfaces.social_credential_store import GrantedTokens


class OAuthExchangeError(Exception):
    """An authorization-code exchange or token refresh failed at the provider (neutral)."""


@dataclass(frozen=True, slots=True)
class OAuthGrant:
    """The result of a successful authorization-code exchange.

    ``external_account_id`` identifies the connected channel/account on the platform;
    ``display_name`` is a non-secret label. ``tokens`` carry the (still-plaintext, in
    transit) credential that the credential service will immediately encrypt.
    """

    external_account_id: str
    display_name: str | None
    tokens: GrantedTokens


class ISocialOAuthClient(ABC):
    """Per-platform OAuth mechanics for connecting + maintaining a destination account."""

    @abstractmethod
    def authorization_url(self, *, state: str, redirect_uri: str) -> str:
        """Build the provider authorization URL the user is redirected to (carries ``state``)."""
        ...

    @abstractmethod
    async def exchange_code(self, *, code: str, redirect_uri: str) -> OAuthGrant:
        """Exchange an authorization ``code`` for tokens + account identity. Raises on failure."""
        ...

    @abstractmethod
    async def refresh(self, *, refresh_token: str) -> GrantedTokens:
        """Obtain a fresh access token from a refresh token. Raises :class:`OAuthExchangeError`."""
        ...

    @abstractmethod
    async def revoke(self, *, token: str) -> None:
        """Best-effort revoke a token at the provider (idempotent; swallows already-revoked)."""
        ...


__all__ = ["ISocialOAuthClient", "OAuthExchangeError", "OAuthGrant"]
