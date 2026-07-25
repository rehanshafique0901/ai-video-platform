"""Integration tests for ``CatalogueReader`` (α8.5e.3).

Runs against the live database (migration 0010 applied) inside a SAVEPOINT that rolls
back on teardown. Seeds a tiny catalogue via ``text()`` inserts (no ORM models exist for
these tables) and asserts ``load_snapshot`` materialises a resolvable snapshot.

Coverage:
  C1 — unseeded catalogue (no registry-meta row) → ``load_snapshot`` returns ``None``.
  C2 — seeded catalogue → snapshot carries provenance, providers, adapters (with JSONB
       hardware + ordered fallbacks), routing and devices, and the resolver can use it.
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.resolver import ExecutionMode, Pricing, ResolveRequest, RoutingStrategy, resolve
from app.domain.resolver.models import RuntimeSnapshot
from app.infrastructure.repositories.catalogue_reader import CatalogueReader

pytestmark = pytest.mark.integration


async def _seed_catalogue(session: AsyncSession, *, tag: str) -> dict[str, str]:
    """Insert a minimal, self-contained catalogue; return the natural keys used."""
    cap = f"imggen_{tag}"
    prov = f"prov_{tag}"
    a1 = f"{prov}.primary"
    a2 = f"{prov}.fallback"
    dev = f"dev_{tag}"

    await session.execute(
        text("INSERT INTO capabilities (id, kind) VALUES (:id, 'image')"), {"id": cap}
    )
    await session.execute(
        text(
            "INSERT INTO providers (id, name, pricing, score_quality, score_cost, "
            "score_speed, score_reliability) "
            "VALUES (:id, :name, 'free', 70, 100, 80, 75)"
        ),
        {"id": prov, "name": f"Provider {tag}"},
    )
    for aid, mode, runtime in (
        (a1, "local", {"hardware": {"minimum_ram_gb": 16, "recommended_ram_gb": 32}}),
        (a2, "cloud", {}),
    ):
        await session.execute(
            text(
                "INSERT INTO provider_adapters "
                "(id, provider_id, capability_id, execution_mode, supports, runtime) "
                "VALUES (:id, :pid, :cap, :mode, "
                "CAST(:supports AS jsonb), CAST(:runtime AS jsonb))"
            ),
            {
                "id": aid,
                "pid": prov,
                "cap": cap,
                "mode": mode,
                "supports": json.dumps({"commercial": True}),
                "runtime": json.dumps(runtime),
            },
        )
    await session.execute(
        text(
            "INSERT INTO adapter_fallbacks (adapter_id, fallback_adapter_id, ordinal) "
            "VALUES (:a, :b, 0)"
        ),
        {"a": a1, "b": a2},
    )
    await session.execute(
        text(
            "INSERT INTO routing_policies (scope, strategy, fallback, selection) "
            "VALUES ('default', 'free_first', 'automatic', 'best_available')"
        )
    )
    await session.execute(
        text("INSERT INTO device_profiles (id, ram_gb, backend) VALUES (:id, 16, 'metal')"),
        {"id": dev},
    )
    await session.execute(
        text(
            "INSERT INTO provider_registry_meta "
            "(id, manifest_digest, catalogue_version, generator_version, generated_at) "
            "VALUES (true, :digest, '2026.07', 'test/1.0', now()) "
            "ON CONFLICT (id) DO UPDATE SET manifest_digest = EXCLUDED.manifest_digest, "
            "catalogue_version = EXCLUDED.catalogue_version"
        ),
        {"digest": f"digest_{tag}"},
    )
    await session.flush()
    return {"cap": cap, "prov": prov, "a1": a1, "a2": a2, "dev": dev}


@pytest.mark.integration
async def test_c1_unseeded_catalogue_returns_none(session: AsyncSession) -> None:
    # Rolled back on teardown; safe to clear the singleton for a clean assertion.
    await session.execute(text("DELETE FROM provider_registry_meta"))
    await session.flush()
    assert await CatalogueReader(session).load_snapshot() is None


@pytest.mark.integration
async def test_c2_seeded_catalogue_materialises_snapshot(session: AsyncSession) -> None:
    tag = uuid4().hex[:8]
    keys = await _seed_catalogue(session, tag=tag)

    snapshot = await CatalogueReader(session).load_snapshot()
    assert snapshot is not None
    assert snapshot.catalogue_version == "2026.07"
    assert snapshot.manifest_digest == f"digest_{tag}"

    # Provider projection.
    provider = snapshot.providers[keys["prov"]]
    assert provider.pricing is Pricing.FREE
    assert provider.score_quality == 70

    # Adapter projection: JSONB hardware extracted, execution_mode coerced, fallbacks ordered.
    primary = next(a for a in snapshot.adapters if a.id == keys["a1"])
    assert primary.execution_mode is ExecutionMode.LOCAL
    assert primary.min_ram_gb == 16 and primary.recommended_ram_gb == 32
    assert primary.supports_commercial is True
    assert primary.fallbacks == (keys["a2"],)

    # Routing + devices.
    assert snapshot.strategy_for(keys["cap"]) is RoutingStrategy.FREE_FIRST
    assert snapshot.devices[keys["dev"]].ram_gb == 16

    # End-to-end: the resolver consumes the materialised snapshot.
    res = resolve(ResolveRequest(capability=keys["cap"]), snapshot, RuntimeSnapshot())
    assert {c.adapter_id for c in res.eligible} == {keys["a1"], keys["a2"]}
