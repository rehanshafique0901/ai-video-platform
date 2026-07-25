"""Port: capability resolver — ask for the best adapters for a *capability*.

The generation use case is capability-first (ADR-0045): it never names a provider.
It asks this port "who can do ``image_generation`` under these execution
constraints?" and receives an ordered, eligible candidate list plus the catalogue
provenance needed to make the request reproducible. The concrete implementation
wraps the pure resolver + catalogue/runtime readers; tests use a fake.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from app.domain.generation.execution import ExecutionConstraints, ExecutionTier
from app.domain.resolver import Resolution


@dataclass(frozen=True, slots=True)
class ResolvedAdapter:
    """One eligible adapter for a capability, best-first by resolver score.

    ``execution_tier`` / ``model_ref`` let the use case drive the Model Cache seam
    for local execution; both are ``None`` for remote adapters.
    """

    adapter_id: str
    provider_id: str
    score: float
    execution_tier: ExecutionTier | None = None
    model_ref: str | None = None


@dataclass(frozen=True, slots=True)
class CapabilityResolution:
    """Ordered eligible candidates for a capability + catalogue provenance."""

    capability: str
    resolver_version: str
    candidates: tuple[ResolvedAdapter, ...]
    catalogue_version: str | None = None
    manifest_digest: str | None = None
    # The underlying pure-resolver decision, carried for the Execution-plane
    # resolution ledger (W8.5e.5 — full ranked list incl. filtered candidates).
    # ``None`` when a fake/simple resolver is used (e.g. in unit tests).
    resolution: Resolution | None = None

    @property
    def top(self) -> ResolvedAdapter | None:
        return self.candidates[0] if self.candidates else None


class ICapabilityResolver(ABC):
    """Resolve a capability to an ordered list of eligible adapters."""

    @abstractmethod
    async def resolve(
        self,
        *,
        capability: str,
        constraints: ExecutionConstraints,
        prompt: str | None = None,
        budget: float | None = None,
    ) -> CapabilityResolution:
        """Return eligible candidates (best-first) for ``capability``.

        ``constraints`` express the execution mode (which tiers are allowed);
        the implementation maps them onto resolver eligibility flags. An empty
        candidate list means nothing eligible could satisfy the request.
        """
        ...
