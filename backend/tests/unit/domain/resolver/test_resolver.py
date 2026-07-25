"""α8.5e.2 — pure resolver behaviour.

Coverage map:
  A  ordering + eligible/ineligible partition
  B  determinism (W8.5e.4) + request fingerprint
  C  tie-breaking (W8.5e.7): score → reliability → adapter_id
  D  eligibility hard filters (Amendment 4): each becomes ineligible-with-reason
  E  routing strategies reorder candidates (free_first / highest_quality)
  F  strategy-specific filters (offline_only / free_only / commercial_only / privacy_first)
  G  explainability (W8.5e.5) + provenance (W8.5e.6) + Resolution helpers
"""

from __future__ import annotations

import pytest

from app.domain.resolver import (
    RESOLVER_VERSION,
    ExecutionMode,
    Pricing,
    ProviderHealth,
    QuotaState,
    ResolveRequest,
    RoutingStrategy,
    get_strategy,
    resolve,
)

from . import _fakes as f

pytestmark = pytest.mark.unit


def _req(**kw: object) -> ResolveRequest:
    kw.setdefault("capability", f.CAP)
    return ResolveRequest(**kw)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# A — ordering
# --------------------------------------------------------------------------- #
def test_a1_balanced_orders_by_score_desc() -> None:
    res = resolve(_req(), f.catalogue(), f.runtime())
    assert [c.adapter_id for c in res.eligible] == [
        "pollinations.image",  # 83.5
        "comfyui.flux_schnell",  # 83.25
        "fal.flux",  # 76.1
    ]
    assert all(c.eligible for c in res.eligible)


def test_a2_ineligible_come_after_eligible() -> None:
    # local_only makes the two cloud adapters ineligible; they must trail the local one.
    res = resolve(_req(local_only=True), f.catalogue(), f.runtime())
    eligible_ids = [c.adapter_id for c in res.candidates if c.eligible]
    trailing = [c for c in res.candidates if not c.eligible]
    assert eligible_ids == ["comfyui.flux_schnell"]
    assert {c.ineligible_reason for c in trailing} == {"not_local"}
    # partition: all eligible precede all ineligible
    first_ineligible = next(i for i, c in enumerate(res.candidates) if not c.eligible)
    assert all(res.candidates[i].eligible for i in range(first_ineligible))


# --------------------------------------------------------------------------- #
# B — determinism
# --------------------------------------------------------------------------- #
def test_b1_same_inputs_identical_output() -> None:
    cat, rt = f.catalogue(), f.runtime()
    assert resolve(_req(), cat, rt) == resolve(_req(), cat, rt)


def test_b2_fingerprint_stable_and_request_sensitive() -> None:
    a = resolve(_req(prompt="cat"), f.catalogue(), f.runtime())
    b = resolve(_req(prompt="cat"), f.catalogue(), f.runtime())
    c = resolve(_req(prompt="dog"), f.catalogue(), f.runtime())
    assert a.request_fingerprint == b.request_fingerprint
    assert a.request_fingerprint != c.request_fingerprint


# --------------------------------------------------------------------------- #
# C — tie-breaking
# --------------------------------------------------------------------------- #
def test_c1_equal_score_breaks_by_reliability_then_id() -> None:
    # Two paid providers tuned to identical balanced score (82.5) but different
    # reliability; the higher-reliability one wins despite a *later* adapter id.
    providers = {
        "hi": f.provider("hi", pricing=Pricing.PAID, quality=70, cost=92, speed=80, reliability=80),
        "lo": f.provider(
            "lo", pricing=Pricing.PAID, quality=70, cost=100, speed=80, reliability=70
        ),
    }
    adapters = (
        f.adapter("z.hi", "hi", mode=ExecutionMode.CLOUD),
        f.adapter("a.lo", "lo", mode=ExecutionMode.CLOUD),
    )
    res = resolve(_req(), f.catalogue(providers=providers, adapters=adapters), f.runtime())
    assert [c.score for c in res.eligible] == [82.5, 82.5]
    assert [c.adapter_id for c in res.eligible] == ["z.hi", "a.lo"]  # reliability beats id


def test_c2_full_tie_breaks_by_adapter_id() -> None:
    providers = {"p": f.provider("p", pricing=Pricing.PAID)}
    adapters = (
        f.adapter("z.one", "p", mode=ExecutionMode.CLOUD),
        f.adapter("a.two", "p", mode=ExecutionMode.CLOUD),
    )
    res = resolve(_req(), f.catalogue(providers=providers, adapters=adapters), f.runtime())
    assert [c.adapter_id for c in res.eligible] == ["a.two", "z.one"]


# --------------------------------------------------------------------------- #
# D — eligibility hard filters
# --------------------------------------------------------------------------- #
def _reason(res: object, adapter_id: str) -> str | None:
    for c in res.candidates:  # type: ignore[attr-defined]
        if c.adapter_id == adapter_id:
            return c.ineligible_reason
    raise AssertionError(f"no candidate {adapter_id}")


def test_d1_privacy_mode_excludes_cloud() -> None:
    res = resolve(_req(privacy_mode=True), f.catalogue(), f.runtime())
    assert _reason(res, "pollinations.image") == "privacy_cloud_egress"
    assert _reason(res, "fal.flux") == "privacy_cloud_egress"
    assert [c.adapter_id for c in res.eligible] == ["comfyui.flux_schnell"]


def test_d2_commercial_terms_not_allowed() -> None:
    providers = {
        "free_nc": f.provider("free_nc", commercial=False),
        "comm": f.provider("comm", commercial=True),
    }
    adapters = (
        f.adapter("free_nc.a", "free_nc", mode=ExecutionMode.CLOUD),
        f.adapter("comm.a", "comm", mode=ExecutionMode.CLOUD),
    )
    res = resolve(
        _req(allow_commercial_terms=False),
        f.catalogue(providers=providers, adapters=adapters),
        f.runtime(),
    )
    assert [c.adapter_id for c in res.eligible] == ["free_nc.a"]
    assert _reason(res, "comm.a") == "commercial_terms_not_allowed"


def test_d2b_paid_providers_not_allowed() -> None:
    # Licensing and cost are orthogonal: fal is paid (excluded) even though it is a
    # commercial provider; the free-but-commercial default providers survive.
    res = resolve(_req(allow_paid_providers=False), f.catalogue(), f.runtime())
    assert _reason(res, "fal.flux") == "paid_not_allowed"
    assert {c.provider_id for c in res.eligible} == {"pollinations", "comfyui"}


def test_d3_budget_zero_excludes_paid() -> None:
    res = resolve(_req(budget=0), f.catalogue(), f.runtime())
    assert _reason(res, "fal.flux") == "budget_zero_paid"
    assert {c.provider_id for c in res.eligible} == {"pollinations", "comfyui"}


def test_d4_over_budget_excludes_expensive_paid() -> None:
    res = resolve(_req(budget=0.005), f.catalogue(), f.runtime())  # fal costs 0.01
    assert _reason(res, "fal.flux") == "over_budget"


def test_d5_quota_exhausted() -> None:
    rt = f.runtime(quota={"fal": (QuotaState(provider_id="fal", window="daily", remaining=0),)})
    res = resolve(_req(), f.catalogue(), rt)
    assert _reason(res, "fal.flux") == "quota_exhausted"


def test_d6_health_down() -> None:
    rt = f.runtime(health={"fal": ProviderHealth(provider_id="fal", health_score=0.0)})
    res = resolve(_req(), f.catalogue(), rt)
    assert _reason(res, "fal.flux") == "health_down"


def test_d7_insufficient_hardware() -> None:
    res = resolve(_req(device="small_box"), f.catalogue(), f.runtime())  # 8GB < min 16
    assert _reason(res, "comfyui.flux_schnell") == "insufficient_hardware"


def test_d8_disabled_provider_and_adapter() -> None:
    providers = {
        "off": f.provider("off", enabled=False),
        "on": f.provider("on"),
    }
    adapters = (
        f.adapter("off.a", "off", mode=ExecutionMode.CLOUD),
        f.adapter("on.disabled", "on", mode=ExecutionMode.CLOUD, enabled=False),
        f.adapter("on.ok", "on", mode=ExecutionMode.CLOUD),
    )
    res = resolve(_req(), f.catalogue(providers=providers, adapters=adapters), f.runtime())
    assert _reason(res, "off.a") == "provider_disabled"
    assert _reason(res, "on.disabled") == "adapter_disabled"
    assert [c.adapter_id for c in res.eligible] == ["on.ok"]


def test_d9_unknown_provider_is_ineligible_not_crash() -> None:
    adapters = (f.adapter("ghost.a", "does_not_exist", mode=ExecutionMode.CLOUD),)
    res = resolve(_req(), f.catalogue(providers={}, adapters=adapters), f.runtime())
    assert _reason(res, "ghost.a") == "unknown_provider"
    assert res.eligible == ()


# --------------------------------------------------------------------------- #
# E — routing strategies reorder
# --------------------------------------------------------------------------- #
def test_e1_free_first_ranks_free_top_and_paid_last() -> None:
    cat = f.catalogue(routing={"default": RoutingStrategy.FREE_FIRST})
    res = resolve(_req(), cat, f.runtime())
    assert res.routing_strategy == RoutingStrategy.FREE_FIRST
    assert res.eligible[0].provider_id in {"comfyui", "pollinations"}  # free
    assert res.eligible[-1].adapter_id == "fal.flux"  # paid, last


def test_e2_highest_quality_ranks_top_quality_first() -> None:
    cat = f.catalogue(routing={"default": RoutingStrategy.HIGHEST_QUALITY})
    res = resolve(_req(), cat, f.runtime())
    assert res.eligible[0].adapter_id == "fal.flux"  # provider quality 90


def test_e3_per_capability_routing_overrides_default() -> None:
    cat = f.catalogue(
        routing={"default": RoutingStrategy.BALANCED, f.CAP: RoutingStrategy.HIGHEST_QUALITY}
    )
    assert resolve(_req(), cat, f.runtime()).routing_strategy == RoutingStrategy.HIGHEST_QUALITY


# --------------------------------------------------------------------------- #
# F — strategy-specific filters
# --------------------------------------------------------------------------- #
def test_f1_offline_only_excludes_cloud() -> None:
    cat = f.catalogue(routing={"default": RoutingStrategy.OFFLINE_ONLY})
    res = resolve(_req(), cat, f.runtime())
    assert [c.adapter_id for c in res.eligible] == ["comfyui.flux_schnell"]
    assert _reason(res, "fal.flux") == "offline_only_non_local"


def test_f2_free_only_excludes_paid() -> None:
    cat = f.catalogue(routing={"default": RoutingStrategy.FREE_ONLY})
    res = resolve(_req(), cat, f.runtime())
    assert _reason(res, "fal.flux") == "free_only_paid"


def test_f3_commercial_only_excludes_non_commercial() -> None:
    providers = {
        "nc": f.provider("nc", commercial=False),
        "c": f.provider("c", commercial=True),
    }
    adapters = (
        f.adapter("nc.a", "nc", mode=ExecutionMode.CLOUD),
        f.adapter("c.a", "c", mode=ExecutionMode.CLOUD),
    )
    cat = f.catalogue(
        providers=providers,
        adapters=adapters,
        routing={"default": RoutingStrategy.COMMERCIAL_ONLY},
    )
    res = resolve(_req(), cat, f.runtime())
    assert [c.adapter_id for c in res.eligible] == ["c.a"]
    assert _reason(res, "nc.a") == "commercial_only_non_commercial"


def test_f4_privacy_first_strategy_excludes_cloud() -> None:
    cat = f.catalogue(routing={"default": RoutingStrategy.PRIVACY_FIRST})
    res = resolve(_req(), cat, f.runtime())
    assert _reason(res, "pollinations.image") == "privacy_first_cloud"


# --------------------------------------------------------------------------- #
# G — explainability + provenance + helpers
# --------------------------------------------------------------------------- #
def test_g1_eligible_carry_breakdown_ineligible_do_not() -> None:
    res = resolve(_req(privacy_mode=True), f.catalogue(), f.runtime())
    top = res.top
    assert top is not None and top.breakdown is not None
    payload = top.breakdown.as_dict()
    assert payload["score_schema"] == 1
    assert set(payload) == {"score_schema", "components", "health_multiplier", "final_score"}
    assert set(payload["components"]) == {"quality", "cost", "speed", "reliability", "hardware"}
    for c in res.candidates:
        if not c.eligible:
            assert c.breakdown is None and c.ineligible_reason is not None


def test_g2_health_multiplier_scales_final_score() -> None:
    rt = f.runtime(
        health={"pollinations": ProviderHealth(provider_id="pollinations", health_score=0.5)}
    )
    res = resolve(_req(), f.catalogue(), rt)
    poll = next(c for c in res.eligible if c.provider_id == "pollinations")
    assert poll.breakdown is not None
    assert poll.breakdown.health_multiplier == 0.5
    assert poll.score == pytest.approx(41.75)  # 83.5 * 0.5


def test_g3_provenance_fields_populated() -> None:
    res = resolve(_req(), f.catalogue(version="2026.07", digest="abc123"), f.runtime())
    assert res.catalogue_version == "2026.07"
    assert res.manifest_digest == "abc123"
    assert res.resolver_version == RESOLVER_VERSION
    assert res.candidates and res.candidates[0].fallbacks == ()


def test_g4_top_is_none_when_nothing_eligible() -> None:
    res = resolve(
        _req(local_only=True),
        f.catalogue(adapters=(f.adapter("x.cloud", "pollinations"),)),
        f.runtime(),
    )
    assert res.top is None
    assert res.eligible == ()


def test_g5_get_strategy_defaults_to_balanced() -> None:
    assert get_strategy(RoutingStrategy.BALANCED).strategy == RoutingStrategy.BALANCED
    # every enum value maps to an implementation
    for strat in RoutingStrategy:
        assert get_strategy(strat).strategy == strat
