"""α8.5c — capability catalogue + provider registry validator (offline).

One *red* fixture per validator rule (§5 of the α8.5c pre-flight) plus *green*
counter-cases, the schema-strictness guarantees (no integer priority, bounded
scores), and an end-to-end check that the committed manifest validates clean.

The validator lives under ``scripts/`` (tooling, never imported by the runtime —
W8.5c.2), so we put that directory on ``sys.path`` before importing it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import provider_manifest as pm  # noqa: E402
import validate_providers as vp  # noqa: E402
from pydantic import ValidationError  # noqa: E402

pytestmark = pytest.mark.unit

_MOCK_IMAGE = "app.infrastructure.ai.providers.mocks.mock_image:MockImageProvider"
_MOCK_LLM = "app.infrastructure.ai.providers.mocks.mock_llm:MockLLMProvider"


# --------------------------------------------------------------------------- #
# Builders (a minimal, valid catalogue + registry that each test mutates)
# --------------------------------------------------------------------------- #


def _catalogue() -> pm.Catalogue:
    return pm.Catalogue(
        capabilities=[
            pm.CapabilityEntry(id="image_generation", kind="image"),
            pm.CapabilityEntry(id="text_generation", kind="llm"),
            pm.CapabilityEntry(id="video_generation", kind="video"),
            pm.CapabilityEntry(id="text_to_speech", kind="voice"),
        ]
    )


def _scores() -> pm.Scores:
    return pm.Scores(quality=70, cost=100, speed=90, reliability=75)


def _free(available: bool = True, **kw) -> pm.FreeTier:
    return pm.FreeTier(available=available, **kw)


def _adapter(id_: str, capability: str, **kw) -> pm.Adapter:
    return pm.Adapter(id=id_, capability=capability, **kw)


def _provider(
    id_: str = "alpha",
    *,
    authentication: str = "none",
    free: pm.FreeTier | None = None,
    config_keys: list[str] | None = None,
    adapters: list[pm.Adapter] | None = None,
    commercial: bool = False,
) -> pm.Provider:
    return pm.Provider(
        id=id_,
        name=id_.title(),
        commercial=commercial,
        authentication=authentication,
        free=free if free is not None else _free(),
        config_keys=config_keys or [],
        scores=_scores(),
        adapters=(
            adapters if adapters is not None else [_adapter(f"{id_}.image", "image_generation")]
        ),
    )


def _routing(strategy: str = "free_first", by_capability: dict | None = None) -> pm.RoutingConfig:
    return pm.RoutingConfig(
        defaults=pm.RoutingDefaults(
            strategy=strategy, fallback="automatic", selection="best_available"
        ),
        by_capability=by_capability or {},
    )


def _registry(
    providers: list[pm.Provider] | None = None,
    families: list[pm.Family] | None = None,
    routing: pm.RoutingConfig | None = None,
) -> pm.Registry:
    return pm.Registry(
        providers=providers if providers is not None else [_provider()],
        families=families or [],
        routing=routing or _routing(),
    )


def _err_rules(report: vp.Report) -> set[str]:
    return {e["rule"] for e in report.errors}


def _warn_rules(report: vp.Report) -> set[str]:
    return {w["rule"] for w in report.warnings}


# --------------------------------------------------------------------------- #
# Baseline + shipped manifest
# --------------------------------------------------------------------------- #


def test_base_manifest_is_valid() -> None:
    report = vp.validate(_catalogue(), _registry())
    assert report.ok, report.errors


def test_committed_manifest_validates_clean() -> None:
    cat = pm.load_catalogue(pm.PROVIDERS_DIR / pm.CAPABILITIES_FILE)
    reg = pm.load_registry(pm.PROVIDERS_DIR / pm.REGISTRY_FILE)
    report = vp.validate(cat, reg)
    assert report.ok, report.errors


def test_main_on_committed_manifest_returns_zero(tmp_path: Path) -> None:
    rc = vp.main(["validate_providers.py", str(tmp_path / "report.json")])
    assert rc == 0
    assert (tmp_path / "report.json").exists()


# --------------------------------------------------------------------------- #
# Rule 1 — uniqueness
# --------------------------------------------------------------------------- #


def test_duplicate_provider_id() -> None:
    reg = _registry(providers=[_provider("alpha"), _provider("alpha")])
    assert "uniqueness" in _err_rules(vp.validate(_catalogue(), reg))


def test_duplicate_adapter_id() -> None:
    prov = _provider(
        adapters=[
            _adapter("alpha.image", "image_generation"),
            _adapter("alpha.image", "text_generation"),
        ]
    )
    assert "uniqueness" in _err_rules(vp.validate(_catalogue(), _registry([prov])))


def test_duplicate_capability_in_catalogue() -> None:
    cat = _catalogue()
    cat.capabilities.append(pm.CapabilityEntry(id="image_generation", kind="image"))
    assert "uniqueness" in _err_rules(vp.validate(cat, _registry()))


# --------------------------------------------------------------------------- #
# Rule 2 — catalogue integrity
# --------------------------------------------------------------------------- #


def test_adapter_capability_not_in_catalogue() -> None:
    prov = _provider(adapters=[_adapter("alpha.x", "not_a_capability")])
    assert "catalogue" in _err_rules(vp.validate(_catalogue(), _registry([prov])))


def test_orphan_capability_is_a_warning_only() -> None:
    report = vp.validate(_catalogue(), _registry())
    assert report.ok
    assert "catalogue" in _warn_rules(report)


# --------------------------------------------------------------------------- #
# Rule 3 — unique (provider, capability)
# --------------------------------------------------------------------------- #


def test_duplicate_provider_capability() -> None:
    prov = _provider(
        adapters=[
            _adapter("alpha.image", "image_generation"),
            _adapter("alpha.image2", "image_generation"),
        ]
    )
    assert "provider_capability" in _err_rules(vp.validate(_catalogue(), _registry([prov])))


# --------------------------------------------------------------------------- #
# Rule 4 — adapter integrity
# --------------------------------------------------------------------------- #


def test_bad_adapter_id_shape() -> None:
    prov = _provider(adapters=[_adapter("BadId", "image_generation")])
    assert "adapter" in _err_rules(vp.validate(_catalogue(), _registry([prov])))


def test_implemented_adapter_requires_import_path() -> None:
    prov = _provider(adapters=[_adapter("alpha.image", "image_generation", status="implemented")])
    assert "adapter" in _err_rules(vp.validate(_catalogue(), _registry([prov])))


def test_implemented_adapter_interface_ok() -> None:
    prov = _provider(
        adapters=[
            _adapter(
                "alpha.image", "image_generation", status="implemented", import_path=_MOCK_IMAGE
            )
        ]
    )
    assert "adapter" not in _err_rules(vp.validate(_catalogue(), _registry([prov])))


def test_implemented_adapter_interface_mismatch() -> None:
    prov = _provider(
        adapters=[
            _adapter("alpha.image", "image_generation", status="implemented", import_path=_MOCK_LLM)
        ]
    )
    assert "adapter" in _err_rules(vp.validate(_catalogue(), _registry([prov])))


# --------------------------------------------------------------------------- #
# Rule 5 — fallback graph
# --------------------------------------------------------------------------- #


def test_fallback_self_reference() -> None:
    prov = _provider(
        adapters=[_adapter("alpha.image", "image_generation", fallback=["alpha.image"])]
    )
    assert "fallback" in _err_rules(vp.validate(_catalogue(), _registry([prov])))


def test_fallback_unknown_target() -> None:
    prov = _provider(
        adapters=[_adapter("alpha.image", "image_generation", fallback=["ghost.image"])]
    )
    assert "fallback" in _err_rules(vp.validate(_catalogue(), _registry([prov])))


def test_fallback_incompatible_capability() -> None:
    prov = _provider(
        adapters=[
            _adapter("alpha.image", "image_generation", fallback=["alpha.text"]),
            _adapter("alpha.text", "text_generation"),
        ]
    )
    assert "fallback" in _err_rules(vp.validate(_catalogue(), _registry([prov])))


def test_fallback_cycle() -> None:
    a = _provider(
        "alpha", adapters=[_adapter("alpha.image", "image_generation", fallback=["beta.image"])]
    )
    b = _provider(
        "beta", adapters=[_adapter("beta.image", "image_generation", fallback=["alpha.image"])]
    )
    assert "fallback" in _err_rules(vp.validate(_catalogue(), _registry([a, b])))


# --------------------------------------------------------------------------- #
# Rule 6 — families
# --------------------------------------------------------------------------- #


def test_family_unknown_provider() -> None:
    fam = pm.Family(id="flux", variants=[pm.Variant(id="flux.dev", provider="ghost")])
    assert "family" in _err_rules(vp.validate(_catalogue(), _registry(families=[fam])))


def test_family_unknown_parent() -> None:
    fam = pm.Family(id="flux", parent="ghost", variants=[])
    assert "family" in _err_rules(vp.validate(_catalogue(), _registry(families=[fam])))


def test_family_inheritance_cycle() -> None:
    families = [pm.Family(id="f1", parent="f2"), pm.Family(id="f2", parent="f1")]
    assert "family" in _err_rules(vp.validate(_catalogue(), _registry(families=families)))


# --------------------------------------------------------------------------- #
# Rule 7 — free-tier consistency
# --------------------------------------------------------------------------- #


def test_free_tier_api_key_without_auth() -> None:
    prov = _provider(authentication="none", free=_free(api_key_required=True))
    assert "free_tier" in _err_rules(vp.validate(_catalogue(), _registry([prov])))


def test_free_tier_unavailable_with_positive_limit() -> None:
    prov = _provider(free=_free(available=False, daily_limit=100, monthly_limit=0), commercial=True)
    assert "free_tier" in _err_rules(vp.validate(_catalogue(), _registry([prov])))


def test_free_tier_unavailable_zeroed_is_ok() -> None:
    prov = _provider(
        free=_free(available=False, daily_limit=0, monthly_limit=0),
        commercial=True,
    )
    report = vp.validate(_catalogue(), _registry([prov], routing=_routing("balanced")))
    assert "free_tier" not in _err_rules(report)


# --------------------------------------------------------------------------- #
# Rule 8 — routing enums / capability references
# --------------------------------------------------------------------------- #


def test_routing_unknown_capability() -> None:
    reg = _registry(routing=_routing(by_capability={"ghost": pm.RoutingPolicy(strategy="fastest")}))
    assert "routing" in _err_rules(vp.validate(_catalogue(), reg))


def test_routing_bad_strategy_rejected_by_schema() -> None:
    with pytest.raises(ValidationError):
        pm.RoutingDefaults(strategy="cheapest", fallback="automatic", selection="best_available")


# --------------------------------------------------------------------------- #
# Rule 9 — scores (no integer priority; bounded 0..100)
# --------------------------------------------------------------------------- #


def test_priority_key_is_rejected() -> None:
    with pytest.raises(ValidationError):
        pm.Scores.model_validate(
            {"quality": 70, "cost": 100, "speed": 90, "reliability": 75, "priority": 5}
        )


def test_provider_priority_key_is_rejected() -> None:
    with pytest.raises(ValidationError):
        pm.Provider.model_validate(
            {
                "id": "alpha",
                "name": "Alpha",
                "free": {"available": True},
                "scores": {"quality": 70, "cost": 100, "speed": 90, "reliability": 75},
                "adapters": [{"id": "alpha.image", "capability": "image_generation"}],
                "priority": 1,
            }
        )


def test_score_out_of_range_rejected() -> None:
    with pytest.raises(ValidationError):
        pm.Scores(quality=200, cost=100, speed=90, reliability=75)


# --------------------------------------------------------------------------- #
# Rule 10 — config keys are names, never values
# --------------------------------------------------------------------------- #


def test_config_key_value_looking_is_error() -> None:
    prov = _provider(config_keys=["SECRET=hunter2"])
    assert "config_keys" in _err_rules(vp.validate(_catalogue(), _registry([prov])))


def test_config_key_lowercase_is_warning_only() -> None:
    prov = _provider(config_keys=["lower_key"])
    report = vp.validate(_catalogue(), _registry([prov]))
    assert "config_keys" not in _err_rules(report)
    assert "config_keys" in _warn_rules(report)


# --------------------------------------------------------------------------- #
# Rule 11 — anti-drift (green: shipped vocabulary matches the code enums)
# --------------------------------------------------------------------------- #


def test_anti_drift_is_clean_for_valid_manifest() -> None:
    assert "anti_drift" not in _err_rules(vp.validate(_catalogue(), _registry()))


# --------------------------------------------------------------------------- #
# Rule 12 — free-provider sanity
# --------------------------------------------------------------------------- #


def test_free_first_requires_a_free_provider() -> None:
    prov = _provider(free=_free(available=False, daily_limit=0, monthly_limit=0), commercial=True)
    report = vp.validate(_catalogue(), _registry([prov], routing=_routing("free_first")))
    assert "free_provider_sanity" in _err_rules(report)


def test_non_free_strategy_skips_sanity() -> None:
    prov = _provider(free=_free(available=False, daily_limit=0, monthly_limit=0), commercial=True)
    report = vp.validate(_catalogue(), _registry([prov], routing=_routing("highest_quality")))
    assert "free_provider_sanity" not in _err_rules(report)
