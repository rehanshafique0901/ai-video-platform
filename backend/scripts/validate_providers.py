"""Offline validator for the α8.5c capability / provider / routing design-time spec.

Pure, deterministic, **no network, no database** (invariant W8.5c.5). Validates
the three manifests under ``backend/providers/`` (``capabilities.yaml`` +
``providers.yaml`` + ``routing.yaml``) against the α8.5c rule set, writes a JSON
report to ``.validation/provider_validation_report.json`` (or ``argv[1]``), and
exits non-zero if any **error** is found. Warnings never fail the gate.

The runtime never imports this module or the YAML it reads (W8.5c.2).
"""

from __future__ import annotations

import importlib
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Make the backend package + this scripts dir importable when run standalone.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from provider_manifest import (  # noqa: E402
    _DURATION_KINDS,
    _FEATURE_KINDS,
    _RESOLUTION_KINDS,
    _VIDEO_ONLY_FEATURES,
    FREE_PRICING,
    KINDS,
    OUTPUT_FORMATS,
    Adapter,
    Catalogue,
    DevicesDoc,
    Provider,
    ProvidersDoc,
    RoutingDoc,
    load_catalogue,
    load_devices,
    load_providers,
    load_routing,
)

from app.infrastructure.generation.registry import (  # noqa: E402
    IMPLEMENTED_IMAGE_ADAPTER_IDS,
)

_ADAPTER_ID_RE = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
_CONFIG_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_PARAM_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# kind -> capability verbs a real adapter class must expose (plus health).
_KIND_VERBS: dict[str, tuple[str, ...]] = {
    "llm": ("generate_text",),
    "image": ("generate_image",),
    "video": ("submit", "resolve"),
    "voice": ("synthesize_voice",),
}


@dataclass
class Report:
    errors: list[dict[str, str]] = field(default_factory=list)
    warnings: list[dict[str, str]] = field(default_factory=list)

    def error(self, rule: str, message: str) -> None:
        self.errors.append({"rule": rule, "message": message})

    def warn(self, rule: str, message: str) -> None:
        self.warnings.append({"rule": rule, "message": message})

    @property
    def ok(self) -> bool:
        return not self.errors


def _dups(values: list[str]) -> list[str]:
    seen: set[str] = set()
    dups: set[str] = set()
    for v in values:
        (dups if v in seen else seen).add(v)
    return sorted(dups)


def _adapters(pdoc: ProvidersDoc) -> list[tuple[Provider, Adapter]]:
    return [(p, a) for p in pdoc.providers for a in p.adapters]


# --------------------------------------------------------------------------- #
# Rules
# --------------------------------------------------------------------------- #


def _rule_uniqueness(cat: Catalogue, pdoc: ProvidersDoc, r: Report) -> None:
    for label, ids in (
        ("provider", [p.id for p in pdoc.providers]),
        ("adapter", [a.id for _, a in _adapters(pdoc)]),
        ("variant", [v.id for f in pdoc.families for v in f.variants]),
        ("capability", [c.id for c in cat.capabilities]),
    ):
        for dup in _dups(ids):
            r.error("uniqueness", f"duplicate {label} id: {dup!r}")


def _rule_capability_metadata(cat: Catalogue, r: Report) -> None:
    for c in cat.capabilities:
        if not c.inputs:
            r.error("capability", f"capability {c.id!r} declares no inputs")
        if not c.outputs:
            r.error("capability", f"capability {c.id!r} declares no outputs")
        for label, params in (("requires", c.requires), ("optional", c.optional)):
            for dup in _dups(params):
                r.error("capability", f"capability {c.id!r} lists {label} param {dup!r} twice")
            for name in params:
                if not _PARAM_RE.match(name):
                    r.error(
                        "capability",
                        f"capability {c.id!r} param {name!r} is not snake_case",
                    )
        overlap = sorted(set(c.requires) & set(c.optional))
        for name in overlap:
            r.error(
                "capability", f"capability {c.id!r} param {name!r} is both required and optional"
            )


def _rule_catalogue_integrity(cat: Catalogue, pdoc: ProvidersDoc, r: Report) -> None:
    cat_ids = {c.id for c in cat.capabilities}
    served: set[str] = set()
    for _, a in _adapters(pdoc):
        served.add(a.capability)
        if a.capability not in cat_ids:
            r.error(
                "catalogue",
                f"adapter {a.id!r} declares capability {a.capability!r} not in the catalogue",
            )
    for cap in sorted(cat_ids - served):
        r.warn("catalogue", f"capability {cap!r} is in the catalogue but served by no adapter")


def _rule_unique_provider_capability(pdoc: ProvidersDoc, r: Report) -> None:
    for p in pdoc.providers:
        for dup in _dups([a.capability for a in p.adapters]):
            r.error(
                "provider_capability",
                f"provider {p.id!r} serves capability {dup!r} more than once",
            )


def _rule_adapter_integrity(cat: Catalogue, pdoc: ProvidersDoc, r: Report) -> None:
    kind_by_cap = {c.id: c.kind for c in cat.capabilities}
    for _, a in _adapters(pdoc):
        if not _ADAPTER_ID_RE.match(a.id):
            r.error("adapter", f"adapter id {a.id!r} does not match '<provider>.<suffix>'")
        if a.status != "implemented":
            continue
        if not a.import_path:
            r.error("adapter", f"implemented adapter {a.id!r} has no import_path")
            continue
        cls = _load_class(a.import_path, a.id, r)
        if cls is None:
            continue
        kind = kind_by_cap.get(a.capability)
        required = ("health", *(_KIND_VERBS.get(str(kind), ())))
        missing = [m for m in required if not hasattr(cls, m)]
        if missing:
            r.error(
                "adapter",
                f"implemented adapter {a.id!r} ({a.import_path}) is missing "
                f"{kind}-protocol members: {', '.join(missing)}",
            )


def _rule_adapter_constraints(cat: Catalogue, pdoc: ProvidersDoc, r: Report) -> None:
    kind_by_cap = {c.id: c.kind for c in cat.capabilities}
    for _, a in _adapters(pdoc):
        kind = str(kind_by_cap.get(a.capability, ""))
        s = a.supports
        if s.max_duration_seconds is not None and kind not in _DURATION_KINDS:
            r.warn(
                "constraints",
                f"adapter {a.id!r} sets max_duration_seconds on a {kind!r} capability",
            )
        if s.max_resolution is not None and kind not in _RESOLUTION_KINDS:
            r.warn(
                "constraints",
                f"adapter {a.id!r} sets max_resolution on a {kind!r} capability",
            )


def _load_class(import_path: str, adapter_id: str, r: Report) -> type | None:
    if ":" not in import_path:
        r.error("adapter", f"adapter {adapter_id!r} import_path must be 'module:Class'")
        return None
    module_name, _, class_name = import_path.partition(":")
    try:
        module = importlib.import_module(module_name)
    except Exception as exc:  # report any import failure as an adapter error
        r.error("adapter", f"adapter {adapter_id!r} import failed: {exc}")
        return None
    cls = getattr(module, class_name, None)
    if not isinstance(cls, type):
        r.error("adapter", f"adapter {adapter_id!r} target {class_name!r} is not a class")
        return None
    return cls


def _rule_fallback_graph(pdoc: ProvidersDoc, r: Report) -> None:
    cap_by_adapter = {a.id: a.capability for _, a in _adapters(pdoc)}
    edges: dict[str, list[str]] = {}
    for _, a in _adapters(pdoc):
        edges[a.id] = list(a.fallback)
        for target in a.fallback:
            if target == a.id:
                r.error("fallback", f"adapter {a.id!r} lists itself as a fallback")
            elif target not in cap_by_adapter:
                r.error("fallback", f"adapter {a.id!r} falls back to unknown adapter {target!r}")
            elif cap_by_adapter[target] != a.capability:
                r.error(
                    "fallback",
                    f"adapter {a.id!r} ({a.capability}) falls back to {target!r} "
                    f"({cap_by_adapter[target]}) — incompatible capability",
                )
    cycle = _find_cycle(edges)
    if cycle:
        r.error("fallback", f"fallback cycle detected: {' -> '.join(cycle)}")


def _rule_families(pdoc: ProvidersDoc, r: Report) -> None:
    provider_ids = {p.id for p in pdoc.providers}
    family_ids = {f.id for f in pdoc.families}
    for f in pdoc.families:
        for v in f.variants:
            if v.provider not in provider_ids:
                r.error("family", f"variant {v.id!r} references unknown provider {v.provider!r}")
        if f.parent is not None and f.parent not in family_ids:
            r.error("family", f"family {f.id!r} has unknown parent {f.parent!r}")
    parent_edges = {f.id: ([f.parent] if f.parent else []) for f in pdoc.families}
    cycle = _find_cycle(parent_edges)
    if cycle:
        r.error("family", f"family inheritance cycle detected: {' -> '.join(cycle)}")


def _rule_pricing(pdoc: ProvidersDoc, r: Report) -> None:
    for p in pdoc.providers:
        for name, limit in (("daily", p.quota.daily), ("monthly", p.quota.monthly)):
            if isinstance(limit, int) and limit <= 0:
                r.error("pricing", f"provider {p.id!r} has a non-positive {name} quota ({limit})")
        if str(p.pricing) in FREE_PRICING and p.quota.daily is None and p.quota.monthly is None:
            r.warn(
                "pricing",
                f"provider {p.id!r} is {p.pricing} but declares no daily/monthly quota",
            )


def _rule_routing(cat: Catalogue, rdoc: RoutingDoc, r: Report) -> None:
    cat_ids = {c.id for c in cat.capabilities}
    for cap in rdoc.by_capability:
        if cap not in cat_ids:
            r.error("routing", f"routing.by_capability references unknown capability {cap!r}")


def _rule_config_keys(pdoc: ProvidersDoc, r: Report) -> None:
    for p in pdoc.providers:
        for key in p.config_keys:
            if "=" in key or re.search(r"\s", key):
                r.error(
                    "config_keys",
                    f"provider {p.id!r} config key {key!r} looks like a value — declare KEY NAMES only",
                )
            elif not _CONFIG_KEY_RE.match(key):
                r.warn(
                    "config_keys",
                    f"provider {p.id!r} config key {key!r} is not UPPER_SNAKE_CASE",
                )


def _rule_anti_drift(cat: Catalogue, r: Report) -> None:
    try:
        from app.application.interfaces.providers import Capability
        from app.infrastructure.db.enums import plugin_kind_enum
    except Exception as exc:  # surface import trouble as a rule failure
        r.error("anti_drift", f"could not import the code capability vocabulary: {exc}")
        return
    code_caps = {c.value for c in Capability}
    if not code_caps <= KINDS:
        r.error(
            "anti_drift", f"code Capability enum drifted from kinds: {sorted(code_caps - KINDS)}"
        )
    enum_values = set(plugin_kind_enum.enums)
    if enum_values != KINDS:
        r.error("anti_drift", f"plugin_kind_enum {sorted(enum_values)} != kinds {sorted(KINDS)}")
    for c in cat.capabilities:
        if c.kind not in KINDS:
            r.error("anti_drift", f"catalogue kind {c.kind!r} not in the coarse routing vocabulary")


def _rule_free_provider_sanity(pdoc: ProvidersDoc, rdoc: RoutingDoc, r: Report) -> None:
    default_strategy = str(rdoc.defaults.strategy)
    by_cap = rdoc.by_capability
    served: dict[str, list[Provider]] = {}
    for p, a in _adapters(pdoc):
        served.setdefault(a.capability, []).append(p)
    for cap, providers in served.items():
        policy = by_cap.get(cap)
        strategy = str(policy.strategy) if policy and policy.strategy else default_strategy
        if strategy in {"free_first", "free_only"} and not any(
            str(p.pricing) in FREE_PRICING for p in providers
        ):
            r.error(
                "free_provider_sanity",
                f"capability {cap!r} uses strategy {strategy!r} but no serving provider is free",
            )


def _find_cycle(edges: dict[str, list[str]]) -> list[str] | None:
    unseen, active, done = 0, 1, 2
    color = {node: unseen for node in edges}
    stack: list[str] = []

    def visit(node: str) -> list[str] | None:
        color[node] = active
        stack.append(node)
        for nxt in edges.get(node, []):
            if nxt not in color:
                continue
            if color[nxt] == active:
                return stack[stack.index(nxt) :] + [nxt]
            if color[nxt] == unseen:
                found = visit(nxt)
                if found:
                    return found
        color[node] = done
        stack.pop()
        return None

    for node in edges:
        if color[node] == unseen:
            found = visit(node)
            if found:
                return found
    return None


def _rule_capability_dependencies(cat: Catalogue, r: Report) -> None:
    cat_ids = {c.id for c in cat.capabilities}
    dep_edges: dict[str, list[str]] = {}
    for c in cat.capabilities:
        req, opt = c.dependencies.requires, c.dependencies.optional
        for label, deps in (("requires", req), ("optional", opt)):
            for dup in _dups(deps):
                r.error(
                    "dependencies", f"capability {c.id!r} lists {label} dependency {dup!r} twice"
                )
            for d in deps:
                if d == c.id:
                    r.error("dependencies", f"capability {c.id!r} depends on itself ({label})")
                elif d not in cat_ids:
                    r.error(
                        "dependencies",
                        f"capability {c.id!r} {label} unknown capability {d!r}",
                    )
        for d in sorted(set(req) & set(opt)):
            r.error(
                "dependencies",
                f"capability {c.id!r} dependency {d!r} is both required and optional",
            )
        dep_edges[c.id] = [d for d in req if d in cat_ids]
    cycle = _find_cycle(dep_edges)
    if cycle:
        r.error("dependencies", f"capability requires-cycle detected: {' -> '.join(cycle)}")


def _rule_features(cat: Catalogue, pdoc: ProvidersDoc, r: Report) -> None:
    kind_by_cap = {c.id: c.kind for c in cat.capabilities}
    for _, a in _adapters(pdoc):
        kind = str(kind_by_cap.get(a.capability, ""))
        for dup in _dups([str(f) for f in a.features]):
            r.error("features", f"adapter {a.id!r} lists feature {dup!r} twice")
        if a.features and kind not in _FEATURE_KINDS:
            r.warn("features", f"adapter {a.id!r} declares features on a {kind!r} capability")
        for f in a.features:
            if str(f) in _VIDEO_ONLY_FEATURES and kind != "video":
                r.warn(
                    "features",
                    f"adapter {a.id!r} sets video-only feature {str(f)!r} on a {kind!r} capability",
                )


def _rule_output_formats(cat: Catalogue, pdoc: ProvidersDoc, r: Report) -> None:
    outputs_by_cap = {c.id: {str(o) for o in c.outputs} for c in cat.capabilities}
    for _, a in _adapters(pdoc):
        cap_outputs = outputs_by_cap.get(a.capability, set())
        for io_type, formats in a.outputs.items():
            if io_type not in OUTPUT_FORMATS:
                r.error("outputs", f"adapter {a.id!r} declares unknown output io-type {io_type!r}")
                continue
            if io_type not in cap_outputs:
                r.error(
                    "outputs",
                    f"adapter {a.id!r} declares {io_type!r} outputs but capability "
                    f"{a.capability!r} does not output {io_type!r}",
                )
            if not formats:
                r.error("outputs", f"adapter {a.id!r} declares an empty {io_type!r} format list")
            for dup in _dups(list(formats)):
                r.error("outputs", f"adapter {a.id!r} lists {io_type} format {dup!r} twice")
            for fmt in formats:
                if fmt not in OUTPUT_FORMATS[io_type]:
                    r.error(
                        "outputs",
                        f"adapter {a.id!r} {io_type} format {fmt!r} not in the {io_type} vocabulary",
                    )


def _rule_resource_estimation(pdoc: ProvidersDoc, r: Report) -> None:
    for _, a in _adapters(pdoc):
        hw = a.runtime.hardware
        if (
            hw.minimum_ram_gb is not None
            and hw.recommended_ram_gb is not None
            and hw.recommended_ram_gb < hw.minimum_ram_gb
        ):
            r.error(
                "runtime",
                f"adapter {a.id!r} recommended_ram_gb ({hw.recommended_ram_gb}) "
                f"< minimum_ram_gb ({hw.minimum_ram_gb})",
            )
        ex = a.runtime.execution
        any_gpu = hw.gpu.metal or hw.gpu.cuda or hw.gpu.rocm or hw.gpu.cpu
        if ex.local and not any_gpu:
            r.warn("runtime", f"local adapter {a.id!r} declares no gpu/cpu backend")
        if (hw.minimum_ram_gb or any_gpu) and not (ex.local or ex.cloud):
            r.warn("runtime", f"adapter {a.id!r} declares hardware but no execution target")


def _rule_cost(pdoc: ProvidersDoc, r: Report) -> None:
    for p, a in _adapters(pdoc):
        if a.cost is not None and str(p.pricing) == "free" and a.cost.amount > 0:
            r.warn(
                "cost",
                f"adapter {a.id!r} on free provider {p.id!r} declares non-zero cost "
                f"({a.cost.amount})",
            )


def _rule_registry_reconciliation(
    pdoc: ProvidersDoc, implemented_adapter_ids: frozenset[str], r: Report
) -> None:
    """Every adapter this build can construct must be a real catalogue adapter id.

    ADR-0054: the registry key *is* the executable-set entry the resolver is told about
    and the identity execution provenance records, so a key that names no catalogue
    adapter is unreachable code with a provenance value nothing can interpret.

    Weakest useful form on purpose. The converse — every ``implemented: true`` manifest
    adapter having code — is not asserted yet; ADR-0045 F5's protocol mismatch has to be
    settled before the manifest's ``implemented`` flag can be held to that standard.
    """
    manifest_ids = {a.id for _, a in _adapters(pdoc)}
    for adapter_id in sorted(implemented_adapter_ids):
        if adapter_id not in manifest_ids:
            r.error(
                "registry_reconciliation",
                f"registered image adapter {adapter_id!r} is not in the provider manifest",
            )


def _rule_devices(devices: DevicesDoc | None, r: Report) -> None:
    if devices is None:
        return
    for dup in _dups([d.id for d in devices.device_profiles]):
        r.error("devices", f"duplicate device profile id: {dup!r}")


def validate(
    cat: Catalogue,
    pdoc: ProvidersDoc,
    rdoc: RoutingDoc,
    devices: DevicesDoc | None = None,
    *,
    implemented_adapter_ids: frozenset[str] | None = None,
) -> Report:
    """Validate the manifests. ``implemented_adapter_ids`` defaults to this build's
    registry keys; tests validating synthetic manifests pass their own set."""
    implemented = (
        IMPLEMENTED_IMAGE_ADAPTER_IDS
        if implemented_adapter_ids is None
        else implemented_adapter_ids
    )
    r = Report()
    _rule_uniqueness(cat, pdoc, r)
    _rule_capability_metadata(cat, r)
    _rule_capability_dependencies(cat, r)
    _rule_catalogue_integrity(cat, pdoc, r)
    _rule_unique_provider_capability(pdoc, r)
    _rule_adapter_integrity(cat, pdoc, r)
    _rule_adapter_constraints(cat, pdoc, r)
    _rule_features(cat, pdoc, r)
    _rule_output_formats(cat, pdoc, r)
    _rule_resource_estimation(pdoc, r)
    _rule_cost(pdoc, r)
    _rule_fallback_graph(pdoc, r)
    _rule_families(pdoc, r)
    _rule_pricing(pdoc, r)
    _rule_routing(cat, rdoc, r)
    _rule_config_keys(pdoc, r)
    _rule_anti_drift(cat, r)
    _rule_free_provider_sanity(pdoc, rdoc, r)
    _rule_registry_reconciliation(pdoc, implemented, r)
    _rule_devices(devices, r)
    return r


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main(argv: list[str]) -> int:
    providers_dir = ROOT / "providers"
    out_path = ROOT / ".validation" / "provider_validation_report.json"
    if len(argv) > 1 and argv[1]:
        out_path = Path(argv[1])
    out_path.parent.mkdir(parents=True, exist_ok=True)

    cap_file = providers_dir / "capabilities.yaml"
    prov_file = providers_dir / "providers.yaml"
    routing_file = providers_dir / "routing.yaml"
    present = [f.exists() for f in (cap_file, prov_file, routing_file)]
    if not any(present):
        print(f"provider manifest not found under {providers_dir} — nothing to validate")
        return 0
    if not all(present):
        missing = [
            f.name
            for f, ok in zip((cap_file, prov_file, routing_file), present, strict=True)
            if not ok
        ]
        report = {
            "ok": False,
            "errors": [{"rule": "manifest", "message": f"incomplete manifest — missing {missing}"}],
            "warnings": [],
        }
        out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"[FAIL] incomplete provider manifest — missing {missing}")
        return 1

    devices_file = providers_dir / "devices.yaml"
    try:
        cat = load_catalogue(cap_file)
        pdoc = load_providers(prov_file)
        rdoc = load_routing(routing_file)
        devices = load_devices(devices_file) if devices_file.exists() else None
    except Exception as exc:  # schema/parse errors become the report
        report = {"ok": False, "errors": [{"rule": "schema", "message": str(exc)}], "warnings": []}
        out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"[FAIL] provider manifest schema invalid:\n{exc}")
        return 1

    r = validate(cat, pdoc, rdoc, devices)
    report = {"ok": r.ok, "errors": r.errors, "warnings": r.warnings}
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    by_rule: dict[str, int] = {}
    for w in r.warnings:
        by_rule[w["rule"]] = by_rule.get(w["rule"], 0) + 1
    for rule, count in sorted(by_rule.items()):
        print(f"  warn [{rule}] {count} warning(s) — see the JSON report for detail")
    for e in r.errors:
        print(f"  ERROR [{e['rule']}] {e['message']}")
    if r.ok:
        n_devices = len(devices.device_profiles) if devices else 0
        print(
            f"[ OK ] provider manifest valid — {len(cat.capabilities)} capabilities, "
            f"{len(pdoc.providers)} providers, {n_devices} device profile(s), "
            f"{len(r.warnings)} warning(s)"
        )
        return 0
    print(f"[FAIL] provider manifest invalid — {len(r.errors)} error(s)")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
