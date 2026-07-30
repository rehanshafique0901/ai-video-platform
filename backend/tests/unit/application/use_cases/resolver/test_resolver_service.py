"""α8.5e.5 — ResolverService composition (fake readers; no DB).

Coverage: snapshots are loaded and handed to the pure resolver; an unseeded catalogue
raises CatalogueNotSeededError before any runtime read; and the deployment's executable
set reaches the resolver so this path cannot recommend an unconstructible adapter
(ADR-0054 DISP-1).
"""

from __future__ import annotations

import pytest

from app.application.interfaces.catalogue_reader import ICatalogueReader
from app.application.interfaces.image_generator import IImageAdapterRegistry, IImageGenerator
from app.application.interfaces.runtime_state_reader import IRuntimeStateReader
from app.application.use_cases.resolver.resolver_service import (
    CatalogueNotSeededError,
    ResolverService,
)
from app.domain.resolver.models import (
    AdapterInfo,
    CatalogueSnapshot,
    ExecutionMode,
    Pricing,
    ProviderInfo,
    ResolveRequest,
    RoutingStrategy,
    RuntimeSnapshot,
)

pytestmark = pytest.mark.unit


class _FakeRegistry(IImageAdapterRegistry):
    """Declares an executable set without constructing anything (never dispatched here)."""

    def __init__(self, adapter_ids: frozenset[str]) -> None:
        self._adapter_ids = adapter_ids

    def for_adapter(self, adapter_id: str) -> IImageGenerator:
        raise AssertionError("the Decision plane must never construct an adapter")

    def supported_adapters(self) -> frozenset[str]:
        return self._adapter_ids


def _registry(*adapter_ids: str) -> _FakeRegistry:
    return _FakeRegistry(frozenset(adapter_ids))


class _FakeCatalogue(ICatalogueReader):
    def __init__(self, snapshot: CatalogueSnapshot | None) -> None:
        self._snapshot = snapshot

    async def load_snapshot(self) -> CatalogueSnapshot | None:
        return self._snapshot


class _FakeRuntime(IRuntimeStateReader):
    def __init__(self, snapshot: RuntimeSnapshot) -> None:
        self._snapshot = snapshot
        self.calls = 0

    async def load_snapshot(self) -> RuntimeSnapshot:
        self.calls += 1
        return self._snapshot


def _catalogue() -> CatalogueSnapshot:
    return CatalogueSnapshot(
        catalogue_version="2026.07",
        manifest_digest="d",
        providers={
            "p": ProviderInfo(
                id="p",
                pricing=Pricing.FREE,
                commercial=True,
                score_quality=70,
                score_cost=100,
                score_speed=80,
                score_reliability=75,
            )
        },
        adapters=(
            AdapterInfo(
                id="p.image",
                provider_id="p",
                capability_id="image_generation",
                execution_mode=ExecutionMode.CLOUD,
            ),
        ),
        routing={"default": RoutingStrategy.BALANCED},
    )


async def test_resolve_composes_snapshots_into_pure_resolver() -> None:
    service = ResolverService(
        _FakeCatalogue(_catalogue()), _FakeRuntime(RuntimeSnapshot()), _registry("p.image")
    )
    res = await service.resolve(ResolveRequest(capability="image_generation"))
    assert res.catalogue_version == "2026.07"
    assert res.top is not None and res.top.adapter_id == "p.image"


async def test_resolve_raises_when_catalogue_unseeded() -> None:
    runtime = _FakeRuntime(RuntimeSnapshot())
    service = ResolverService(_FakeCatalogue(None), runtime, _registry("p.image"))
    with pytest.raises(CatalogueNotSeededError):
        await service.resolve(ResolveRequest(capability="image_generation"))
    assert runtime.calls == 0  # fails fast before reading runtime state


async def test_resolve_excludes_adapters_this_deployment_cannot_construct() -> None:
    # Second Decision-plane entry point, same DISP-1 guarantee as the generation path.
    service = ResolverService(
        _FakeCatalogue(_catalogue()), _FakeRuntime(RuntimeSnapshot()), _registry()
    )
    res = await service.resolve(ResolveRequest(capability="image_generation"))
    assert res.top is None
    assert [c.ineligible_reason for c in res.candidates] == ["not_executable"]
