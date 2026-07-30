"""Unit tests for the capability-resolver adapter (fake readers; no DB)."""

from __future__ import annotations

import pytest

from app.application.interfaces.catalogue_reader import ICatalogueReader
from app.application.interfaces.runtime_state_reader import IRuntimeStateReader
from app.application.use_cases.generation.capability_resolver import ResolverCapabilityResolver
from app.application.use_cases.resolver.resolver_service import CatalogueNotSeededError
from app.domain.generation.execution import ExecutionMode, ExecutionTier, constraints_for
from app.domain.resolver.models import (
    AdapterInfo,
    CatalogueSnapshot,
    ExecutionMode as ResolverExecutionMode,
    Pricing,
    ProviderInfo,
    RoutingStrategy,
    RuntimeSnapshot,
)

from ._fakes import FakeAdapterRegistry, FakeImageGenerator

pytestmark = pytest.mark.unit

CAP = "image_generation"


class _FakeCatalogue(ICatalogueReader):
    def __init__(self, snapshot: CatalogueSnapshot | None) -> None:
        self._snapshot = snapshot

    async def load_snapshot(self) -> CatalogueSnapshot | None:
        return self._snapshot


class _FakeRuntime(IRuntimeStateReader):
    async def load_snapshot(self) -> RuntimeSnapshot:
        return RuntimeSnapshot()


def _provider(pid: str, pricing: Pricing) -> ProviderInfo:
    return ProviderInfo(
        id=pid,
        pricing=pricing,
        commercial=True,
        score_quality=70,
        score_cost=80,
        score_speed=80,
        score_reliability=80,
    )


def _catalogue() -> CatalogueSnapshot:
    return CatalogueSnapshot(
        catalogue_version="2026.07.25",
        manifest_digest="digest123",
        providers={
            "localp": _provider("localp", Pricing.FREE),
            "freep": _provider("freep", Pricing.FREE),
            "paidp": _provider("paidp", Pricing.PAID),
        },
        adapters=(
            AdapterInfo(
                id="local.img",
                provider_id="localp",
                capability_id=CAP,
                execution_mode=ResolverExecutionMode.LOCAL,
            ),
            AdapterInfo(
                id="free.img",
                provider_id="freep",
                capability_id=CAP,
                execution_mode=ResolverExecutionMode.CLOUD,
            ),
            AdapterInfo(
                id="paid.img",
                provider_id="paidp",
                capability_id=CAP,
                execution_mode=ResolverExecutionMode.CLOUD,
            ),
        ),
        routing={"default": RoutingStrategy.BALANCED},
    )


def _registry(*adapter_ids: str) -> FakeAdapterRegistry:
    """A deployment that can construct the given ids (all three by default)."""
    ids = adapter_ids or ("local.img", "free.img", "paid.img")
    return FakeAdapterRegistry({aid: FakeImageGenerator() for aid in ids})


def _resolver(registry: FakeAdapterRegistry | None = None) -> ResolverCapabilityResolver:
    return ResolverCapabilityResolver(
        _FakeCatalogue(_catalogue()), _FakeRuntime(), registry or _registry()
    )


async def test_auto_cascades_to_local_only() -> None:
    res = await _resolver().resolve(capability=CAP, constraints=constraints_for(ExecutionMode.AUTO))
    assert [c.adapter_id for c in res.candidates] == ["local.img"]
    assert res.top is not None and res.top.execution_tier is ExecutionTier.LOCAL


async def test_free_remote_only_excludes_local_and_paid() -> None:
    res = await _resolver().resolve(
        capability=CAP, constraints=constraints_for(ExecutionMode.FREE_REMOTE_ONLY)
    )
    adapters = {c.adapter_id for c in res.candidates}
    assert adapters == {"free.img"}
    assert all(c.execution_tier is ExecutionTier.FREE_REMOTE for c in res.candidates)


async def test_commercial_only_returns_paid() -> None:
    res = await _resolver().resolve(
        capability=CAP, constraints=constraints_for(ExecutionMode.COMMERCIAL_ONLY)
    )
    assert {c.adapter_id for c in res.candidates} == {"paid.img"}
    assert all(c.execution_tier is ExecutionTier.COMMERCIAL for c in res.candidates)


async def test_local_only_returns_local() -> None:
    res = await _resolver().resolve(
        capability=CAP, constraints=constraints_for(ExecutionMode.LOCAL_ONLY)
    )
    assert {c.adapter_id for c in res.candidates} == {"local.img"}


async def test_hybrid_keeps_all_tiers() -> None:
    res = await _resolver().resolve(
        capability=CAP, constraints=constraints_for(ExecutionMode.HYBRID)
    )
    adapters = {c.adapter_id for c in res.candidates}
    assert adapters == {"local.img", "free.img", "paid.img"}


async def test_provenance_is_populated() -> None:
    res = await _resolver().resolve(capability=CAP, constraints=constraints_for(ExecutionMode.AUTO))
    assert res.capability == CAP
    assert res.catalogue_version == "2026.07.25"
    assert res.manifest_digest == "digest123"
    assert res.resolver_version


async def test_unseeded_catalogue_raises() -> None:
    resolver = ResolverCapabilityResolver(_FakeCatalogue(None), _FakeRuntime(), _registry())
    with pytest.raises(CatalogueNotSeededError):
        await resolver.resolve(capability=CAP, constraints=constraints_for(ExecutionMode.AUTO))


async def test_only_executable_adapters_are_recommended() -> None:
    # ADR-0054 DISP-1: the tier cascade would prefer local.img, but this deployment cannot
    # construct it, so the decision must fall to a tier it can actually run.
    res = await _resolver(_registry("free.img")).resolve(
        capability=CAP, constraints=constraints_for(ExecutionMode.AUTO)
    )
    assert [c.adapter_id for c in res.candidates] == ["free.img"]
    assert res.top is not None and res.top.execution_tier is ExecutionTier.FREE_REMOTE


async def test_non_executable_adapters_are_explained_in_the_candidate_list() -> None:
    # The exclusion is recorded rather than silent — this is what lets the ledger explain
    # the decision without persisting the executable set (ADR-0054 D1).
    res = await _resolver(_registry("free.img")).resolve(
        capability=CAP, constraints=constraints_for(ExecutionMode.HYBRID)
    )
    assert res.resolution is not None
    excluded = {
        c.adapter_id: c.ineligible_reason for c in res.resolution.candidates if not c.eligible
    }
    assert excluded == {"local.img": "not_executable", "paid.img": "not_executable"}


async def test_a_deployment_that_can_construct_nothing_recommends_nothing() -> None:
    res = await _resolver(FakeAdapterRegistry({})).resolve(
        capability=CAP, constraints=constraints_for(ExecutionMode.AUTO)
    )
    assert res.candidates == ()
    assert res.top is None
