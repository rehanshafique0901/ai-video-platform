"""Unit tests for the real ``YouTubeOAuthClient`` (α8.6c) — network-free via MockTransport."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import httpx
import pytest

from app.application.interfaces.clock import IClock
from app.application.interfaces.social_oauth_client import OAuthExchangeError
from app.infrastructure.publishing.oauth.youtube_oauth_client import YouTubeOAuthClient

_UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"


class _FixedClock(IClock):
    def now(self) -> datetime:
        return datetime(2026, 7, 27, 12, 0, tzinfo=UTC)


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> YouTubeOAuthClient:
    return YouTubeOAuthClient(
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        client_id="cid",
        client_secret="secret",
        clock=_FixedClock(),
        scopes=(_UPLOAD_SCOPE,),
        authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",
        revoke_url="https://oauth2.googleapis.com/revoke",
        api_base_url="https://www.googleapis.com",
    )


def _no_network(_: httpx.Request) -> httpx.Response:  # pragma: no cover - safety net
    raise AssertionError("no HTTP call expected")


@pytest.mark.unit
def test_authorization_url_carries_offline_consent_and_state() -> None:
    url = _client(_no_network).authorization_url(
        state="st.ate", redirect_uri="https://app/callback?a=b"
    )
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "client_id=cid" in url
    assert "response_type=code" in url
    assert "access_type=offline" in url
    assert "prompt=consent" in url
    assert "state=st.ate" in url
    # redirect_uri + scope are URL-encoded.
    assert "redirect_uri=https%3A%2F%2Fapp%2Fcallback%3Fa%3Db" in url
    assert "youtube.upload" in url


@pytest.mark.unit
async def test_exchange_code_returns_grant_with_channel_identity() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(
                200,
                json={
                    "access_token": "ya29.access",
                    "refresh_token": "1//refresh",
                    "expires_in": 3600,
                    "scope": _UPLOAD_SCOPE,
                },
            )
        if request.url.path == "/youtube/v3/channels":
            assert request.headers["Authorization"] == "Bearer ya29.access"
            return httpx.Response(
                200,
                json={"items": [{"id": "UC_channel", "snippet": {"title": "My Channel"}}]},
            )
        raise AssertionError(f"unexpected path {request.url.path}")

    grant = await _client(handler).exchange_code(code="AUTH", redirect_uri="https://app/callback")
    assert grant.external_account_id == "UC_channel"
    assert grant.display_name == "My Channel"
    assert grant.tokens.access_token == "ya29.access"
    assert grant.tokens.refresh_token == "1//refresh"
    assert grant.tokens.expires_at == datetime(2026, 7, 27, 13, 0, tzinfo=UTC)
    assert grant.tokens.scopes == (_UPLOAD_SCOPE,)


@pytest.mark.unit
async def test_exchange_code_without_refresh_token_is_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": "a", "expires_in": 3600})

    with pytest.raises(OAuthExchangeError):
        await _client(handler).exchange_code(code="AUTH", redirect_uri="https://app/cb")


@pytest.mark.unit
async def test_exchange_code_with_no_channel_is_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(
                200, json={"access_token": "a", "refresh_token": "r", "expires_in": 60}
            )
        return httpx.Response(200, json={"items": []})

    with pytest.raises(OAuthExchangeError):
        await _client(handler).exchange_code(code="AUTH", redirect_uri="https://app/cb")


@pytest.mark.unit
async def test_exchange_code_token_error_status_is_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    with pytest.raises(OAuthExchangeError):
        await _client(handler).exchange_code(code="AUTH", redirect_uri="https://app/cb")


@pytest.mark.unit
async def test_refresh_reuses_input_refresh_token_when_omitted() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/token"
        return httpx.Response(200, json={"access_token": "ya29.new", "expires_in": 1800})

    tokens = await _client(handler).refresh(refresh_token="1//keepme")
    assert tokens.access_token == "ya29.new"
    assert tokens.refresh_token == "1//keepme"
    assert tokens.expires_at == datetime(2026, 7, 27, 12, 30, tzinfo=UTC)


@pytest.mark.unit
async def test_refresh_error_status_is_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid_client"})

    with pytest.raises(OAuthExchangeError):
        await _client(handler).refresh(refresh_token="r")


@pytest.mark.unit
async def test_revoke_swallows_errors_and_is_noop() -> None:
    def ok(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/revoke"
        return httpx.Response(200)

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    assert await _client(ok).revoke(token="t") is None
    assert await _client(boom).revoke(token="t") is None
