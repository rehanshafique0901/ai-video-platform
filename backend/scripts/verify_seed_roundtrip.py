"""α8.5d Phase 3 — provider-catalogue seed **round-trip** verification (live DB).

Runs the full acceptance loop against a real PostgreSQL (the schema must already
be at head — in CI this follows the stage-7 ``alembic upgrade head``), proving the
seeder is deterministic and idempotent end-to-end:

  1. empty DB      → seed → creates everything (row counts == manifest)
  2. re-seed       → zero writes, digest unchanged, revision unchanged
  3. single change → only the corresponding row updates (nothing else)
  4. remove entity → provider + adapter disabled (not deleted); fallback edge synced
  5. registry meta → digest + revision + catalogue_version updated correctly

The DB is left seeded from the committed manifests (a final ``force`` re-seed), so
this stage doubles as "populate the validation DB". Destructive only to the eight
catalogue tables; safe on the ephemeral / validation DB the CI gate provisions.

Usage
-----
    python scripts/verify_seed_roundtrip.py            # uses DATABASE_URL
    python scripts/verify_seed_roundtrip.py --database-url postgresql+psycopg://…
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import seed_providers as sp
from provider_manifest import (
    CAPABILITIES_FILE,
    DEVICES_FILE,
    PROVIDERS_DIR,
    PROVIDERS_FILE,
    ROUTING_FILE,
)

_CATALOGUE_TABLES = (*(s.name for s in sp.TABLE_SPECS), "provider_registry_meta")


class _Checks:
    def __init__(self) -> None:
        self.failures = 0

    def check(self, ok: bool, label: str, detail: str = "") -> None:
        tag = "[ OK ]" if ok else "[FAIL]"
        suffix = f" — {detail}" if detail else ""
        print(f"{tag} {label}{suffix}", flush=True)
        if not ok:
            self.failures += 1


def _copy_manifests(dst: Path) -> Path:
    dst.mkdir(parents=True, exist_ok=True)
    for fname in (CAPABILITIES_FILE, PROVIDERS_FILE, ROUTING_FILE, DEVICES_FILE):
        src = PROVIDERS_DIR / fname
        if src.exists():
            shutil.copyfile(src, dst / fname)
    return dst


def _truncate(conn: object, md: object) -> None:
    import sqlalchemy as sa

    tables = ", ".join(_CATALOGUE_TABLES)
    conn.execute(sa.text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))  # type: ignore[attr-defined]


def _row_counts(conn: object, md: object) -> dict[str, int]:
    import sqlalchemy as sa

    counts: dict[str, int] = {}
    for spec in sp.TABLE_SPECS:
        table = md.tables[spec.name]  # type: ignore[attr-defined]
        counts[spec.name] = conn.execute(  # type: ignore[attr-defined]
            sa.select(sa.func.count()).select_from(table)
        ).scalar_one()
    return counts


def run(database_url: str) -> int:
    import sqlalchemy as sa

    checks = _Checks()
    md = sp._catalogue_metadata()
    engine = sa.create_engine(database_url, future=True)

    cat, pdoc, rdoc, dev = sp.load_manifests(PROVIDERS_DIR)
    desired = sp.build_desired(cat, pdoc, rdoc, dev)
    digest = sp.manifest_digest(PROVIDERS_DIR)
    expected_counts = {name: len(rows) for name, rows in desired.items()}

    try:
        # --- 1. empty DB → seed creates everything --------------------------
        with engine.begin() as conn:
            _truncate(conn, md)
        with engine.begin() as conn:
            plan_a = sp.plan_and_apply(conn, md, desired, digest, force=False, dry_run=False)
        total_desired = sum(expected_counts.values())
        creates = sum(len(t.creates) for t in plan_a.tables)
        checks.check(
            plan_a.changed and creates == total_desired and plan_a.writes == total_desired,
            "empty DB → seed creates everything",
            f"{creates}/{total_desired} creates, {plan_a.writes} writes",
        )
        with engine.connect() as conn:
            counts = _row_counts(conn, md)
        checks.check(counts == expected_counts, "row counts match manifest", f"{counts}")

        # --- 2. re-seed unchanged → zero writes -----------------------------
        with engine.connect() as conn:
            stored_before, rev_before = sp.read_stored_digest(conn, md)
        with engine.begin() as conn:
            plan_b = sp.plan_and_apply(conn, md, desired, digest, force=False, dry_run=False)
        checks.check(
            not plan_b.changed and plan_b.writes == 0,
            "re-seed unchanged → zero writes (digest short-circuit)",
            f"changed={plan_b.changed}, writes={plan_b.writes}",
        )
        with engine.connect() as conn:
            stored_after, rev_after = sp.read_stored_digest(conn, md)
        checks.check(
            stored_after == stored_before == digest and rev_after == rev_before,
            "digest + revision unchanged after re-seed",
            f"digest={stored_after[:12]}…, revision {rev_before}→{rev_after}",
        )

        # --- 3. single manifest change → only that row updates --------------
        with tempfile.TemporaryDirectory() as td:
            d = _copy_manifests(Path(td) / "m")
            prov = d / PROVIDERS_FILE
            prov.write_text(
                prov.read_text(encoding="utf-8").replace(
                    "scores: { quality: 70, cost: 100, speed: 90, reliability: 72 }",
                    "scores: { quality: 71, cost: 100, speed: 90, reliability: 72 }",
                    1,
                ),
                encoding="utf-8",
            )
            cat_c, pdoc_c, rdoc_c, dev_c = sp.load_manifests(d)
            desired_c = sp.build_desired(cat_c, pdoc_c, rdoc_c, dev_c)
            digest_c = sp.manifest_digest(d)
            with engine.begin() as conn:
                plan_c = sp.plan_and_apply(
                    conn, md, desired_c, digest_c, force=False, dry_run=False
                )
            prov_updates = [r["id"] for r in plan_c.table("providers").updates]
            other_writes = plan_c.writes - len(plan_c.table("providers").updates)
            checks.check(
                plan_c.writes == 1 and prov_updates == ["pollinations"] and other_writes == 0,
                "single manifest change → only that row updates",
                f"writes={plan_c.writes}, providers.updates={prov_updates}",
            )

        # restore canonical so the removal test starts from a clean baseline
        with engine.begin() as conn:
            sp.plan_and_apply(conn, md, desired, digest, force=True, dry_run=False)

        # --- 4. remove a provider → disabled, not deleted -------------------
        with tempfile.TemporaryDirectory() as td:
            d = _copy_manifests(Path(td) / "m")
            cat_d, pdoc_d, rdoc_d, dev_d = sp.load_manifests(d)
            desired_d = sp.build_desired(cat_d, pdoc_d, rdoc_d, dev_d)
            for name, key in (
                ("providers", ("comfyui",)),
                ("provider_adapters", ("comfyui.flux_schnell",)),
                ("adapter_fallbacks", ("comfyui.flux_schnell", "pollinations.image")),
            ):
                desired_d[name].pop(key, None)
            digest_d = "removed-comfyui-" + digest  # any value != stored ⇒ not short-circuited
            with engine.begin() as conn:
                plan_d = sp.plan_and_apply(
                    conn, md, desired_d, digest_d, force=False, dry_run=False
                )
            prov_disabled = ("comfyui",) in plan_d.table("providers").disables
            adp_disabled = ("comfyui.flux_schnell",) in plan_d.table("provider_adapters").disables
            edge_deleted = ("comfyui.flux_schnell", "pollinations.image") in plan_d.table(
                "adapter_fallbacks"
            ).deletes
            with engine.connect() as conn:
                prov_row = conn.execute(
                    sa.text("SELECT enabled FROM providers WHERE id = 'comfyui'")
                ).scalar_one()
                adp_row = conn.execute(
                    sa.text(
                        "SELECT enabled FROM provider_adapters WHERE id = 'comfyui.flux_schnell'"
                    )
                ).scalar_one()
                edge_exists = conn.execute(
                    sa.text(
                        "SELECT count(*) FROM adapter_fallbacks "
                        "WHERE adapter_id = 'comfyui.flux_schnell'"
                    )
                ).scalar_one()
            checks.check(
                prov_disabled and adp_disabled and edge_deleted,
                "removed provider → plan disables entity, deletes derived edge",
            )
            checks.check(
                prov_row is False and adp_row is False and edge_exists == 0,
                "removed provider persisted as enabled=false, row not deleted",
                f"provider.enabled={prov_row}, adapter.enabled={adp_row}, edges={edge_exists}",
            )

        # --- 5. registry metadata + restore to committed manifest ----------
        with engine.begin() as conn:
            plan_final = sp.plan_and_apply(conn, md, desired, digest, force=True, dry_run=False)
        with engine.connect() as conn:
            meta = (
                conn.execute(
                    sa.text(
                        "SELECT manifest_digest, manifest_revision, catalogue_version, generator_version "
                        "FROM provider_registry_meta WHERE id IS TRUE"
                    )
                )
                .mappings()
                .one()
            )
            final_counts = _row_counts(conn, md)
        checks.check(
            meta["manifest_digest"] == digest
            and meta["manifest_revision"] > rev_before
            and meta["catalogue_version"] == sp.CATALOGUE_VERSION
            and meta["generator_version"] == sp.GENERATOR_VERSION,
            "registry metadata updated correctly",
            f"digest={meta['manifest_digest'][:12]}…, revision={meta['manifest_revision']}, "
            f"catalogue_version={meta['catalogue_version']}",
        )
        checks.check(
            final_counts == expected_counts,
            "catalogue restored to committed manifest (re-enabled comfyui)",
            f"revision now {plan_final.revision}",
        )
    finally:
        engine.dispose()

    print("\n" + "=" * 70)
    if checks.failures:
        print(f"Result: FAILED ({checks.failures} check(s) failed)")
        return 1
    print("Result: PASSED — seed round-trip verified on live PostgreSQL")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Verify the provider-catalogue seed round-trip.")
    parser.add_argument("--database-url", default=None, help="Postgres URL (else DATABASE_URL env)")
    args = parser.parse_args(argv[1:])

    database_url = args.database_url or os.environ.get("DATABASE_URL")
    if not database_url:
        print("[FAIL] DATABASE_URL is not set — a live PostgreSQL is required for Phase 3")
        return 2
    return run(database_url)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
