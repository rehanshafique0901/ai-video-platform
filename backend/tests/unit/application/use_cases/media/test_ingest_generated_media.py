"""Unit tests for ``IngestGeneratedMedia`` (Slice α8.4a).

Drives the use case against in-memory fakes: a succeeded run whose steps carry the
opaque provider-output envelopes the runner persists, plus fake object storage +
downloader. Asserts the observational contract (W8.4.2 — reads runs, only creates
``MediaAsset`` + storage objects) and deterministic-key idempotency.
"""

from __future__ import annotations

import hashlib
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.application.interfaces.media_downloader import DownloadedMedia, IMediaDownloader
from app.application.interfaces.object_storage import IObjectStorage, StoredObject
from app.application.use_cases.media.ingest_generated_media import IngestGeneratedMedia
from app.domain.workflow.workflow_run_status import WorkflowRunStatus
from app.infrastructure.storage import StorageResolver
from tests.unit.application.use_cases.auth._fakes import (
    FakeMediaRepository,
    FakeProjectRepository,
    FakeUnitOfWork,
    FakeWorkflowRunRepository,
)
from tests.unit.application.use_cases.media._helpers import make_project

pytestmark = pytest.mark.unit


class FakeDownloader(IMediaDownloader):
    def __init__(self, mapping: dict[str, tuple[bytes, str | None]]) -> None:
        self._mapping = mapping
        self.calls: list[str] = []

    async def download(self, url: str) -> DownloadedMedia:
        self.calls.append(url)
        content, mime = self._mapping[url]
        return DownloadedMedia(content=content, mime_type=mime, size_bytes=len(content))


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


def _view(provider: str, request_id: str, **output: Any) -> dict[str, Any]:
    return {
        "provider": provider,
        "request_id": request_id,
        "status": "succeeded",
        "output": output,
    }


class _Fixture:
    def __init__(self) -> None:
        self.owner = uuid4()
        self.tenant = uuid4()
        project = make_project(tenant_id=self.tenant, owner_user_id=self.owner)
        self.project_id = project.id
        self.projects = FakeProjectRepository(_rows={project.id: project})
        self.runs = FakeWorkflowRunRepository()
        self.media = FakeMediaRepository()
        self.uow = FakeUnitOfWork(projects=self.projects, workflow_runs=self.runs, media=self.media)

    async def seed_run(
        self,
        *,
        status: str = WorkflowRunStatus.SUCCEEDED.value,
        step_outputs: list[dict[str, Any]] | None = None,
        project_id: UUID | None = None,
    ) -> UUID:
        run = await self.runs.add(
            project_id=project_id if project_id is not None else self.project_id,
            workflow_key="gen",
            workflow_version="1",
            status=WorkflowRunStatus.RUNNING.value,
            input_snapshot={},
            triggered_by_user_id=self.owner,
            idempotency_key=None,
        )
        outputs = step_outputs or []
        specs = [(i, f"step{i}") for i in range(len(outputs))]
        if specs:
            await self.runs.seed_steps(run.id, specs)
        for i, out in enumerate(outputs):
            await self.runs.mark_step_running(run.id, i)
            await self.runs.mark_step_succeeded(run.id, i, out)
        if status == WorkflowRunStatus.SUCCEEDED.value:
            await self.runs.mark_run_succeeded(run.id, {})
        return run.id


async def test_happy_path_registers_one_image_asset() -> None:
    fx = _Fixture()
    url = "https://cdn.example/out.png"
    run_id = await fx.seed_run(
        step_outputs=[{"provider_outputs": [_view("openai-image", "rid-1", image_ref=url)]}]
    )
    downloader = FakeDownloader({url: (b"PNGDATA", "image/png")})
    storage = FakeObjectStorage()
    uc = IngestGeneratedMedia(
        uow=fx.uow, storage=StorageResolver.single(storage), downloader=downloader
    )

    result = await uc.execute(project_id=fx.project_id, workflow_run_id=run_id)

    assert result.status == "ingested"
    assert len(result.registered_media_ids) == 1
    assert result.skipped_existing == 0
    assert downloader.calls == [url]

    assets = list(fx.media._media.values())
    assert len(assets) == 1
    asset = assets[0]
    assert asset.source == "generated"
    assert asset.kind == "image"
    assert asset.provider == "openai-image"
    assert asset.project_id == fx.project_id
    assert asset.tenant_id == fx.tenant
    assert asset.owner_user_id == fx.owner
    assert asset.storage_backend == "local"
    assert asset.storage_bucket == "generated"
    assert asset.checksum_sha256 == hashlib.sha256(b"PNGDATA").digest()
    assert asset.source_metadata["source_url"] == url
    assert asset.source_metadata["workflow_run_id"] == str(run_id)

    # Deterministic, run/step-scoped key ending with the mime-derived extension.
    assert len(storage.put_keys) == 1
    key = storage.put_keys[0]
    assert key.startswith(f"{fx.tenant}/{fx.project_id}/{run_id}/0/")
    assert key.endswith(".png")


async def test_idempotent_on_redelivery() -> None:
    fx = _Fixture()
    url = "https://cdn.example/out.png"
    run_id = await fx.seed_run(
        step_outputs=[{"provider_outputs": [_view("openai-image", "rid-1", image_ref=url)]}]
    )
    storage = FakeObjectStorage()
    downloader = FakeDownloader({url: (b"PNGDATA", "image/png")})
    uc = IngestGeneratedMedia(
        uow=fx.uow, storage=StorageResolver.single(storage), downloader=downloader
    )

    first = await uc.execute(project_id=fx.project_id, workflow_run_id=run_id)
    second = await uc.execute(project_id=fx.project_id, workflow_run_id=run_id)

    assert first.status == "ingested" and len(first.registered_media_ids) == 1
    assert second.status == "noop"
    assert second.skipped_existing == 1
    # The deterministic key collided → exactly one asset persists.
    assert len(fx.media._media) == 1


async def test_multiple_refs_across_steps() -> None:
    fx = _Fixture()
    img = "https://cdn.example/a.png"
    vid = "https://cdn.example/b.mp4"
    run_id = await fx.seed_run(
        step_outputs=[
            {"provider_outputs": [_view("openai-image", "rid-0", image_ref=img)]},
            {"provider_outputs": [_view("fal-video", "rid-1", video_ref=vid)]},
        ]
    )
    storage = FakeObjectStorage()
    downloader = FakeDownloader({img: (b"IMG", "image/png"), vid: (b"VID", "video/mp4")})
    uc = IngestGeneratedMedia(
        uow=fx.uow, storage=StorageResolver.single(storage), downloader=downloader
    )

    result = await uc.execute(project_id=fx.project_id, workflow_run_id=run_id)

    assert result.status == "ingested"
    assert len(result.registered_media_ids) == 2
    assert sorted(downloader.calls) == sorted([img, vid])
    kinds = {a.kind for a in fx.media._media.values()}
    assert kinds == {"image", "video"}
    assert any(k.endswith(".mp4") for k in storage.put_keys)


async def test_run_without_media_is_noop() -> None:
    fx = _Fixture()
    run_id = await fx.seed_run(
        step_outputs=[{"provider_outputs": [_view("llm", "rid-1", text="hello")]}]
    )
    storage = FakeObjectStorage()
    downloader = FakeDownloader({})
    uc = IngestGeneratedMedia(
        uow=fx.uow, storage=StorageResolver.single(storage), downloader=downloader
    )

    result = await uc.execute(project_id=fx.project_id, workflow_run_id=run_id)

    assert result.status == "noop"
    assert downloader.calls == []
    assert len(fx.media._media) == 0


async def test_non_succeeded_run_is_noop() -> None:
    fx = _Fixture()
    url = "https://cdn.example/out.png"
    run_id = await fx.seed_run(
        status=WorkflowRunStatus.RUNNING.value,
        step_outputs=[{"provider_outputs": [_view("openai-image", "rid-1", image_ref=url)]}],
    )
    downloader = FakeDownloader({url: (b"PNGDATA", "image/png")})
    uc = IngestGeneratedMedia(
        uow=fx.uow, storage=StorageResolver.single(FakeObjectStorage()), downloader=downloader
    )

    result = await uc.execute(project_id=fx.project_id, workflow_run_id=run_id)

    assert result.status == "noop"
    assert downloader.calls == []


async def test_missing_project_ownership_is_noop() -> None:
    fx = _Fixture()
    orphan_project = uuid4()  # not registered in the projects fake
    url = "https://cdn.example/out.png"
    run_id = await fx.seed_run(
        project_id=orphan_project,
        step_outputs=[{"provider_outputs": [_view("openai-image", "rid-1", image_ref=url)]}],
    )
    downloader = FakeDownloader({url: (b"PNGDATA", "image/png")})
    uc = IngestGeneratedMedia(
        uow=fx.uow, storage=StorageResolver.single(FakeObjectStorage()), downloader=downloader
    )

    result = await uc.execute(project_id=orphan_project, workflow_run_id=run_id)

    assert result.status == "noop"
    assert downloader.calls == []
    assert len(fx.media._media) == 0
