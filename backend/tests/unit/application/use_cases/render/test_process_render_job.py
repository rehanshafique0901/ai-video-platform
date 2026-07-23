"""Unit tests for ``ProcessRenderJob`` (Slice α8.4b).

Drives the render use case against in-memory fakes: a queued ``RenderJob`` whose
project timeline references video ``MediaAsset``s stored in a fake object storage,
plus a fake ``IRenderer`` (no real FFmpeg). Asserts the pure Timeline → Media
transform contract (W8.4b.1/W8.4b.2), the queued→running→succeeded lifecycle,
deterministic-key idempotency, and the failure path.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from app.application.interfaces.object_storage import IObjectStorage, StoredObject
from app.application.interfaces.renderer import (
    IRenderer,
    RenderError,
    RenderResult,
    RenderSpec,
)
from app.application.use_cases.render.process_render_job import ProcessRenderJob
from app.domain.render.render_status import RenderStatus
from tests.unit.application.use_cases.auth._fakes import (
    FakeMediaRepository,
    FakeProjectRepository,
    FakeRenderJobRepository,
    FakeTimelineRepository,
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


class FakeRenderer(IRenderer):
    """Writes deterministic output bytes; records the spec it was handed."""

    def __init__(self, *, output: bytes = b"RENDERED-MP4", error: Exception | None = None) -> None:
        self._output = output
        self._error = error
        self.specs: list[RenderSpec] = []

    async def render(self, spec: RenderSpec) -> RenderResult:
        self.specs.append(spec)
        if self._error is not None:
            raise self._error
        Path(spec.output_path).write_bytes(self._output)
        return RenderResult(
            output_path=spec.output_path,
            size_bytes=len(self._output),
            duration_seconds=5.0,
            width=1920,
            height=1080,
            codec="h264",
        )


class _Fixture:
    def __init__(self) -> None:
        self.owner = uuid4()
        self.tenant = uuid4()
        project = make_project(tenant_id=self.tenant, owner_user_id=self.owner)
        self.project_id = project.id
        self.projects = FakeProjectRepository(_rows={project.id: project})
        self.timeline_repo = FakeTimelineRepository()
        self.media = FakeMediaRepository()
        self.render_jobs = FakeRenderJobRepository()
        self.storage = FakeObjectStorage()
        self.uow = FakeUnitOfWork(
            projects=self.projects,
            timeline=self.timeline_repo,
            media=self.media,
            render_jobs=self.render_jobs,
        )
        self.timeline_id: UUID | None = None

    async def seed_video_asset(self, key: str, data: bytes = b"SRC") -> UUID:
        self.storage.objects[key] = data
        asset = await self.media.add(
            tenant_id=self.tenant,
            owner_user_id=self.owner,
            kind="video",
            source="generated",
            storage_backend="local",
            storage_bucket="generated",
            storage_key=key,
            mime_type="video/mp4",
            size_bytes=len(data),
            checksum_sha256=b"\x00" * 32,
            project_id=self.project_id,
            scene_id=None,
            prompt_id=None,
            model_id=None,
            provider=None,
            width=1920,
            height=1080,
            duration_seconds=5.0,
            source_metadata={},
        )
        return asset.id

    async def seed_timeline(self, asset_ids: list[UUID]) -> UUID:
        timeline = await self.timeline_repo.add(
            project_id=self.project_id,
            aspect_ratio="16:9",
            frame_rate=30,
            background_color="#000000",
        )
        track = await self.timeline_repo.add_track(
            timeline_id=timeline.id,
            kind="video",
            z_index=0,
            name="V1",
            locked=False,
            muted=False,
        )
        for i, asset_id in enumerate(asset_ids):
            await self.timeline_repo.add_clip(
                track_id=track.id,
                media_asset_id=asset_id,
                start_seconds=float(i),
                end_seconds=float(i) + 1.0,
                source_start_seconds=0.0,
                source_end_seconds=1.0,
                volume=1.0,
                locked=False,
            )
        self.timeline_id = timeline.id
        return timeline.id

    async def seed_job(self, timeline_id: UUID, *, status: str = "queued") -> UUID:
        job = await self.render_jobs.add(
            project_id=self.project_id,
            timeline_id=timeline_id,
            pipeline="render",
            pipeline_version="1",
            queue="default",
            priority=0,
            status=status,
            idempotency_key=None,
        )
        return job.id


async def _process(fx: _Fixture, renderer: FakeRenderer, job_id: UUID):
    uc = ProcessRenderJob(uow=fx.uow, storage=fx.storage, renderer=renderer)
    return await uc.process(project_id=fx.project_id, render_job_id=job_id)


async def test_happy_path_renders_and_registers_output_asset() -> None:
    fx = _Fixture()
    asset_id = await fx.seed_video_asset("srcA")
    timeline_id = await fx.seed_timeline([asset_id])
    job_id = await fx.seed_job(timeline_id)
    renderer = FakeRenderer()

    result = await _process(fx, renderer, job_id)

    assert result.status == "rendered"
    assert result.output_media_asset_id is not None
    # Job settled succeeded with the output asset id.
    job = await fx.render_jobs.get_owned(fx.project_id, job_id)
    assert job is not None
    assert job.status == RenderStatus.SUCCEEDED.value
    assert job.output_media_asset_id == result.output_media_asset_id
    assert job.progress == "100.00"
    # Output MediaAsset registered as generated video with probed metadata.
    output = await fx.media.get_owned(result.output_media_asset_id, fx.tenant, fx.owner)
    assert output is not None
    assert output.kind == "video"
    assert output.source == "generated"
    assert output.mime_type == "video/mp4"
    assert output.width == 1920 and output.height == 1080
    assert output.duration_seconds == 5.0
    assert output.source_metadata["origin"] == "render"
    # Deterministic output key.
    expected_key = f"renders/{fx.tenant}/{fx.project_id}/{job_id}.mp4"
    assert output.storage_key == expected_key
    assert expected_key in fx.storage.objects
    # A RenderJobSucceeded event was emitted (carrying the output asset id).
    succeeded = [e for e in fx.uow.outbox.events if e["event_type"] == "RenderJobSucceeded"]
    assert len(succeeded) == 1
    assert succeeded[0]["payload"]["output_media_asset_id"] == str(result.output_media_asset_id)


async def test_orders_clips_and_passes_all_inputs_to_renderer() -> None:
    fx = _Fixture()
    a = await fx.seed_video_asset("srcA", b"AAA")
    b = await fx.seed_video_asset("srcB", b"BBB")
    timeline_id = await fx.seed_timeline([a, b])
    job_id = await fx.seed_job(timeline_id)
    renderer = FakeRenderer()

    await _process(fx, renderer, job_id)

    assert len(renderer.specs) == 1
    assert len(renderer.specs[0].inputs) == 2


async def test_idempotent_rerender_recovers_existing_output_asset() -> None:
    fx = _Fixture()
    asset_id = await fx.seed_video_asset("srcA")
    timeline_id = await fx.seed_timeline([asset_id])
    renderer = FakeRenderer()

    job1 = await fx.seed_job(timeline_id)
    first = await _process(fx, renderer, job1)

    # Idempotency: the output storage key is deterministic in the job, so a
    # re-render (simulated here by resetting the job to queued and reprocessing)
    # hits the media storage-coords uniqueness → the existing asset is recovered,
    # never duplicated.
    fx.render_jobs._jobs[job1] = replace(
        fx.render_jobs._jobs[job1],
        status=RenderStatus.QUEUED.value,
        output_media_asset_id=None,
    )
    before = len(fx.media._media)
    second = await _process(fx, renderer, job1)

    assert second.status == "rendered"
    assert second.output_media_asset_id == first.output_media_asset_id
    assert len(fx.media._media) == before  # no duplicate asset registered


async def test_render_failure_marks_job_failed_and_emits_event() -> None:
    fx = _Fixture()
    asset_id = await fx.seed_video_asset("srcA")
    timeline_id = await fx.seed_timeline([asset_id])
    job_id = await fx.seed_job(timeline_id)
    renderer = FakeRenderer(error=RenderError("ffmpeg blew up"))

    result = await _process(fx, renderer, job_id)

    assert result.status == "failed"
    job = await fx.render_jobs.get_owned(fx.project_id, job_id)
    assert job is not None
    assert job.status == RenderStatus.FAILED.value
    assert job.error is not None and job.error["code"] == "render_failed"
    types = [e["event_type"] for e in fx.uow.outbox.events]
    assert "RenderJobFailed" in types


async def test_timeline_with_no_video_clips_fails() -> None:
    fx = _Fixture()
    # A timeline with a track but no clips → nothing renderable.
    timeline = await fx.timeline_repo.add(
        project_id=fx.project_id, aspect_ratio="16:9", frame_rate=30, background_color="#000"
    )
    await fx.timeline_repo.add_track(
        timeline_id=timeline.id, kind="video", z_index=0, name="V1", locked=False, muted=False
    )
    job_id = await fx.seed_job(timeline.id)
    renderer = FakeRenderer()

    result = await _process(fx, renderer, job_id)

    assert result.status == "failed"
    job = await fx.render_jobs.get_owned(fx.project_id, job_id)
    assert job is not None and job.status == RenderStatus.FAILED.value


async def test_non_queued_job_is_noop() -> None:
    fx = _Fixture()
    asset_id = await fx.seed_video_asset("srcA")
    timeline_id = await fx.seed_timeline([asset_id])
    job_id = await fx.seed_job(timeline_id, status="succeeded")
    renderer = FakeRenderer()

    result = await _process(fx, renderer, job_id)

    assert result.status == "noop"
    assert result.reason == "not_queued"
    assert renderer.specs == []


async def test_locked_job_is_skipped() -> None:
    fx = _Fixture()
    asset_id = await fx.seed_video_asset("srcA")
    timeline_id = await fx.seed_timeline([asset_id])
    job_id = await fx.seed_job(timeline_id)
    # Pre-acquire the job's lease so the worker cannot claim it.
    async with fx.uow:
        await fx.uow.locks.acquire(
            key=f"render_job:{job_id}", owner="someone-else", lease=timedelta(seconds=60)
        )
        await fx.uow.commit()
    renderer = FakeRenderer()

    result = await _process(fx, renderer, job_id)

    assert result.status == "skipped"
    assert result.reason == "locked"
    assert renderer.specs == []
