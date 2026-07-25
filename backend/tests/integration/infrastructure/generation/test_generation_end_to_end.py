"""α8.6 Increment 5 — the authoritative end-to-end generation slice.

Drives the **real** runtime against live PostgreSQL + a real ffmpeg/ffprobe:

    Prompt → Planner → Storyboard → Resolver → Image Generator → Verifier →
    Repair (if needed) → Timeline → FFmpeg → MP4 → Execution-Runtime persistence

and validates the five Increment-5 acceptance dimensions — Functional, Persistence,
Architectural, Reproducibility, Explainability — from the *database alone* where the
criterion demands it.

Provider independence (the whole point of the architecture): the image bytes come
from :class:`OfflineDeterministicImageGenerator`, a throwaway ``IImageGenerator``
that produces real PNGs with Pillow. Nothing in the planner, resolver, verifier,
repair, renderer, or execution runtime knows or cares which ``IImageGenerator`` is
wired — Pollinations, ComfyUI, or this offline double are interchangeable. The
resolver still *chooses* an adapter from the seeded catalogue; the chosen id is
handed to the generator, which (like any real adapter) simply produces bytes.

Determinism: the request pins ``FREE_REMOTE_ONLY`` and the test seeds one
free-remote adapter (``golden_provider.image``) whose provider outscores everything
in the catalogue, so the winning adapter is stable and inspectable regardless of
what else is seeded. Generation rows are cleaned up on teardown; for *manual*
inspection of a run, use ``scripts/generate_demo.py`` against a throwaway database.

The catalogue seed + generation rows are committed (the store owns its own
sessions), so this test manages its own cleanup rather than leaning on the
SAVEPOINT ``session`` fixture. A gated live-Pollinations variant proves the same
flow over the real network when ``AIVP_E2E_POLLINATIONS=1``.
"""

from __future__ import annotations

import json
import os
import shutil
from io import BytesIO
from uuid import UUID, uuid4

import pytest
from PIL import Image, ImageDraw
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.interfaces.image_generator import GeneratedImage, IImageGenerator
from app.application.use_cases.generation.capability_resolver import ResolverCapabilityResolver
from app.application.use_cases.generation.generate_video import GenerateVideo
from app.application.use_cases.generation.request import GenerateVideoRequest
from app.application.use_cases.generation.results import GenerateVideoResult, GenerationStatus
from app.infrastructure.generation.execution_runtime_store import SqlExecutionRuntimeStore
from app.infrastructure.generation.model_cache_manager import ModelCacheManager
from app.infrastructure.generation.pillow_feature_extractor import PillowFeatureExtractor
from app.infrastructure.render.ffmpeg_slideshow_renderer import FfmpegSlideshowRenderer
from app.infrastructure.render.ffprobe_video_probe import FfprobeVideoProbe
from app.infrastructure.repositories.catalogue_reader import CatalogueReader
from app.infrastructure.repositories.runtime_state_reader import RuntimeStateReader
from app.infrastructure.storage.local_object_storage import LocalObjectStorage
from tests.fixtures.golden.scenario import GOLDEN_JSON, fox_request

_HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg/ffprobe not installed"),
]

_BUCKET = "generations"
_PROVIDER_ID = "golden_provider"
_ADAPTER_ID = "golden_provider.image"


# --------------------------------------------------------------------------- #
# Offline, provider-agnostic image generator (produces real PNG bytes)
# --------------------------------------------------------------------------- #
def _render_png(index: int, width: int, height: int) -> bytes:
    """A real PNG that is non-blank, similar frame-to-frame, yet uniquely hashed.

    A left→right gradient body keeps every frame non-blank and cross-shot similar
    (so per-shot verification + consistency pass); a crisp per-frame threshold
    pattern painted across the top cell-row guarantees a distinct perceptual hash
    (so the timeline duplicate gate exercises real, distinct hashes). Kept in the
    top row so the naive watermark heuristic (bottom-right) never trips.
    """
    base = Image.new("RGB", (256, 1))
    base.putdata([(v, (v * 2) % 256, 255 - v) for v in range(256)])
    img = base.resize((max(1, width), max(1, height)))
    draw = ImageDraw.Draw(img)
    cols = 8
    cell_w = max(1, width // cols)
    band_h = max(1, height // cols)
    k = index % cols
    for j in range(cols):
        colour = (0, 0, 0) if j <= k else (255, 255, 255)
        x0 = j * cell_w
        x1 = width if j == cols - 1 else (j + 1) * cell_w
        draw.rectangle((x0, 0, x1, band_h), fill=colour)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class OfflineDeterministicImageGenerator(IImageGenerator):
    """Test-only ``IImageGenerator`` — real PNG bytes, no network, deterministic.

    Ignores ``adapter_id`` exactly like a single-provider adapter would: the
    resolver's choice flows *through* the port, it does not change what an adapter
    fundamentally does (produce bytes). Records every call so the test can prove the
    resolver-chosen adapter id reached the generator.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self._counter = 0

    async def generate(
        self,
        *,
        adapter_id: str,
        prompt: str,
        seed: int,
        width: int,
        height: int,
        negative_prompt: str | None = None,
        reference_image_refs: tuple[str, ...] = (),
        local_model_path: str | None = None,
    ) -> GeneratedImage:
        index = self._counter
        self._counter += 1
        self.calls.append(
            {"adapter_id": adapter_id, "prompt": prompt, "seed": seed, "index": index}
        )
        return GeneratedImage(
            data=_render_png(index, width, height),
            content_type="image/png",
            adapter_id=adapter_id,
        )


# --------------------------------------------------------------------------- #
# Catalogue seeding + cleanup (committed; the store owns its own sessions)
# --------------------------------------------------------------------------- #
async def _seed_golden_catalogue(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """Seed one free-remote ``image_generation`` adapter that outscores everything.

    Conflict-tolerant so it works whether the DB is freshly migrated (local
    ephemeral) or already carries the real seeded catalogue (CI). All-100 scores +
    free pricing make it the deterministic resolver winner under FREE_REMOTE_ONLY.
    """
    async with session_factory() as s:
        await s.execute(
            text(
                "INSERT INTO capabilities (id, kind) VALUES ('image_generation', 'image') "
                "ON CONFLICT DO NOTHING"
            )
        )
        await s.execute(
            text(
                "INSERT INTO providers (id, name, pricing, score_quality, score_cost, "
                "score_speed, score_reliability, enabled) "
                "VALUES (:id, 'Golden Provider', 'free', 100, 100, 100, 100, true) "
                "ON CONFLICT (id) DO UPDATE SET enabled = true, pricing = 'free', "
                "score_quality = 100, score_cost = 100, score_speed = 100, score_reliability = 100"
            ),
            {"id": _PROVIDER_ID},
        )
        await s.execute(
            text(
                "INSERT INTO provider_adapters (id, provider_id, capability_id, execution_mode, "
                "enabled) VALUES (:id, :pid, 'image_generation', 'cloud', true) "
                "ON CONFLICT (id) DO UPDATE SET enabled = true, execution_mode = 'cloud'"
            ),
            {"id": _ADAPTER_ID, "pid": _PROVIDER_ID},
        )
        await s.execute(
            text(
                "INSERT INTO routing_policies (scope, strategy, fallback, selection) "
                "VALUES ('image_generation', 'free_first', 'automatic', 'best_available') "
                "ON CONFLICT DO NOTHING"
            )
        )
        await s.execute(
            text(
                "INSERT INTO provider_registry_meta "
                "(id, manifest_digest, catalogue_version, generator_version, generated_at) "
                "VALUES (true, 'golden-digest', '2026.07', 'golden/1.0', now()) "
                "ON CONFLICT (id) DO UPDATE SET manifest_digest = EXCLUDED.manifest_digest, "
                "catalogue_version = EXCLUDED.catalogue_version"
            )
        )
        await s.commit()


async def _cleanup(
    session_factory: async_sessionmaker[AsyncSession], generation_ids: list[UUID]
) -> None:
    async with session_factory() as s:
        for gid in generation_ids:
            await s.execute(
                text("DELETE FROM event_outbox WHERE aggregate_id = CAST(:g AS uuid)"),
                {"g": str(gid)},
            )
            await s.execute(
                text(
                    "DELETE FROM generation_resolution_ledger "
                    "WHERE generation_id = CAST(:g AS uuid)"
                ),
                {"g": str(gid)},
            )
            # generation_shots + generation_assets cascade on this delete (FK ON DELETE CASCADE).
            await s.execute(
                text("DELETE FROM generations WHERE id = CAST(:g AS uuid)"), {"g": str(gid)}
            )
        await s.execute(text("DELETE FROM provider_adapters WHERE id = :id"), {"id": _ADAPTER_ID})
        await s.execute(text("DELETE FROM providers WHERE id = :id"), {"id": _PROVIDER_ID})
        await s.commit()


async def _run(
    session_factory: async_sessionmaker[AsyncSession],
    storage: LocalObjectStorage,
    generator: IImageGenerator,
    request: GenerateVideoRequest,
) -> GenerateVideoResult:
    """Compose the real runtime and execute one generation.

    The resolver reads the catalogue through a dedicated request session; the
    Execution-Runtime store + model cache use their own short-lived sessions
    (generation is long-running — no single transaction spans it, per ADR-0046).
    """
    async with session_factory() as read_session:
        resolver = ResolverCapabilityResolver(
            CatalogueReader(read_session), RuntimeStateReader(read_session)
        )
        use_case = GenerateVideo(
            resolver=resolver,
            image_generator=generator,
            feature_extractor=PillowFeatureExtractor(),
            renderer=FfmpegSlideshowRenderer(),
            video_probe=FfprobeVideoProbe(),
            storage=storage,
            model_manager=ModelCacheManager(session_factory),
            store=SqlExecutionRuntimeStore(session_factory),
        )
        return await use_case.execute(request)


# --------------------------------------------------------------------------- #
# Persistence + explainability assertions (answered from the DB alone)
# --------------------------------------------------------------------------- #
async def _assert_persistence(
    session_factory: async_sessionmaker[AsyncSession], gid: UUID, golden: dict
) -> None:
    shot_count = golden["storyboard"]["shot_count"]
    async with session_factory() as s:
        gen = (
            (
                await s.execute(
                    text(
                        "SELECT status, chosen_provider, chosen_adapter, execution_tier, "
                        "execution_mode, shot_count, final_video_asset_id, video_key, "
                        "duration_seconds, provenance, planner_version, storyboard_version, "
                        "prompt_builder_version, resolver_version, verifier_version, "
                        "repair_version, renderer_version, score_schema_version, "
                        "catalogue_version, manifest_digest "
                        "FROM generations WHERE id = CAST(:g AS uuid)"
                    ),
                    {"g": str(gid)},
                )
            )
            .mappings()
            .one()
        )
        assert gen["status"] == "completed"
        assert gen["chosen_adapter"] == _ADAPTER_ID
        assert gen["chosen_provider"] == _PROVIDER_ID
        assert gen["execution_tier"] == "free_remote"
        assert gen["execution_mode"] == golden["resolver"]["expected_execution_mode"]
        assert gen["shot_count"] == shot_count
        assert gen["final_video_asset_id"] is not None
        assert gen["video_key"]
        assert gen["duration_seconds"] is not None
        # Provenance versions recorded (both as columns and inside the JSONB head).
        for column in (
            "planner_version",
            "storyboard_version",
            "prompt_builder_version",
            "resolver_version",
            "verifier_version",
            "repair_version",
            "renderer_version",
            "catalogue_version",
            "manifest_digest",
        ):
            assert gen[column], f"missing provenance column {column}"
        assert gen["score_schema_version"] is not None
        prov = gen["provenance"]
        if isinstance(prov, str):
            prov = json.loads(prov)
        assert prov["chosen_adapter"] == _ADAPTER_ID
        assert prov["candidate_adapters"][0] == _ADAPTER_ID
        versions = prov["versions"]
        assert versions["planner"] and versions["resolver"] and versions["renderer"]
        assert versions["score_schema"] is not None

        # generation_shots — one accepted row per shot, all on the chosen adapter.
        shots = (
            (
                await s.execute(
                    text(
                        "SELECT shot_number, accepted, adapter_used, seed, asset_id, repair_count "
                        "FROM generation_shots WHERE generation_id = CAST(:g AS uuid) "
                        "ORDER BY shot_number"
                    ),
                    {"g": str(gid)},
                )
            )
            .mappings()
            .all()
        )
        assert len(shots) == shot_count
        assert [r["shot_number"] for r in shots] == list(range(shot_count))
        assert all(r["accepted"] for r in shots)
        assert all(r["adapter_used"] == _ADAPTER_ID for r in shots)
        assert all(r["asset_id"] is not None for r in shots)

        # generation_assets — one frame per shot + exactly one final video.
        kinds = {
            r["asset_kind"]: r["n"]
            for r in (
                await s.execute(
                    text(
                        "SELECT asset_kind, count(*) AS n FROM generation_assets "
                        "WHERE generation_id = CAST(:g AS uuid) GROUP BY asset_kind"
                    ),
                    {"g": str(gid)},
                )
            )
            .mappings()
            .all()
        }
        assert kinds.get("frame") == shot_count
        assert kinds.get("video") == 1

        # resolution ledger — the explainability record.
        ledger = (
            (
                await s.execute(
                    text(
                        "SELECT capability, routing_strategy, chosen_adapter, execution_result, "
                        "candidate_list FROM generation_resolution_ledger "
                        "WHERE generation_id = CAST(:g AS uuid)"
                    ),
                    {"g": str(gid)},
                )
            )
            .mappings()
            .all()
        )
        assert len(ledger) == 1
        led = ledger[0]
        assert led["capability"] == "image_generation"
        assert led["chosen_adapter"] == _ADAPTER_ID
        assert led["execution_result"] == "success"
        candidates = led["candidate_list"]
        if isinstance(candidates, str):
            candidates = json.loads(candidates)
        # "Why this adapter?" — it is present, eligible, and top of the ranked list.
        assert candidates[0]["adapter_id"] == _ADAPTER_ID
        chosen = candidates[0]
        assert chosen["eligible"] is True
        assert chosen["score"] is not None
        assert chosen["breakdown"] is not None  # per-factor score explanation
        # "What alternatives / every score?" — every candidate carries score+eligibility.
        assert all("score" in c and "eligible" in c for c in candidates)
        # Ineligible alternatives explain *why* they were dropped.
        assert all(
            c["eligible"] or c["ineligible_reason"] for c in candidates
        ), "an ineligible candidate lacks a reason"

        # outbox events — the lifecycle is externally observable from the DB.
        events = (
            (
                await s.execute(
                    text(
                        "SELECT event_type FROM event_outbox "
                        "WHERE aggregate_id = CAST(:g AS uuid)"
                    ),
                    {"g": str(gid)},
                )
            )
            .scalars()
            .all()
        )
        assert "generation.started" in events
        assert events.count("generation.shot_generated") == shot_count
        assert "generation.video_rendered" in events
        assert "generation.export_completed" in events


def _assert_reproducible(a: GenerateVideoResult, b: GenerateVideoResult) -> None:
    """Same request twice → identical orchestration decisions (bytes may differ)."""
    pa, pb = a.provenance, b.provenance
    assert pa.chosen_adapter == pb.chosen_adapter
    assert pa.chosen_provider == pb.chosen_provider
    assert pa.execution_tier == pb.execution_tier
    assert pa.execution_mode == pb.execution_mode
    assert pa.candidate_adapters == pb.candidate_adapters  # identical resolver ordering
    assert pa.resolver_version == pb.resolver_version
    assert pa.catalogue_version == pb.catalogue_version
    assert pa.manifest_digest == pb.manifest_digest
    assert pa.score_schema_version == pb.score_schema_version
    assert [s.index for s in a.shots] == [s.index for s in b.shots]
    assert [s.seed for s in a.shots] == [s.seed for s in b.shots]  # identical seed sequence


# --------------------------------------------------------------------------- #
# The slice
# --------------------------------------------------------------------------- #
async def test_generation_end_to_end(
    session_factory: async_sessionmaker[AsyncSession], tmp_path
) -> None:
    golden = json.loads(GOLDEN_JSON.read_text())
    shot_count = golden["storyboard"]["shot_count"]
    storage = LocalObjectStorage(root=str(tmp_path), bucket=_BUCKET)
    await _seed_golden_catalogue(session_factory)
    created: list[UUID] = []
    try:
        # ---- Run 1 -----------------------------------------------------------
        gen1 = OfflineDeterministicImageGenerator()
        result1 = await _run(session_factory, storage, gen1, fox_request(generation_id=uuid4()))
        created.append(result1.generation_id)

        # ---- Functional ------------------------------------------------------
        assert result1.status is GenerationStatus.SUCCEEDED, result1.reason
        assert result1.title == golden["storyboard"]["title"]
        assert len(result1.shots) == shot_count
        assert all(s.accepted for s in result1.shots)
        assert result1.video_key
        video_path = tmp_path / _BUCKET / result1.video_key
        assert video_path.is_file() and video_path.stat().st_size > 1024
        # A real MP4 that ffprobe measured.
        assert result1.width == 720 and result1.height == 1280
        assert result1.duration_seconds is not None and result1.duration_seconds > 0
        # Resolver selected the seeded free-remote adapter, and it reached the generator.
        assert result1.provenance.chosen_adapter == golden["resolver"]["expected_top_adapter"]
        assert result1.provenance.chosen_provider == golden["resolver"]["expected_top_provider"]
        assert result1.provenance.execution_tier == golden["resolver"]["expected_execution_tier"]
        assert len(gen1.calls) == shot_count
        assert {c["adapter_id"] for c in gen1.calls} == {_ADAPTER_ID}

        # ---- Persistence + Explainability -----------------------------------
        await _assert_persistence(session_factory, result1.generation_id, golden)

        # ---- Reproducibility -------------------------------------------------
        gen2 = OfflineDeterministicImageGenerator()
        result2 = await _run(session_factory, storage, gen2, fox_request(generation_id=uuid4()))
        created.append(result2.generation_id)
        assert result2.status is GenerationStatus.SUCCEEDED, result2.reason
        _assert_reproducible(result1, result2)
    finally:
        await _cleanup(session_factory, created)


@pytest.mark.skipif(
    os.environ.get("AIVP_E2E_POLLINATIONS") != "1",
    reason="live Pollinations e2e is opt-in (set AIVP_E2E_POLLINATIONS=1 with network access)",
)
async def test_generation_end_to_end_live_pollinations(
    settings, session_factory: async_sessionmaker[AsyncSession], tmp_path
) -> None:
    """Same flow over the *real* Pollinations network path (opt-in, not in CI).

    One shot only: the current minimal storyboard reuses the same prompt+seed for
    every shot, so a deterministic remote provider would otherwise trip the timeline
    duplicate gate. This proves the real HTTP adapter integrates end-to-end; multi-
    shot remote variation is a later-increment storyboard concern.
    """
    import httpx

    from app.infrastructure.generation.pollinations_image_generator import (
        PollinationsImageGenerator,
    )

    storage = LocalObjectStorage(root=str(tmp_path), bucket=_BUCKET)
    await _seed_golden_catalogue(session_factory)
    created: list[UUID] = []
    client = httpx.AsyncClient(
        base_url=settings.pollinations_base_url,
        timeout=settings.pollinations_timeout_seconds,
        follow_redirects=True,
    )
    try:
        generator = PollinationsImageGenerator(client=client, model=settings.pollinations_model)
        request = fox_request(generation_id=uuid4(), target_duration_seconds=3.0)
        result = await _run(session_factory, storage, generator, request)
        created.append(result.generation_id)
        assert result.status is GenerationStatus.SUCCEEDED, result.reason
        assert result.video_key
        assert (tmp_path / _BUCKET / result.video_key).is_file()
    finally:
        await client.aclose()
        await _cleanup(session_factory, created)
