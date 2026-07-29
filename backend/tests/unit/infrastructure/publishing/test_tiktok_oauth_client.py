"""Unit tests for ``TikTokOAuthClient`` (α9.6) — network-free via MockTransport.

The headline case is **refresh-token rotation**: TikTok issues a new refresh token on refresh
and invalidates the previous one, so the client must surface the rotated value rather than the
one it was given. Dropping it silently breaks the connection ~24h later.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app.application.interfaces.clock import IClock
from app.application.interfaces.social_oauth_client import OAuthExchangeError
from app.infrastructure.publishing.oauth.tiktok_oauth_client import TikTokOAuthClient

_API = "https://open.tiktokapis.com"
_TOKEN_URL = f"{_API}/v2/oauth/token/"
_AUTHORIZE_URL = "https://www.tiktok.com/v2/auth/authorize/"
_REVOKE_URL = f"{_API}/v2/oauth/revoke/"


class _StubClock(IClock):
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


_NOW = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> TikTokOAuthClient:
    return TikTokOAuthClient(
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        client_key="test-key",
        client_secret="test-secret",
        clock=_StubClock(_NOW),
        scopes=("user.info.basic", "video.publish"),
        authorize_url=_AUTHORIZE_URL,
        token_url=_TOKEN_URL,
        revoke_url=_REVOKE_URL,
        api_base_url=_API,
    )


def _token_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "open_id": "open-abc",
        "access_token": "act.first",
        "refresh_token": "rft.first",
        "expires_in": 86400,
        "refresh_expires_in": 31536000,
        "scope": "user.info.basic,video.publish",
        "token_type": "Bearer",
    }
    payload.update(overrides)
    return payload


@pytest.mark.unit
def test_authorization_url_uses_client_key_and_comma_scopes() -> None:
    client = _client(lambda _: httpx.Response(200))
    url = client.authorization_url(state="st8", redirect_uri="https://app.example/cb")
    query = parse_qs(urlparse(url).query)

    assert url.startswith(_AUTHORIZE_URL)
    # TikTok names it client_key, not client_id.
    assert query["client_key"] == ["test-key"]
    assert "client_id" not in query
    assert query["scope"] == ["user.info.basic,video.publish"]
    assert query["state"] == ["st8"]
    assert query["response_type"] == ["code"]


@pytest.mark.unit
async def test_exchange_code_returns_open_id_and_display_name() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == _TOKEN_URL:
            return httpx.Response(200, json=_token_payload())
        if request.url.path == "/v2/user/info/":
            return httpx.Response(200, json={"data": {"user": {"display_name": "Creator"}}})
        raise AssertionError(f"unexpected {request.url}")

    grant = await _client(handler).exchange_code(code="c0de", redirect_uri="https://app/cb")

    assert grant.external_account_id == "open-abc"
    assert grant.display_name == "Creator"
    assert grant.tokens.access_token == "act.first"
    assert grant.tokens.refresh_token == "rft.first"
    assert grant.tokens.expires_at == _NOW + timedelta(seconds=86400)
    assert grant.tokens.scopes == ("user.info.basic", "video.publish")


@pytest.mark.unit
async def test_exchange_code_tolerates_display_name_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == _TOKEN_URL:
            return httpx.Response(200, json=_token_payload())
        return httpx.Response(500)

    grant = await _client(handler).exchange_code(code="c0de", redirect_uri="https://app/cb")
    assert grant.external_account_id == "open-abc"
    assert grant.display_name is None


@pytest.mark.unit
async def test_exchange_code_requires_refresh_token_and_open_id() -> None:
    def without_refresh(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_token_payload(refresh_token=None))

    with pytest.raises(OAuthExchangeError):
        await _client(without_refresh).exchange_code(code="c", redirect_uri="r")

    def without_open_id(request: httpx.Request) -> httpx.Response:
        if str(request.url) == _TOKEN_URL:
            return httpx.Response(200, json=_token_payload(open_id=""))
        return httpx.Response(200, json={"data": {"user": {}}})

    with pytest.raises(OAuthExchangeError):
        await _client(without_open_id).exchange_code(code="c", redirect_uri="r")


@pytest.mark.unit
async def test_refresh_surfaces_the_rotated_refresh_token() -> None:
    """TikTok ROTATES refresh tokens; the new value must be returned, not the one we sent."""
    captured: list[dict[str, list[str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(parse_qs(request.content.decode()))
        return httpx.Response(
            200,
            json=_token_payload(access_token="act.second", refresh_token="rft.ROTATED"),
        )

    tokens = await _client(handler).refresh(refresh_token="rft.first")

    assert tokens.access_token == "act.second"
    assert tokens.refresh_token == "rft.ROTATED"
    assert tokens.refresh_token != "rft.first"
    assert captured[0]["grant_type"] == ["refresh_token"]
    assert captured[0]["refresh_token"] == ["rft.first"]
    assert captured[0]["client_key"] == ["test-key"]


@pytest.mark.unit
async def test_refresh_falls_back_when_provider_omits_a_new_token() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_token_payload(refresh_token=None))

    tokens = await _client(handler).refresh(refresh_token="rft.kept")
    assert tokens.refresh_token == "rft.kept"


@pytest.mark.unit
async def test_token_endpoint_failures_surface_as_oauth_errors() -> None:
    def http_error(_: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    with pytest.raises(OAuthExchangeError):
        await _client(http_error).refresh(refresh_token="rft")

    def error_envelope(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": "invalid_request", "access_token": "x"})

    with pytest.raises(OAuthExchangeError):
        await _client(error_envelope).refresh(refresh_token="rft")

    def transport_error(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    with pytest.raises(OAuthExchangeError):
        await _client(transport_error).refresh(refresh_token="rft")


@pytest.mark.unit
async def test_revoke_is_best_effort() -> None:
    def transport_error(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    # Must not raise — the local credential is deleted regardless.
    await _client(transport_error).revoke(token="rft")
