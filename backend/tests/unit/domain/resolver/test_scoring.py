"""α8.5e.2 — scoring component math (pure).

Coverage: cost_fit (free vs paid vs zero-cost), latency penalty on speed, hardware-fit
tiers (recommended / minimum / neutral), health multiplier, and the exact weighted sum.
"""

from __future__ import annotations

import pytest

from app.domain.resolver.models import (
    AdapterMetrics,
    ExecutionMode,
    Pricing,
    ProviderHealth,
)
from app.domain.resolver.scoring import score
from app.domain.resolver.strategy import Weights

from . import _fakes as f

pytestmark = pytest.mark.unit

_BALANCED = Weights(quality=0.25, cost=0.25, speed=0.20, reliability=0.20, hardware=0.10)


def test_cost_fit_free_is_full() -> None:
    prov = f.provider("p", pricing=Pricing.FREE, cost=10)
    adp = f.adapter("p.a", "p", mode=ExecutionMode.CLOUD)
    bd = score(adp, prov, None, f.runtime(), f.catalogue(), _BALANCED)
    assert bd.cost == 100.0


def test_cost_fit_paid_uses_score_cost() -> None:
    prov = f.provider("p", pricing=Pricing.PAID, cost=40)
    adp = f.adapter("p.a", "p", mode=ExecutionMode.CLOUD, cost_amount=0.02)
    bd = score(adp, prov, None, f.runtime(), f.catalogue(), _BALANCED)
    assert bd.cost == 40.0


def test_cost_fit_zero_cost_paid_is_full() -> None:
    prov = f.provider("p", pricing=Pricing.PAID, cost=40)
    adp = f.adapter("p.a", "p", mode=ExecutionMode.CLOUD, cost_amount=0)
    bd = score(adp, prov, None, f.runtime(), f.catalogue(), _BALANCED)
    assert bd.cost == 100.0


def test_speed_latency_penalty() -> None:
    prov = f.provider("p", speed=90)
    adp = f.adapter("p.a", "p", mode=ExecutionMode.CLOUD)
    rt = f.runtime(metrics={"p.a": AdapterMetrics(adapter_id="p.a", avg_latency_ms=5000)})
    bd = score(adp, prov, None, rt, f.catalogue(), _BALANCED)
    assert bd.speed == 85.0  # 90 - min(20, 5000/1000=5)


def test_speed_latency_penalty_capped() -> None:
    prov = f.provider("p", speed=90)
    adp = f.adapter("p.a", "p", mode=ExecutionMode.CLOUD)
    rt = f.runtime(metrics={"p.a": AdapterMetrics(adapter_id="p.a", avg_latency_ms=999999)})
    bd = score(adp, prov, None, rt, f.catalogue(), _BALANCED)
    assert bd.speed == 70.0  # capped at -20


def test_hardware_fit_recommended_full() -> None:
    prov = f.provider("p")
    adp = f.adapter("p.a", "p", mode=ExecutionMode.LOCAL, min_ram_gb=16, recommended_ram_gb=32)
    bd = score(adp, prov, "workstation", f.runtime(), f.catalogue(), _BALANCED)  # 64GB
    assert bd.hardware == 100.0


def test_hardware_fit_minimum_only_partial() -> None:
    prov = f.provider("p")
    adp = f.adapter("p.a", "p", mode=ExecutionMode.LOCAL, min_ram_gb=16, recommended_ram_gb=32)
    bd = score(adp, prov, "macbook_m1", f.runtime(), f.catalogue(), _BALANCED)  # 16GB
    assert bd.hardware == 70.0


def test_hardware_fit_cloud_is_neutral() -> None:
    prov = f.provider("p")
    adp = f.adapter("p.a", "p", mode=ExecutionMode.CLOUD)
    bd = score(adp, prov, "small_box", f.runtime(), f.catalogue(), _BALANCED)
    assert bd.hardware == 100.0


def test_health_multiplier_applied() -> None:
    prov = f.provider("p", pricing=Pricing.FREE, quality=70, speed=80, reliability=75)
    adp = f.adapter("p.a", "p", mode=ExecutionMode.CLOUD)
    rt = f.runtime(health={"p": ProviderHealth(provider_id="p", health_score=0.5)})
    bd = score(adp, prov, None, rt, f.catalogue(), _BALANCED)
    # raw = .25*70 + .25*100 + .2*80 + .2*75 + .1*100 = 83.5 ; * 0.5
    assert bd.health_multiplier == 0.5
    assert bd.final_score == pytest.approx(41.75)


def test_exact_weighted_sum_no_health() -> None:
    prov = f.provider("p", pricing=Pricing.FREE, quality=70, speed=80, reliability=75)
    adp = f.adapter("p.a", "p", mode=ExecutionMode.CLOUD)
    bd = score(adp, prov, None, f.runtime(), f.catalogue(), _BALANCED)
    assert bd.final_score == pytest.approx(83.5)
    assert bd.health_multiplier == 1.0
