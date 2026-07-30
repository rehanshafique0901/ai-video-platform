"""α8.5c — capability / provider / routing manifest validator (offline).

One *red* fixture per validator rule (uniqueness, capability metadata, catalogue
integrity, unique provider+capability, adapter shape/interface, adapter
constraints, fallback graph, families, pricing/quota, routing, config-keys,
anti-drift, free-provider sanity) plus *green* counter-cases, the schema
strictness guarantees (no integer priority, bounded scores), and an end-to-end
check that the three committed manifests validate clean.

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
# Builders (a minimal, valid catalogue + providers + routing that tests mutate)
# --------------------------------------------------------------------------- #


def _catalogue() -> pm.Catalogue:
    return pm.Catalogue(
        capabilities=[
            pm.CapabilityEntry(
                id="image_generation",
                kind="image",
                inputs=["text"],
                outputs=["image"],
                requires=["prompt"],
                optional=["seed"],
            ),
            pm.CapabilityEntry(
                id="text_generation",
                kind="llm",
                inputs=["text"],
                outputs=["text"],
                requires=["prompt"],
            ),
            pm.CapabilityEntry(
                id="video_generation",
                kind="video",
                inputs=["text"],
                outputs=["video"],
                requires=["prompt"],
            ),
            pm.CapabilityEntry(
                id="text_to_speech",
                kind="voice",
                inputs=["text"],
                outputs=["audio"],
                requires=["text"],
            ),
        ]
    )


def _scores() -> pm.Scores:
    return pm.Scores(quality=70, cost=100, speed=90, reliability=75)


def _quota(daily="unlimited", monthly="unlimited") -> pm.Quota:
    return pm.Quota(daily=daily, monthly=monthly)


def _adapter(id_: str, capability: str, **kw) -> pm.Adapter:
    return pm.Adapter(id=id_, capability=capability, **kw)


def _provider(
    id_: str = "alpha",
    *,
    authentication: str = "none",
    pricing: str = "free",
    quota: pm.Quota | None = None,
    requires_login: bool = False,
    config_keys: list[str] | None = None,
    adapters: list[pm.Adapter] | None = None,
    commercial: bool = False,
) -> pm.Provider:
    return pm.Provider(
        id=id_,
        name=id_.title(),
        commercial=commercial,
        authentication=authentication,
        requires_login=requires_login,
        pricing=pricing,
        quota=quota if quota is not None else _quota(),
        config_keys=config_keys or [],
        scores=_scores(),
        adapters=(
            adapters if adapters is not None else [_adapter(f"{id_}.image", "image_generation")]
        ),
    )


def _providers(
    providers: list[pm.Provider] | None = None, families: list[pm.Family] | None = None
) -> pm.ProvidersDoc:
    return pm.ProvidersDoc(
        providers=providers if providers is not None else [_provider()],
        families=families or [],
    )


def _routing(strategy: str = "free_first", by_capability: dict | None = None) -> pm.RoutingDoc:
    return pm.RoutingDoc(
        defaults=pm.RoutingDefaults(
            strategy=strategy, fallback="automatic", selection="best_available"
        ),
        by_capability=by_capability or {},
    )


def _validate(
    cat=None, pdoc=None, rdoc=None, devices=None, *, implemented=frozenset()
) -> vp.Report:
    """Validate a synthetic manifest. The synthetic build implements nothing by default,
    so registry reconciliation is opted into by the tests that exercise it."""
    return vp.validate(
        cat or _catalogue(),
        pdoc or _providers(),
        rdoc or _routing(),
        devices,
        implemented_adapter_ids=implemented,
    )


def _err_rules(report: vp.Report) -> set[str]:
    return {e["rule"] for e in report.errors}


def _warn_rules(report: vp.Report) -> set[str]:
    return {w["rule"] for w in report.warnings}


# --------------------------------------------------------------------------- #
# Baseline + shipped manifest
# --------------------------------------------------------------------------- #


def test_base_manifest_is_valid() -> None:
    report = _validate()
    assert report.ok, report.errors


def test_registered_adapter_must_exist_in_the_manifest() -> None:
    # ADR-0054: a registry key is both an executable-set entry and a provenance value, so
    # one that names no catalogue adapter is unreachable code writing an uninterpretable id.
    report = _validate(implemented=frozenset({"ghost.image"}))
    assert "registry_reconciliation" in _err_rules(report)


def test_a_registered_adapter_present_in_the_manifest_is_clean() -> None:
    report = _validate(implemented=frozenset({"alpha.image"}))
    assert "registry_reconciliation" not in _err_rules(report)


def test_the_real_build_reconciles_with_the_committed_manifest() -> None:
    # The assertion that actually guards the repository: this build's registry keys are
    # real catalogue adapter ids. Uses validate()'s default implemented set.
    cat = pm.load_catalogue(pm.PROVIDERS_DIR / pm.CAPABILITIES_FILE)
    pdoc = pm.load_providers(pm.PROVIDERS_DIR / pm.PROVIDERS_FILE)
    rdoc = pm.load_routing(pm.PROVIDERS_DIR / pm.ROUTING_FILE)
    report = vp.validate(cat, pdoc, rdoc, pm.load_devices(pm.PROVIDERS_DIR / pm.DEVICES_FILE))
    assert "registry_reconciliation" not in _err_rules(report)


def test_committed_manifest_validates_clean() -> None:
    cat = pm.load_catalogue(pm.PROVIDERS_DIR / pm.CAPABILITIES_FILE)
    pdoc = pm.load_providers(pm.PROVIDERS_DIR / pm.PROVIDERS_FILE)
    rdoc = pm.load_routing(pm.PROVIDERS_DIR / pm.ROUTING_FILE)
    dev = pm.load_devices(pm.PROVIDERS_DIR / pm.DEVICES_FILE)
    report = vp.validate(cat, pdoc, rdoc, dev)
    assert report.ok, report.errors


def test_main_on_committed_manifest_returns_zero(tmp_path: Path) -> None:
    rc = vp.main(["validate_providers.py", str(tmp_path / "report.json")])
    assert rc == 0
    assert (tmp_path / "report.json").exists()


# --------------------------------------------------------------------------- #
# Rule — uniqueness
# --------------------------------------------------------------------------- #


def test_duplicate_provider_id() -> None:
    assert "uniqueness" in _err_rules(_validate(pdoc=_providers([_provider("a"), _provider("a")])))


def test_duplicate_adapter_id() -> None:
    prov = _provider(
        adapters=[
            _adapter("alpha.image", "image_generation"),
            _adapter("alpha.image", "text_generation"),
        ]
    )
    assert "uniqueness" in _err_rules(_validate(pdoc=_providers([prov])))


def test_duplicate_capability_in_catalogue() -> None:
    cat = _catalogue()
    cat.capabilities.append(
        pm.CapabilityEntry(id="image_generation", kind="image", inputs=["text"], outputs=["image"])
    )
    assert "uniqueness" in _err_rules(_validate(cat=cat))


# --------------------------------------------------------------------------- #
# Rule — capability metadata (I/O + params)
# --------------------------------------------------------------------------- #


def test_capability_without_outputs() -> None:
    cat = pm.Catalogue(
        capabilities=[
            pm.CapabilityEntry(id="image_generation", kind="image", inputs=["text"], outputs=[])
        ]
    )
    assert "capability" in _err_rules(_validate(cat=cat))


def test_capability_duplicate_param() -> None:
    cat = pm.Catalogue(
        capabilities=[
            pm.CapabilityEntry(
                id="image_generation",
                kind="image",
                inputs=["text"],
                outputs=["image"],
                requires=["prompt", "prompt"],
            )
        ]
    )
    assert "capability" in _err_rules(_validate(cat=cat))


def test_capability_param_required_and_optional() -> None:
    cat = pm.Catalogue(
        capabilities=[
            pm.CapabilityEntry(
                id="image_generation",
                kind="image",
                inputs=["text"],
                outputs=["image"],
                requires=["prompt"],
                optional=["prompt"],
            )
        ]
    )
    assert "capability" in _err_rules(_validate(cat=cat))


def test_capability_param_not_snake_case() -> None:
    cat = pm.Catalogue(
        capabilities=[
            pm.CapabilityEntry(
                id="image_generation",
                kind="image",
                inputs=["text"],
                outputs=["image"],
                requires=["Prompt"],
            )
        ]
    )
    assert "capability" in _err_rules(_validate(cat=cat))


def test_unknown_io_type_rejected_by_schema() -> None:
    with pytest.raises(ValidationError):
        pm.CapabilityEntry(id="x", kind="image", inputs=["hologram"], outputs=["image"])


# --------------------------------------------------------------------------- #
# Rule — catalogue integrity
# --------------------------------------------------------------------------- #


def test_adapter_capability_not_in_catalogue() -> None:
    prov = _provider(adapters=[_adapter("alpha.x", "not_a_capability")])
    assert "catalogue" in _err_rules(_validate(pdoc=_providers([prov])))


def test_orphan_capability_is_a_warning_only() -> None:
    report = _validate()
    assert report.ok
    assert "catalogue" in _warn_rules(report)


# --------------------------------------------------------------------------- #
# Rule — unique (provider, capability)
# --------------------------------------------------------------------------- #


def test_duplicate_provider_capability() -> None:
    prov = _provider(
        adapters=[
            _adapter("alpha.image", "image_generation"),
            _adapter("alpha.image2", "image_generation"),
        ]
    )
    assert "provider_capability" in _err_rules(_validate(pdoc=_providers([prov])))


# --------------------------------------------------------------------------- #
# Rule — adapter integrity
# --------------------------------------------------------------------------- #


def test_bad_adapter_id_shape() -> None:
    prov = _provider(adapters=[_adapter("BadId", "image_generation")])
    assert "adapter" in _err_rules(_validate(pdoc=_providers([prov])))


def test_implemented_adapter_requires_import_path() -> None:
    prov = _provider(adapters=[_adapter("alpha.image", "image_generation", status="implemented")])
    assert "adapter" in _err_rules(_validate(pdoc=_providers([prov])))


def test_implemented_adapter_interface_ok() -> None:
    prov = _provider(
        adapters=[
            _adapter(
                "alpha.image", "image_generation", status="implemented", import_path=_MOCK_IMAGE
            )
        ]
    )
    assert "adapter" not in _err_rules(_validate(pdoc=_providers([prov])))


def test_implemented_adapter_interface_mismatch() -> None:
    prov = _provider(
        adapters=[
            _adapter("alpha.image", "image_generation", status="implemented", import_path=_MOCK_LLM)
        ]
    )
    assert "adapter" in _err_rules(_validate(pdoc=_providers([prov])))


# --------------------------------------------------------------------------- #
# Rule — adapter constraints applicability
# --------------------------------------------------------------------------- #


def test_max_duration_on_image_warns() -> None:
    prov = _provider(
        adapters=[
            _adapter(
                "alpha.image",
                "image_generation",
                supports=pm.AdapterSupports(max_duration_seconds=10),
            )
        ]
    )
    assert "constraints" in _warn_rules(_validate(pdoc=_providers([prov])))


def test_max_duration_on_video_is_ok() -> None:
    prov = _provider(
        adapters=[
            _adapter(
                "alpha.video",
                "video_generation",
                supports=pm.AdapterSupports(max_duration_seconds=30, max_resolution="1080p"),
            )
        ]
    )
    assert "constraints" not in _warn_rules(_validate(pdoc=_providers([prov], families=None)))


def test_supports_async_alias_parses() -> None:
    a = pm.Adapter.model_validate(
        {"id": "alpha.video", "capability": "video_generation", "supports": {"async": True}}
    )
    assert a.supports.asynchronous is True


def test_non_positive_max_duration_rejected_by_schema() -> None:
    with pytest.raises(ValidationError):
        pm.AdapterSupports(max_duration_seconds=0)


# --------------------------------------------------------------------------- #
# Rule — fallback graph
# --------------------------------------------------------------------------- #


def test_fallback_self_reference() -> None:
    prov = _provider(
        adapters=[_adapter("alpha.image", "image_generation", fallback=["alpha.image"])]
    )
    assert "fallback" in _err_rules(_validate(pdoc=_providers([prov])))


def test_fallback_unknown_target() -> None:
    prov = _provider(
        adapters=[_adapter("alpha.image", "image_generation", fallback=["ghost.image"])]
    )
    assert "fallback" in _err_rules(_validate(pdoc=_providers([prov])))


def test_fallback_incompatible_capability() -> None:
    prov = _provider(
        adapters=[
            _adapter("alpha.image", "image_generation", fallback=["alpha.text"]),
            _adapter("alpha.text", "text_generation"),
        ]
    )
    assert "fallback" in _err_rules(_validate(pdoc=_providers([prov])))


def test_fallback_cycle() -> None:
    a = _provider(
        "alpha", adapters=[_adapter("alpha.image", "image_generation", fallback=["beta.image"])]
    )
    b = _provider(
        "beta", adapters=[_adapter("beta.image", "image_generation", fallback=["alpha.image"])]
    )
    assert "fallback" in _err_rules(_validate(pdoc=_providers([a, b])))


# --------------------------------------------------------------------------- #
# Rule — families
# --------------------------------------------------------------------------- #


def test_family_unknown_provider() -> None:
    fam = pm.Family(id="flux", variants=[pm.Variant(id="flux.dev", provider="ghost")])
    assert "family" in _err_rules(_validate(pdoc=_providers(families=[fam])))


def test_family_unknown_parent() -> None:
    fam = pm.Family(id="flux", parent="ghost", variants=[])
    assert "family" in _err_rules(_validate(pdoc=_providers(families=[fam])))


def test_family_inheritance_cycle() -> None:
    families = [pm.Family(id="f1", parent="f2"), pm.Family(id="f2", parent="f1")]
    assert "family" in _err_rules(_validate(pdoc=_providers(families=families)))


# --------------------------------------------------------------------------- #
# Rule — pricing / quota
# --------------------------------------------------------------------------- #


def test_non_positive_quota_is_error() -> None:
    prov = _provider(quota=_quota(daily=0))
    assert "pricing" in _err_rules(_validate(pdoc=_providers([prov])))


def test_free_provider_without_quota_warns() -> None:
    prov = _provider(pricing="free", quota=pm.Quota())
    assert "pricing" in _warn_rules(_validate(pdoc=_providers([prov])))


def test_bad_pricing_rejected_by_schema() -> None:
    with pytest.raises(ValidationError):
        _provider(pricing="cheap")


# --------------------------------------------------------------------------- #
# Rule — routing
# --------------------------------------------------------------------------- #


def test_routing_unknown_capability() -> None:
    rdoc = _routing(by_capability={"ghost": pm.RoutingPolicy(strategy="fastest")})
    assert "routing" in _err_rules(_validate(rdoc=rdoc))


def test_routing_bad_strategy_rejected_by_schema() -> None:
    with pytest.raises(ValidationError):
        pm.RoutingDefaults(strategy="cheapest", fallback="automatic", selection="best_available")


# --------------------------------------------------------------------------- #
# Rule — scores (no integer priority; bounded 0..100)
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
                "pricing": "free",
                "scores": {"quality": 70, "cost": 100, "speed": 90, "reliability": 75},
                "adapters": [{"id": "alpha.image", "capability": "image_generation"}],
                "priority": 1,
            }
        )


def test_score_out_of_range_rejected() -> None:
    with pytest.raises(ValidationError):
        pm.Scores(quality=200, cost=100, speed=90, reliability=75)


# --------------------------------------------------------------------------- #
# Rule — config keys are names, never values
# --------------------------------------------------------------------------- #


def test_config_key_value_looking_is_error() -> None:
    prov = _provider(config_keys=["SECRET=hunter2"])
    assert "config_keys" in _err_rules(_validate(pdoc=_providers([prov])))


def test_config_key_lowercase_is_warning_only() -> None:
    prov = _provider(config_keys=["lower_key"])
    report = _validate(pdoc=_providers([prov]))
    assert "config_keys" not in _err_rules(report)
    assert "config_keys" in _warn_rules(report)


# --------------------------------------------------------------------------- #
# Rule — anti-drift (green: shipped vocabulary matches the code enums)
# --------------------------------------------------------------------------- #


def test_anti_drift_is_clean_for_valid_manifest() -> None:
    assert "anti_drift" not in _err_rules(_validate())


# --------------------------------------------------------------------------- #
# Rule — free-provider sanity (free = pricing ∈ {free, freemium})
# --------------------------------------------------------------------------- #


def test_free_first_requires_a_free_provider() -> None:
    prov = _provider(pricing="paid", quota=_quota(daily=100), commercial=True)
    report = _validate(pdoc=_providers([prov]), rdoc=_routing("free_first"))
    assert "free_provider_sanity" in _err_rules(report)


def test_freemium_satisfies_free_first() -> None:
    prov = _provider(pricing="freemium", quota=_quota(daily=100))
    report = _validate(pdoc=_providers([prov]), rdoc=_routing("free_first"))
    assert "free_provider_sanity" not in _err_rules(report)


def test_non_free_strategy_skips_sanity() -> None:
    prov = _provider(pricing="paid", quota=_quota(daily=100), commercial=True)
    report = _validate(pdoc=_providers([prov]), rdoc=_routing("highest_quality"))
    assert "free_provider_sanity" not in _err_rules(report)


# --------------------------------------------------------------------------- #
# α8.5d — capability dependencies (capability → capability)
# --------------------------------------------------------------------------- #


def _cat_with_video_deps(**kw) -> pm.Catalogue:
    cat = _catalogue()
    cat.capabilities[2].dependencies = pm.CapabilityDependencies(**kw)  # video_generation
    return cat


def test_dependency_unknown_capability() -> None:
    assert "dependencies" in _err_rules(_validate(cat=_cat_with_video_deps(requires=["ghost"])))


def test_dependency_self_reference() -> None:
    cat = _cat_with_video_deps(requires=["video_generation"])
    assert "dependencies" in _err_rules(_validate(cat=cat))


def test_dependency_required_and_optional_overlap() -> None:
    cat = _cat_with_video_deps(requires=["image_generation"], optional=["image_generation"])
    assert "dependencies" in _err_rules(_validate(cat=cat))


def test_dependency_cycle() -> None:
    cat = _catalogue()
    cat.capabilities[0].dependencies = pm.CapabilityDependencies(requires=["video_generation"])
    cat.capabilities[2].dependencies = pm.CapabilityDependencies(requires=["image_generation"])
    assert "dependencies" in _err_rules(_validate(cat=cat))


def test_dependency_valid_graph_is_clean() -> None:
    cat = _cat_with_video_deps(requires=["image_generation"], optional=["text_to_speech"])
    report = _validate(cat=cat)
    assert "dependencies" not in _err_rules(report)


# --------------------------------------------------------------------------- #
# α8.5d — feature matrix
# --------------------------------------------------------------------------- #


def test_video_only_feature_on_image_warns() -> None:
    prov = _provider(
        adapters=[_adapter("alpha.image", "image_generation", features=["motion_control"])]
    )
    assert "features" in _warn_rules(_validate(pdoc=_providers([prov])))


def test_features_on_non_media_capability_warn() -> None:
    prov = _provider(adapters=[_adapter("alpha.text", "text_generation", features=["txt2img"])])
    assert "features" in _warn_rules(_validate(pdoc=_providers([prov])))


def test_unknown_feature_rejected_by_schema() -> None:
    with pytest.raises(ValidationError):
        pm.Adapter(id="alpha.image", capability="image_generation", features=["upscale8k"])


def test_valid_image_features_clean() -> None:
    prov = _provider(
        adapters=[_adapter("alpha.image", "image_generation", features=["txt2img", "img2img"])]
    )
    report = _validate(pdoc=_providers([prov]))
    assert "features" not in _err_rules(report) and "features" not in _warn_rules(report)


# --------------------------------------------------------------------------- #
# α8.5d — output characteristics
# --------------------------------------------------------------------------- #


def test_output_unknown_io_type_error() -> None:
    prov = _provider(
        adapters=[_adapter("alpha.image", "image_generation", outputs={"hologram": ["x"]})]
    )
    assert "outputs" in _err_rules(_validate(pdoc=_providers([prov])))


def test_output_io_type_not_in_capability_outputs() -> None:
    prov = _provider(
        adapters=[_adapter("alpha.image", "image_generation", outputs={"video": ["mp4"]})]
    )
    assert "outputs" in _err_rules(_validate(pdoc=_providers([prov])))


def test_output_bad_format_token() -> None:
    prov = _provider(
        adapters=[_adapter("alpha.image", "image_generation", outputs={"image": ["tiff"]})]
    )
    assert "outputs" in _err_rules(_validate(pdoc=_providers([prov])))


def test_output_empty_format_list() -> None:
    prov = _provider(adapters=[_adapter("alpha.image", "image_generation", outputs={"image": []})])
    assert "outputs" in _err_rules(_validate(pdoc=_providers([prov])))


def test_output_valid_is_clean() -> None:
    prov = _provider(
        adapters=[_adapter("alpha.image", "image_generation", outputs={"image": ["png", "jpg"]})]
    )
    assert "outputs" not in _err_rules(_validate(pdoc=_providers([prov])))


# --------------------------------------------------------------------------- #
# α8.5d — resource estimation
# --------------------------------------------------------------------------- #


def test_recommended_ram_below_minimum_error() -> None:
    rt = pm.AdapterRuntime(hardware=pm.Hardware(minimum_ram_gb=32, recommended_ram_gb=16))
    prov = _provider(adapters=[_adapter("alpha.image", "image_generation", runtime=rt)])
    assert "runtime" in _err_rules(_validate(pdoc=_providers([prov])))


def test_local_adapter_without_gpu_backend_warns() -> None:
    rt = pm.AdapterRuntime(execution=pm.Execution(local=True))
    prov = _provider(adapters=[_adapter("alpha.image", "image_generation", runtime=rt)])
    assert "runtime" in _warn_rules(_validate(pdoc=_providers([prov])))


def test_negative_estimate_rejected_by_schema() -> None:
    with pytest.raises(ValidationError):
        pm.Estimated(image_seconds=-1)


# --------------------------------------------------------------------------- #
# α8.5d — cost hints (estimation-only)
# --------------------------------------------------------------------------- #


def test_free_provider_nonzero_cost_warns() -> None:
    cost = pm.AdapterCost(unit="image", amount=5.0, currency="GBP")
    prov = _provider(
        pricing="free", adapters=[_adapter("alpha.image", "image_generation", cost=cost)]
    )
    assert "cost" in _warn_rules(_validate(pdoc=_providers([prov])))


def test_bad_cost_unit_rejected_by_schema() -> None:
    with pytest.raises(ValidationError):
        pm.AdapterCost(unit="banana", amount=1.0, currency="GBP")


def test_bad_currency_length_rejected_by_schema() -> None:
    with pytest.raises(ValidationError):
        pm.AdapterCost(unit="image", amount=1.0, currency="POUND")


# --------------------------------------------------------------------------- #
# α8.5d — device profiles (optional manifest)
# --------------------------------------------------------------------------- #


def test_duplicate_device_profile_id() -> None:
    devices = pm.DevicesDoc(
        device_profiles=[
            pm.DeviceProfile(id="dev", backend="cpu"),
            pm.DeviceProfile(id="dev", backend="metal"),
        ]
    )
    assert "devices" in _err_rules(_validate(devices=devices))


def test_bad_device_backend_rejected_by_schema() -> None:
    with pytest.raises(ValidationError):
        pm.DeviceProfile(id="dev", backend="tpu")


def test_committed_devices_manifest_is_valid() -> None:
    dev = pm.load_devices(pm.PROVIDERS_DIR / pm.DEVICES_FILE)
    assert "devices" not in _err_rules(_validate(devices=dev))
