"""Unit tests for α8.1/α8.2 provider-registry composition in the DI container.

Proves the signed-off wiring at the composition root:

* no key         → the capability resolves to its deterministic mock,
* OpenAI key set → IMAGE resolves to the real ``OpenAIImageProvider`` (α8.1),
* Fal key set    → VIDEO resolves to the real ``FalVideoProvider`` (α8.2),
* IMAGE and VIDEO compose **independently**; LLM / VOICE are always mock,
* exactly one provider per capability (no fallback / selection),
* the shared clients carry the injected key so the providers never see it
  (W8.1.1 — constructors receive secrets, never retrieve them; Fal uses the
  ``Key`` auth scheme, OpenAI uses ``Bearer``).
"""

from __future__ import annotations

import httpx
import pytest

from app.application.interfaces.providers import Capability
from app.core import container
from app.core.config import Settings
from app.infrastructure.ai.providers.fal import FalVideoProvider
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


def _mock_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))


# --- registry composition ----------------------------------------------------


def test_registry_uses_all_mocks_when_no_clients() -> None:
    registry = container._build_provider_registry(None, None)
    assert isinstance(registry.resolve(Capability.IMAGE), MockImageProvider)
    assert isinstance(registry.resolve(Capability.LLM), MockLLMProvider)
    assert isinstance(registry.resolve(Capability.VIDEO), MockVideoProvider)
    assert isinstance(registry.resolve(Capability.VOICE), MockVoiceProvider)


async def test_registry_uses_openai_image_when_only_openai_client() -> None:
    client = _mock_client()
    try:
        registry = container._build_provider_registry(client, None)
        # IMAGE is real; VIDEO stays mock (independent composition).
        assert isinstance(registry.resolve(Capability.IMAGE), OpenAIImageProvider)
        assert isinstance(registry.resolve(Capability.VIDEO), MockVideoProvider)
        assert isinstance(registry.resolve(Capability.LLM), MockLLMProvider)
        assert isinstance(registry.resolve(Capability.VOICE), MockVoiceProvider)
    finally:
        await client.aclose()


async def test_registry_uses_fal_video_when_only_fal_client() -> None:
    client = _mock_client()
    try:
        registry = container._build_provider_registry(None, client)
        # VIDEO is real; IMAGE stays mock (independent composition).
        assert isinstance(registry.resolve(Capability.VIDEO), FalVideoProvider)
        assert isinstance(registry.resolve(Capability.IMAGE), MockImageProvider)
        assert isinstance(registry.resolve(Capability.LLM), MockLLMProvider)
        assert isinstance(registry.resolve(Capability.VOICE), MockVoiceProvider)
    finally:
        await client.aclose()


async def test_registry_uses_both_real_when_both_clients_present() -> None:
    openai_client, fal_client = _mock_client(), _mock_client()
    try:
        registry = container._build_provider_registry(openai_client, fal_client)
        assert isinstance(registry.resolve(Capability.IMAGE), OpenAIImageProvider)
        assert isinstance(registry.resolve(Capability.VIDEO), FalVideoProvider)
        # Still exactly one provider per capability; LLM / VOICE remain mock.
        assert isinstance(registry.resolve(Capability.LLM), MockLLMProvider)
        assert isinstance(registry.resolve(Capability.VOICE), MockVoiceProvider)
    finally:
        await openai_client.aclose()
        await fal_client.aclose()


# --- client construction (secret injection) ----------------------------------


def test_build_openai_client_is_none_without_key() -> None:
    assert container._build_openai_client(_settings()) is None


def test_build_fal_client_is_none_without_key() -> None:
    assert container._build_fal_client(_settings()) is None


async def test_build_openai_client_bakes_in_the_injected_secret() -> None:
    client = container._build_openai_client(
        _settings(openai_api_key="sk-super-secret", openai_base_url="https://api.openai.com/v1")
    )
    assert client is not None
    try:
        assert client.headers["authorization"] == "Bearer sk-super-secret"
        assert str(client.base_url) == "https://api.openai.com/v1/"
    finally:
        await client.aclose()


async def test_build_fal_client_bakes_in_the_injected_secret() -> None:
    client = container._build_fal_client(
        _settings(fal_api_key="fal-super-secret", fal_base_url="https://queue.fal.run")
    )
    assert client is not None
    try:
        # W8.1.1: the key is baked into the client's Authorization header at the
        # composition root — the provider receives the client, not the key. Fal
        # uses the ``Key`` scheme (not ``Bearer``).
        assert client.headers["authorization"] == "Key fal-super-secret"
        assert str(client.base_url) == "https://queue.fal.run"
    finally:
        await client.aclose()
