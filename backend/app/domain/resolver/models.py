"""α8.5e — resolver domain value objects (pure; no DB, no I/O).

These are domain-local types (import-linter forbids `app.domain` reaching into
`app.application`/`app.infrastructure`/scripts). Wire-string enum values mirror the
α8.5d catalogue (migration `0010`) and the design-time manifest so the pure core stays
aligned with the DB without importing tooling.

See `docs/engineering/RESOLVER_RUNTIME_CONTRACT.md` (§1–4) for the governing contract.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

# --------------------------------------------------------------------------- #
# Enums (mirror the α8.5d catalogue / manifest wire strings)
# --------------------------------------------------------------------------- #


class RoutingStrategy(StrEnum):
    """Routing strategy = a weight vector (+ optional strategy filter). Mirrors the
    `routing_strategy` Postgres enum / manifest `RoutingStrategy`."""

    FREE_FIRST = "free_first"
    LOWEST_COST = "lowest_cost"
    HIGHEST_QUALITY = "highest_quality"
    FASTEST = "fastest"
    BALANCED = "balanced"
    OFFLINE_ONLY = "offline_only"
    PRIVACY_FIRST = "privacy_first"
    COMMERCIAL_ONLY = "commercial_only"
    FREE_ONLY = "free_only"


class ExecutionMode(StrEnum):
    """Mirrors `adapter_execution_mode`."""

    LOCAL = "local"
    CLOUD = "cloud"
    HYBRID = "hybrid"


class Pricing(StrEnum):
    """Mirrors `provider_pricing`. FREE + FREEMIUM are "free-capable"."""

    FREE = "free"
    FREEMIUM = "freemium"
    PAID = "paid"


# --------------------------------------------------------------------------- #
# Static — catalogue snapshot (§A.1 projection the resolver needs)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ProviderInfo:
    id: str
    pricing: Pricing
    commercial: bool
    score_quality: int
    score_cost: int
    score_speed: int
    score_reliability: int
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class AdapterInfo:
    id: str
    provider_id: str
    capability_id: str
    execution_mode: ExecutionMode | None = None
    enabled: bool = True
    implemented: bool = False
    min_ram_gb: int | None = None
    recommended_ram_gb: int | None = None
    cost_amount: float | None = None
    supports_commercial: bool | None = None
    fallbacks: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DeviceProfile:
    id: str
    ram_gb: int | None = None
    backend: str | None = None


@dataclass(frozen=True, slots=True)
class CatalogueSnapshot:
    """A single immutable catalogue view for one resolution (W8.5e.6)."""

    catalogue_version: str
    manifest_digest: str
    providers: Mapping[str, ProviderInfo]
    adapters: tuple[AdapterInfo, ...]
    routing: Mapping[str, RoutingStrategy]  # scope (capability | "default") -> strategy
    devices: Mapping[str, DeviceProfile] = field(default_factory=dict)

    def strategy_for(self, capability: str) -> RoutingStrategy:
        """Per-capability routing, falling back to the ``default`` scope then BALANCED."""
        return (
            self.routing.get(capability) or self.routing.get("default") or RoutingStrategy.BALANCED
        )


# --------------------------------------------------------------------------- #
# Operational — runtime snapshot (§A.2; read-only to the resolver, W8.5e.3)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ProviderHealth:
    provider_id: str
    health_score: float = 1.0  # [0,1] multiplier
    error_rate: float = 0.0


@dataclass(frozen=True, slots=True)
class QuotaState:
    provider_id: str
    window: str  # "daily" | "monthly"
    remaining: int | None  # None ⇒ unlimited


@dataclass(frozen=True, slots=True)
class AdapterMetrics:
    adapter_id: str
    avg_latency_ms: int | None = None
    success_rate: float | None = None


@dataclass(frozen=True, slots=True)
class RuntimeSnapshot:
    health: Mapping[str, ProviderHealth] = field(default_factory=dict)
    quota: Mapping[str, tuple[QuotaState, ...]] = field(default_factory=dict)  # provider_id ->
    metrics: Mapping[str, AdapterMetrics] = field(default_factory=dict)  # adapter_id ->


# --------------------------------------------------------------------------- #
# Request — the current generation
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ResolveRequest:
    capability: str
    prompt: str | None = None
    duration_seconds: float | None = None
    budget: float | None = None  # None ⇒ unconstrained; 0 ⇒ free-only
    quality: str | None = None
    device: str | None = None
    privacy_mode: bool = False
    local_only: bool = False
    # Cost and licensing are orthogonal (a provider can be free-but-commercial,
    # paid-but-open, freemium, locally hosted, or enterprise licensed), so they are
    # two independent gates rather than one flag.
    allow_paid_providers: bool = True  # False ⇒ pricing == paid is ineligible
    allow_commercial_terms: bool = True  # False ⇒ commercial-licensed providers ineligible


# --------------------------------------------------------------------------- #
# Output — ordered candidates + explainable score (W8.5e.5)
# --------------------------------------------------------------------------- #


# Bump when the ScoreBreakdown wire shape changes, so historical ledger rows /
# analytics remain interpretable across scoring evolutions (replay-safe).
SCORE_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class ScoreBreakdown:
    """Machine-readable decomposition of a candidate's score (W8.5e.5).

    Serialised with an explicit ``score_schema`` version so scoring can evolve without
    breaking historical replay or analytics.
    """

    quality: float
    cost: float
    speed: float
    reliability: float
    hardware: float
    health_multiplier: float
    final_score: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "score_schema": SCORE_SCHEMA_VERSION,
            "components": {
                "quality": self.quality,
                "cost": self.cost,
                "speed": self.speed,
                "reliability": self.reliability,
                "hardware": self.hardware,
            },
            "health_multiplier": self.health_multiplier,
            "final_score": self.final_score,
        }


@dataclass(frozen=True, slots=True)
class Candidate:
    adapter_id: str
    provider_id: str
    score: float
    eligible: bool
    ineligible_reason: str | None = None
    breakdown: ScoreBreakdown | None = None
    fallbacks: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Resolution:
    capability: str
    routing_strategy: RoutingStrategy
    catalogue_version: str
    manifest_digest: str
    resolver_version: str
    request_fingerprint: str
    candidates: tuple[Candidate, ...]

    @property
    def eligible(self) -> tuple[Candidate, ...]:
        return tuple(c for c in self.candidates if c.eligible)

    @property
    def top(self) -> Candidate | None:
        eligible = self.eligible
        return eligible[0] if eligible else None
