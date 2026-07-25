"""α8.5d Phase 2 — seeder digest + plan (pure, offline; no database).

Covers the deterministic pieces the reviewer asked for: digest stability /
whitespace-invariance / change-sensitivity, and the diff plan (fresh create,
idempotent re-seed, single-row update, provider removal ⇒ disable-not-delete).
The Postgres apply layer is exercised by the Phase 3 CI round-trip.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import provider_manifest as pm  # noqa: E402
import seed_providers as sp  # noqa: E402

pytestmark = pytest.mark.unit


def _desired() -> dict[str, sp.Rows]:
    cat = pm.load_catalogue(pm.PROVIDERS_DIR / pm.CAPABILITIES_FILE)
    pdoc = pm.load_providers(pm.PROVIDERS_DIR / pm.PROVIDERS_FILE)
    rdoc = pm.load_routing(pm.PROVIDERS_DIR / pm.ROUTING_FILE)
    dev = pm.load_devices(pm.PROVIDERS_DIR / pm.DEVICES_FILE)
    return sp.build_desired(cat, pdoc, rdoc, dev)


def _snapshot(desired: dict[str, sp.Rows]) -> dict[str, sp.Rows]:
    return {name: {k: dict(row) for k, row in rows.items()} for name, rows in desired.items()}


def _copy_manifests(dst: Path) -> Path:
    dst.mkdir(parents=True, exist_ok=True)
    for fname in (pm.CAPABILITIES_FILE, pm.PROVIDERS_FILE, pm.ROUTING_FILE, pm.DEVICES_FILE):
        shutil.copyfile(pm.PROVIDERS_DIR / fname, dst / fname)
    return dst


# --------------------------------------------------------------------------- #
# Digest
# --------------------------------------------------------------------------- #
def test_digest_is_deterministic() -> None:
    assert sp.manifest_digest(pm.PROVIDERS_DIR) == sp.manifest_digest(pm.PROVIDERS_DIR)


def test_digest_is_whitespace_and_comment_invariant(tmp_path: Path) -> None:
    d = _copy_manifests(tmp_path / "m")
    # Append blank lines + comments only — no semantic change.
    for fname in (pm.CAPABILITIES_FILE, pm.PROVIDERS_FILE, pm.ROUTING_FILE, pm.DEVICES_FILE):
        p = d / fname
        p.write_text(p.read_text(encoding="utf-8") + "\n\n# trailing comment\n", encoding="utf-8")
    assert sp.manifest_digest(d) == sp.manifest_digest(pm.PROVIDERS_DIR)


def test_digest_changes_when_a_provider_changes(tmp_path: Path) -> None:
    d = _copy_manifests(tmp_path / "m")
    prov = d / pm.PROVIDERS_FILE
    prov.write_text(
        prov.read_text(encoding="utf-8").replace("quality: 70", "quality: 71", 1), encoding="utf-8"
    )
    assert sp.manifest_digest(d) != sp.manifest_digest(pm.PROVIDERS_DIR)


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #
def test_builders_shape_committed_manifest() -> None:
    d = _desired()
    assert ("pollinations",) in d["providers"]
    assert d["provider_adapters"][("pollinations.image",)]["capability_id"] == "image_generation"
    # execution_mode is derived from runtime.execution booleans
    assert d["provider_adapters"][("kokoro.tts",)]["execution_mode"] == "local"
    assert d["provider_adapters"][("pollinations.image",)]["execution_mode"] == "cloud"
    assert ("video_generation", "image_generation", "requires") in d["capability_dependencies"]
    assert d["adapter_fallbacks"][("huggingface.image", "pollinations.image")]["ordinal"] == 0
    assert ("default",) in d["routing_policies"]
    assert ("intel_macbook_2019",) in d["device_profiles"]


def test_quota_unlimited_maps_to_none() -> None:
    d = _desired()
    assert d["providers"][("pollinations",)]["quota_daily"] is None


# --------------------------------------------------------------------------- #
# Plan
# --------------------------------------------------------------------------- #
def test_fresh_database_creates_everything() -> None:
    desired = _desired()
    plan = sp.build_plan(desired, {}, digest="abc", stored_digest=None)
    assert plan.changed is True
    for tp in plan.tables:
        assert len(tp.creates) == len(desired[tp.name])
        assert not tp.updates and not tp.disables and not tp.deletes


def test_reseed_unchanged_writes_nothing() -> None:
    desired = _desired()
    plan = sp.build_plan(desired, _snapshot(desired), digest="abc", stored_digest="abc")
    assert plan.writes == 0
    assert plan.changed is False


def test_single_adapter_change_updates_only_that_row() -> None:
    desired = _desired()
    current = _snapshot(desired)
    current["provider_adapters"][("pollinations.image",)]["cost_amount"] = 999.0
    plan = sp.build_plan(desired, current, digest="new", stored_digest="old")
    assert plan.writes == 1
    adapters = next(t for t in plan.tables if t.name == "provider_adapters")
    assert [r["id"] for r in adapters.updates] == ["pollinations.image"]


def test_removed_provider_is_disabled_not_deleted() -> None:
    desired = _desired()
    current = _snapshot(desired)
    # Remove the comfyui provider + its adapter + its fallback edge from desired.
    del desired["providers"][("comfyui",)]
    del desired["provider_adapters"][("comfyui.flux_schnell",)]
    del desired["adapter_fallbacks"][("comfyui.flux_schnell", "pollinations.image")]

    plan = sp.build_plan(desired, current, digest="new", stored_digest="old")
    providers = next(t for t in plan.tables if t.name == "providers")
    adapters = next(t for t in plan.tables if t.name == "provider_adapters")
    fallbacks = next(t for t in plan.tables if t.name == "adapter_fallbacks")

    assert ("comfyui",) in providers.disables and not providers.deletes
    assert ("comfyui.flux_schnell",) in adapters.disables and not adapters.deletes
    # derived edges are synced (deleted), not disabled
    assert ("comfyui.flux_schnell", "pollinations.image") in fallbacks.deletes


def test_numeric_cost_from_db_does_not_count_as_change() -> None:
    # DB Numeric columns round-trip as Decimal; the manifest carries floats.
    # The diff must treat Decimal('0.02000000') == 0.02 (regression guard: the
    # live round-trip once reported spurious adapter updates for this reason).
    from decimal import Decimal

    desired = _desired()
    current = _snapshot(desired)
    for row in current["provider_adapters"].values():
        if row.get("cost_amount") is not None:
            row["cost_amount"] = Decimal(str(row["cost_amount"])).quantize(Decimal("0.00000001"))
    plan = sp.build_plan(desired, current, digest="same", stored_digest="same")
    assert plan.writes == 0


def test_already_disabled_row_is_not_rewritten() -> None:
    desired = _desired()
    current = _snapshot(desired)
    del desired["providers"][("comfyui",)]
    current["providers"][("comfyui",)]["enabled"] = False  # already disabled
    plan = sp.build_plan(desired, current, digest="new", stored_digest="old")
    providers = next(t for t in plan.tables if t.name == "providers")
    assert ("comfyui",) not in providers.disables
