"""Unit tests for the GenerateVideo use case (orchestration behind port fakes)."""

from __future__ import annotations

from uuid import UUID

import pytest

from app.application.interfaces.capability_resolver import ICapabilityResolver
from app.application.interfaces.image_generator import AdapterNotRegisteredError
from app.application.use_cases.generation.generate_video import GenerateVideo
from app.application.use_cases.generation.request import GenerateVideoRequest
from app.application.use_cases.generation.results import GenerationStatus
from app.core.errors import ValidationFailedError
from app.domain.generation.execution import ExecutionMode, ExecutionTier
from app.domain.generation.identity import Character, GlobalStyle, IdentityProfile, Location

from ._fakes import (
    FakeAdapterRegistry,
    FakeCapabilityResolver,
    FakeFeatureExtractor,
    FakeImageGenerator,
    FakeModelManager,
    FakeObjectStorage,
    FakeSlideshowRenderer,
    FakeVideoProbe,
    RecordingExecutionRuntimeStore,
    local_adapter,
    remote_adapter,
)

pytestmark = pytest.mark.unit


def _identity() -> IdentityProfile:
    return IdentityProfile(
        seed=1234,
        global_style=GlobalStyle.PIXAR,
        characters=(Character(id="mia", name="Mia", clothing="yellow dress"),),
        locations=(Location(id="park", name="sunny park"),),
        camera_style="wide shot",
    )


def _request(**overrides: object) -> GenerateVideoRequest:
    base: dict[str, object] = {
        "prompt": "Mia plays in the park. She finds a red balloon. She laughs.",
        "identity": _identity(),
        "target_duration_seconds": 18.0,
        "per_shot_seconds": 3.0,
        "width": 720,
        "height": 1280,
        "min_width": 512,
        "min_height": 512,
    }
    base.update(overrides)
    return GenerateVideoRequest(**base)  # type: ignore[arg-type]


def _use_case(
    *,
    candidates=(remote_adapter(),),
    extractor: FakeFeatureExtractor | None = None,
    probe: FakeVideoProbe | None = None,
    model_manager: FakeModelManager | None = None,
    storage: FakeObjectStorage | None = None,
    generator: FakeImageGenerator | None = None,
    resolver: ICapabilityResolver | None = None,
    store: RecordingExecutionRuntimeStore | None = None,
    registry: FakeAdapterRegistry | None = None,
) -> tuple[GenerateVideo, dict[str, object]]:
    gen = generator or FakeImageGenerator()
    ext = extractor or FakeFeatureExtractor()
    rnd = FakeSlideshowRenderer()
    prb = probe or FakeVideoProbe(duration=18.0, width=720, height=1280)
    obj = storage or FakeObjectStorage()
    res = resolver or FakeCapabilityResolver(candidates)
    runtime_store = store or RecordingExecutionRuntimeStore()
    # Bind the generator under every candidate id, so whichever the decision picks is
    # constructible — a deployment conformant with DISP-1.
    reg = registry or FakeAdapterRegistry({c.adapter_id: gen for c in candidates})
    uc = GenerateVideo(
        resolver=res,
        adapter_registry=reg,
        feature_extractor=ext,
        renderer=rnd,
        video_probe=prb,
        storage=obj,
        model_manager=model_manager,
        store=runtime_store,
    )
    return uc, {
        "generator": gen,
        "extractor": ext,
        "renderer": rnd,
        "probe": prb,
        "storage": obj,
        "resolver": res,
        "store": runtime_store,
        "registry": reg,
    }


async def test_happy_path_produces_video_with_provenance() -> None:
    uc, fx = _use_case()
    result = await uc.execute(_request())

    assert result.status is GenerationStatus.SUCCEEDED
    assert result.succeeded
    assert result.video_key == f"renders/{result.generation_id}.mp4"
    assert result.duration_seconds == 18.0
    assert result.width == 720 and result.height == 1280

    # 18s / 3s per shot => 6 shots, all accepted, all frames stored + final video.
    assert len(result.shots) == 6
    assert all(s.accepted for s in result.shots)
    store: FakeObjectStorage = fx["storage"]  # type: ignore[assignment]
    assert result.video_key in store.objects
    assert sum(1 for k in store.objects if k.startswith("frames/")) == 6

    # Provenance is capability-first and fully attributed.
    prov = result.provenance
    assert prov.capability == "image_generation"
    assert prov.chosen_adapter == "pollinations.image"
    assert prov.chosen_provider == "pollinations"
    assert prov.resolver_version == "test-resolver-1"
    assert prov.catalogue_version == "2026.07.25"
    assert prov.manifest_digest == "deadbeef"
    assert prov.execution_mode == ExecutionMode.AUTO.value
    assert prov.candidate_adapters == ("pollinations.image",)


async def test_prompts_are_identity_anchored() -> None:
    uc, fx = _use_case()
    await uc.execute(_request())
    gen: FakeImageGenerator = fx["generator"]  # type: ignore[assignment]
    first_prompt = str(gen.calls[0]["prompt"])
    # World state flows Identity Runtime -> Prompt Builder -> Generator.
    assert "Mia (wearing yellow dress)" in first_prompt
    assert "sunny park" in first_prompt
    # Planner V2: the opening beat is an establishing shot (per-shot cinematic intent).
    assert "establishing shot" in first_prompt
    assert first_prompt.endswith("pixar style")
    # Planner V2 derives a distinct per-shot seed (no more single-seed duplicate scene).
    seeds = [call["seed"] for call in gen.calls]
    assert len(set(seeds)) == len(seeds)
    assert 1234 not in seeds


async def test_no_eligible_provider_fails_gracefully() -> None:
    uc, _ = _use_case(candidates=())
    result = await uc.execute(_request())
    assert result.status is GenerationStatus.FAILED
    assert "no eligible provider" in result.reason
    assert result.video_key is None


async def test_repair_retries_then_accepts() -> None:
    # First attempt of shot 0 is blank -> FAIL -> retry with a fresh seed, then pass.
    extractor = FakeFeatureExtractor(fail_first=1)
    uc, fx = _use_case(extractor=extractor)
    result = await uc.execute(_request(target_duration_seconds=15.0, per_shot_seconds=3.0))

    assert result.status is GenerationStatus.SUCCEEDED
    shot0 = result.shots[0]
    assert len(shot0.attempts) == 2
    assert shot0.attempts[0].action == "retry"
    assert shot0.attempts[1].action == "accept"
    # Retry used a different (derived) seed than the first attempt.
    assert shot0.attempts[0].seed != shot0.attempts[1].seed
    assert shot0.seed == shot0.attempts[1].seed


async def test_gives_up_after_max_attempts_and_fails_generation() -> None:
    # Every attempt is blank -> exhaust attempts -> give up -> whole generation fails.
    extractor = FakeFeatureExtractor(fail_first=99)
    uc, fx = _use_case(extractor=extractor)
    result = await uc.execute(_request(max_attempts=3))

    assert result.status is GenerationStatus.FAILED
    assert "failed verification" in result.reason
    first = result.shots[0]
    assert not first.accepted
    assert len(first.attempts) == 3
    assert first.attempts[-1].action == "give_up"
    # Failing shot 0 short-circuits: renderer never ran, no video stored.
    renderer: FakeSlideshowRenderer = fx["renderer"]  # type: ignore[assignment]
    assert renderer.frame_count == 0


async def test_video_verification_rejects_wrong_duration() -> None:
    probe = FakeVideoProbe(duration=2.0, width=720, height=1280)  # far off expected ~18s
    uc, _ = _use_case(probe=probe)
    result = await uc.execute(_request())
    assert result.status is GenerationStatus.FAILED
    assert "duration" in result.reason
    # The (bad) render is still stored for debugging.
    assert result.video_key is not None


async def test_video_verification_rejects_wrong_dimensions() -> None:
    probe = FakeVideoProbe(duration=18.0, width=640, height=480)
    uc, _ = _use_case(probe=probe)
    result = await uc.execute(_request())
    assert result.status is GenerationStatus.FAILED
    assert "dimensions" in result.reason


async def test_empty_prompt_raises_validation_error() -> None:
    uc, _ = _use_case()
    with pytest.raises(ValidationFailedError):
        await uc.execute(_request(prompt="   "))


async def test_local_tier_drives_model_cache() -> None:
    model_manager = FakeModelManager()
    uc, _ = _use_case(candidates=(local_adapter("flux-schnell"),), model_manager=model_manager)
    result = await uc.execute(_request())
    assert result.status is GenerationStatus.SUCCEEDED
    # Model Cache seam exercised once per shot for the local adapter.
    assert model_manager.calls
    assert set(model_manager.calls) == {"flux-schnell"}


async def test_remote_tier_never_touches_model_cache() -> None:
    model_manager = FakeModelManager()
    uc, _ = _use_case(candidates=(remote_adapter(),), model_manager=model_manager)
    result = await uc.execute(_request())
    assert result.status is GenerationStatus.SUCCEEDED
    assert model_manager.calls == []


async def test_generation_id_is_honoured_when_supplied() -> None:
    gid = UUID("11111111-1111-1111-1111-111111111111")
    uc, _ = _use_case()
    result = await uc.execute(_request(generation_id=gid))
    assert result.generation_id == gid
    assert result.video_key == f"renders/{gid}.mp4"


async def test_execution_mode_reaches_resolver_constraints() -> None:
    resolver = FakeCapabilityResolver((remote_adapter(),))
    uc, _ = _use_case(resolver=resolver)
    await uc.execute(_request(execution_mode=ExecutionMode.FREE_REMOTE_ONLY))
    assert resolver.calls
    constraints = resolver.calls[0]["constraints"]
    # FREE_REMOTE_ONLY must not allow local or commercial tiers.
    assert constraints.allowed == (ExecutionTier.FREE_REMOTE,)  # type: ignore[union-attr]


async def test_persists_full_lifecycle_to_store() -> None:
    store = RecordingExecutionRuntimeStore()
    uc, _ = _use_case(store=store)
    result = await uc.execute(_request())

    assert result.succeeded
    assert store.began and store.completed
    assert store.failed_reason is None
    # Execution state machine advances through each persisted phase.
    assert store.statuses == [
        "planning",
        "resolving",
        "generating",
        "rendering",
        "exporting",
        "completed",
    ]
    assert store.resolutions == ["success"]
    # 6 frame assets + 1 video asset registered; every shot recorded + accepted.
    frame_assets = [a for a in store.assets if a.asset_kind.value == "frame"]
    video_assets = [a for a in store.assets if a.asset_kind.value == "video"]
    assert len(frame_assets) == 6
    assert len(video_assets) == 1
    assert len(store.shots) == 6
    assert all(s.accepted and s.asset_id is not None for s in store.shots)
    # Artefacts carry storage coordinates + a checksum for provenance.
    assert all(a.storage_backend == "memory" and a.checksum_sha256 for a in store.assets)


async def test_no_eligible_provider_records_failure_to_store() -> None:
    store = RecordingExecutionRuntimeStore()
    uc, _ = _use_case(candidates=(), store=store)
    result = await uc.execute(_request())

    assert result.status is GenerationStatus.FAILED
    assert store.began
    assert not store.completed
    assert store.resolutions == ["none"]
    assert store.failed_reason is not None and "no eligible provider" in store.failed_reason


async def test_shot_failure_records_failed_shot_to_store() -> None:
    store = RecordingExecutionRuntimeStore()
    uc, _ = _use_case(extractor=FakeFeatureExtractor(fail_first=99), store=store)
    result = await uc.execute(_request(max_attempts=2))

    assert result.status is GenerationStatus.FAILED
    assert store.failed_reason is not None and "failed verification" in store.failed_reason
    # The failed shot is persisted (not accepted) before the generation fails.
    assert len(store.shots) == 1
    assert store.shots[0].accepted is False
    assert store.shots[0].asset_id is None
    assert "failed" in store.statuses


# --------------------------------------------------------------------------- #
# Dispatch + execution provenance (ADR-0054)
# --------------------------------------------------------------------------- #
async def test_execution_dispatches_on_the_chosen_adapter_id() -> None:
    store = RecordingExecutionRuntimeStore()
    uc, fx = _use_case(candidates=(remote_adapter("fal.flux"),), store=store)
    registry: FakeAdapterRegistry = fx["registry"]  # type: ignore[assignment]
    result = await uc.execute(_request())

    assert result.succeeded
    assert set(registry.requested) == {"fal.flux"}
    assert all(s.adapter_used == "fal.flux" for s in store.shots)


async def test_producer_identity_comes_from_the_binding_not_the_adapters_self_report() -> None:
    # DISP-2: an adapter that misreports itself cannot corrupt provenance, because the
    # registry key we dispatched on is the authority.
    store = RecordingExecutionRuntimeStore()
    liar = FakeImageGenerator(reports_as="someone.else")
    uc, _ = _use_case(generator=liar, store=store)
    result = await uc.execute(_request())

    assert result.succeeded
    assert all(s.adapter_used == "pollinations.image" for s in store.shots)


async def test_a_wrong_registry_binding_is_visible_in_provenance() -> None:
    # The negative of the previous test: bind the chosen id to an implementation that is
    # not the one the catalogue means, and provenance must name what we dispatched on —
    # which is what makes a misbinding detectable rather than silently absorbed.
    store = RecordingExecutionRuntimeStore()
    registry = FakeAdapterRegistry({"pollinations.image": FakeImageGenerator(reports_as="comfyui")})
    uc, _ = _use_case(registry=registry, store=store)
    await uc.execute(_request())

    produced = {s.adapter_used for s in store.shots}
    assert produced == {"pollinations.image"}
    assert "comfyui" not in produced


async def test_rejected_bytes_still_record_their_producer() -> None:
    # Acceptance and production are different events (ADR-0054 D2): verification failed,
    # the image was discarded, but something did produce it and the record says so.
    store = RecordingExecutionRuntimeStore()
    uc, _ = _use_case(extractor=FakeFeatureExtractor(fail_first=99), store=store)
    await uc.execute(_request(max_attempts=2))

    assert len(store.shots) == 1
    assert store.shots[0].accepted is False
    assert store.shots[0].adapter_used == "pollinations.image"


async def test_an_unconstructible_adapter_records_no_producer() -> None:
    # Non-conformant wiring: the decision named an adapter the deployment cannot build.
    # Fail closed — no shot row, so no execution record claims an adapter ran.
    store = RecordingExecutionRuntimeStore()
    uc, _ = _use_case(registry=FakeAdapterRegistry({}), store=store)

    with pytest.raises(AdapterNotRegisteredError):
        await uc.execute(_request())

    assert store.shots == []
    # The decision is still recorded: a selection was made, nothing executed.
    assert store.resolutions == ["success"]


async def test_deterministic_result_shape() -> None:
    uc1, _ = _use_case()
    uc2, _ = _use_case()
    gid = UUID("22222222-2222-2222-2222-222222222222")
    r1 = await uc1.execute(_request(generation_id=gid))
    r2 = await uc2.execute(_request(generation_id=gid))
    # Same inputs -> same shot seeds and same chosen adapter (deterministic core).
    assert [s.seed for s in r1.shots] == [s.seed for s in r2.shots]
    assert r1.provenance.chosen_adapter == r2.provenance.chosen_adapter
