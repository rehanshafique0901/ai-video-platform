"""α8.5e — soft scoring (pure, explainable).

Stage 2 of selection: rank the *eligible* survivors with a transparent weighted sum of
named components, then a health multiplier (§4.2). Every contribution is returned in a
structured ``ScoreBreakdown`` — no opaque ranking (W8.5e.5).
"""

from __future__ import annotations

from app.domain.resolver.models import (
    AdapterInfo,
    CatalogueSnapshot,
    ExecutionMode,
    Pricing,
    ProviderInfo,
    RuntimeSnapshot,
    ScoreBreakdown,
)
from app.domain.resolver.strategy import Weights

# A local adapter whose device meets *recommended* RAM scores full; only *minimum*
# scores partial. Cloud/unknown-requirement adapters are hardware-neutral.
_HARDWARE_FULL = 100.0
_HARDWARE_MIN_ONLY = 70.0
_MAX_LATENCY_PENALTY = 20.0
_LOCAL_MODES = (ExecutionMode.LOCAL, ExecutionMode.HYBRID)


def _cost_fit(provider: ProviderInfo, adapter: AdapterInfo) -> float:
    if provider.pricing in (Pricing.FREE, Pricing.FREEMIUM):
        return 100.0
    if adapter.cost_amount is not None and adapter.cost_amount == 0:
        return 100.0
    return float(provider.score_cost)


def _speed(provider: ProviderInfo, runtime: RuntimeSnapshot, adapter_id: str) -> float:
    speed = float(provider.score_speed)
    metrics = runtime.metrics.get(adapter_id)
    if metrics is not None and metrics.avg_latency_ms is not None:
        penalty = min(_MAX_LATENCY_PENALTY, metrics.avg_latency_ms / 1000.0)
        speed = max(0.0, speed - penalty)
    return speed


def _hardware_fit(
    adapter: AdapterInfo, catalogue: CatalogueSnapshot, device_id: str | None
) -> float:
    if adapter.execution_mode not in _LOCAL_MODES:
        return _HARDWARE_FULL
    if device_id is None:
        return _HARDWARE_FULL
    device = catalogue.devices.get(device_id)
    if device is None or device.ram_gb is None:
        return _HARDWARE_FULL
    if adapter.recommended_ram_gb is not None and device.ram_gb >= adapter.recommended_ram_gb:
        return _HARDWARE_FULL
    if adapter.min_ram_gb is not None and device.ram_gb >= adapter.min_ram_gb:
        return _HARDWARE_MIN_ONLY
    return _HARDWARE_FULL  # no declared requirements ⇒ neutral


def _health_multiplier(provider_id: str, runtime: RuntimeSnapshot) -> float:
    health = runtime.health.get(provider_id)
    return health.health_score if health is not None else 1.0


def score(
    adapter: AdapterInfo,
    provider: ProviderInfo,
    request_device: str | None,
    runtime: RuntimeSnapshot,
    catalogue: CatalogueSnapshot,
    weights: Weights,
) -> ScoreBreakdown:
    quality = float(provider.score_quality)
    cost = _cost_fit(provider, adapter)
    speed = _speed(provider, runtime, adapter.id)
    reliability = float(provider.score_reliability)
    hardware = _hardware_fit(adapter, catalogue, request_device)
    health_multiplier = _health_multiplier(provider.id, runtime)

    raw = (
        weights.quality * quality
        + weights.cost * cost
        + weights.speed * speed
        + weights.reliability * reliability
        + weights.hardware * hardware
    )
    final = round(raw * health_multiplier, 2)
    return ScoreBreakdown(
        quality=quality,
        cost=cost,
        speed=speed,
        reliability=reliability,
        hardware=hardware,
        health_multiplier=round(health_multiplier, 4),
        final_score=final,
    )
