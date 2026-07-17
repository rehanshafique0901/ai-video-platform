"""Unit tests for ``ProviderRegistry`` (Slice α7.4).

Coverage:

* R1 — ``default_registry`` wires exactly the four mocks (one capability each).
* R2 — ``resolve`` returns the registered provider; missing capability raises
  ``NoProviderAvailable``.
* R3 — discovery: ``supports`` / ``has_provider`` / ``list_capabilities`` (stable
  enum order) / ``list_providers`` (metadata).
* R4 — ``register`` is idempotent per instance and appends new providers.
* R5 — ``PROVIDER_REGISTRY`` singleton is a wired registry.
"""

from __future__ import annotations

import pytest

from app.application.interfaces.providers import Capability, NoProviderAvailable
from app.infrastructure.ai.providers.mocks import MockImageProvider, MockLLMProvider
from app.infrastructure.ai.providers.registry import (
    PROVIDER_REGISTRY,
    ProviderRegistry,
    default_registry,
)


def test_r1_default_registry_wires_four_mocks() -> None:
    registry = default_registry()
    assert registry.list_capabilities() == [
        Capability.LLM,
        Capability.IMAGE,
        Capability.VIDEO,
        Capability.VOICE,
    ]


def test_r2_resolve_and_missing() -> None:
    registry = ProviderRegistry()
    provider = MockImageProvider()
    registry.register(provider=provider, capabilities=[Capability.IMAGE])

    assert registry.resolve(Capability.IMAGE) is provider
    with pytest.raises(NoProviderAvailable):
        registry.resolve(Capability.VIDEO)


def test_r3_discovery() -> None:
    registry = ProviderRegistry()
    registry.register(provider=MockImageProvider(), capabilities=[Capability.IMAGE])

    assert registry.supports(Capability.IMAGE) is True
    assert registry.has_provider(Capability.IMAGE) is True
    assert registry.supports(Capability.VOICE) is False
    assert registry.list_capabilities() == [Capability.IMAGE]

    metas = registry.list_providers(Capability.IMAGE)
    assert [m.id for m in metas] == ["mock-image"]
    assert registry.list_providers(Capability.VIDEO) == []


def test_r4_register_idempotent_and_appends() -> None:
    registry = ProviderRegistry()
    provider = MockLLMProvider()
    registry.register(provider=provider, capabilities=[Capability.LLM])
    registry.register(provider=provider, capabilities=[Capability.LLM])  # same instance
    assert len(registry.list_providers(Capability.LLM)) == 1

    registry.register(provider=MockLLMProvider(), capabilities=[Capability.LLM])
    assert len(registry.list_providers(Capability.LLM)) == 2


def test_r5_singleton_is_wired() -> None:
    assert isinstance(PROVIDER_REGISTRY, ProviderRegistry)
    assert PROVIDER_REGISTRY.supports(Capability.IMAGE) is True
