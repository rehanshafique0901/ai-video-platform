"""α8.5e — the resolver core (pure, deterministic).

`resolve(request, catalogue, runtime, executable)` → an ordered, explainable candidate list.
Executability is an input (ADR-0054 D1), never an import. It never
executes, never mutates the catalogue (W8.5e.2) or runtime health (W8.5e.3); given
identical inputs it returns byte-identical output (W8.5e.4) against a single immutable
snapshot (W8.5e.6). No provider-specific branching lives here (W8.5e.8) — everything is
driven by catalogue metadata + operational state + the routing strategy.
"""

from __future__ import annotations

import hashlib
import json

from app.domain.resolver.eligibility import ineligibility_reason
from app.domain.resolver.models import (
    Candidate,
    CatalogueSnapshot,
    ExecutableAdapters,
    Resolution,
    ResolveRequest,
    RuntimeSnapshot,
)
from app.domain.resolver.scoring import score
from app.domain.resolver.strategy import get_strategy

RESOLVER_VERSION = "resolver/1.0"


def _fingerprint(request: ResolveRequest) -> str:
    """Deterministic hash of the request (part of provenance; pure, no I/O)."""
    payload = {
        "capability": request.capability,
        "prompt": request.prompt,
        "duration_seconds": request.duration_seconds,
        "budget": request.budget,
        "quality": request.quality,
        "device": request.device,
        "privacy_mode": request.privacy_mode,
        "local_only": request.local_only,
        "allow_paid_providers": request.allow_paid_providers,
        "allow_commercial_terms": request.allow_commercial_terms,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def resolve(
    request: ResolveRequest,
    catalogue: CatalogueSnapshot,
    runtime: RuntimeSnapshot,
    executable: ExecutableAdapters,
    *,
    resolver_version: str = RESOLVER_VERSION,
) -> Resolution:
    routing_strategy = catalogue.strategy_for(request.capability)
    strategy = get_strategy(routing_strategy)
    weights = strategy.weights()

    # Deterministic input order (never rely on catalogue iteration order — W8.5e.4/7).
    adapters = sorted(
        (a for a in catalogue.adapters if a.capability_id == request.capability),
        key=lambda a: a.id,
    )

    eligible: list[Candidate] = []
    ineligible: list[Candidate] = []

    for adapter in adapters:
        provider = catalogue.providers.get(adapter.provider_id)
        if provider is None:
            ineligible.append(
                Candidate(
                    adapter_id=adapter.id,
                    provider_id=adapter.provider_id,
                    score=0.0,
                    eligible=False,
                    ineligible_reason="unknown_provider",
                    fallbacks=adapter.fallbacks,
                )
            )
            continue

        reason = ineligibility_reason(
            adapter, provider, request, runtime, catalogue, executable, strategy
        )
        if reason is not None:
            ineligible.append(
                Candidate(
                    adapter_id=adapter.id,
                    provider_id=adapter.provider_id,
                    score=0.0,
                    eligible=False,
                    ineligible_reason=reason,
                    fallbacks=adapter.fallbacks,
                )
            )
            continue

        breakdown = score(adapter, provider, request.device, runtime, catalogue, weights)
        eligible.append(
            Candidate(
                adapter_id=adapter.id,
                provider_id=adapter.provider_id,
                score=breakdown.final_score,
                eligible=True,
                breakdown=breakdown,
                fallbacks=adapter.fallbacks,
            )
        )

    # Explicit total-order comparator (W8.5e.7): score desc → reliability desc → id asc.
    eligible.sort(
        key=lambda c: (
            -c.score,
            -catalogue.providers[c.provider_id].score_reliability,
            c.adapter_id,
        )
    )
    ineligible.sort(key=lambda c: c.adapter_id)

    return Resolution(
        capability=request.capability,
        routing_strategy=routing_strategy,
        catalogue_version=catalogue.catalogue_version,
        manifest_digest=catalogue.manifest_digest,
        resolver_version=resolver_version,
        request_fingerprint=_fingerprint(request),
        candidates=(*eligible, *ineligible),
    )
