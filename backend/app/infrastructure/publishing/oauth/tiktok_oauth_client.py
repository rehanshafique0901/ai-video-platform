"""``TikTokOAuthClient`` — the real TikTok OAuth 2.0 client (α9.6).

The credential-*acquisition* half of the second real destination. It fills the same α8.6a
:class:`~app.application.interfaces.social_oauth_client.ISocialOAuthClient` seam that
``YouTubeOAuthClient`` fills for Google, so the credential service can connect + refresh a real
TikTok credential. It is **distinct** from the upload adapter
(:class:`~app.infrastructure.publishing.destinations.tiktok.TikTokDestination`); the two
responsibilities are never merged.

* **Configuration-blind (W8.1.1).** Client key/secret, scopes, and endpoints are injected at
  construction; the client never reads ``Settings`` itself.
* **Thin httpx.** All network I/O goes through an injected :class:`httpx.AsyncClient` (a
  ``MockTransport`` in tests); no TikTok SDK.
* **Neutral errors.** Any provider failure surfaces as :class:`OAuthExchangeError`; client
  secrets and tokens never appear in error text or logs.

Two TikTok-specific details that differ from the Google client and are easy to get wrong:

* TikTok names the client identifier **``client_key``**, not ``client_id``, and expects
  comma-separated scopes.
* **TikTok ROTATES the refresh token on every refresh.** The response's ``refresh_token`` may
  differ from the one sent, and the previous value stops working. :meth:`refresh` therefore
  always surfaces the newly issued token so the credential store re-encrypts and persists it —
  see the rotation regression test. Dropping the rotated value silently breaks the connection
  roughly 24 hours later, when the current access token expires.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from urllib.parse import urlencode

import httpx

from app.application.interfaces.clock import IClock
from app.application.interfaces.social_credential_store import GrantedTokens
from app.application.interfaces.social_oauth_client import (
    ISocialOAuthClient,
    OAuthExchangeError,
    OAuthGrant,
)


class TikTokOAuthClient(ISocialOAuthClient):
    """TikTok OAuth 2.0 mechanics for connecting + maintaining a creator account."""

    def __init__(
        self,
        *,
        http: httpx.AsyncClient,
        client_key: str,
        client_secret: str,
        clock: IClock,
        scopes: tuple[str, ...],
        authorize_url: str,
        token_url: str,
        revoke_url: str,
        api_base_url: str,
    ) -> None:
        self._http = http
        self._client_key = client_key
        self._client_secret = client_secret
        self._clock = clock
        self._scopes = scopes
        self._authorize_url = authorize_url
        self._token_url = token_url
        self._revoke_url = revoke_url
        self._api_base_url = api_base_url.rstrip("/")

    def authorization_url(self, *, state: str, redirect_uri: str) -> str:
        """Build the TikTok consent URL (``client_key`` + comma-separated scopes)."""
        params = {
            "client_key": self._client_key,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": ",".join(self._scopes),
            "state": state,
        }
        return f"{self._authorize_url}?{urlencode(params)}"

    async def exchange_code(self, *, code: str, redirect_uri: str) -> OAuthGrant:
        """Exchange an authorization code for tokens; ``open_id`` is the account identity."""
        payload = await self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
            }
        )
        tokens = self._tokens_from_payload(payload, fallback_refresh_token=None)
        if tokens.refresh_token is None:
            # Without a refresh token the 24h access token cannot be maintained.
            raise OAuthExchangeError("token response did not include a refresh token")
        open_id = str(payload.get("open_id") or "").strip()
        if not open_id:
            raise OAuthExchangeError("token response did not include an open_id")
        display_name = await self._resolve_display_name(tokens.access_token)
        return OAuthGrant(
            external_account_id=open_id,
            display_name=display_name,
            tokens=tokens,
        )

    async def refresh(self, *, refresh_token: str) -> GrantedTokens:
        """Obtain a fresh access token, surfacing TikTok's **rotated** refresh token.

        TikTok issues a new refresh token on most refreshes and invalidates the old one, so the
        returned value must be persisted. The fallback only applies if TikTok omits the field.
        """
        payload = await self._token_request(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            }
        )
        return self._tokens_from_payload(payload, fallback_refresh_token=refresh_token)

    async def revoke(self, *, token: str) -> None:
        """Best-effort revoke at TikTok; an already-revoked/unknown token counts as success."""
        try:
            await self._http.post(
                self._revoke_url,
                data={
                    "client_key": self._client_key,
                    "client_secret": self._client_secret,
                    "token": token,
                },
            )
        except httpx.HTTPError:
            # Revocation is best-effort + idempotent (the local credential is deleted regardless).
            return None
        return None

    async def _token_request(self, extra: dict[str, str]) -> dict[str, Any]:
        data = {
            "client_key": self._client_key,
            "client_secret": self._client_secret,
            **extra,
        }
        try:
            response = await self._http.post(
                self._token_url,
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except httpx.HTTPError as exc:
            raise OAuthExchangeError(f"token endpoint transport error: {exc}") from exc
        if response.status_code != httpx.codes.OK:
            raise OAuthExchangeError(f"token endpoint returned HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError as exc:
            raise OAuthExchangeError("token endpoint returned a non-JSON body") from exc
        if not isinstance(body, dict):
            raise OAuthExchangeError("token endpoint returned an unexpected body")
        # TikTok reports failures in an `error` envelope alongside a 200.
        error = body.get("error")
        if isinstance(error, str) and error and error != "ok":
            raise OAuthExchangeError(f"token endpoint returned error {error}")
        if "access_token" not in body:
            raise OAuthExchangeError("token endpoint response missing access_token")
        return body

    def _tokens_from_payload(
        self, payload: dict[str, Any], *, fallback_refresh_token: str | None
    ) -> GrantedTokens:
        access_token = str(payload["access_token"])
        refresh_token = payload.get("refresh_token")
        expires_in = payload.get("expires_in")
        expires_at = None
        if isinstance(expires_in, int | float):
            expires_at = self._clock.now() + timedelta(seconds=int(expires_in))
        raw_scope = payload.get("scope")
        # TikTok returns a comma-separated scope list (Google uses spaces).
        scopes = (
            tuple(s for s in str(raw_scope).replace(" ", "").split(",") if s)
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

    async def _resolve_display_name(self, access_token: str) -> str | None:
        """Best-effort display name. Never fatal — the connection works without it."""
        try:
            response = await self._http.get(
                f"{self._api_base_url}/v2/user/info/",
                params={"fields": "display_name"},
                headers={"Authorization": f"Bearer {access_token}"},
            )
        except httpx.HTTPError:
            return None
        if response.status_code != httpx.codes.OK:
            return None
        try:
            body = response.json()
        except ValueError:
            return None
        data = body.get("data") if isinstance(body, dict) else None
        user = data.get("user") if isinstance(data, dict) else None
        if isinstance(user, dict) and user.get("display_name"):
            return str(user["display_name"])
        return None


__all__ = ["TikTokOAuthClient"]
