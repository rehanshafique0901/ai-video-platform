"""``GenerateVideo`` — orchestrate the first end-to-end generation slice.

Prompt -> Planner -> Storyboard -> Resolver -> (per shot: Generate -> Verify ->
Repair)* -> Slideshow assembly -> Video verify -> store, with full provenance.

This is the Execution plane: it *composes* the pure Decision-plane policies
(planner, prompt builder, verification, repair) with side-effecting ports (image
generator, feature extractor, renderer, probe, storage, model cache). It contains
no provider-specific branching and never scores providers itself — it asks the
capability resolver for candidates and executes the best eligible one
(capability-first, ADR-0045). Everything runs behind ports so it is fully
testable with fakes; real adapters wire in later increments.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, replace
from uuid import UUID, uuid4

import structlog

from app.application.interfaces.capability_resolver import (
    CapabilityResolution,
    ICapabilityResolver,
    ResolvedAdapter,
)
from app.application.interfaces.execution_runtime_store import (
    IExecutionRuntimeStore,
    NewGenerationAsset,
    NullExecutionRuntimeStore,
    ShotRecord,
)
from app.application.interfaces.image_feature_extractor import IImageFeatureExtractor
from app.application.interfaces.image_generator import GeneratedImage, IImageGenerator
from app.application.interfaces.model_manager import IModelManager
from app.application.interfaces.object_storage import IObjectStorage
from app.application.interfaces.resolution_ledger import ExecutionOutcome
from app.application.interfaces.slideshow_renderer import (
    ISlideshowRenderer,
    SlideshowFrame,
    SlideshowSpec,
)
from app.application.interfaces.video_probe import IVideoProbe, ObservedVideo
from app.application.use_cases.generation.request import GenerateVideoRequest
from app.application.use_cases.generation.results import (
    AttemptRecord,
    GenerateVideoResult,
    GenerationProvenance,
    GenerationStatus,
    ShotResult,
)
from app.core.errors import ValidationFailedError
from app.domain.generation.execution import ExecutionTier, constraints_for
from app.domain.generation.execution_state import ExecutionStatus, GenerationAssetKind
from app.domain.generation.planner import PlanningError, PlanRequest, plan_from_prompt
from app.domain.generation.repair import RepairAction, decide_repair
from app.domain.generation.storyboard import ShotPrompt, build_storyboard
from app.domain.generation.timeline_verification import TimelineFrame, verify_timeline
from app.domain.generation.verification import (
    ObservedImage,
    VerificationExpectation,
    verify_image,
)
from app.domain.generation.versions import (
    PLANNER_VERSION,
    PROMPT_BUILDER_VERSION,
    RENDERER_VERSION,
    REPAIR_VERSION,
    SCORE_SCHEMA_VERSION,
    STORYBOARD_VERSION,
    VERIFIER_VERSION,
)

_LOGGER = structlog.get_logger(__name__)

# The catalogue capability id the resolver understands (not the runtime enum).
IMAGE_CAPABILITY = "image_generation"

_ASPECT_TOLERANCE = 0.08
# Rendered video may drift from the exact plan length (frame quantisation, fps);
# accept anything within this fraction of the expected total.
_DURATION_TOLERANCE = 0.25


class GenerateVideo:
    """Compose the generation pipeline behind ports."""

    def __init__(
        self,
        *,
        resolver: ICapabilityResolver,
        image_generator: IImageGenerator,
        feature_extractor: IImageFeatureExtractor,
        renderer: ISlideshowRenderer,
        video_probe: IVideoProbe,
        storage: IObjectStorage,
        model_manager: IModelManager | None = None,
        store: IExecutionRuntimeStore | None = None,
    ) -> None:
        self._resolver = resolver
        self._image_generator = image_generator
        self._features = feature_extractor
        self._renderer = renderer
        self._probe = video_probe
        self._storage = storage
        self._model_manager = model_manager
        # Null-object default: without a store the pipeline runs unpersisted
        # (simple unit tests). Production wires a SqlExecutionRuntimeStore.
        self._store: IExecutionRuntimeStore = store or NullExecutionRuntimeStore()

    async def execute(self, request: GenerateVideoRequest) -> GenerateVideoResult:
        generation_id = request.generation_id or uuid4()
        log = _LOGGER.bind(generation_id=str(generation_id))

        try:
            plan = plan_from_prompt(
                PlanRequest(
                    prompt=request.prompt,
                    identity=request.identity,
                    aspect_ratio=request.aspect_ratio,
                    target_platform=request.target_platform,
                    target_duration_seconds=request.target_duration_seconds,
                    per_shot_seconds=request.per_shot_seconds,
                    title=request.title,
                )
            )
        except PlanningError as exc:
            raise ValidationFailedError(str(exc), details={"prompt": request.prompt}) from exc

        storyboard = build_storyboard(plan)
        constraints = constraints_for(request.execution_mode)

        resolution = await self._resolver.resolve(
            capability=IMAGE_CAPABILITY,
            constraints=constraints,
            prompt=request.prompt,
            budget=request.budget,
        )
        provenance = _provenance(generation_id, request, resolution)
        chosen = resolution.top

        await self._store.begin(
            generation_id=generation_id,
            request=request,
            provenance=provenance,
            title=plan.title,
            shot_count=len(storyboard),
        )
        await self._store.set_status(generation_id=generation_id, status=ExecutionStatus.RESOLVING)
        await self._store.record_resolution(
            generation_id=generation_id,
            resolution=resolution,
            outcome=ExecutionOutcome.SUCCESS if chosen else ExecutionOutcome.NONE,
        )

        if chosen is None:
            log.warning("generation.no_eligible_provider", capability=IMAGE_CAPABILITY)
            reason = "no eligible provider for capability"
            await self._store.fail(generation_id=generation_id, reason=reason)
            return _failed(generation_id, plan.title, provenance, reason=reason)

        log.info(
            "generation.started",
            shots=len(storyboard),
            adapter=chosen.adapter_id,
            execution_mode=request.execution_mode.value,
        )
        await self._store.set_status(generation_id=generation_id, status=ExecutionStatus.GENERATING)

        shot_results: list[ShotResult] = []
        frames: list[SlideshowFrame] = []
        timeline_frames: list[TimelineFrame] = []
        reference_bytes: bytes | None = None

        for shot in storyboard:
            outcome = await self._render_shot(
                shot, request=request, chosen=chosen, reference=reference_bytes
            )
            if not outcome.result.accepted or outcome.image is None:
                await self._store.record_shot(
                    _shot_record(generation_id, shot, outcome, chosen, asset_id=None)
                )
                shot_results.append(outcome.result)
                log.warning(
                    "generation.shot_gave_up", index=shot.index, reason=outcome.result.reason
                )
                reason = f"shot {shot.index} failed verification: {outcome.result.reason}"
                await self._store.fail(generation_id=generation_id, reason=reason)
                return _failed(
                    generation_id, plan.title, provenance, shots=tuple(shot_results), reason=reason
                )
            # Persist the accepted frame (asset reuse / debugging / future repair).
            frame_key = f"frames/{generation_id}/{shot.index:03d}.png"
            stored = await self._storage.put(
                key=frame_key, data=outcome.image.data, content_type=outcome.image.content_type
            )
            obs = outcome.observed
            asset_id = await self._store.register_asset(
                NewGenerationAsset(
                    generation_id=generation_id,
                    asset_kind=GenerationAssetKind.FRAME,
                    storage_backend=stored.backend,
                    storage_bucket=stored.bucket,
                    storage_key=stored.key,
                    mime_type=outcome.image.content_type,
                    shot_number=shot.index,
                    size_bytes=len(outcome.image.data),
                    checksum_sha256=hashlib.sha256(outcome.image.data).digest(),
                    width=obs.width if obs else None,
                    height=obs.height if obs else None,
                    metadata={"seed": outcome.result.seed},
                )
            )
            await self._store.record_shot(
                _shot_record(generation_id, shot, outcome, chosen, asset_id=asset_id)
            )
            shot_results.append(replace(outcome.result, frame_key=frame_key))
            frames.append(
                SlideshowFrame(data=outcome.image.data, duration_seconds=shot.duration_seconds)
            )
            timeline_frames.append(
                TimelineFrame(
                    index=shot.index,
                    duration_seconds=shot.duration_seconds,
                    width=obs.width if obs else None,
                    height=obs.height if obs else None,
                    content_hash=obs.perceptual_hash if obs else None,
                )
            )
            if reference_bytes is None:
                reference_bytes = outcome.image.data  # anchor consistency to first accepted frame

        # Timeline gate — catch missing/duplicate/out-of-order/duration/aspect issues
        # cheaply, before spending an ffmpeg render on a broken timeline.
        timeline = verify_timeline(
            tuple(timeline_frames),
            expected_count=len(storyboard),
            aspect_ratio=request.aspect_ratio,
            aspect_ratio_tolerance=_ASPECT_TOLERANCE,
        )
        if not timeline.passed:
            reasons = [f"{c.name}: {c.detail}" for c in timeline.failures]
            log.warning("generation.timeline_verification_failed", checks=reasons)
            reason = "; ".join(reasons)
            await self._store.fail(generation_id=generation_id, reason=reason)
            return _failed(
                generation_id,
                plan.title,
                provenance,
                shots=tuple(shot_results),
                reason=reason,
                checks=tuple(reasons),
            )

        await self._store.set_status(generation_id=generation_id, status=ExecutionStatus.RENDERING)
        spec = SlideshowSpec(width=request.width, height=request.height, fps=request.fps)
        rendered = await self._renderer.render(frames=tuple(frames), spec=spec)
        video_key = f"renders/{generation_id}.mp4"
        stored_video = await self._storage.put(
            key=video_key, data=rendered.data, content_type=rendered.content_type
        )

        await self._store.set_status(generation_id=generation_id, status=ExecutionStatus.EXPORTING)
        observed_video = await self._probe.probe(rendered.data)
        expected_total = sum(f.duration_seconds for f in frames)
        ok, checks = _verify_video(observed_video, expected_total, request.width, request.height)

        video_asset_id = await self._store.register_asset(
            NewGenerationAsset(
                generation_id=generation_id,
                asset_kind=GenerationAssetKind.VIDEO,
                storage_backend=stored_video.backend,
                storage_bucket=stored_video.bucket,
                storage_key=stored_video.key,
                mime_type=rendered.content_type,
                size_bytes=len(rendered.data),
                checksum_sha256=hashlib.sha256(rendered.data).digest(),
                width=observed_video.width,
                height=observed_video.height,
                duration_ms=(
                    int(observed_video.duration_seconds * 1000)
                    if observed_video.duration_seconds is not None
                    else None
                ),
                metadata={"checks": list(checks)},
            )
        )

        if not ok:
            log.warning("generation.video_verification_failed", checks=checks)
            reason = "; ".join(checks)
            await self._store.fail(generation_id=generation_id, reason=reason)
            return _failed(
                generation_id,
                plan.title,
                provenance,
                shots=tuple(shot_results),
                video_key=video_key,
                reason=reason,
                checks=tuple(checks),
            )

        await self._store.complete(
            generation_id=generation_id,
            final_video_asset_id=video_asset_id,
            storage_backend=stored_video.backend,
            storage_bucket=stored_video.bucket,
            storage_key=stored_video.key,
            duration_seconds=observed_video.duration_seconds,
            width=observed_video.width,
            height=observed_video.height,
        )
        log.info(
            "generation.succeeded", video_key=video_key, duration=observed_video.duration_seconds
        )
        return GenerateVideoResult(
            status=GenerationStatus.SUCCEEDED,
            generation_id=generation_id,
            title=plan.title,
            provenance=provenance,
            shots=tuple(shot_results),
            video_key=video_key,
            duration_seconds=observed_video.duration_seconds,
            width=observed_video.width,
            height=observed_video.height,
            checks=tuple(checks),
        )

    async def _render_shot(
        self,
        shot: ShotPrompt,
        *,
        request: GenerateVideoRequest,
        chosen: ResolvedAdapter,
        reference: bytes | None,
    ) -> _ShotOutcome:
        """Generate -> verify -> repair loop for a single shot."""
        expectation = VerificationExpectation(
            min_width=request.min_width,
            min_height=request.min_height,
            aspect_ratio=request.aspect_ratio,
            # consistency only kicks in once we have a reference frame.
            min_similarity=request.min_similarity if reference is not None else None,
            aspect_ratio_tolerance=_ASPECT_TOLERANCE,
        )

        local_path = await self._ensure_model(chosen)
        seed = shot.seed
        attempts: list[AttemptRecord] = []

        for attempt in range(1, request.max_attempts + 1):
            image = await self._image_generator.generate(
                adapter_id=chosen.adapter_id,
                prompt=shot.prompt_text,
                seed=seed,
                width=request.width,
                height=request.height,
                negative_prompt=shot.negative_prompt,
                reference_image_refs=shot.reference_image_refs,
                local_model_path=local_path,
            )
            observed: ObservedImage = await self._features.extract(image.data, reference=reference)
            report = verify_image(observed, expectation)
            decision = decide_repair(
                report, attempt=attempt, current_seed=seed, max_attempts=request.max_attempts
            )
            attempts.append(
                AttemptRecord(
                    attempt=attempt,
                    seed=seed,
                    verification_passed=report.passed,
                    action=decision.action.value,
                    reason=decision.reason,
                )
            )
            if decision.action is RepairAction.ACCEPT:
                return _ShotOutcome(
                    result=ShotResult(
                        index=shot.index,
                        accepted=True,
                        frame_key=None,
                        seed=seed,
                        attempts=tuple(attempts),
                    ),
                    image=image,
                    observed=observed,
                )
            if decision.action is RepairAction.RETRY and decision.next_seed is not None:
                seed = decision.next_seed
                continue
            break

        return _ShotOutcome(
            result=ShotResult(
                index=shot.index,
                accepted=False,
                frame_key=None,
                seed=seed,
                attempts=tuple(attempts),
                reason=attempts[-1].reason if attempts else "no attempt",
            ),
            image=None,
            observed=None,
        )

    async def _ensure_model(self, chosen: ResolvedAdapter) -> str | None:
        """Drive the Model Cache seam for local-tier adapters only."""
        if (
            self._model_manager is not None
            and chosen.execution_tier is ExecutionTier.LOCAL
            and chosen.model_ref is not None
        ):
            handle = await self._model_manager.ensure_available(chosen.model_ref)
            return handle.local_path
        return None


class _ShotOutcome:
    __slots__ = ("result", "image", "observed")

    def __init__(
        self,
        *,
        result: ShotResult,
        image: GeneratedImage | None,
        observed: ObservedImage | None,
    ) -> None:
        self.result = result
        self.image = image
        self.observed = observed


def _shot_record(
    generation_id: UUID,
    shot: ShotPrompt,
    outcome: _ShotOutcome,
    chosen: ResolvedAdapter,
    *,
    asset_id: UUID | None,
) -> ShotRecord:
    attempts = tuple(asdict(a) for a in outcome.result.attempts)
    return ShotRecord(
        generation_id=generation_id,
        shot_number=shot.index,
        prompt=shot.prompt_text,
        accepted=outcome.result.accepted,
        negative_prompt=shot.negative_prompt,
        reference_images=tuple(shot.reference_image_refs),
        adapter_used=chosen.adapter_id,
        seed=outcome.result.seed,
        verification={
            "accepted": outcome.result.accepted,
            "attempts": len(attempts),
            "reason": outcome.result.reason,
        },
        attempts=attempts,
        repair_count=max(0, len(attempts) - 1),
        asset_id=asset_id,
        reason=outcome.result.reason or None,
    )


def _provenance(
    generation_id: UUID, request: GenerateVideoRequest, resolution: CapabilityResolution
) -> GenerationProvenance:
    top = resolution.top
    return GenerationProvenance(
        generation_id=generation_id,
        capability=resolution.capability,
        execution_mode=request.execution_mode.value,
        resolver_version=resolution.resolver_version,
        chosen_adapter=top.adapter_id if top else None,
        chosen_provider=top.provider_id if top else None,
        execution_tier=(
            top.execution_tier.value if top and top.execution_tier is not None else None
        ),
        catalogue_version=resolution.catalogue_version,
        manifest_digest=resolution.manifest_digest,
        candidate_adapters=tuple(c.adapter_id for c in resolution.candidates),
        planner_version=PLANNER_VERSION,
        storyboard_version=STORYBOARD_VERSION,
        prompt_builder_version=PROMPT_BUILDER_VERSION,
        verifier_version=VERIFIER_VERSION,
        repair_version=REPAIR_VERSION,
        renderer_version=RENDERER_VERSION,
        score_schema_version=SCORE_SCHEMA_VERSION,
    )


def _verify_video(
    observed: ObservedVideo, expected_total: float, width: int, height: int
) -> tuple[bool, list[str]]:
    checks: list[str] = []
    ok = True
    if observed.duration_seconds is None:
        checks.append("duration: not measured")
    elif expected_total > 0 and abs(observed.duration_seconds - expected_total) > (
        _DURATION_TOLERANCE * expected_total
    ):
        ok = False
        checks.append(
            f"duration {observed.duration_seconds:.2f}s off expected {expected_total:.2f}s"
        )
    else:
        checks.append("duration: ok")

    if observed.width is not None and observed.height is not None:
        if observed.width != width or observed.height != height:
            ok = False
            checks.append(f"dimensions {observed.width}x{observed.height} != {width}x{height}")
        else:
            checks.append("dimensions: ok")
    else:
        checks.append("dimensions: not measured")
    return ok, checks


def _failed(
    generation_id: UUID,
    title: str,
    provenance: GenerationProvenance,
    *,
    shots: tuple[ShotResult, ...] = (),
    video_key: str | None = None,
    reason: str = "",
    checks: tuple[str, ...] = (),
) -> GenerateVideoResult:
    return GenerateVideoResult(
        status=GenerationStatus.FAILED,
        generation_id=generation_id,
        title=title,
        provenance=provenance,
        shots=shots,
        video_key=video_key,
        reason=reason,
        checks=checks,
    )
