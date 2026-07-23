"""Unit tests for ``EnrichGeneratedMedia`` + ``MediaEnrichmentWorker`` (Slice α8.4c).

Drives enrichment against in-memory fakes: a generated video ``MediaAsset`` whose
bytes live in a fake object storage, plus a fake ``IThumbnailer`` (no real FFmpeg).
Asserts the observational/downstream contract (W8.4c.1), the pure-function-of-parent
contract (W8.4c.3 — the fixture wires **no** render-job/checkpoint repos), the
derived thumbnail + parent-metadata writes, deterministic-key idempotency (E1), and
the lease/failure paths. The worker tests assert the poll-drain + batch behaviour and
the shrinking claim set.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID, uuid4

import pytest

from app.application.interfaces.object_storage import IObjectStorage, StoredObject
from app.application.interfaces.thumbnailer import IThumbnailer, Thumbnail, ThumbnailError
from app.application.use_cases.media.enrich_generated_media import EnrichGeneratedMedia
from app.application.use_cases.media.media_enrichment_worker import MediaEnrichmentWorker
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
        self.calls: list[tuple[str, float]] = []

    async def thumbnail(self, *, source_path: str, at_seconds: float) -> Thumbnail:
        self.calls.append((source_path, at_seconds))
        if self._error is not None:
            raise self._error
        return Thumbnail(
            image=b"JPEGDATA",
            mime_type="image/jpeg",
            width=320,
            height=180,
            source_bitrate=1_500_000,
        )


class _Fixture:
    def __init__(self) -> None:
        self.owner = uuid4()
        self.tenant = uuid4()
        project = make_project(tenant_id=self.tenant, owner_user_id=self.owner)
        self.project_id = project.id
        self.projects = FakeProjectRepository(_rows={project.id: project})
        self.media = FakeMediaRepository()
        self.storage = FakeObjectStorage()
        # No render_jobs / workflow_runs / checkpoints wired — enrichment must be a
        # pure function of the parent MediaAsset (W8.4c.3).
        self.uow = FakeUnitOfWork(projects=self.projects, media=self.media)

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

    def use_case(self, thumbnailer: FakeThumbnailer) -> EnrichGeneratedMedia:
        return EnrichGeneratedMedia(self.uow, self.storage, thumbnailer, thumbnail_at_seconds=1.0)

    def worker(
        self, thumbnailer: FakeThumbnailer, *, batch_size: int = 10
    ) -> MediaEnrichmentWorker:
        return MediaEnrichmentWorker(
            uow=self.uow, enrich=self.use_case(thumbnailer), batch_size=batch_size
        )


# --------------------------------------------------------------------------
# EnrichGeneratedMedia
# --------------------------------------------------------------------------


async def test_happy_path_derives_thumbnail_and_marks_parent() -> None:
    fx = _Fixture()
    asset_id = await fx.seed_video()
    parent = await fx.media.get_owned(asset_id, fx.tenant, fx.owner)
    thumbnailer = FakeThumbnailer()

    result = await fx.use_case(thumbnailer).execute(asset=parent)

    assert result.status == "enriched"
    assert result.thumbnail_media_asset_id is not None
    assert len(thumbnailer.calls) == 1 and thumbnailer.calls[0][1] == 1.0

    # A derived thumbnail MediaAsset (image, generated) cross-linked to the parent.
    thumb = await fx.media.get_owned(result.thumbnail_media_asset_id, fx.tenant, fx.owner)
    assert thumb is not None
    assert thumb.kind == "image"
    assert thumb.source == "generated"
    assert thumb.mime_type == "image/jpeg"
    assert thumb.width == 320 and thumb.height == 180
    assert thumb.project_id == fx.project_id
    assert thumb.source_metadata["origin"] == "thumbnail"
    assert thumb.source_metadata["parent_media_asset_id"] == str(asset_id)

    # Deterministic thumbnail storage key.
    expected_key = f"thumbnails/{fx.tenant}/{asset_id}.jpg"
    assert thumb.storage_key == expected_key
    assert expected_key in fx.storage.objects

    # Parent augmented (not replaced) with the enrichment marker.
    refreshed = await fx.media.get_owned(asset_id, fx.tenant, fx.owner)
    assert refreshed.source_metadata["origin"] == "render"  # original key preserved
    enr = refreshed.source_metadata["enrichment"]
    assert enr["thumbnail_media_asset_id"] == str(result.thumbnail_media_asset_id)
    assert enr["bitrate"] == 1_500_000
    assert "enriched_at" in enr


async def test_enriched_asset_drops_out_of_claim_scan() -> None:
    fx = _Fixture()
    asset_id = await fx.seed_video()
    parent = await fx.media.get_owned(asset_id, fx.tenant, fx.owner)

    before = await fx.media.list_unenriched_generated_videos(limit=10)
    assert [a.id for a in before] == [asset_id]

    await fx.use_case(FakeThumbnailer()).execute(asset=parent)

    after = await fx.media.list_unenriched_generated_videos(limit=10)
    assert after == []


async def test_idempotent_rerun_recovers_existing_thumbnail() -> None:
    fx = _Fixture()
    asset_id = await fx.seed_video()
    parent = await fx.media.get_owned(asset_id, fx.tenant, fx.owner)
    thumbnailer = FakeThumbnailer()

    first = await fx.use_case(thumbnailer).execute(asset=parent)

    # Simulate a crash *after* the thumbnail was stored+registered but *before* the
    # parent marker committed: strip the enrichment marker and re-run. The
    # deterministic thumbnail key collides → the existing asset is recovered, not
    # duplicated.
    stripped = {k: v for k, v in parent.source_metadata.items() if k != "enrichment"}
    await fx.media.update_owned(asset_id, fx.tenant, fx.owner, {"source_metadata": stripped})
    reloaded = await fx.media.get_owned(asset_id, fx.tenant, fx.owner)
    before = len(fx.media._media)

    second = await fx.use_case(thumbnailer).execute(asset=reloaded)

    assert second.status == "enriched"
    assert second.thumbnail_media_asset_id == first.thumbnail_media_asset_id
    assert len(fx.media._media) == before  # no duplicate thumbnail


async def test_already_enriched_is_noop() -> None:
    fx = _Fixture()
    asset_id = await fx.seed_video(source_metadata={"origin": "render", "enrichment": {"x": 1}})
    parent = await fx.media.get_owned(asset_id, fx.tenant, fx.owner)
    thumbnailer = FakeThumbnailer()

    result = await fx.use_case(thumbnailer).execute(asset=parent)

    assert result.status == "noop"
    assert result.reason == "already_enriched"
    assert thumbnailer.calls == []


async def test_non_video_asset_is_noop() -> None:
    fx = _Fixture()
    asset_id = await fx.seed_video(kind="image")
    parent = await fx.media.get_owned(asset_id, fx.tenant, fx.owner)
    thumbnailer = FakeThumbnailer()

    result = await fx.use_case(thumbnailer).execute(asset=parent)

    assert result.status == "noop"
    assert result.reason == "not_target"
    assert thumbnailer.calls == []


async def test_unsupported_storage_is_noop() -> None:
    fx = _Fixture()
    asset_id = await fx.seed_video()
    # Rewrite the parent's storage bucket to something the worker's storage can't read.
    await fx.media.update_owned(asset_id, fx.tenant, fx.owner, {"storage_bucket": "elsewhere"})
    parent = await fx.media.get_owned(asset_id, fx.tenant, fx.owner)
    thumbnailer = FakeThumbnailer()

    result = await fx.use_case(thumbnailer).execute(asset=parent)

    assert result.status == "noop"
    assert result.reason == "unsupported_storage"
    assert thumbnailer.calls == []


async def test_locked_asset_is_skipped() -> None:
    fx = _Fixture()
    asset_id = await fx.seed_video()
    parent = await fx.media.get_owned(asset_id, fx.tenant, fx.owner)
    async with fx.uow:
        await fx.uow.locks.acquire(
            key=f"media_enrichment:{asset_id}", owner="someone-else", lease=timedelta(seconds=60)
        )
        await fx.uow.commit()
    thumbnailer = FakeThumbnailer()

    result = await fx.use_case(thumbnailer).execute(asset=parent)

    assert result.status == "skipped"
    assert result.reason == "locked"
    assert thumbnailer.calls == []


async def test_thumbnail_failure_leaves_asset_unenriched() -> None:
    fx = _Fixture()
    asset_id = await fx.seed_video()
    parent = await fx.media.get_owned(asset_id, fx.tenant, fx.owner)
    thumbnailer = FakeThumbnailer(error=ThumbnailError("ffmpeg exploded"))

    result = await fx.use_case(thumbnailer).execute(asset=parent)

    assert result.status == "failed"
    # No thumbnail registered; parent stays un-enriched so a later scan retries.
    assert len(fx.media._media) == 1
    still = await fx.media.list_unenriched_generated_videos(limit=10)
    assert [a.id for a in still] == [asset_id]


# --------------------------------------------------------------------------
# MediaEnrichmentWorker
# --------------------------------------------------------------------------


async def test_worker_drains_all_unenriched_videos() -> None:
    fx = _Fixture()
    a = await fx.seed_video(key="renders/a.mp4")
    b = await fx.seed_video(key="renders/b.mp4")
    thumbnailer = FakeThumbnailer()

    result = await fx.worker(thumbnailer).run_once()

    assert result.scanned == 2
    assert {o.status for o in result.outcomes} == {"enriched"}
    assert {o.media_asset_id for o in result.outcomes} == {a, b}

    # A second pass finds nothing left to do.
    second = await fx.worker(thumbnailer).run_once()
    assert second.scanned == 0


async def test_worker_respects_batch_size() -> None:
    fx = _Fixture()
    for i in range(3):
        await fx.seed_video(key=f"renders/{i}.mp4")
    thumbnailer = FakeThumbnailer()

    result = await fx.worker(thumbnailer, batch_size=2).run_once()

    assert result.scanned == 2


async def test_worker_empty_scan_is_noop() -> None:
    fx = _Fixture()
    result = await fx.worker(FakeThumbnailer()).run_once()
    assert result.scanned == 0 and result.outcomes == []
