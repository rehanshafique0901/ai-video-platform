"""Small in-memory catalogue/runtime snapshots for pure resolver unit tests."""

from __future__ import annotations

from app.domain.resolver.models import (
    AdapterInfo,
    AdapterMetrics,
    CatalogueSnapshot,
    DeviceProfile,
    ExecutionMode,
    Pricing,
    ProviderHealth,
    ProviderInfo,
    QuotaState,
    RoutingStrategy,
    RuntimeSnapshot,
)

CAP = "image_generation"


def provider(
    pid: str,
    *,
    pricing: Pricing = Pricing.FREE,
    commercial: bool = True,
    quality: int = 70,
    cost: int = 80,
    speed: int = 80,
    reliability: int = 75,
    enabled: bool = True,
) -> ProviderInfo:
    return ProviderInfo(
        id=pid,
        pricing=pricing,
        commercial=commercial,
        score_quality=quality,
        score_cost=cost,
        score_speed=speed,
        score_reliability=reliability,
        enabled=enabled,
    )


def adapter(
    aid: str,
    provider_id: str,
    *,
    capability: str = CAP,
    mode: ExecutionMode | None = ExecutionMode.CLOUD,
    enabled: bool = True,
    min_ram_gb: int | None = None,
    recommended_ram_gb: int | None = None,
    cost_amount: float | None = None,
    fallbacks: tuple[str, ...] = (),
) -> AdapterInfo:
    return AdapterInfo(
        id=aid,
        provider_id=provider_id,
        capability_id=capability,
        execution_mode=mode,
        enabled=enabled,
        min_ram_gb=min_ram_gb,
        recommended_ram_gb=recommended_ram_gb,
        cost_amount=cost_amount,
        fallbacks=fallbacks,
    )


def catalogue(
    *,
    providers: dict[str, ProviderInfo] | None = None,
    adapters: tuple[AdapterInfo, ...] | None = None,
    routing: dict[str, RoutingStrategy] | None = None,
    devices: dict[str, DeviceProfile] | None = None,
    version: str = "2026.07",
    digest: str = "deadbeef",
) -> CatalogueSnapshot:
    if providers is None:
        providers = {
            "pollinations": provider("pollinations", pricing=Pricing.FREE, quality=70, cost=100),
            "fal": provider("fal", pricing=Pricing.PAID, quality=90, cost=40, reliability=88),
            "comfyui": provider("comfyui", pricing=Pricing.FREE, quality=85, cost=100, speed=60),
        }
    if adapters is None:
        adapters = (
            adapter("pollinations.image", "pollinations", mode=ExecutionMode.CLOUD),
            adapter("fal.flux", "fal", mode=ExecutionMode.CLOUD, cost_amount=0.01),
            adapter(
                "comfyui.flux_schnell",
                "comfyui",
                mode=ExecutionMode.LOCAL,
                min_ram_gb=16,
                recommended_ram_gb=32,
            ),
        )
    if routing is None:
        routing = {"default": RoutingStrategy.BALANCED}
    if devices is None:
        devices = {
            "macbook_m1": DeviceProfile(id="macbook_m1", ram_gb=16, backend="metal"),
            "small_box": DeviceProfile(id="small_box", ram_gb=8, backend="cpu"),
            "workstation": DeviceProfile(id="workstation", ram_gb=64, backend="cuda"),
        }
    return CatalogueSnapshot(
        catalogue_version=version,
        manifest_digest=digest,
        providers=providers,
        adapters=adapters,
        routing=routing,
        devices=devices,
    )


def runtime(
    *,
    health: dict[str, ProviderHealth] | None = None,
    quota: dict[str, tuple[QuotaState, ...]] | None = None,
    metrics: dict[str, AdapterMetrics] | None = None,
) -> RuntimeSnapshot:
    return RuntimeSnapshot(
        health=health or {},
        quota=quota or {},
        metrics=metrics or {},
    )
