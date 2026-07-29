"""Unit tests for the α9.6 TikTok composition-root wiring (fail-soft registration).

TikTok must be registered **only** when both OAuth credentials are configured. Unconfigured —
which is how CI always runs — it is simply absent from the destination registry and the OAuth
client map, so a ``platform="tiktok"`` publish fails *create-time validation* rather than at
runtime, and no HTTP client is ever opened.

The registry accessors are module-private; a wiring test is the one legitimate caller, since the
whole point is to assert what the composition root builds.
"""

from __future__ import annotations

import pytest

from app.core import container
from app.core.config import Settings

_DB_URL = "postgresql+psycopg://user:pass@localhost:5432/does_not_connect"
_JWT = "unit-test-jwt-secret-at-least-32-characters"


def _settings(*, key: str | None = None, secret: str | None = None) -> Settings:
    return Settings(  # type: ignore[call-arg]
        database_url=_DB_URL,
        jwt_secret=_JWT,  # type: ignore[arg-type]
        environment="local",  # type: ignore[arg-type]
        tiktok_oauth_client_key=key,
        tiktok_oauth_client_secret=secret,  # type: ignore[arg-type]
    )


@pytest.mark.unit
def test_unconfigured_tiktok_is_not_registered() -> None:
    container.reset()
    try:
        container.init(_settings())
        assert "tiktok" not in container._get_destination_registry().supported_platforms()
        assert "tiktok" not in container._get_oauth_clients()
        # Fail-soft: no HTTP client is opened on the unconfigured path.
        assert container._tiktok_client is None
    finally:
        container.reset()


@pytest.mark.unit
def test_partially_configured_tiktok_is_not_registered() -> None:
    """A key without a secret (or vice versa) must not half-register the destination."""
    container.reset()
    try:
        container.init(_settings(key="only-a-key"))
        assert "tiktok" not in container._get_destination_registry().supported_platforms()
        assert "tiktok" not in container._get_oauth_clients()
    finally:
        container.reset()


@pytest.mark.unit
async def test_configured_tiktok_is_registered_alongside_mock() -> None:
    container.reset()
    try:
        container.init(_settings(key="client-key", secret="client-secret"))
        platforms = container._get_destination_registry().supported_platforms()
        assert "tiktok" in platforms
        assert "mock" in platforms
        assert "tiktok" in container._get_oauth_clients()
        assert container._get_destination_registry().for_platform("tiktok").platform == "tiktok"
    finally:
        # Closes the lazily-built TikTok httpx client.
        await container.shutdown()
        container.reset()
