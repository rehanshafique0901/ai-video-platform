"""``YouTubeOAuthClient`` — the real Google OAuth 2.0 client for YouTube (α8.6c).

The credential-*acquisition* half of the first real destination (grounding §2). It fills
the α8.6a :class:`~app.application.interfaces.social_oauth_client.ISocialOAuthClient` seam
that ``MockSocialOAuthClient`` proved, so the credential service can connect + refresh a
real Google credential. It is **distinct** from the upload adapter
(:class:`~app.infrastructure.publishing.destinations.youtube.YouTubeDestination`); the two
responsibilities are never merged (EQ1).

* **Configuration-blind (W8.1.1).** Client id/secret, scopes, and endpoints are injected at
  construction; the client never reads ``Settings`` itself.
* **Thin httpx (EQ2).** All network I/O goes through an injected :class:`httpx.AsyncClient`
  (a ``MockTransport`` in tests); no Google SDK.
* **Neutral errors.** Any provider failure surfaces as :class:`OAuthExchangeError` with a
  safe message — client secrets and tokens never appear in error text or logs.
"""

from __future__ import annotations

from datetime import timedelta
from urllib.parse import urlencode

import httpx

from app.application.interfaces.clock import IClock
from app.application.interfaces.social_credential_store import GrantedTokens
from app.application.interfaces.social_oauth_client import (
    ISocialOAuthClient,
    OAuthExchangeError,
    OAuthGrant,
)


class YouTubeOAuthClient(ISocialOAuthClient):
    """Google OAuth 2.0 mechanics for connecting + maintaining a YouTube channel."""

    def __init__(
        self,
        *,
        http: httpx.AsyncClient,
        client_id: str,
        client_secret: str,
        clock: IClock,
        scopes: tuple[str, ...],
        authorize_url: str,
        token_url: str,
        revoke_url: str,
        api_base_url: str,
    ) -> None:
        self._http = http
        self._client_id = client_id
        self._client_secret = client_secret
        self._clock = clock
        self._scopes = scopes
        self._authorize_url = authorize_url
        self._token_url = token_url
        self._revoke_url = revoke_url
        self._api_base_url = api_base_url.rstrip("/")

    def authorization_url(self, *, state: str, redirect_uri: str) -> str:
        """Build the Google consent URL (offline access + forced consent for a refresh token)."""
        params = {
            "client_id": self._client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(self._scopes),
            "access_type": "offline",
            "include_granted_scopes": "true",
            "prompt": "consent",
            "state": state,
        }
        return f"{self._authorize_url}?{urlencode(params)}"

    async def exchange_code(self, *, code: str, redirect_uri: str) -> OAuthGrant:
        """Exchange an authorization code for tokens, then resolve the channel identity."""
        payload = await self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
            }
        )
        tokens = self._tokens_from_payload(payload, fallback_refresh_token=None)
        if tokens.refresh_token is None:
            # Without a refresh token we cannot maintain the connection (fail-closed later).
            raise OAuthExchangeError("token response did not include a refresh token")
        channel_id, display_name = await self._resolve_channel(tokens.access_token)
        return OAuthGrant(
            external_account_id=channel_id,
            display_name=display_name,
            tokens=tokens,
        )

    async def refresh(self, *, refresh_token: str) -> GrantedTokens:
        """Obtain a fresh access token. Google usually omits a new refresh token on refresh."""
        payload = await self._token_request(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            }
        )
        return self._tokens_from_payload(payload, fallback_refresh_token=refresh_token)

    async def revoke(self, *, token: str) -> None:
        """Best-effort revoke at Google; an already-revoked/unknown token is treated as success."""
        try:
            await self._http.post(self._revoke_url, data={"token": token})
        except httpx.HTTPError:
            # Revocation is best-effort + idempotent (the local credential is deleted regardless).
            return None
        return None

    async def _token_request(self, extra: dict[str, str]) -> dict[str, object]:
        data = {
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            **extra,
        }
        try:
            response = await self._http.post(self._token_url, data=data)
        except httpx.HTTPError as exc:
            raise OAuthExchangeError(f"token endpoint transport error: {exc}") from exc
        if response.status_code != httpx.codes.OK:
            raise OAuthExchangeError(f"token endpoint returned HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError as exc:
            raise OAuthExchangeError("token endpoint returned a non-JSON body") from exc
        if not isinstance(body, dict) or "access_token" not in body:
            raise OAuthExchangeError("token endpoint response missing access_token")
        return body

    def _tokens_from_payload(
        self, payload: dict[str, object], *, fallback_refresh_token: str | None
    ) -> GrantedTokens:
        access_token = str(payload["access_token"])
        refresh_token = payload.get("refresh_token")
        expires_in = payload.get("expires_in")
        expires_at = None
        if isinstance(expires_in, int | float):
            expires_at = self._clock.now() + timedelta(seconds=int(expires_in))
        raw_scope = payload.get("scope")
        scopes = (
            tuple(str(raw_scope).split())
            if isinstance(raw_scope, str) and raw_scope
            else self._scopes
        )
        return GrantedTokens(
            access_token=access_token,
            refresh_token=(
                str(refresh_token) if refresh_token is not None else fallback_refresh_token
            ),
            expires_at=expires_at,
            scopes=scopes,
        )

    async def _resolve_channel(self, access_token: str) -> tuple[str, str | None]:
        url = f"{self._api_base_url}/youtube/v3/channels"
        try:
            response = await self._http.get(
                url,
                params={"part": "snippet", "mine": "true"},
                headers={"Authorization": f"Bearer {access_token}"},
            )
        except httpx.HTTPError as exc:
            raise OAuthExchangeError(f"channel lookup transport error: {exc}") from exc
        if response.status_code != httpx.codes.OK:
            raise OAuthExchangeError(f"channel lookup returned HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError as exc:
            raise OAuthExchangeError("channel lookup returned a non-JSON body") from exc
        items = body.get("items") if isinstance(body, dict) else None
        if not items:
            raise OAuthExchangeError("no YouTube channel is associated with this account")
        first = items[0]
        channel_id = str(first.get("id", "")).strip()
        if not channel_id:
            raise OAuthExchangeError("channel lookup response carried no channel id")
        snippet = first.get("snippet") if isinstance(first, dict) else None
        display_name = None
        if isinstance(snippet, dict) and snippet.get("title"):
            display_name = str(snippet["title"])
        return channel_id, display_name


__all__ = ["YouTubeOAuthClient"]
