"""Integration tests for ``RuntimeStateReader`` (α8.5e.4).

Runs against the live database (migrations 0010 + 0011 applied) inside a SAVEPOINT that
rolls back on teardown. Seeds a provider/adapter (catalogue FKs) plus operational rows
via ``text()`` inserts and asserts ``load_snapshot`` materialises the RuntimeSnapshot.

Coverage:
  R1 — empty operational tables → empty snapshot (maps present, no rows).
  R2 — seeded health/quota/metrics → mapped correctly (Decimal→float, quota grouped).
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.repositories.runtime_state_reader import RuntimeStateReader

pytestmark = pytest.mark.integration


async def _seed_provider_and_adapter(session: AsyncSession, *, tag: str) -> dict[str, str]:
    cap = f"imggen_{tag}"
    prov = f"prov_{tag}"
    adp = f"{prov}.a"
    await session.execute(
        text("INSERT INTO capabilities (id, kind) VALUES (:id, 'image')"), {"id": cap}
    )
    await session.execute(
        text(
            "INSERT INTO providers (id, name, pricing, score_quality, score_cost, "
            "score_speed, score_reliability) VALUES (:id, :name, 'free', 70, 100, 80, 75)"
        ),
        {"id": prov, "name": f"Provider {tag}"},
    )
    await session.execute(
        text(
            "INSERT INTO provider_adapters (id, provider_id, capability_id, execution_mode) "
            "VALUES (:id, :pid, :cap, 'cloud')"
        ),
        {"id": adp, "pid": prov, "cap": cap},
    )
    await session.flush()
    return {"cap": cap, "prov": prov, "adp": adp}


@pytest.mark.integration
async def test_r1_empty_operational_tables_yield_empty_snapshot(session: AsyncSession) -> None:
    tag = uuid4().hex[:8]
    keys = await _seed_provider_and_adapter(session, tag=tag)
    snapshot = await RuntimeStateReader(session).load_snapshot()
    # Our freshly-seeded provider/adapter have no operational rows.
    assert keys["prov"] not in snapshot.health
    assert keys["prov"] not in snapshot.quota
    assert keys["adp"] not in snapshot.metrics


@pytest.mark.integration
async def test_r2_seeded_operational_state_maps_correctly(session: AsyncSession) -> None:
    tag = uuid4().hex[:8]
    keys = await _seed_provider_and_adapter(session, tag=tag)

    await session.execute(
        text(
            "INSERT INTO provider_health (provider_id, health_score, error_rate) "
            "VALUES (:pid, 0.5, 0.12)"
        ),
        {"pid": keys["prov"]},
    )
    await session.execute(
        text(
            'INSERT INTO provider_quota_state (provider_id, "window", remaining) VALUES '
            "(:pid, 'daily', 0), (:pid, 'monthly', 500)"
        ),
        {"pid": keys["prov"]},
    )
    await session.execute(
        text(
            "INSERT INTO adapter_runtime_metrics "
            "(adapter_id, avg_latency_ms, success_rate) VALUES (:aid, 1200, 0.99)"
        ),
        {"aid": keys["adp"]},
    )
    await session.flush()

    snapshot = await RuntimeStateReader(session).load_snapshot()

    health = snapshot.health[keys["prov"]]
    assert health.health_score == pytest.approx(0.5)
    assert health.error_rate == pytest.approx(0.12)

    windows = snapshot.quota[keys["prov"]]
    assert [(q.window, q.remaining) for q in windows] == [("daily", 0), ("monthly", 500)]

    metrics = snapshot.metrics[keys["adp"]]
    assert metrics.avg_latency_ms == 1200
    assert metrics.success_rate == pytest.approx(0.99)
