"""α8.5d Phase 2 — provider capability-catalogue **seeder**.

One responsibility only: **populate and maintain the provider catalogue database
from the validated YAML manifests** (migration `0010` tables). It is *not* a
resolver, *not* a planner; it performs no provider selection, scoring, health,
routing, or cost optimisation (those are α8.5e+). See
`docs/engineering/PROVIDER_RUNTIME_DATA_MODEL.md` and the α8.5d pre-flight §6.

Flow (W8.5c.2 / W8.5d.1 — runtime never reads YAML; seeder is the only writer):

    capabilities.yaml + providers.yaml + routing.yaml + devices.yaml
        │  load + VALIDATE (never seed an invalid manifest)
        ▼
    manifest digest  (SHA-256 of the canonicalised manifests)
        │  == provider_registry_meta.manifest_digest ?  → YES: exit, no writes
        ▼  (NO)
    idempotent upsert by natural key, in dependency order
        │  removed entities → disabled (enabled=false), never deleted (W8.5d.3)
        │  removed derived edges (deps/fallbacks) → synced (deleted)
        ▼
    provider_registry_meta (digest, revision++, catalogue_version, provenance)

Design: a **pure plan layer** (digest + desired-state builders + diff) with no DB,
fully unit-tested; and a **thin apply layer** (Postgres upserts) exercised by the
Phase 3 CI round-trip against a real database. W8.5d.10 is honoured structurally —
this seeder only ever writes catalogue columns; operational state is out of scope.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from provider_manifest import (  # noqa: E402
    CAPABILITIES_FILE,
    DEVICES_FILE,
    PROVIDERS_DIR,
    PROVIDERS_FILE,
    ROUTING_FILE,
    AdapterStatus,
    Authentication,
    Catalogue,
    CostSource,
    CostUnit,
    DeviceBackend,
    DevicesDoc,
    FallbackMode,
    GenerationMode,
    Kind,
    Pricing,
    ProvidersDoc,
    RoutingDoc,
    RoutingStrategy,
    Selection,
    load_catalogue,
    load_devices,
    load_providers,
    load_routing,
)
from validate_providers import validate  # noqa: E402

# --------------------------------------------------------------------------- #
# Provenance constants
# --------------------------------------------------------------------------- #
CATALOGUE_VERSION = "2026.07"  # declared human date-version; bump on notable releases
GENERATOR_VERSION = "seed_providers/1.0"

Key = tuple[str, ...]
Rows = dict[Key, dict[str, Any]]


# --------------------------------------------------------------------------- #
# Digest — SHA-256 over the canonicalised manifests (stable key order, UTF-8)
# --------------------------------------------------------------------------- #
def _canonical(path: Path) -> str:
    obj = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else None
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def manifest_digest(providers_dir: Path) -> str:
    """Deterministic digest of the four manifests (missing devices.yaml ⇒ null)."""
    parts = [
        f"{label}:{_canonical(providers_dir / fname)}"
        for label, fname in (
            ("capabilities", CAPABILITIES_FILE),
            ("providers", PROVIDERS_FILE),
            ("routing", ROUTING_FILE),
            ("devices", DEVICES_FILE),
        )
    ]
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Desired-state builders (Pydantic models → catalogue column dicts)
# --------------------------------------------------------------------------- #
def _limit(v: Any) -> int | None:
    return None if v is None or v == "unlimited" else int(v)


def _execution_mode(local: bool, cloud: bool) -> str | None:
    if local and cloud:
        return "hybrid"
    if local:
        return "local"
    if cloud:
        return "cloud"
    return None


def build_capabilities(cat: Catalogue) -> Rows:
    return {
        (c.id,): {
            "id": c.id,
            "kind": str(c.kind),
            "inputs": [str(x) for x in c.inputs],
            "outputs": [str(x) for x in c.outputs],
            "requires": list(c.requires),
            "optional": list(c.optional),
        }
        for c in cat.capabilities
    }


def build_capability_dependencies(cat: Catalogue) -> Rows:
    rows: Rows = {}
    for c in cat.capabilities:
        for kind, deps in (
            ("requires", c.dependencies.requires),
            ("optional", c.dependencies.optional),
        ):
            for dep in deps:
                rows[(c.id, dep, kind)] = {
                    "capability_id": c.id,
                    "depends_on_id": dep,
                    "kind": kind,
                }
    return rows


def build_providers(pdoc: ProvidersDoc) -> Rows:
    return {
        (p.id,): {
            "id": p.id,
            "name": p.name,
            "homepage": p.homepage,
            "license": p.license,
            "commercial": p.commercial,
            "authentication": str(p.authentication),
            "requires_login": p.requires_login,
            "pricing": str(p.pricing),
            "quota_daily": _limit(p.quota.daily),
            "quota_monthly": _limit(p.quota.monthly),
            "config_keys": list(p.config_keys),
            "score_quality": p.scores.quality,
            "score_cost": p.scores.cost,
            "score_speed": p.scores.speed,
            "score_reliability": p.scores.reliability,
            "enabled": True,
        }
        for p in pdoc.providers
    }


def build_provider_adapters(pdoc: ProvidersDoc) -> Rows:
    rows: Rows = {}
    for p in pdoc.providers:
        for a in p.adapters:
            cost = a.cost
            rows[(a.id,)] = {
                "id": a.id,
                "provider_id": p.id,
                "capability_id": a.capability,
                "status": str(a.status),
                "execution_mode": _execution_mode(
                    a.runtime.execution.local, a.runtime.execution.cloud
                ),
                "implemented": a.status == AdapterStatus.IMPLEMENTED,
                "enabled": True,
                "import_path": a.import_path,
                "cost_unit": str(cost.unit) if cost else None,
                "cost_amount": float(cost.amount) if cost else None,
                "cost_currency": cost.currency if cost else None,
                "cost_source": str(cost.source) if cost else None,
                # derived estimates (D-G) are computed by a later slice; None for now.
                "estimated_generation_cost": None,
                "estimated_download_cost": None,
                "estimated_gpu_minutes": None,
                "supports": a.supports.model_dump(mode="json", by_alias=True, exclude_none=True),
                "runtime": a.runtime.model_dump(mode="json", exclude_none=True),
                "features": [str(f) for f in a.features],
                "outputs": {k: list(v) for k, v in a.outputs.items()},
            }
    return rows


def build_adapter_fallbacks(pdoc: ProvidersDoc) -> Rows:
    rows: Rows = {}
    for p in pdoc.providers:
        for a in p.adapters:
            for ordinal, fb in enumerate(a.fallback):
                rows[(a.id, fb)] = {
                    "adapter_id": a.id,
                    "fallback_adapter_id": fb,
                    "reason": None,
                    "ordinal": ordinal,
                }
    return rows


def build_routing_policies(rdoc: RoutingDoc) -> Rows:
    d = rdoc.defaults
    rows: Rows = {
        ("default",): {
            "scope": "default",
            "strategy": str(d.strategy),
            "fallback": str(d.fallback),
            "selection": str(d.selection),
        }
    }
    # Flatten defaults into each per-capability policy (no runtime inheritance).
    for scope, pol in rdoc.by_capability.items():
        rows[(scope,)] = {
            "scope": scope,
            "strategy": str(pol.strategy or d.strategy),
            "fallback": str(pol.fallback or d.fallback),
            "selection": str(pol.selection or d.selection),
        }
    return rows


def build_device_profiles(devices: DevicesDoc | None) -> Rows:
    if devices is None:
        return {}
    return {
        (dp.id,): {
            "id": dp.id,
            "ram_gb": dp.ram_gb,
            "gpu": dp.gpu,
            "backend": str(dp.backend),
            "unified_memory": dp.unified_memory,
            "preferred_mode": str(dp.preferred_mode),
        }
        for dp in devices.device_profiles
    }


# --------------------------------------------------------------------------- #
# Table specs (dependency order) + desired-state assembly
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class TableSpec:
    name: str
    key: tuple[str, ...]
    columns: tuple[str, ...]
    removal: str  # "disable" | "delete" | "retain"
    has_updated_at: bool = True
    has_enabled: bool = False


TABLE_SPECS: tuple[TableSpec, ...] = (
    TableSpec(
        "capabilities",
        ("id",),
        ("id", "kind", "inputs", "outputs", "requires", "optional"),
        removal="retain",
    ),
    TableSpec(
        "capability_dependencies",
        ("capability_id", "depends_on_id", "kind"),
        ("capability_id", "depends_on_id", "kind"),
        removal="delete",
        has_updated_at=False,
    ),
    TableSpec(
        "providers",
        ("id",),
        (
            "id",
            "name",
            "homepage",
            "license",
            "commercial",
            "authentication",
            "requires_login",
            "pricing",
            "quota_daily",
            "quota_monthly",
            "config_keys",
            "score_quality",
            "score_cost",
            "score_speed",
            "score_reliability",
            "enabled",
        ),
        removal="disable",
        has_enabled=True,
    ),
    TableSpec(
        "provider_adapters",
        ("id",),
        (
            "id",
            "provider_id",
            "capability_id",
            "status",
            "execution_mode",
            "implemented",
            "enabled",
            "import_path",
            "cost_unit",
            "cost_amount",
            "cost_currency",
            "cost_source",
            "estimated_generation_cost",
            "estimated_download_cost",
            "estimated_gpu_minutes",
            "supports",
            "runtime",
            "features",
            "outputs",
        ),
        removal="disable",
        has_enabled=True,
    ),
    TableSpec(
        "adapter_fallbacks",
        ("adapter_id", "fallback_adapter_id"),
        ("adapter_id", "fallback_adapter_id", "reason", "ordinal"),
        removal="delete",
        has_updated_at=False,
    ),
    TableSpec(
        "routing_policies",
        ("scope",),
        ("scope", "strategy", "fallback", "selection"),
        removal="retain",
    ),
    TableSpec(
        "device_profiles",
        ("id",),
        ("id", "ram_gb", "gpu", "backend", "unified_memory", "preferred_mode"),
        removal="retain",
    ),
)

SPEC_BY_NAME = {s.name: s for s in TABLE_SPECS}


def build_desired(
    cat: Catalogue, pdoc: ProvidersDoc, rdoc: RoutingDoc, devices: DevicesDoc | None
) -> dict[str, Rows]:
    return {
        "capabilities": build_capabilities(cat),
        "capability_dependencies": build_capability_dependencies(cat),
        "providers": build_providers(pdoc),
        "provider_adapters": build_provider_adapters(pdoc),
        "adapter_fallbacks": build_adapter_fallbacks(pdoc),
        "routing_policies": build_routing_policies(rdoc),
        "device_profiles": build_device_profiles(devices),
    }


# --------------------------------------------------------------------------- #
# Plan (pure diff of desired vs current snapshot)
# --------------------------------------------------------------------------- #
@dataclass
class TablePlan:
    name: str
    creates: list[dict[str, Any]] = field(default_factory=list)
    updates: list[dict[str, Any]] = field(default_factory=list)
    disables: list[Key] = field(default_factory=list)
    deletes: list[Key] = field(default_factory=list)
    unchanged: int = 0

    @property
    def writes(self) -> int:
        return len(self.creates) + len(self.updates) + len(self.disables) + len(self.deletes)


@dataclass
class SeedPlan:
    digest: str
    changed: bool
    tables: list[TablePlan] = field(default_factory=list)

    @property
    def writes(self) -> int:
        return sum(t.writes for t in self.tables)


def _norm(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def _differs(desired: dict[str, Any], current: dict[str, Any]) -> bool:
    return any(_norm(v) != _norm(current.get(k)) for k, v in desired.items())


def build_table_plan(spec: TableSpec, desired: Rows, current: Rows) -> TablePlan:
    plan = TablePlan(name=spec.name)
    for key, row in desired.items():
        cur = current.get(key)
        if cur is None:
            plan.creates.append(row)
        elif _differs(row, cur):
            plan.updates.append(row)
        else:
            plan.unchanged += 1
    for key, cur in current.items():
        if key in desired:
            continue
        if spec.removal == "disable":
            if cur.get("enabled", False):  # only disable rows still active
                plan.disables.append(key)
        elif spec.removal == "delete":
            plan.deletes.append(key)
        # "retain": leave stale rows untouched (non-destructive; rare, reviewed)
    return plan


def build_plan(
    desired: dict[str, Rows], current: dict[str, Rows], digest: str, stored_digest: str | None
) -> SeedPlan:
    plan = SeedPlan(digest=digest, changed=digest != stored_digest)
    for spec in TABLE_SPECS:
        plan.tables.append(
            build_table_plan(spec, desired.get(spec.name, {}), current.get(spec.name, {}))
        )
    return plan


# --------------------------------------------------------------------------- #
# DB layer (thin apply — Postgres; exercised by the Phase 3 CI round-trip)
# --------------------------------------------------------------------------- #
def _catalogue_metadata() -> Any:
    """Local SQLAlchemy table definitions for writing (script-local, not runtime models)."""
    import sqlalchemy as sa
    from sqlalchemy.dialects.postgresql import ARRAY, ENUM, JSONB

    def enum(name: str, values: list[str]) -> ENUM:
        return ENUM(*values, name=name, create_type=False)

    md = sa.MetaData()
    ts = lambda: sa.DateTime(timezone=True)  # noqa: E731

    sa.Table(
        "capabilities",
        md,
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("kind", enum("plugin_kind", [e.value for e in Kind])),
        sa.Column("inputs", ARRAY(sa.Text)),
        sa.Column("outputs", ARRAY(sa.Text)),
        sa.Column("requires", ARRAY(sa.Text)),
        sa.Column("optional", ARRAY(sa.Text)),
        sa.Column("updated_at", ts()),
    )
    sa.Table(
        "capability_dependencies",
        md,
        sa.Column("capability_id", sa.Text, primary_key=True),
        sa.Column("depends_on_id", sa.Text, primary_key=True),
        sa.Column("kind", enum("capability_dep_kind", ["requires", "optional"]), primary_key=True),
    )
    sa.Table(
        "providers",
        md,
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("name", sa.Text),
        sa.Column("homepage", sa.Text),
        sa.Column("license", sa.Text),
        sa.Column("commercial", sa.Boolean),
        sa.Column("authentication", enum("provider_auth", [e.value for e in Authentication])),
        sa.Column("requires_login", sa.Boolean),
        sa.Column("pricing", enum("provider_pricing", [e.value for e in Pricing])),
        sa.Column("quota_daily", sa.Integer),
        sa.Column("quota_monthly", sa.Integer),
        sa.Column("config_keys", ARRAY(sa.Text)),
        sa.Column("score_quality", sa.SmallInteger),
        sa.Column("score_cost", sa.SmallInteger),
        sa.Column("score_speed", sa.SmallInteger),
        sa.Column("score_reliability", sa.SmallInteger),
        sa.Column("enabled", sa.Boolean),
        sa.Column("updated_at", ts()),
    )
    sa.Table(
        "provider_adapters",
        md,
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("provider_id", sa.Text),
        sa.Column("capability_id", sa.Text),
        sa.Column("status", enum("adapter_status", [e.value for e in AdapterStatus])),
        sa.Column("execution_mode", enum("adapter_execution_mode", ["local", "cloud", "hybrid"])),
        sa.Column("implemented", sa.Boolean),
        sa.Column("enabled", sa.Boolean),
        sa.Column("import_path", sa.Text),
        sa.Column("cost_unit", enum("cost_unit", [e.value for e in CostUnit])),
        sa.Column("cost_amount", sa.Numeric(18, 8)),
        sa.Column("cost_currency", sa.String(3)),
        sa.Column("cost_source", enum("cost_source", [e.value for e in CostSource])),
        sa.Column("estimated_generation_cost", sa.Numeric(18, 8)),
        sa.Column("estimated_download_cost", sa.Numeric(18, 8)),
        sa.Column("estimated_gpu_minutes", sa.Numeric(12, 4)),
        sa.Column("supports", JSONB),
        sa.Column("runtime", JSONB),
        sa.Column("features", JSONB),
        sa.Column("outputs", JSONB),
        sa.Column("updated_at", ts()),
    )
    sa.Table(
        "adapter_fallbacks",
        md,
        sa.Column("adapter_id", sa.Text, primary_key=True),
        sa.Column("fallback_adapter_id", sa.Text, primary_key=True),
        sa.Column("reason", sa.Text),
        sa.Column("ordinal", sa.Integer),
    )
    sa.Table(
        "routing_policies",
        md,
        sa.Column("scope", sa.Text, primary_key=True),
        sa.Column("strategy", enum("routing_strategy", [e.value for e in RoutingStrategy])),
        sa.Column("fallback", enum("fallback_mode", [e.value for e in FallbackMode])),
        sa.Column("selection", enum("selection_mode", [e.value for e in Selection])),
        sa.Column("updated_at", ts()),
    )
    sa.Table(
        "device_profiles",
        md,
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("ram_gb", sa.Integer),
        sa.Column("gpu", sa.Text),
        sa.Column("backend", enum("gpu_backend", [e.value for e in DeviceBackend])),
        sa.Column("unified_memory", sa.Boolean),
        sa.Column("preferred_mode", enum("generation_mode", [e.value for e in GenerationMode])),
        sa.Column("updated_at", ts()),
    )
    sa.Table(
        "provider_registry_meta",
        md,
        sa.Column("id", sa.Boolean, primary_key=True),
        sa.Column("manifest_digest", sa.Text),
        sa.Column("manifest_revision", sa.Integer),
        sa.Column("catalogue_version", sa.Text),
        sa.Column("generator_version", sa.Text),
        sa.Column("generated_at", ts()),
        sa.Column("seeded_at", ts()),
    )
    return md


def snapshot(conn: Any, md: Any) -> dict[str, Rows]:
    import sqlalchemy as sa

    current: dict[str, Rows] = {}
    for spec in TABLE_SPECS:
        table = md.tables[spec.name]
        cols = [table.c[c] for c in spec.columns]
        rows: Rows = {}
        for r in conn.execute(sa.select(*cols)).mappings():
            key = tuple(str(r[k]) for k in spec.key)
            rows[key] = {c: r[c] for c in spec.columns}
        current[spec.name] = rows
    return current


def read_stored_digest(conn: Any, md: Any) -> tuple[str | None, int]:
    import sqlalchemy as sa

    meta = md.tables["provider_registry_meta"]
    row = (
        conn.execute(sa.select(meta.c.manifest_digest, meta.c.manifest_revision)).mappings().first()
    )
    if row is None:
        return None, 0
    return row["manifest_digest"], int(row["manifest_revision"])


def apply_plan(conn: Any, md: Any, plan: SeedPlan, revision: int, now: datetime) -> None:
    import sqlalchemy as sa
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    for tp in plan.tables:
        spec = SPEC_BY_NAME[tp.name]
        table = md.tables[tp.name]
        rows_to_write = tp.creates + tp.updates
        if rows_to_write:
            for row in rows_to_write:
                values = dict(row)
                if spec.has_updated_at:
                    values["updated_at"] = now
                stmt = pg_insert(table).values(**values)
                update_cols = {c: stmt.excluded[c] for c in row if c not in spec.key}
                if spec.has_updated_at:
                    update_cols["updated_at"] = now
                if update_cols:
                    stmt = stmt.on_conflict_do_update(
                        index_elements=list(spec.key), set_=update_cols
                    )
                else:
                    stmt = stmt.on_conflict_do_nothing(index_elements=list(spec.key))
                conn.execute(stmt)
        for key in tp.disables:
            where = sa.and_(*(table.c[k] == v for k, v in zip(spec.key, key, strict=True)))
            conn.execute(sa.update(table).where(where).values(enabled=False, updated_at=now))
        for key in tp.deletes:
            where = sa.and_(*(table.c[k] == v for k, v in zip(spec.key, key, strict=True)))
            conn.execute(sa.delete(table).where(where))

    meta = md.tables["provider_registry_meta"]
    stmt = pg_insert(meta).values(
        id=True,
        manifest_digest=plan.digest,
        manifest_revision=revision,
        catalogue_version=CATALOGUE_VERSION,
        generator_version=GENERATOR_VERSION,
        generated_at=now,
        seeded_at=now,
    )
    conn.execute(
        stmt.on_conflict_do_update(
            index_elements=["id"],
            set_={
                "manifest_digest": plan.digest,
                "manifest_revision": revision,
                "catalogue_version": CATALOGUE_VERSION,
                "generator_version": GENERATOR_VERSION,
                "generated_at": now,
                "seeded_at": now,
            },
        )
    )


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def load_manifests(
    providers_dir: Path,
) -> tuple[Catalogue, ProvidersDoc, RoutingDoc, DevicesDoc | None]:
    cat = load_catalogue(providers_dir / CAPABILITIES_FILE)
    pdoc = load_providers(providers_dir / PROVIDERS_FILE)
    rdoc = load_routing(providers_dir / ROUTING_FILE)
    devices_path = providers_dir / DEVICES_FILE
    devices = load_devices(devices_path) if devices_path.exists() else None
    return cat, pdoc, rdoc, devices


def _print_plan(plan: SeedPlan) -> None:
    for tp in plan.tables:
        if tp.writes or tp.unchanged:
            print(
                f"  {tp.name:<24} +{len(tp.creates)} ~{len(tp.updates)} "
                f"disable {len(tp.disables)} delete {len(tp.deletes)} "
                f"(={tp.unchanged} unchanged)"
            )


def seed(providers_dir: Path, database_url: str | None, *, dry_run: bool, force: bool) -> int:
    cat, pdoc, rdoc, devices = load_manifests(providers_dir)

    report = validate(cat, pdoc, rdoc, devices)
    if not report.ok:
        for e in report.errors:
            print(f"  ERROR [{e['rule']}] {e['message']}")
        print(f"[FAIL] refusing to seed — manifest invalid ({len(report.errors)} error(s))")
        return 3

    digest = manifest_digest(providers_dir)
    desired = build_desired(cat, pdoc, rdoc, devices)

    if database_url is None:
        # Offline planning: no DB to diff against; report desired counts + digest.
        print(f"digest {digest}")
        for spec in TABLE_SPECS:
            print(f"  {spec.name:<24} {len(desired.get(spec.name, {}))} desired row(s)")
        print("[ OK ] offline plan only — provide a database URL to apply")
        return 0

    import sqlalchemy as sa

    md = _catalogue_metadata()
    engine = sa.create_engine(database_url, future=True)
    try:
        with engine.begin() as conn:
            stored_digest, revision = read_stored_digest(conn, md)
            if stored_digest == digest and not force:
                print(f"[ OK ] catalogue already at digest {digest[:12]}… — no changes")
                return 0
            plan = build_plan(desired, snapshot(conn, md), digest, stored_digest)
            _print_plan(plan)
            if dry_run:
                print(f"[dry-run] {plan.writes} write(s) would be applied; no changes made")
                return 0
            apply_plan(conn, md, plan, revision + 1, datetime.now(UTC))
        print(
            f"[ OK ] seeded catalogue — digest {digest[:12]}…, revision {revision + 1}, "
            f"{plan.writes} write(s), catalogue_version {CATALOGUE_VERSION}"
        )
        return 0
    finally:
        engine.dispose()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Seed the provider catalogue from YAML manifests.")
    parser.add_argument("--database-url", default=None, help="Postgres URL (else DATABASE_URL env)")
    parser.add_argument("--providers-dir", default=str(PROVIDERS_DIR))
    parser.add_argument("--dry-run", action="store_true", help="compute the plan; write nothing")
    parser.add_argument("--force", action="store_true", help="seed even if the digest is unchanged")
    args = parser.parse_args(argv[1:])

    import os

    database_url = args.database_url or os.environ.get("DATABASE_URL")
    return seed(Path(args.providers_dir), database_url, dry_run=args.dry_run, force=args.force)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
