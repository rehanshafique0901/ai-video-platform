"""Unit tests for the deterministic Mock OAuth client (α8.6a, OQ1)."""

from __future__ import annotations

from datetime import UTC, datetime

from app.application.interfaces.clock import IClock
from app.infrastructure.publishing.oauth.mock_oauth_client import MockSocialOAuthClient


class _FixedClock(IClock):
    def now(self) -> datetime:
        return datetime(2026, 7, 26, 12, 0, tzinfo=UTC)


def _client() -> MockSocialOAuthClient:
    return MockSocialOAuthClient(clock=_FixedClock())


def test_authorization_url_embeds_state_and_redirect() -> None:
    url = _client().authorization_url(state="abc.def", redirect_uri="http://x/callback?a=b")
    assert url.startswith("https://mock.oauth.local/authorize")
    assert "state=abc.def" in url
    # redirect_uri is URL-encoded.
    assert "redirect_uri=http%3A%2F%2Fx%2Fcallback%3Fa%3Db" in url


async def test_exchange_code_is_deterministic() -> None:
    client = _client()
    grant_a = await client.exchange_code(code="XYZ", redirect_uri="http://x/callback")
    grant_b = await client.exchange_code(code="XYZ", redirect_uri="http://x/callback")
    assert grant_a == grant_b
    assert grant_a.external_account_id == "mock-account-XYZ"
    assert grant_a.tokens.access_token == "mock-access-XYZ"
    assert grant_a.tokens.refresh_token == "mock-refresh-XYZ"
    assert grant_a.tokens.expires_at == datetime(2026, 7, 26, 13, 0, tzinfo=UTC)
    assert grant_a.tokens.scopes == ("publish",)


async def test_refresh_returns_new_access_token() -> None:
    tokens = await _client().refresh(refresh_token="mock-refresh-XYZ")
    assert tokens.access_token == "mock-access-refreshed-mock-refresh-XYZ"
    assert tokens.refresh_token == "mock-refresh-XYZ"


async def test_revoke_is_noop() -> None:
    assert await _client().revoke(token="anything") is None
