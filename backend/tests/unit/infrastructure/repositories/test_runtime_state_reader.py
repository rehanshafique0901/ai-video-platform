"""α8.5e.4 — pure row → value-object mapping for the runtime-state reader (no DB).

Locks Decimal→float coercion, nullable handling, quota grouping/ordering, and that the
assembled RuntimeSnapshot drives resolver eligibility/scoring (health multiplier).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.domain.resolver import (
    Pricing,
    ProviderInfo,
    ResolveRequest,
    resolve,
)
from app.domain.resolver.models import (
    AdapterInfo,
    CatalogueSnapshot,
    ExecutableAdapters,
    ExecutionMode,
    RoutingStrategy,
)
from app.infrastructure.repositories.runtime_state_reader import (
    build_runtime_snapshot,
    group_quota,
    health_from_row,
    metrics_from_row,
    quota_from_row,
)

pytestmark = pytest.mark.unit


def test_health_from_row_coerces_decimal() -> None:
    h = health_from_row(
        {"provider_id": "fal", "health_score": Decimal("0.5000"), "error_rate": Decimal("0.1200")}
    )
    assert h.provider_id == "fal"
    assert h.health_score == pytest.approx(0.5)
    assert h.error_rate == pytest.approx(0.12)


def test_quota_from_row_null_remaining_is_unlimited() -> None:
    assert (
        quota_from_row({"provider_id": "p", "window": "daily", "remaining": None}).remaining is None
    )
    assert quota_from_row({"provider_id": "p", "window": "monthly", "remaining": 5}).remaining == 5


def test_metrics_from_row_nullable_fields() -> None:
    m = metrics_from_row(
        {"adapter_id": "a", "avg_latency_ms": 1200, "success_rate": Decimal("0.9900")}
    )
    assert m.avg_latency_ms == 1200 and m.success_rate == pytest.approx(0.99)
    m2 = metrics_from_row({"adapter_id": "b", "avg_latency_ms": None, "success_rate": None})
    assert m2.avg_latency_ms is None and m2.success_rate is None


def test_group_quota_groups_by_provider() -> None:
    rows = [
        {"provider_id": "p", "window": "daily", "remaining": 0},
        {"provider_id": "p", "window": "monthly", "remaining": 100},
        {"provider_id": "q", "window": "daily", "remaining": None},
    ]
    grouped = group_quota(rows)
    assert [s.window for s in grouped["p"]] == ["daily", "monthly"]
    assert grouped["q"][0].remaining is None


def test_build_runtime_snapshot_drives_resolver_health() -> None:
    snapshot = build_runtime_snapshot(
        health_rows=[
            {"provider_id": "poll", "health_score": Decimal("0.5"), "error_rate": Decimal("0")}
        ],
        quota_rows=[],
        metrics_rows=[],
    )
    catalogue = CatalogueSnapshot(
        catalogue_version="2026.07",
        manifest_digest="d",
        providers={
            "poll": ProviderInfo(
                id="poll",
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
                id="poll.image",
                provider_id="poll",
                capability_id="image_generation",
                execution_mode=ExecutionMode.CLOUD,
            ),
        ),
        routing={"default": RoutingStrategy.BALANCED},
    )
    res = resolve(
        ResolveRequest(capability="image_generation"),
        catalogue,
        snapshot,
        ExecutableAdapters(frozenset({"poll.image"})),
    )
    top = res.top
    assert top is not None and top.breakdown is not None
    assert top.breakdown.health_multiplier == 0.5
    assert top.score == pytest.approx(41.75)  # 83.5 * 0.5
