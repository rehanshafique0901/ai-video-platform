"""``MockSocialOAuthClient`` — a deterministic, offline OAuth client for α8.6a (OQ1).

Proves the connection + credential boundary end-to-end without a real destination API or
network. Every operation is a pure function of its inputs (plus the injected clock for
expiry), so tests and the golden connection flow are fully reproducible. It is a real
:class:`ISocialOAuthClient`, exercising the exact seam the α8.6c YouTube client will fill.
"""

from __future__ import annotations

from datetime import timedelta
from urllib.parse import quote

from app.application.interfaces.clock import IClock
from app.application.interfaces.social_credential_store import GrantedTokens
from app.application.interfaces.social_oauth_client import ISocialOAuthClient, OAuthGrant


class MockSocialOAuthClient(ISocialOAuthClient):
    """An in-memory OAuth provider that mints deterministic tokens from an auth code."""

    def __init__(
        self,
        *,
        clock: IClock,
        platform: str = "mock",
        authorize_endpoint: str = "https://mock.oauth.local/authorize",
        access_ttl_seconds: int = 3600,
        scopes: tuple[str, ...] = ("publish",),
    ) -> None:
        self._clock = clock
        self._platform = platform
        self._authorize_endpoint = authorize_endpoint
        self._access_ttl = timedelta(seconds=access_ttl_seconds)
        self._scopes = scopes

    def authorization_url(self, *, state: str, redirect_uri: str) -> str:
        return (
            f"{self._authorize_endpoint}"
            f"?response_type=code&state={quote(state, safe='')}"
            f"&redirect_uri={quote(redirect_uri, safe='')}"
        )

    async def exchange_code(self, *, code: str, redirect_uri: str) -> OAuthGrant:
        return OAuthGrant(
            external_account_id=f"{self._platform}-account-{code}",
            display_name=f"Mock {self._platform.title()} Channel",
            tokens=GrantedTokens(
                access_token=f"mock-access-{code}",
                refresh_token=f"mock-refresh-{code}",
                expires_at=self._clock.now() + self._access_ttl,
                scopes=self._scopes,
            ),
        )

    async def refresh(self, *, refresh_token: str) -> GrantedTokens:
        return GrantedTokens(
            access_token=f"mock-access-refreshed-{refresh_token}",
            refresh_token=refresh_token,
            expires_at=self._clock.now() + self._access_ttl,
            scopes=self._scopes,
        )

    async def revoke(self, *, token: str) -> None:
        return None


__all__ = ["MockSocialOAuthClient"]
