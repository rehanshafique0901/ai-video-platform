"""Offline validator for the α8.5c capability catalogue + provider registry.

Pure, deterministic, **no network, no database** (invariant W8.5c.5). Validates
the design-time YAML spec under ``backend/providers/`` against the rule set in
``docs/engineering/PHASE3_ALPHA8_5c_PREFLIGHT.md`` §5, writes a JSON report to
``.validation/provider_validation_report.json`` (or ``argv[1]``), and exits
non-zero if any **error** is found. Warnings never fail the gate.

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
    KINDS,
    Catalogue,
    Provider,
    Registry,
    load_catalogue,
    load_registry,
)

_ADAPTER_ID_RE = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
_CONFIG_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_FREE_STRATEGIES = {"free_first", "free_only"}

# kind -> capability verbs a real adapter class must expose (plus metadata/health).
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


def _adapters(reg: Registry) -> list[tuple[Provider, object]]:
    return [(p, a) for p in reg.providers for a in p.adapters]


# --------------------------------------------------------------------------- #
# Rules
# --------------------------------------------------------------------------- #


def _rule_uniqueness(cat: Catalogue, reg: Registry, r: Report) -> None:
    for label, ids in (
        ("provider", [p.id for p in reg.providers]),
        ("adapter", [a.id for _, a in _adapters(reg)]),
        ("variant", [v.id for f in reg.families for v in f.variants]),
        ("capability", [c.id for c in cat.capabilities]),
    ):
        for dup in _dups(ids):
            r.error("uniqueness", f"duplicate {label} id: {dup!r}")


def _rule_catalogue_integrity(cat: Catalogue, reg: Registry, r: Report) -> None:
    cat_ids = {c.id for c in cat.capabilities}
    for c in cat.capabilities:
        if c.kind not in KINDS:
            r.error("catalogue", f"capability {c.id!r} has unknown kind {c.kind!r}")
    served: set[str] = set()
    for _, a in _adapters(reg):
        served.add(a.capability)
        if a.capability not in cat_ids:
            r.error(
                "catalogue",
                f"adapter {a.id!r} declares capability {a.capability!r} not in the catalogue",
            )
    for cap in sorted(cat_ids - served):
        r.warn("catalogue", f"capability {cap!r} is in the catalogue but served by no adapter")


def _rule_unique_provider_capability(reg: Registry, r: Report) -> None:
    for p in reg.providers:
        for dup in _dups([a.capability for a in p.adapters]):
            r.error(
                "provider_capability",
                f"provider {p.id!r} serves capability {dup!r} more than once",
            )


def _rule_adapter_integrity(cat: Catalogue, reg: Registry, r: Report) -> None:
    kind_by_cap = {c.id: c.kind for c in cat.capabilities}
    for _, a in _adapters(reg):
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


def _rule_fallback_graph(reg: Registry, r: Report) -> None:
    cap_by_adapter = {a.id: a.capability for _, a in _adapters(reg)}
    edges: dict[str, list[str]] = {}
    for _, a in _adapters(reg):
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


def _rule_families(reg: Registry, r: Report) -> None:
    provider_ids = {p.id for p in reg.providers}
    family_ids = {f.id for f in reg.families}
    for f in reg.families:
        for v in f.variants:
            if v.provider not in provider_ids:
                r.error("family", f"variant {v.id!r} references unknown provider {v.provider!r}")
        if f.parent is not None and f.parent not in family_ids:
            r.error("family", f"family {f.id!r} has unknown parent {f.parent!r}")
    parent_edges = {f.id: ([f.parent] if f.parent else []) for f in reg.families}
    cycle = _find_cycle(parent_edges)
    if cycle:
        r.error("family", f"family inheritance cycle detected: {' -> '.join(cycle)}")


def _rule_free_tier(reg: Registry, r: Report) -> None:
    for p in reg.providers:
        free = p.free
        if free.api_key_required and p.authentication == "none":
            r.error(
                "free_tier",
                f"provider {p.id!r} sets api_key_required but authentication is 'none'",
            )
        if not free.available:
            for name, limit in (
                ("daily_limit", free.daily_limit),
                ("monthly_limit", free.monthly_limit),
            ):
                if limit == "unlimited" or (isinstance(limit, int) and limit > 0):
                    r.error(
                        "free_tier",
                        f"provider {p.id!r} has free.available=false but a positive {name} ({limit!r})",
                    )


def _rule_routing(cat: Catalogue, reg: Registry, r: Report) -> None:
    cat_ids = {c.id for c in cat.capabilities}
    for cap in reg.routing.by_capability:
        if cap not in cat_ids:
            r.error("routing", f"routing.by_capability references unknown capability {cap!r}")


def _rule_config_keys(reg: Registry, r: Report) -> None:
    for p in reg.providers:
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


def _rule_free_provider_sanity(cat: Catalogue, reg: Registry, r: Report) -> None:
    default_strategy = str(reg.routing.defaults.strategy)
    by_cap = reg.routing.by_capability
    served: dict[str, list[Provider]] = {}
    for p, a in _adapters(reg):
        served.setdefault(a.capability, []).append(p)
    for cap, providers in served.items():
        policy = by_cap.get(cap)
        strategy = str(policy.strategy) if policy and policy.strategy else default_strategy
        if strategy in _FREE_STRATEGIES and not any(p.free.available for p in providers):
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


def validate(cat: Catalogue, reg: Registry) -> Report:
    r = Report()
    _rule_uniqueness(cat, reg, r)
    _rule_catalogue_integrity(cat, reg, r)
    _rule_unique_provider_capability(reg, r)
    _rule_adapter_integrity(cat, reg, r)
    _rule_fallback_graph(reg, r)
    _rule_families(reg, r)
    _rule_free_tier(reg, r)
    _rule_routing(cat, reg, r)
    _rule_config_keys(reg, r)
    _rule_anti_drift(cat, r)
    _rule_free_provider_sanity(cat, reg, r)
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
    reg_file = providers_dir / "registry.yaml"
    if not cap_file.exists() or not reg_file.exists():
        print(f"provider manifest not found under {providers_dir} — nothing to validate")
        return 0

    try:
        cat = load_catalogue(cap_file)
        reg = load_registry(reg_file)
    except Exception as exc:  # schema/parse errors become the report
        report = {"ok": False, "errors": [{"rule": "schema", "message": str(exc)}], "warnings": []}
        out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"[FAIL] provider manifest schema invalid:\n{exc}")
        return 1

    r = validate(cat, reg)
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
        print(
            f"[ OK ] provider manifest valid — {len(cat.capabilities)} capabilities, "
            f"{len(reg.providers)} providers, {len(r.warnings)} warning(s)"
        )
        return 0
    print(f"[FAIL] provider manifest invalid — {len(r.errors)} error(s)")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
