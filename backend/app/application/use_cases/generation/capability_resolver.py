"""Capability-resolver adapter — translate execution modes onto the resolver.

A thin translation layer between the generation use case (which speaks
``ExecutionConstraints`` from execution modes) and the pure resolver (which speaks
``ResolveRequest`` + score). It:

1. maps constraints -> a ``ResolveRequest`` (local_only / allow_paid),
2. runs the pure resolver against a single immutable catalogue snapshot (W8.5e.6) and the
   deployment's executable adapter set, derived from the registry port (ADR-0054 D1),
3. enriches each eligible candidate with its execution *tier* (derived from the
   adapter's execution mode + the provider's pricing — never a provider name), and
4. applies the execution-mode policy: AUTO/single-tier modes cascade to the first
   available tier in preference order; HYBRID keeps all allowed tiers in resolver
   score order.

It performs no scoring of its own and no provider execution.
"""

from __future__ import annotations

from app.application.interfaces.capability_resolver import (
    CapabilityResolution,
    ICapabilityResolver,
    ResolvedAdapter,
)
from app.application.interfaces.catalogue_reader import ICatalogueReader
from app.application.interfaces.image_generator import IImageAdapterRegistry
from app.application.interfaces.runtime_state_reader import IRuntimeStateReader
from app.application.use_cases.resolver.resolver_service import CatalogueNotSeededError
from app.domain.generation.execution import ExecutionConstraints, ExecutionTier
from app.domain.resolver import RESOLVER_VERSION, ResolveRequest, resolve as resolve_candidates
from app.domain.resolver.models import (
    AdapterInfo,
    Candidate,
    CatalogueSnapshot,
    ExecutableAdapters,
    ExecutionMode as ResolverExecutionMode,
    Pricing,
    ProviderInfo,
)


def _tier_for(adapter: AdapterInfo | None, provider: ProviderInfo | None) -> ExecutionTier | None:
    """Derive the execution tier from catalogue metadata (no provider names)."""
    if adapter is None:
        return None
    if adapter.execution_mode in (ResolverExecutionMode.LOCAL, ResolverExecutionMode.HYBRID):
        return ExecutionTier.LOCAL  # hybrid adapters can run locally -> prefer local
    if provider is not None and provider.pricing is Pricing.PAID:
        return ExecutionTier.COMMERCIAL
    return ExecutionTier.FREE_REMOTE


def _to_request(
    capability: str,
    constraints: ExecutionConstraints,
    prompt: str | None,
    budget: float | None,
) -> ResolveRequest:
    local_only = constraints.allowed == (ExecutionTier.LOCAL,)
    allow_paid = ExecutionTier.COMMERCIAL in constraints.allowed
    return ResolveRequest(
        capability=capability,
        prompt=prompt,
        budget=budget,
        local_only=local_only,
        allow_paid_providers=allow_paid,
        # Licensing (commercial terms) is orthogonal to cost tier; don't filter here.
        allow_commercial_terms=True,
    )


def _apply_constraints(
    resolved: list[ResolvedAdapter], constraints: ExecutionConstraints
) -> list[ResolvedAdapter]:
    allowed = set(constraints.allowed)
    filtered = [r for r in resolved if r.execution_tier in allowed]
    if not constraints.stop_at_first_available:
        return filtered  # HYBRID: all allowed tiers, in resolver score order
    # AUTO / single-tier: cascade to the first preferred tier that has candidates.
    for tier in constraints.preference:
        tier_candidates = [r for r in filtered if r.execution_tier is tier]
        if tier_candidates:
            return tier_candidates  # preserves resolver score order within the tier
    return []


class ResolverCapabilityResolver(ICapabilityResolver):
    def __init__(
        self,
        catalogue_reader: ICatalogueReader,
        runtime_reader: IRuntimeStateReader,
        adapter_registry: IImageAdapterRegistry,
    ) -> None:
        self._catalogue = catalogue_reader
        self._runtime = runtime_reader
        self._adapters = adapter_registry

    async def resolve(
        self,
        *,
        capability: str,
        constraints: ExecutionConstraints,
        prompt: str | None = None,
        budget: float | None = None,
    ) -> CapabilityResolution:
        snapshot = await self._catalogue.load_snapshot()
        if snapshot is None:
            raise CatalogueNotSeededError(
                "provider catalogue is not seeded; run scripts/seed_providers.py"
            )
        runtime = await self._runtime.load_snapshot()
        # ADR-0054 D1: what this deployment can construct is a resolver *input*, so the
        # Decision plane can never name an adapter Execution would fail to build.
        executable = ExecutableAdapters(adapter_ids=self._adapters.supported_adapters())
        resolution = resolve_candidates(
            _to_request(capability, constraints, prompt, budget),
            snapshot,
            runtime,
            executable,
            resolver_version=RESOLVER_VERSION,
        )

        resolved = _enrich(resolution.eligible, snapshot)
        candidates = _apply_constraints(resolved, constraints)
        return CapabilityResolution(
            capability=capability,
            resolver_version=resolution.resolver_version,
            candidates=tuple(candidates),
            catalogue_version=resolution.catalogue_version,
            manifest_digest=resolution.manifest_digest,
            resolution=resolution,
        )


def _enrich(eligible: tuple[Candidate, ...], snapshot: CatalogueSnapshot) -> list[ResolvedAdapter]:
    adapters_by_id = {a.id: a for a in snapshot.adapters}
    resolved: list[ResolvedAdapter] = []
    for candidate in eligible:
        adapter = adapters_by_id.get(candidate.adapter_id)
        provider = snapshot.providers.get(candidate.provider_id)
        resolved.append(
            ResolvedAdapter(
                adapter_id=candidate.adapter_id,
                provider_id=candidate.provider_id,
                score=candidate.score,
                execution_tier=_tier_for(adapter, provider),
                model_ref=None,  # AdapterInfo carries no model ref yet (Increment 6)
            )
        )
    return resolved
