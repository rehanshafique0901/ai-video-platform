"""α8.5e.3 — pure row → value-object mapping for the catalogue reader (no DB).

The async query plumbing is exercised by the integration test; here we lock the mapping:
enum coercion, JSONB extraction (runtime.hardware, supports.commercial), Decimal→float
cost, execution_mode None, ordered fallback grouping, and a snapshot the resolver can use.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.domain.resolver import ExecutionMode, Pricing, ResolveRequest, RoutingStrategy, resolve
from app.domain.resolver.models import ExecutableAdapters, RuntimeSnapshot
from app.infrastructure.repositories.catalogue_reader import (
    adapter_from_row,
    build_snapshot,
    device_from_row,
    group_fallbacks,
    provider_from_row,
)

pytestmark = pytest.mark.unit


def test_provider_from_row_coerces_enum_and_scores() -> None:
    row = {
        "id": "fal",
        "pricing": "paid",
        "commercial": True,
        "score_quality": 90,
        "score_cost": 40,
        "score_speed": 85,
        "score_reliability": 88,
        "enabled": True,
    }
    p = provider_from_row(row)
    assert p.pricing is Pricing.PAID
    assert (p.score_quality, p.score_cost, p.score_speed, p.score_reliability) == (90, 40, 85, 88)
    assert p.commercial is True and p.enabled is True


def test_adapter_from_row_extracts_jsonb_and_cost() -> None:
    row = {
        "id": "comfyui.flux",
        "provider_id": "comfyui",
        "capability_id": "image_generation",
        "execution_mode": "local",
        "enabled": True,
        "implemented": False,
        "cost_amount": Decimal("0.02000000"),
        "supports": {"commercial": True, "nsfw": False},
        "runtime": {"hardware": {"minimum_ram_gb": 16, "recommended_ram_gb": 32}},
    }
    a = adapter_from_row(row, ("fal.flux",))
    assert a.execution_mode is ExecutionMode.LOCAL
    assert a.min_ram_gb == 16 and a.recommended_ram_gb == 32
    assert a.cost_amount == pytest.approx(0.02)
    assert a.supports_commercial is True
    assert a.fallbacks == ("fal.flux",)


def test_adapter_from_row_handles_nulls_and_empty_jsonb() -> None:
    row = {
        "id": "x.a",
        "provider_id": "x",
        "capability_id": "image_generation",
        "execution_mode": None,
        "enabled": False,
        "implemented": False,
        "cost_amount": None,
        "supports": {},
        "runtime": {},
    }
    a = adapter_from_row(row, ())
    assert a.execution_mode is None
    assert a.min_ram_gb is None and a.recommended_ram_gb is None
    assert a.cost_amount is None and a.supports_commercial is None
    assert a.enabled is False


def test_device_from_row() -> None:
    d = device_from_row({"id": "m1", "ram_gb": 16, "backend": "metal"})
    assert d.id == "m1" and d.ram_gb == 16 and d.backend == "metal"


def test_group_fallbacks_preserves_query_order() -> None:
    rows = [
        {"adapter_id": "a", "fallback_adapter_id": "b"},
        {"adapter_id": "a", "fallback_adapter_id": "c"},
        {"adapter_id": "z", "fallback_adapter_id": "y"},
    ]
    grouped = group_fallbacks(rows)
    assert grouped == {"a": ("b", "c"), "z": ("y",)}


def test_build_snapshot_produces_resolvable_catalogue() -> None:
    snapshot = build_snapshot(
        catalogue_version="2026.07",
        manifest_digest="abc123",
        provider_rows=[
            {
                "id": "pollinations",
                "pricing": "free",
                "commercial": True,
                "score_quality": 70,
                "score_cost": 100,
                "score_speed": 80,
                "score_reliability": 75,
                "enabled": True,
            }
        ],
        adapter_rows=[
            {
                "id": "pollinations.image",
                "provider_id": "pollinations",
                "capability_id": "image_generation",
                "execution_mode": "cloud",
                "enabled": True,
                "implemented": False,
                "cost_amount": None,
                "supports": {},
                "runtime": {},
            }
        ],
        fallback_rows=[
            {"adapter_id": "pollinations.image", "fallback_adapter_id": "fal.flux"},
        ],
        routing_rows=[{"scope": "default", "strategy": "free_first"}],
        device_rows=[{"id": "m1", "ram_gb": 16, "backend": "metal"}],
    )
    assert snapshot.catalogue_version == "2026.07"
    assert snapshot.manifest_digest == "abc123"
    assert snapshot.strategy_for("image_generation") is RoutingStrategy.FREE_FIRST
    assert snapshot.adapters[0].fallbacks == ("fal.flux",)

    res = resolve(
        ResolveRequest(capability="image_generation"),
        snapshot,
        RuntimeSnapshot(),
        ExecutableAdapters(frozenset(a.id for a in snapshot.adapters)),
    )
    top = res.top
    assert top is not None and top.adapter_id == "pollinations.image"
    assert res.routing_strategy is RoutingStrategy.FREE_FIRST
