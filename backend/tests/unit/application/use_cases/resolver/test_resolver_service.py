"""α8.5e.5 — ResolverService composition (fake readers; no DB).

Coverage: snapshots are loaded and handed to the pure resolver; an unseeded catalogue
raises CatalogueNotSeededError before any runtime read.
"""

from __future__ import annotations

import pytest

from app.application.interfaces.catalogue_reader import ICatalogueReader
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
    service = ResolverService(_FakeCatalogue(_catalogue()), _FakeRuntime(RuntimeSnapshot()))
    res = await service.resolve(ResolveRequest(capability="image_generation"))
    assert res.catalogue_version == "2026.07"
    assert res.top is not None and res.top.adapter_id == "p.image"


async def test_resolve_raises_when_catalogue_unseeded() -> None:
    runtime = _FakeRuntime(RuntimeSnapshot())
    service = ResolverService(_FakeCatalogue(None), runtime)
    with pytest.raises(CatalogueNotSeededError):
        await service.resolve(ResolveRequest(capability="image_generation"))
    assert runtime.calls == 0  # fails fast before reading runtime state
