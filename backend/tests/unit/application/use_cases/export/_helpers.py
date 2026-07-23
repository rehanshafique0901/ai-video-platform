"""Shared scaffolding for the α8.5a Export use-case unit tests.

Wires a UoW with one owned live project, a **succeeded** render job, and its master
``MediaAsset`` (stored in a fake object storage). Export ownership is derived through the
render job → project, so the fixture shares one ``FakeRenderJobRepository`` with the
``FakeExportJobRepository`` (the UoW does this automatically).
"""

from __future__ import annotations

from uuid import UUID, uuid4

from app.application.interfaces.object_storage import (
    IObjectStorage,
    ObjectStorageError,
    StoredObject,
)
from tests.unit.application.use_cases.auth._fakes import (
    FakeMediaRepository,
    FakeProjectRepository,
    FakeRenderJobRepository,
    FakeUnitOfWork,
)
from tests.unit.application.use_cases.media._helpers import make_project

_MASTER_BACKEND = "local"
_MASTER_BUCKET = "generated"


class FakeObjectStorage(IObjectStorage):
    """In-memory object storage (``local``/``generated``) with put/get bookkeeping."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.put_keys: list[str] = []

    @property
    def backend(self) -> str:
        return _MASTER_BACKEND

    @property
    def bucket(self) -> str:
        return _MASTER_BUCKET

    async def put(self, *, key: str, data: bytes, content_type: str | None) -> StoredObject:
        self.objects[key] = data
        self.put_keys.append(key)
        return StoredObject(backend=_MASTER_BACKEND, bucket=_MASTER_BUCKET, key=key)

    async def get(self, *, key: str) -> bytes:
        try:
            return self.objects[key]
        except KeyError as exc:  # mirror LocalObjectStorage: missing object → ObjectStorageError
            raise ObjectStorageError(f"failed to read object {key!r}: not found") from exc

    async def exists(self, *, key: str) -> bool:
        return key in self.objects

    async def delete(self, *, key: str) -> None:
        self.objects.pop(key, None)


class ExportFixture:
    """A project + one succeeded render job + its master asset, ready to export."""

    def __init__(self) -> None:
        self.owner = uuid4()
        self.tenant = uuid4()
        project = make_project(tenant_id=self.tenant, owner_user_id=self.owner)
        self.project_id = project.id
        self.projects = FakeProjectRepository(_rows={project.id: project})
        self.media = FakeMediaRepository()
        self.render_jobs = FakeRenderJobRepository()
        self.storage = FakeObjectStorage()
        self.uow = FakeUnitOfWork(
            projects=self.projects,
            media=self.media,
            render_jobs=self.render_jobs,
        )
        # Export jobs are render-derived — reach the same fake the UoW wired.
        self.exports = self.uow._fake_export_jobs
        self._master_seq = 0

    async def seed_master(
        self,
        *,
        width: int | None = 1920,
        height: int | None = 1080,
        key: str | None = None,
        data: bytes = b"MASTER-BYTES",
        backend: str = _MASTER_BACKEND,
        bucket: str = _MASTER_BUCKET,
    ) -> UUID:
        """Register a master render ``MediaAsset`` + stash its bytes in storage."""
        if key is None:
            self._master_seq += 1
            key = f"renders/master-{self._master_seq}.mp4"
        self.storage.objects[key] = data
        asset = await self.media.add(
            tenant_id=self.tenant,
            owner_user_id=self.owner,
            kind="video",
            source="generated",
            storage_backend=backend,
            storage_bucket=bucket,
            storage_key=key,
            mime_type="video/mp4",
            size_bytes=len(data),
            checksum_sha256=b"\x00" * 32,
            project_id=self.project_id,
            scene_id=None,
            prompt_id=None,
            model_id=None,
            provider=None,
            width=width,
            height=height,
            duration_seconds=5.0,
            source_metadata={"origin": "render"},
        )
        return asset.id

    async def seed_succeeded_render(self, *, master_id: UUID) -> UUID:
        """Create a render job and drive it queued → running → succeeded(master_id)."""
        job = await self.render_jobs.add(
            project_id=self.project_id,
            timeline_id=uuid4(),
            pipeline="ffmpeg",
            pipeline_version="0.0.0",
            queue="normal",
            priority=0,
            status="queued",
            idempotency_key=None,
        )
        await self.render_jobs.mark_running(job.id)
        await self.render_jobs.mark_succeeded(job.id, output_media_asset_id=master_id)
        return job.id

    async def seed_ready(
        self, *, width: int | None = 1920, height: int | None = 1080
    ) -> tuple[UUID, UUID]:
        """Convenience: seed a master + succeeded render; return ``(render_job_id, master_id)``."""
        master_id = await self.seed_master(width=width, height=height)
        render_job_id = await self.seed_succeeded_render(master_id=master_id)
        return render_job_id, master_id

    async def seed_delivery_asset(
        self,
        *,
        format: str = "mp4",
        kind: str = "video",
        mime_type: str = "video/mp4",
        data: bytes = b"DELIVERY-BYTES",
        key: str | None = None,
    ) -> UUID:
        """Register a delivery ``MediaAsset`` (origin=export) + stash its bytes in storage."""
        if key is None:
            self._master_seq += 1
            key = f"exports/delivery-{self._master_seq}.{format}"
        self.storage.objects[key] = data
        asset = await self.media.add(
            tenant_id=self.tenant,
            owner_user_id=self.owner,
            kind=kind,
            source="generated",
            storage_backend=_MASTER_BACKEND,
            storage_bucket=_MASTER_BUCKET,
            storage_key=key,
            mime_type=mime_type,
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
            source_metadata={"origin": "export"},
        )
        return asset.id

    async def seed_succeeded_export(
        self,
        *,
        render_job_id: UUID,
        delivery_id: UUID,
        format: str = "mp4",
        quality: str = "hd_1080p",
        orientation: str = "horizontal",
        file_size_bytes: int = 14,
    ) -> UUID:
        """Create an export job and drive it queued → running → succeeded(delivery_id)."""
        job = await self.exports.add(
            render_job_id=render_job_id,
            requested_by_user_id=self.owner,
            format=format,
            quality=quality,
            orientation=orientation,
            status="queued",
        )
        await self.exports.mark_running(job.id)
        await self.exports.mark_succeeded(
            job.id, output_media_asset_id=delivery_id, file_size_bytes=file_size_bytes
        )
        return job.id
