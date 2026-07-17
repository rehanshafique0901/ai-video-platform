"""Provider registry — explicit registration + capability discovery (Slice α7.4).

A framework-free catalogue that maps a :class:`Capability` to the registered
providers serving it. Registration is **explicit** (``register(...)``, no
decorators — those become sugar in α8+). ``resolve`` returns the provider for a
capability or raises :class:`NoProviderAvailable`; per the α7.4 sign-off (Q4)
there is **no fallback / weighting / priority / health-ordering** yet — with a
single provider per capability, resolution is a direct lookup.

The discovery surface (``list_capabilities`` / ``list_providers`` / ``has_provider``
/ ``supports``) lets α7.6 pipelines stay generic — asking
``registry.supports(Capability.IMAGE)`` instead of hard-coding ``if image:``.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.application.interfaces.providers import (
    Capability,
    NoProviderAvailable,
    ProviderMetadata,
)
from app.infrastructure.ai.providers.mocks import (
    MockImageProvider,
    MockLLMProvider,
    MockVideoProvider,
    MockVoiceProvider,
)
from app.infrastructure.ai.providers.ports import Provider


class ProviderRegistry:
    """In-memory catalogue of providers keyed by capability."""

    def __init__(self) -> None:
        self._by_capability: dict[Capability, list[Provider]] = {}

    def register(self, *, provider: Provider, capabilities: Sequence[Capability]) -> None:
        """Register ``provider`` under each of ``capabilities`` (idempotent per instance)."""
        for capability in capabilities:
            bucket = self._by_capability.setdefault(capability, [])
            if provider not in bucket:
                bucket.append(provider)

    def resolve(self, capability: Capability) -> Provider:
        """Return the provider serving ``capability`` or raise :class:`NoProviderAvailable`.

        With one provider per capability in α7.4 this is a direct lookup; when
        multiple real providers exist, selection precedence is layered on here
        without changing callers.
        """
        providers = self._by_capability.get(capability, [])
        if not providers:
            raise NoProviderAvailable(f"no provider registered for capability {capability.value!r}")
        return providers[0]

    # -- discovery ---------------------------------------------------------- #

    def supports(self, capability: Capability) -> bool:
        """Whether at least one provider is registered for ``capability``."""
        return bool(self._by_capability.get(capability))

    def has_provider(self, capability: Capability) -> bool:
        """Alias of :meth:`supports` (reads naturally at call sites)."""
        return self.supports(capability)

    def list_capabilities(self) -> list[Capability]:
        """The capabilities currently served by a registered provider (stable order)."""
        return [c for c in Capability if self._by_capability.get(c)]

    def list_providers(self, capability: Capability) -> list[ProviderMetadata]:
        """Metadata for every provider registered under ``capability``."""
        return [p.metadata for p in self._by_capability.get(capability, [])]


def default_registry() -> ProviderRegistry:
    """A fresh registry wired with the four deterministic mocks (one per capability)."""
    registry = ProviderRegistry()
    registry.register(provider=MockLLMProvider(), capabilities=[Capability.LLM])
    registry.register(provider=MockImageProvider(), capabilities=[Capability.IMAGE])
    registry.register(provider=MockVideoProvider(), capabilities=[Capability.VIDEO])
    registry.register(provider=MockVoiceProvider(), capabilities=[Capability.VOICE])
    return registry


PROVIDER_REGISTRY = default_registry()
