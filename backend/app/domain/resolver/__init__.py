"""α8.5e — provider resolver (Decision plane, pure).

`resolve(request, catalogue_snapshot, runtime_snapshot, executable_adapters)` turns a
capability request into an ordered, explainable candidate list. Pure and deterministic: no
DB, no I/O, no provider execution. Catalogue/runtime readers (α8.5e.3/.4) build the
snapshots and the composition root declares what the deployment can construct (ADR-0054
D1); execution (α8.5e.5) consumes the candidates. Governed by
`RESOLVER_RUNTIME_CONTRACT.md`.
"""

from __future__ import annotations

from app.domain.resolver.models import (
    SCORE_SCHEMA_VERSION,
    AdapterInfo,
    AdapterMetrics,
    Candidate,
    CatalogueSnapshot,
    DeviceProfile,
    ExecutableAdapters,
    ExecutionMode,
    Pricing,
    ProviderHealth,
    ProviderInfo,
    QuotaState,
    Resolution,
    ResolveRequest,
    RoutingStrategy,
    RuntimeSnapshot,
    ScoreBreakdown,
)
from app.domain.resolver.service import RESOLVER_VERSION, resolve
from app.domain.resolver.strategy import ResolverStrategy, Weights, get_strategy

__all__ = [
    "RESOLVER_VERSION",
    "SCORE_SCHEMA_VERSION",
    "AdapterInfo",
    "AdapterMetrics",
    "Candidate",
    "CatalogueSnapshot",
    "DeviceProfile",
    "ExecutableAdapters",
    "ExecutionMode",
    "Pricing",
    "ProviderHealth",
    "ProviderInfo",
    "QuotaState",
    "ResolveRequest",
    "Resolution",
    "ResolverStrategy",
    "RoutingStrategy",
    "RuntimeSnapshot",
    "ScoreBreakdown",
    "Weights",
    "get_strategy",
    "resolve",
]
