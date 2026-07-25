"""α8.6 Increment 4 — Execution Runtime repositories integration (live PG, SAVEPOINT).

Exercises the raw-SQL ``generations`` / ``generation_shots`` / ``generation_assets``
/ ``model_cache`` writers against real Postgres (enum casts, jsonb, self-FK
lineage, ON CONFLICT upserts). Everything runs inside the SAVEPOINT ``session``
fixture, so nothing persists to the shared DB.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.interfaces.execution_runtime_store import NewGenerationAsset, ShotRecord
from app.application.use_cases.generation.request import GenerateVideoRequest
from app.application.use_cases.generation.results import GenerationProvenance
from app.domain.generation.execution import ExecutionMode
from app.domain.generation.execution_state import ExecutionStatus, GenerationAssetKind
from app.domain.generation.identity import IdentityProfile
from app.infrastructure.repositories.generation_asset_repository import GenerationAssetRepository
from app.infrastructure.repositories.generation_ledger_repository import GenerationLedgerRepository
from app.infrastructure.repositories.model_cache_repository import ModelCacheRepository

pytestmark = pytest.mark.integration


def _request() -> GenerateVideoRequest:
    return GenerateVideoRequest(
        prompt="a short clip about a robot",
        identity=IdentityProfile(seed=4242),
        execution_mode=ExecutionMode.AUTO,
        width=720,
        height=1280,
        fps=30,
    )


def _provenance(generation_id: UUID) -> GenerationProvenance:
    return GenerationProvenance(
        generation_id=generation_id,
        capability="image_generation",
        execution_mode="auto",
        resolver_version="resolver/1.0",
        chosen_adapter="pollinations.image",
        chosen_provider="pollinations",
        execution_tier="free_remote",
        catalogue_version="2026.07",
        manifest_digest="abc123",
        candidate_adapters=("pollinations.image",),
        planner_version="planner/1.0",
        score_schema_version=1,
    )


async def _new_generation(session: AsyncSession, *, shot_count: int = 2) -> UUID:
    gid = uuid4()
    await GenerationLedgerRepository(session).insert_generation(
        generation_id=gid,
        request=_request(),
        provenance=_provenance(gid),
        title="Robot clip",
        shot_count=shot_count,
    )
    return gid


async def test_generation_lifecycle_round_trip(session: AsyncSession) -> None:
    repo = GenerationLedgerRepository(session)
    gid = await _new_generation(session)

    row = (
        (await session.execute(text("SELECT * FROM generations WHERE id = :id"), {"id": str(gid)}))
        .mappings()
        .one()
    )
    assert row["status"] == "planning"
    assert row["prompt"] == "a short clip about a robot"
    assert row["execution_tier"] == "free_remote"
    assert row["seed"] == 4242
    assert row["planner_version"] == "planner/1.0"
    assert row["score_schema_version"] == 1
    assert row["provenance"]["versions"]["resolver"] == "resolver/1.0"

    await repo.update_status(generation_id=gid, status=ExecutionStatus.GENERATING)
    status = (
        await session.execute(
            text("SELECT status FROM generations WHERE id = :id"), {"id": str(gid)}
        )
    ).scalar_one()
    assert status == "generating"


async def test_shot_upsert_on_conflict(session: AsyncSession) -> None:
    repo = GenerationLedgerRepository(session)
    gid = await _new_generation(session)

    await repo.insert_shot(
        ShotRecord(
            generation_id=gid,
            shot_number=0,
            prompt="first",
            accepted=False,
            reason="blank",
            attempts=({"attempt": 1, "action": "give_up"},),
        )
    )
    # Same (generation_id, shot_number) upserts rather than duplicating.
    await repo.insert_shot(
        ShotRecord(
            generation_id=gid,
            shot_number=0,
            prompt="first-retry",
            accepted=True,
            repair_count=1,
            reference_images=("ref:a",),
        )
    )
    rows = (
        (
            await session.execute(
                text("SELECT * FROM generation_shots WHERE generation_id = :id"), {"id": str(gid)}
            )
        )
        .mappings()
        .all()
    )
    assert len(rows) == 1
    assert rows[0]["accepted"] is True
    assert rows[0]["prompt"] == "first-retry"
    assert rows[0]["repair_count"] == 1
    assert rows[0]["reference_images"] == ["ref:a"]


async def test_asset_registry_lineage_graph(session: AsyncSession) -> None:
    gid = await _new_generation(session)
    assets = GenerationAssetRepository(session)

    frame_id = await assets.insert(
        NewGenerationAsset(
            generation_id=gid,
            asset_kind=GenerationAssetKind.FRAME,
            storage_backend="local",
            storage_bucket="b",
            storage_key=f"frames/{gid}/000.png",
            mime_type="image/png",
            shot_number=0,
            size_bytes=1024,
            checksum_sha256=b"\x00\x01\x02\x03",
            width=720,
            height=1280,
        )
    )
    # A repaired frame points back at the original -> lineage graph, not overwrite.
    repaired_id = await assets.insert(
        NewGenerationAsset(
            generation_id=gid,
            asset_kind=GenerationAssetKind.FRAME,
            storage_backend="local",
            storage_bucket="b",
            storage_key=f"frames/{gid}/000-repaired.png",
            mime_type="image/png",
            shot_number=0,
            parent_asset_id=frame_id,
        )
    )
    parent = (
        await session.execute(
            text("SELECT parent_asset_id FROM generation_assets WHERE id = :id"),
            {"id": str(repaired_id)},
        )
    ).scalar_one()
    assert parent == frame_id


async def test_mark_completed_and_failed(session: AsyncSession) -> None:
    repo = GenerationLedgerRepository(session)
    gid = await _new_generation(session)
    video_asset_id = await GenerationAssetRepository(session).insert(
        NewGenerationAsset(
            generation_id=gid,
            asset_kind=GenerationAssetKind.VIDEO,
            storage_backend="local",
            storage_bucket="b",
            storage_key=f"renders/{gid}.mp4",
            mime_type="video/mp4",
        )
    )
    await repo.mark_completed(
        generation_id=gid,
        final_video_asset_id=video_asset_id,
        video_backend="local",
        video_bucket="b",
        video_key=f"renders/{gid}.mp4",
        duration_seconds=18.0,
        width=720,
        height=1280,
    )
    row = (
        (
            await session.execute(
                text(
                    "SELECT status, final_video_asset_id, video_key, duration_seconds, finished_at "
                    "FROM generations WHERE id = :id"
                ),
                {"id": str(gid)},
            )
        )
        .mappings()
        .one()
    )
    assert row["status"] == "completed"
    assert row["final_video_asset_id"] == video_asset_id
    assert row["video_key"] == f"renders/{gid}.mp4"
    assert float(row["duration_seconds"]) == 18.0
    assert row["finished_at"] is not None

    other = await _new_generation(session)
    await repo.mark_failed(generation_id=other, reason="boom")
    failed = (
        (
            await session.execute(
                text("SELECT status, failure_reason FROM generations WHERE id = :id"),
                {"id": str(other)},
            )
        )
        .mappings()
        .one()
    )
    assert failed["status"] == "failed"
    assert failed["failure_reason"] == "boom"


async def test_model_cache_upsert_get_touch(session: AsyncSession) -> None:
    repo = ModelCacheRepository(session)
    ref = f"comfyui/flux-{uuid4().hex[:8]}"

    await repo.upsert(
        model_ref=ref,
        status="ready",
        version="1.0",
        backend="metal",
        execution_tier="local",
        supported_capabilities=["image_generation"],
        local_path="/cache/flux",
    )
    cached = await repo.get(ref)
    assert cached is not None
    assert cached.status == "ready"
    assert cached.execution_tier == "local"
    assert cached.supported_capabilities == ("image_generation",)

    # Upsert again flips status; touch stamps last_used_at.
    await repo.upsert(model_ref=ref, status="registered")
    await repo.touch_last_used(ref)
    row = (
        (
            await session.execute(
                text("SELECT status, last_used_at FROM model_cache WHERE model_ref = :r"),
                {"r": ref},
            )
        )
        .mappings()
        .one()
    )
    assert row["status"] == "registered"
    assert row["last_used_at"] is not None
