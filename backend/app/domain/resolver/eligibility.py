"""α8.5e — hard eligibility filters (pure).

Stage 1 of selection: a candidate is either eligible or dropped **with a reason** — a
hard constraint never becomes a score penalty (Amendment 4). Filtered candidates are
still returned (eligible=false + reason); §4.1 of the resolver contract.
"""

from __future__ import annotations

from app.domain.resolver.models import (
    AdapterInfo,
    CatalogueSnapshot,
    ExecutionMode,
    Pricing,
    ProviderInfo,
    ResolveRequest,
    RuntimeSnapshot,
)
from app.domain.resolver.strategy import ResolverStrategy

# Below this health score a provider is treated as "down" (hard filter).
HEALTH_DOWN_THRESHOLD = 0.05

_LOCAL_MODES = (ExecutionMode.LOCAL, ExecutionMode.HYBRID)


def ineligibility_reason(
    adapter: AdapterInfo,
    provider: ProviderInfo,
    request: ResolveRequest,
    runtime: RuntimeSnapshot,
    catalogue: CatalogueSnapshot,
    strategy: ResolverStrategy,
) -> str | None:
    """Return the first failing hard-constraint reason, or ``None`` if eligible."""
    if not provider.enabled:
        return "provider_disabled"
    if not adapter.enabled:
        return "adapter_disabled"

    if request.local_only and adapter.execution_mode not in _LOCAL_MODES:
        return "not_local"
    if request.privacy_mode and adapter.execution_mode == ExecutionMode.CLOUD:
        return "privacy_cloud_egress"
    if not request.commercial_allowed and provider.commercial:
        return "commercial_not_allowed"

    # Budget: 0 ⇒ free-only; >0 ⇒ paid adapter whose declared cost exceeds it is out.
    if request.budget is not None and provider.pricing == Pricing.PAID:
        if request.budget <= 0:
            return "budget_zero_paid"
        if adapter.cost_amount is not None and adapter.cost_amount > request.budget:
            return "over_budget"

    # Device hardware (local/hybrid only, and only when the device is known).
    if adapter.execution_mode in _LOCAL_MODES and request.device is not None:
        device = catalogue.devices.get(request.device)
        if (
            device is not None
            and device.ram_gb is not None
            and adapter.min_ram_gb is not None
            and adapter.min_ram_gb > device.ram_gb
        ):
            return "insufficient_hardware"

    # Operational: exhausted quota in any window.
    for quota in runtime.quota.get(provider.id, ()):
        if quota.remaining is not None and quota.remaining <= 0:
            return "quota_exhausted"

    # Operational: provider currently down.
    health = runtime.health.get(provider.id)
    if health is not None and health.health_score <= HEALTH_DOWN_THRESHOLD:
        return "health_down"

    return strategy.extra_ineligibility(adapter, provider, request)
