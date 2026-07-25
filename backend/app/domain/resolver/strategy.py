"""α8.5e — resolver strategies (pure).

A ``ResolverStrategy`` encapsulates a routing mode as a **weight vector** (§4.3) plus an
optional strategy-specific eligibility filter (e.g. ``offline_only`` excludes cloud).
Scoring itself stays uniform and explainable; adding a new routing mode is a new small
class + one registry entry — no changes to the scorer or the resolver core.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from app.domain.resolver.models import (
    AdapterInfo,
    ExecutionMode,
    Pricing,
    ProviderInfo,
    ResolveRequest,
    RoutingStrategy,
)


@dataclass(frozen=True, slots=True)
class Weights:
    """Component weights; sum to 1.0 so a raw score stays in [0, 100]."""

    quality: float
    cost: float
    speed: float
    reliability: float
    hardware: float


class ResolverStrategy(ABC):
    """Base routing strategy: a weight vector + an optional extra filter."""

    strategy: RoutingStrategy

    @abstractmethod
    def weights(self) -> Weights: ...

    def extra_ineligibility(
        self, adapter: AdapterInfo, provider: ProviderInfo, request: ResolveRequest
    ) -> str | None:
        """Strategy-specific hard filter; ``None`` ⇒ no additional constraint."""
        return None


# --------------------------------------------------------------------------- #
# Weight vectors
# --------------------------------------------------------------------------- #
_BALANCED = Weights(quality=0.25, cost=0.25, speed=0.20, reliability=0.20, hardware=0.10)
_FREE_FIRST = Weights(quality=0.20, cost=0.50, speed=0.10, reliability=0.10, hardware=0.10)
_LOWEST_COST = Weights(quality=0.10, cost=0.60, speed=0.10, reliability=0.15, hardware=0.05)
_HIGHEST_QUALITY = Weights(quality=0.55, cost=0.05, speed=0.10, reliability=0.20, hardware=0.10)
_FASTEST = Weights(quality=0.15, cost=0.10, speed=0.50, reliability=0.20, hardware=0.05)
_PRIVACY_FIRST = Weights(quality=0.30, cost=0.10, speed=0.20, reliability=0.30, hardware=0.10)


class BalancedStrategy(ResolverStrategy):
    strategy = RoutingStrategy.BALANCED

    def weights(self) -> Weights:
        return _BALANCED


class FreeFirstStrategy(ResolverStrategy):
    strategy = RoutingStrategy.FREE_FIRST

    def weights(self) -> Weights:
        return _FREE_FIRST


class LowestCostStrategy(ResolverStrategy):
    strategy = RoutingStrategy.LOWEST_COST

    def weights(self) -> Weights:
        return _LOWEST_COST


class HighestQualityStrategy(ResolverStrategy):
    strategy = RoutingStrategy.HIGHEST_QUALITY

    def weights(self) -> Weights:
        return _HIGHEST_QUALITY


class FastestStrategy(ResolverStrategy):
    strategy = RoutingStrategy.FASTEST

    def weights(self) -> Weights:
        return _FASTEST


class PrivacyFirstStrategy(ResolverStrategy):
    strategy = RoutingStrategy.PRIVACY_FIRST

    def weights(self) -> Weights:
        return _PRIVACY_FIRST

    def extra_ineligibility(
        self, adapter: AdapterInfo, provider: ProviderInfo, request: ResolveRequest
    ) -> str | None:
        return "privacy_first_cloud" if adapter.execution_mode == ExecutionMode.CLOUD else None


class OfflineOnlyStrategy(ResolverStrategy):
    strategy = RoutingStrategy.OFFLINE_ONLY

    def weights(self) -> Weights:
        return _BALANCED

    def extra_ineligibility(
        self, adapter: AdapterInfo, provider: ProviderInfo, request: ResolveRequest
    ) -> str | None:
        local = adapter.execution_mode in (ExecutionMode.LOCAL, ExecutionMode.HYBRID)
        return None if local else "offline_only_non_local"


class CommercialOnlyStrategy(ResolverStrategy):
    strategy = RoutingStrategy.COMMERCIAL_ONLY

    def weights(self) -> Weights:
        return _BALANCED

    def extra_ineligibility(
        self, adapter: AdapterInfo, provider: ProviderInfo, request: ResolveRequest
    ) -> str | None:
        return None if provider.commercial else "commercial_only_non_commercial"


class FreeOnlyStrategy(ResolverStrategy):
    strategy = RoutingStrategy.FREE_ONLY

    def weights(self) -> Weights:
        return _FREE_FIRST

    def extra_ineligibility(
        self, adapter: AdapterInfo, provider: ProviderInfo, request: ResolveRequest
    ) -> str | None:
        free = provider.pricing in (Pricing.FREE, Pricing.FREEMIUM)
        return None if free else "free_only_paid"


_STRATEGIES: dict[RoutingStrategy, ResolverStrategy] = {
    s.strategy: s
    for s in (
        BalancedStrategy(),
        FreeFirstStrategy(),
        LowestCostStrategy(),
        HighestQualityStrategy(),
        FastestStrategy(),
        PrivacyFirstStrategy(),
        OfflineOnlyStrategy(),
        CommercialOnlyStrategy(),
        FreeOnlyStrategy(),
    )
}


def get_strategy(strategy: RoutingStrategy) -> ResolverStrategy:
    """Return the strategy implementation, defaulting to Balanced if unmapped."""
    return _STRATEGIES.get(strategy, _STRATEGIES[RoutingStrategy.BALANCED])
