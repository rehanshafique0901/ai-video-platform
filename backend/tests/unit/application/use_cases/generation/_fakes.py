"""In-memory port fakes for the ``GenerateVideo`` use case tests."""

from __future__ import annotations

from app.application.interfaces.capability_resolver import (
    CapabilityResolution,
    ICapabilityResolver,
    ResolvedAdapter,
)
from app.application.interfaces.image_feature_extractor import IImageFeatureExtractor
from app.application.interfaces.image_generator import GeneratedImage, IImageGenerator
from app.application.interfaces.model_manager import IModelManager, LocalModel
from app.application.interfaces.object_storage import IObjectStorage, StoredObject
from app.application.interfaces.slideshow_renderer import (
    ISlideshowRenderer,
    RenderedVideo,
    SlideshowFrame,
    SlideshowSpec,
)
from app.application.interfaces.video_probe import IVideoProbe, ObservedVideo
from app.domain.generation.execution import ExecutionTier
from app.domain.generation.verification import ObservedImage


class FakeCapabilityResolver(ICapabilityResolver):
    """Returns a fixed candidate list; records the constraints it was asked with."""

    def __init__(self, candidates: tuple[ResolvedAdapter, ...]) -> None:
        self._candidates = candidates
        self.calls: list[dict[str, object]] = []

    async def resolve(
        self, *, capability, constraints, prompt=None, budget=None
    ) -> CapabilityResolution:
        self.calls.append({"capability": capability, "constraints": constraints, "budget": budget})
        return CapabilityResolution(
            capability=capability,
            resolver_version="test-resolver-1",
            candidates=self._candidates,
            catalogue_version="2026.07.25",
            manifest_digest="deadbeef",
        )


class FakeImageGenerator(IImageGenerator):
    """Produces deterministic bytes; records every generate call."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def generate(
        self,
        *,
        adapter_id,
        prompt,
        seed,
        width,
        height,
        negative_prompt=None,
        reference_image_refs=(),
        local_model_path=None,
    ) -> GeneratedImage:
        self.calls.append(
            {
                "adapter_id": adapter_id,
                "prompt": prompt,
                "seed": seed,
                "negative_prompt": negative_prompt,
                "reference_image_refs": reference_image_refs,
                "local_model_path": local_model_path,
            }
        )
        return GeneratedImage(
            data=f"img:{adapter_id}:{seed}:{prompt}".encode(),
            content_type="image/png",
            adapter_id=adapter_id,
            provider_id="p",
        )


class FakeFeatureExtractor(IImageFeatureExtractor):
    """Returns canned features; optionally fails the first N calls (repair tests)."""

    def __init__(
        self,
        *,
        width: int = 720,
        height: int = 1280,
        similarity: float = 0.95,
        fail_first: int = 0,
    ) -> None:
        self._width = width
        self._height = height
        self._similarity = similarity
        self._fail_first = fail_first
        self.calls = 0

    async def extract(self, image: bytes, *, reference: bytes | None = None) -> ObservedImage:
        self.calls += 1
        blank = self.calls <= self._fail_first  # first N attempts look blank -> FAIL
        return ObservedImage(
            produced=True,
            width=self._width,
            height=self._height,
            is_blank=blank,
            similarity_to_reference=None if reference is None else self._similarity,
            has_watermark=False,
        )


class FakeSlideshowRenderer(ISlideshowRenderer):
    def __init__(self) -> None:
        self.rendered: list[SlideshowSpec] = []
        self.frame_count = 0

    async def render(
        self,
        *,
        frames: tuple[SlideshowFrame, ...],
        spec: SlideshowSpec,
        audio: bytes | None = None,
    ) -> RenderedVideo:
        self.rendered.append(spec)
        self.frame_count = len(frames)
        return RenderedVideo(data=b"MP4:" + str(len(frames)).encode())


class FakeVideoProbe(IVideoProbe):
    def __init__(self, *, duration: float | None, width: int | None, height: int | None) -> None:
        self._observed = ObservedVideo(duration_seconds=duration, width=width, height=height)

    async def probe(self, video: bytes) -> ObservedVideo:
        return self._observed


class FakeObjectStorage(IObjectStorage):
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    @property
    def backend(self) -> str:
        return "memory"

    @property
    def bucket(self) -> str:
        return "test"

    async def put(self, *, key: str, data: bytes, content_type: str | None) -> StoredObject:
        self.objects[key] = data
        return StoredObject(backend=self.backend, bucket=self.bucket, key=key)

    async def get(self, *, key: str) -> bytes:
        return self.objects[key]

    async def exists(self, *, key: str) -> bool:
        return key in self.objects

    async def delete(self, *, key: str) -> None:
        self.objects.pop(key, None)


class FakeModelManager(IModelManager):
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def ensure_available(self, model_ref: str) -> LocalModel:
        self.calls.append(model_ref)
        return LocalModel(
            model_ref=model_ref,
            local_path=f"/cache/{model_ref}",
            from_cache=len(self.calls) > 1,
        )


def remote_adapter(adapter_id: str = "pollinations.image", score: float = 90.0) -> ResolvedAdapter:
    return ResolvedAdapter(
        adapter_id=adapter_id,
        provider_id="pollinations",
        score=score,
        execution_tier=ExecutionTier.FREE_REMOTE,
    )


def local_adapter(model_ref: str = "flux-schnell") -> ResolvedAdapter:
    return ResolvedAdapter(
        adapter_id="comfyui.flux",
        provider_id="comfyui",
        score=95.0,
        execution_tier=ExecutionTier.LOCAL,
        model_ref=model_ref,
    )
