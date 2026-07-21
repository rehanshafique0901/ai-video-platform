"""Unit tests for α8.1 provider-registry composition in the DI container.

Proves the signed-off Q5 / W8.1.2 wiring at the composition root:

* no OpenAI key  → IMAGE resolves to ``MockImageProvider``,
* OpenAI key set → IMAGE resolves to the real ``OpenAIImageProvider``,
* LLM / VIDEO / VOICE are **always** mock (one real capability only, W8.1.2),
* the shared client carries the injected key so the provider never sees it
  (W8.1.1 / Q4 — constructors receive secrets, never retrieve them).
"""

from __future__ import annotations

import httpx
import pytest

from app.application.interfaces.providers import Capability
from app.core import container
from app.core.config import Settings
from app.infrastructure.ai.providers.mocks import (
    MockImageProvider,
    MockLLMProvider,
    MockVideoProvider,
    MockVoiceProvider,
)
from app.infrastructure.ai.providers.openai import OpenAIImageProvider

pytestmark = pytest.mark.unit

_JWT = "test-secret-do-not-use-in-production-32chars"


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "database_url": "postgresql+psycopg://u:p@h:5432/d",
        "jwt_secret": _JWT,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg,arg-type]


def test_registry_uses_mock_image_when_no_client() -> None:
    registry = container._build_provider_registry(None)
    assert isinstance(registry.resolve(Capability.IMAGE), MockImageProvider)
    assert isinstance(registry.resolve(Capability.LLM), MockLLMProvider)
    assert isinstance(registry.resolve(Capability.VIDEO), MockVideoProvider)
    assert isinstance(registry.resolve(Capability.VOICE), MockVoiceProvider)


async def test_registry_uses_openai_image_when_client_present() -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    try:
        registry = container._build_provider_registry(client)
        # Exactly one real capability: IMAGE is real, the rest stay mock (W8.1.2).
        assert isinstance(registry.resolve(Capability.IMAGE), OpenAIImageProvider)
        assert isinstance(registry.resolve(Capability.LLM), MockLLMProvider)
        assert isinstance(registry.resolve(Capability.VIDEO), MockVideoProvider)
        assert isinstance(registry.resolve(Capability.VOICE), MockVoiceProvider)
    finally:
        await client.aclose()


def test_build_openai_client_is_none_without_key() -> None:
    assert container._build_openai_client(_settings()) is None


async def test_build_openai_client_bakes_in_the_injected_secret() -> None:
    client = container._build_openai_client(
        _settings(openai_api_key="sk-super-secret", openai_base_url="https://api.openai.com/v1")
    )
    assert client is not None
    try:
        # Q4/W8.1.1: the key is baked into the client's Authorization header at the
        # composition root — the provider adapter receives the client, not the key.
        assert client.headers["authorization"] == "Bearer sk-super-secret"
        # httpx normalises base_url with a trailing slash; the effective request
        # path stays /v1/images/generations (see the adapter's path test).
        assert str(client.base_url) == "https://api.openai.com/v1/"
    finally:
        await client.aclose()
