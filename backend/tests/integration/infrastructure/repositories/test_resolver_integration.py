"""α8.5e.5 — end-to-end resolver integration (live PG, SAVEPOINT).

Seed → resolve (via ResolverService over the real readers) → record the resolution in the
ledger → read it back. No provider execution: this proves the Decision-plane stack wiring
(catalogue + runtime readers → pure resolver → provenance ledger) against real Postgres.
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.interfaces.resolution_ledger import ExecutionOutcome
from app.application.use_cases.resolver.resolver_service import ResolverService
from app.domain.resolver import ResolveRequest, RoutingStrategy
from app.infrastructure.repositories.catalogue_reader import CatalogueReader
from app.infrastructure.repositories.resolution_ledger_writer import ResolutionLedgerWriter
from app.infrastructure.repositories.runtime_state_reader import RuntimeStateReader

pytestmark = pytest.mark.integration


async def _seed(session: AsyncSession, *, tag: str) -> dict[str, str]:
    cap = f"imggen_{tag}"
    prov = f"prov_{tag}"
    a1 = f"{prov}.free"
    dev = f"dev_{tag}"
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
        {"id": a1, "pid": prov, "cap": cap},
    )
    await session.execute(
        text(
            "INSERT INTO routing_policies (scope, strategy, fallback, selection) "
            "VALUES (:scope, 'free_first', 'automatic', 'best_available')"
        ),
        {"scope": cap},
    )
    await session.execute(
        text(
            "INSERT INTO provider_registry_meta "
            "(id, manifest_digest, catalogue_version, generator_version, generated_at) "
            "VALUES (true, :digest, '2026.07', 'test/1.0', now()) "
            "ON CONFLICT (id) DO UPDATE SET manifest_digest = EXCLUDED.manifest_digest"
        ),
        {"digest": f"digest_{tag}"},
    )
    await session.flush()
    return {"cap": cap, "prov": prov, "a1": a1, "dev": dev}


@pytest.mark.integration
async def test_seed_resolve_and_record_ledger(session: AsyncSession) -> None:
    tag = uuid4().hex[:8]
    keys = await _seed(session, tag=tag)

    service = ResolverService(CatalogueReader(session), RuntimeStateReader(session))
    resolution = await service.resolve(ResolveRequest(capability=keys["cap"]))

    assert resolution.routing_strategy is RoutingStrategy.FREE_FIRST
    top = resolution.top
    assert top is not None and top.adapter_id == keys["a1"]

    generation_id = uuid4()
    ledger = ResolutionLedgerWriter(session)
    ledger_id = await ledger.record(
        generation_id=generation_id,
        resolution=resolution,
        chosen_adapter=top.adapter_id,
        execution_result=ExecutionOutcome.SUCCESS,
    )
    await session.flush()

    row = (
        (
            await session.execute(
                text(
                    "SELECT generation_id, capability, catalogue_version, manifest_digest, "
                    "resolver_version, routing_strategy, candidate_list, chosen_adapter, "
                    "execution_result FROM generation_resolution_ledger WHERE id = :id"
                ),
                {"id": str(ledger_id)},
            )
        )
        .mappings()
        .one()
    )

    assert str(row["generation_id"]) == str(generation_id)
    assert row["capability"] == keys["cap"]
    assert row["catalogue_version"] == "2026.07"
    assert row["manifest_digest"] == f"digest_{tag}"
    assert row["routing_strategy"] == "free_first"
    assert row["chosen_adapter"] == keys["a1"]
    assert row["execution_result"] == "success"

    candidates = row["candidate_list"]
    if isinstance(candidates, str):  # driver may return jsonb as text
        candidates = json.loads(candidates)
    assert any(c["adapter_id"] == keys["a1"] and c["eligible"] for c in candidates)
