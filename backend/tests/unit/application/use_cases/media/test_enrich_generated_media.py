"""Unit tests for the α8.4c/d enrichment pipeline + ``MediaEnrichmentWorker``.

Drives `EnrichGeneratedMedia` (now a pipeline of independent enrichers: thumbnail +
preview + gif + waveform) against in-memory fakes: a generated video ``MediaAsset``
whose bytes live in fake object storage, plus fake FFmpeg ports. Asserts the full
derived set, the versioned marker + backfill (Fork D), the recursion guard / W8.4d.1
(derived media is terminal), per-artifact failure isolation, waveform-not-applicable,
deterministic-key idempotency (Fork F), and the lease path. Worker tests assert the
poll-drain + batch behaviour and the shrinking claim set.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID, uuid4

import pytest

from app.application.interfaces.gif_previewer import GifPreview, GifPreviewError, IGifPreviewer
from app.application.interfaces.object_storage import IObjectStorage, StoredObject
from app.application.interfaces.preview_clipper import (
    IPreviewClipper,
    PreviewClip,
    PreviewClipError,
)
from app.application.interfaces.thumbnailer import IThumbnailer, Thumbnail, ThumbnailError
from app.application.interfaces.waveform_renderer import (
    IWaveformRenderer,
    Waveform,
    WaveformError,
)
from app.application.use_cases.media.enrich_generated_media import (
    CURRENT_ENRICHMENT_VERSION,
    EnrichGeneratedMedia,
)
from app.application.use_cases.media.enrichers import (
    GifEnricher,
    PreviewEnricher,
    ThumbnailEnricher,
    WaveformEnricher,
)
from app.application.use_cases.media.media_enrichment_worker import MediaEnrichmentWorker
from app.infrastructure.storage import StorageResolver
from tests.unit.application.use_cases.auth._fakes import (
    FakeMediaRepository,
    FakeProjectRepository,
    FakeUnitOfWork,
)
from tests.unit.application.use_cases.media._helpers import make_project

pytestmark = pytest.mark.unit


class FakeObjectStorage(IObjectStorage):
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.put_keys: list[str] = []

    @property
    def backend(self) -> str:
        return "local"

    @property
    def bucket(self) -> str:
        return "generated"

    async def put(self, *, key: str, data: bytes, content_type: str | None) -> StoredObject:
        self.objects[key] = data
        self.put_keys.append(key)
        return StoredObject(backend="local", bucket="generated", key=key)

    async def get(self, *, key: str) -> bytes:
        return self.objects[key]

    async def exists(self, *, key: str) -> bool:
        return key in self.objects

    async def delete(self, *, key: str) -> None:
        self.objects.pop(key, None)


class FakeThumbnailer(IThumbnailer):
    def __init__(self, *, error: Exception | None = None) -> None:
        self._error = error
        self.calls = 0

    async def thumbnail(self, *, source_path: str, at_seconds: float) -> Thumbnail:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return Thumbnail(
            image=b"JPG", mime_type="image/jpeg", width=320, height=180, source_bitrate=1_500_000
        )


class FakePreviewClipper(IPreviewClipper):
    def __init__(self, *, error: Exception | None = None) -> None:
        self._error = error
        self.calls = 0

    async def preview(self, *, source_path: str, max_seconds: float, max_width: int) -> PreviewClip:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return PreviewClip(
            data=b"MP4", mime_type="video/mp4", width=640, height=360, duration_seconds=5.0
        )


class FakeGifPreviewer(IGifPreviewer):
    def __init__(self, *, error: Exception | None = None) -> None:
        self._error = error
        self.calls = 0

    async def gif(
        self, *, source_path: str, max_seconds: float, fps: int, max_width: int
    ) -> GifPreview:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return GifPreview(data=b"GIF", mime_type="image/gif", width=480, height=270)


class FakeWaveformRenderer(IWaveformRenderer):
    def __init__(self, *, has_audio: bool = True, error: Exception | None = None) -> None:
        self._has_audio = has_audio
        self._error = error
        self.calls = 0

    async def waveform(self, *, source_path: str, width: int, height: int) -> Waveform | None:
        self.calls += 1
        if self._error is not None:
            raise self._error
        if not self._has_audio:
            return None
        return Waveform(data=b"PNG", mime_type="image/png", width=width, height=height)


class _Fixture:
    def __init__(self) -> None:
        self.owner = uuid4()
        self.tenant = uuid4()
        project = make_project(tenant_id=self.tenant, owner_user_id=self.owner)
        self.project_id = project.id
        self.projects = FakeProjectRepository(_rows={project.id: project})
        self.media = FakeMediaRepository()
        self.storage = FakeObjectStorage()
        # No render_jobs / workflow_runs / checkpoints wired — enrichment is a pure
        # function of the parent MediaAsset (W8.4c.3).
        self.uow = FakeUnitOfWork(projects=self.projects, media=self.media)
        self.thumb = FakeThumbnailer()
        self.clip = FakePreviewClipper()
        self.gif = FakeGifPreviewer()
        self.wave = FakeWaveformRenderer()

    async def seed_video(
        self,
        *,
        key: str = "renders/out.mp4",
        kind: str = "video",
        source: str = "generated",
        source_metadata: dict | None = None,
    ) -> UUID:
        self.storage.objects[key] = b"VIDEOBYTES"
        asset = await self.media.add(
            tenant_id=self.tenant,
            owner_user_id=self.owner,
            kind=kind,
            source=source,
            storage_backend="local",
            storage_bucket="generated",
            storage_key=key,
            mime_type="video/mp4",
            size_bytes=10,
            checksum_sha256=b"\x00" * 32,
            project_id=self.project_id,
            scene_id=None,
            prompt_id=None,
            model_id=None,
            provider=None,
            width=1920,
            height=1080,
            duration_seconds=5.0,
            source_metadata=(
                source_metadata if source_metadata is not None else {"origin": "render"}
            ),
        )
        return asset.id

    def enrichers(self) -> list:
        return [
            ThumbnailEnricher(self.thumb),
            PreviewEnricher(self.clip),
            GifEnricher(self.gif),
            WaveformEnricher(self.wave),
        ]

    def use_case(self) -> EnrichGeneratedMedia:
        return EnrichGeneratedMedia(
            self.uow, StorageResolver.single(self.storage), self.enrichers()
        )

    def worker(self, *, batch_size: int = 10) -> MediaEnrichmentWorker:
        return MediaEnrichmentWorker(uow=self.uow, enrich=self.use_case(), batch_size=batch_size)

    async def parent(self, asset_id: UUID):
        return await self.media.get_owned(asset_id, self.tenant, self.owner)


# --------------------------------------------------------------------------
# Pipeline — happy path & marker
# --------------------------------------------------------------------------


async def test_happy_path_derives_full_set_and_versions_marker() -> None:
    fx = _Fixture()
    asset_id = await fx.seed_video()

    result = await fx.use_case().execute(asset=await fx.parent(asset_id))

    assert result.status == "enriched"
    assert set(result.derived_media_ids) == {"thumbnail", "preview", "gif", "waveform"}
    assert result.failed == ()
    # All four enrichers ran exactly once off a single materialization.
    assert (fx.thumb.calls, fx.clip.calls, fx.gif.calls, fx.wave.calls) == (1, 1, 1, 1)

    # Four derived assets registered (+ the parent = 5 rows).
    assert len(fx.media._media) == 5
    origins = {
        m.source_metadata.get("origin")
        for m in fx.media._media.values()
        if m.source_metadata.get("origin") in {"thumbnail", "preview", "gif", "waveform"}
    }
    assert origins == {"thumbnail", "preview", "gif", "waveform"}
    # Each derived asset is cross-linked to the parent (W8.4d.1 provenance).
    for origin, mid in result.derived_media_ids.items():
        d = await fx.parent(mid)
        assert d.source_metadata["parent_media_asset_id"] == str(asset_id)
        assert d.source_metadata["origin"] == origin

    # Deterministic keys.
    assert f"thumbnails/{fx.tenant}/{asset_id}.jpg" in fx.storage.objects
    assert f"previews/{fx.tenant}/{asset_id}.mp4" in fx.storage.objects
    assert f"gifs/{fx.tenant}/{asset_id}.gif" in fx.storage.objects
    assert f"waveforms/{fx.tenant}/{asset_id}.png" in fx.storage.objects

    # Parent marker: version + ids + scalars, original keys preserved.
    refreshed = await fx.parent(asset_id)
    enr = refreshed.source_metadata["enrichment"]
    assert refreshed.source_metadata["origin"] == "render"
    assert enr["version"] == CURRENT_ENRICHMENT_VERSION
    assert enr["bitrate"] == 1_500_000
    assert enr["thumbnail_media_asset_id"] == str(result.derived_media_ids["thumbnail"])
    assert enr["preview_media_asset_id"] == str(result.derived_media_ids["preview"])
    assert enr["gif_media_asset_id"] == str(result.derived_media_ids["gif"])
    assert enr["waveform_media_asset_id"] == str(result.derived_media_ids["waveform"])
    assert "enriched_at" in enr


async def test_enriched_asset_drops_out_of_claim_scan() -> None:
    fx = _Fixture()
    asset_id = await fx.seed_video()

    before = await fx.media.list_enrichable_generated_videos(
        target_version=CURRENT_ENRICHMENT_VERSION, limit=10
    )
    assert [a.id for a in before] == [asset_id]

    await fx.use_case().execute(asset=await fx.parent(asset_id))

    after = await fx.media.list_enrichable_generated_videos(
        target_version=CURRENT_ENRICHMENT_VERSION, limit=10
    )
    assert after == []


# --------------------------------------------------------------------------
# Backfill (Fork D) & idempotency (Fork F)
# --------------------------------------------------------------------------


async def test_backfill_reclaims_alpha84c_era_asset() -> None:
    fx = _Fixture()
    # An α8.4c-era marker: thumbnail only, NO version field → counts as version 0.
    asset_id = await fx.seed_video(
        source_metadata={
            "origin": "render",
            "enrichment": {"thumbnail_media_asset_id": str(uuid4()), "bitrate": 900_000},
        }
    )

    claimable = await fx.media.list_enrichable_generated_videos(
        target_version=CURRENT_ENRICHMENT_VERSION, limit=10
    )
    assert [a.id for a in claimable] == [asset_id]  # version 0 < target → re-claimed

    result = await fx.use_case().execute(asset=await fx.parent(asset_id))

    assert result.status == "enriched"
    assert set(result.derived_media_ids) == {"thumbnail", "preview", "gif", "waveform"}
    enr = (await fx.parent(asset_id)).source_metadata["enrichment"]
    assert enr["version"] == CURRENT_ENRICHMENT_VERSION


async def test_idempotent_rerun_recovers_all_derived_assets() -> None:
    fx = _Fixture()
    asset_id = await fx.seed_video()

    first = await fx.use_case().execute(asset=await fx.parent(asset_id))
    count_after_first = len(fx.media._media)

    # Simulate a version bump / crash before the marker settled: strip the version so
    # the asset is re-claimable. Deterministic keys → ConflictError recovery, no dupes.
    parent = await fx.parent(asset_id)
    md = dict(parent.source_metadata)
    enr = {k: v for k, v in md["enrichment"].items() if k != "version"}
    md["enrichment"] = enr
    await fx.media.update_owned(asset_id, fx.tenant, fx.owner, {"source_metadata": md})

    second = await fx.use_case().execute(asset=await fx.parent(asset_id))

    assert second.status == "enriched"
    assert second.derived_media_ids == first.derived_media_ids  # same ids recovered
    assert len(fx.media._media) == count_after_first  # no duplicates


async def test_already_enriched_at_target_version_is_noop() -> None:
    fx = _Fixture()
    asset_id = await fx.seed_video(
        source_metadata={"origin": "render", "enrichment": {"version": CURRENT_ENRICHMENT_VERSION}}
    )

    result = await fx.use_case().execute(asset=await fx.parent(asset_id))

    assert result.status == "noop"
    assert result.reason == "already_enriched"
    assert fx.thumb.calls == 0


# --------------------------------------------------------------------------
# Recursion guard / W8.4d.1
# --------------------------------------------------------------------------


async def test_derived_asset_is_never_claimed_or_enriched() -> None:
    fx = _Fixture()
    # A derived preview *video* (kind=video, source=generated) with a parent link.
    derived_id = await fx.seed_video(
        key="previews/x.mp4",
        source_metadata={"origin": "preview", "parent_media_asset_id": str(uuid4())},
    )

    # Never appears in the claim scan (recursion guard).
    claimable = await fx.media.list_enrichable_generated_videos(
        target_version=CURRENT_ENRICHMENT_VERSION, limit=10
    )
    assert derived_id not in [a.id for a in claimable]

    # And a direct execute is a terminal no-op.
    result = await fx.use_case().execute(asset=await fx.parent(derived_id))
    assert result.status == "noop"
    assert result.reason == "derived"
    assert fx.thumb.calls == 0


# --------------------------------------------------------------------------
# Per-artifact isolation & applicability
# --------------------------------------------------------------------------


async def test_one_failing_enricher_does_not_block_others() -> None:
    fx = _Fixture()
    fx.gif = FakeGifPreviewer(error=GifPreviewError("boom"))
    asset_id = await fx.seed_video()

    result = await fx.use_case().execute(asset=await fx.parent(asset_id))

    assert result.status == "partial"
    assert set(result.derived_media_ids) == {"thumbnail", "preview", "waveform"}
    assert result.failed == ("gif",)
    # Version NOT bumped → still claimable so a later pass retries the gif.
    enr = (await fx.parent(asset_id)).source_metadata["enrichment"]
    assert "version" not in enr
    still = await fx.media.list_enrichable_generated_videos(
        target_version=CURRENT_ENRICHMENT_VERSION, limit=10
    )
    assert asset_id in [a.id for a in still]


async def test_waveform_not_applicable_is_clean_and_terminal() -> None:
    fx = _Fixture()
    fx.wave = FakeWaveformRenderer(has_audio=False)
    asset_id = await fx.seed_video()

    result = await fx.use_case().execute(asset=await fx.parent(asset_id))

    assert result.status == "enriched"  # no failure — waveform just skipped
    assert set(result.derived_media_ids) == {"thumbnail", "preview", "gif"}
    enr = (await fx.parent(asset_id)).source_metadata["enrichment"]
    assert enr["version"] == CURRENT_ENRICHMENT_VERSION  # terminally enriched
    assert "waveform_media_asset_id" not in enr


async def test_all_enrichers_failing_is_failed_and_reclaimable() -> None:
    fx = _Fixture()
    fx.thumb = FakeThumbnailer(error=ThumbnailError("x"))
    fx.clip = FakePreviewClipper(error=PreviewClipError("x"))
    fx.gif = FakeGifPreviewer(error=GifPreviewError("x"))
    fx.wave = FakeWaveformRenderer(error=WaveformError("x"))
    asset_id = await fx.seed_video()

    result = await fx.use_case().execute(asset=await fx.parent(asset_id))

    assert result.status == "failed"
    assert result.derived_media_ids == {}
    assert len(fx.media._media) == 1  # only the parent
    enr = (await fx.parent(asset_id)).source_metadata["enrichment"]
    assert "version" not in enr


# --------------------------------------------------------------------------
# Guards
# --------------------------------------------------------------------------


async def test_non_video_asset_is_noop() -> None:
    fx = _Fixture()
    asset_id = await fx.seed_video(kind="image")
    result = await fx.use_case().execute(asset=await fx.parent(asset_id))
    assert result.status == "noop"
    assert result.reason == "not_target"


async def test_unsupported_storage_is_noop() -> None:
    fx = _Fixture()
    asset_id = await fx.seed_video()
    await fx.media.update_owned(asset_id, fx.tenant, fx.owner, {"storage_bucket": "elsewhere"})
    result = await fx.use_case().execute(asset=await fx.parent(asset_id))
    assert result.status == "noop"
    assert result.reason == "unsupported_storage"


async def test_locked_asset_is_skipped() -> None:
    fx = _Fixture()
    asset_id = await fx.seed_video()
    async with fx.uow:
        await fx.uow.locks.acquire(
            key=f"media_enrichment:{asset_id}", owner="someone-else", lease=timedelta(seconds=60)
        )
        await fx.uow.commit()
    result = await fx.use_case().execute(asset=await fx.parent(asset_id))
    assert result.status == "skipped"
    assert result.reason == "locked"
    assert fx.thumb.calls == 0


# --------------------------------------------------------------------------
# Worker
# --------------------------------------------------------------------------


async def test_worker_drains_all_claimable_videos() -> None:
    fx = _Fixture()
    a = await fx.seed_video(key="renders/a.mp4")
    b = await fx.seed_video(key="renders/b.mp4")

    result = await fx.worker().run_once()

    assert result.scanned == 2
    assert {o.status for o in result.outcomes} == {"enriched"}
    assert {o.media_asset_id for o in result.outcomes} == {a, b}

    second = await fx.worker().run_once()
    assert second.scanned == 0


async def test_worker_respects_batch_size() -> None:
    fx = _Fixture()
    for i in range(3):
        await fx.seed_video(key=f"renders/{i}.mp4")

    result = await fx.worker(batch_size=2).run_once()

    assert result.scanned == 2


async def test_worker_empty_scan_is_noop() -> None:
    fx = _Fixture()
    result = await fx.worker().run_once()
    assert result.scanned == 0 and result.outcomes == []
