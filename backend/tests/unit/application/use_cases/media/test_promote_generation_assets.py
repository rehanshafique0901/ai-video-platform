"""Unit tests for ``PromoteGenerationAssets`` (α8.8 — Asset Promotion Bridge).

Drives the use case against in-memory fakes: a fake read-only ``IGenerationReader``, a
single fake object store (source + destination share the local backend, as in
production), and the shared media/project UoW fakes. Asserts the X8 promotion contract —
project-scoped ownership authorization, copy (not reference) with a recomputed
checksum, provenance carried onto ``media_assets(source='generated')``,
deterministic-key idempotency (a replay is a ``noop``), and the 404/422 guards.
"""

from __future__ import annotations

import hashlib
from uuid import UUID, uuid4

import pytest

from app.application.interfaces.generation_reader import (
    IGenerationReader,
    PromotableGenerationVideo,
)
from app.application.interfaces.object_storage import IObjectStorage, StoredObject
from app.application.use_cases.media.promote_generation_assets import PromoteGenerationAssets
from app.core.errors import NotFoundError, ValidationFailedError
from app.infrastructure.storage import StorageResolver
from tests.unit.application.use_cases.media._helpers import build_env

pytestmark = pytest.mark.unit

_SOURCE_KEY = "generations/gen-1/final.mp4"
_VIDEO_BYTES = b"FAKE-MP4-BYTES"


class _FakeGenerationReader(IGenerationReader):
    def __init__(self, video: PromotableGenerationVideo | None) -> None:
        self._video = video
        self.calls: list[UUID] = []

    async def load_final_video(self, generation_id: UUID) -> PromotableGenerationVideo | None:
        self.calls.append(generation_id)
        return self._video


class _FakeObjectStorage(IObjectStorage):
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.put_keys: list[str] = []

    @property
    def backend(self) -> str:
        return "local"

    @property
    def bucket(self) -> str:
        return "media"

    async def put(self, *, key: str, data: bytes, content_type: str | None) -> StoredObject:
        self.objects[key] = data
        self.put_keys.append(key)
        return StoredObject(backend="local", bucket="media", key=key)

    async def get(self, *, key: str) -> bytes:
        return self.objects[key]

    async def exists(self, *, key: str) -> bool:
        return key in self.objects

    async def delete(self, *, key: str) -> None:
        self.objects.pop(key, None)


def _video(
    generation_id: UUID,
    *,
    status: str = "completed",
    final_video_asset_id: UUID | None = None,
) -> PromotableGenerationVideo:
    return PromotableGenerationVideo(
        generation_id=generation_id,
        status=status,
        final_video_asset_id=final_video_asset_id if final_video_asset_id is not None else uuid4(),
        chosen_provider="golden_provider",
        chosen_adapter="golden_provider.video",
        seed=42,
        title="A fox in the snow",
        target_platform="youtube",
        storage_backend="local",
        storage_bucket="media",
        storage_key=_SOURCE_KEY,
        mime_type="video/mp4",
        size_bytes=len(_VIDEO_BYTES),
        checksum_sha256=b"\x00" * 32,
        width=720,
        height=1280,
        duration_ms=2500,
    )


def _wire(video: PromotableGenerationVideo | None) -> tuple:
    env = build_env()
    storage = _FakeObjectStorage()
    storage.objects[_SOURCE_KEY] = _VIDEO_BYTES
    reader = _FakeGenerationReader(video)
    uc = PromoteGenerationAssets(
        uow=env.uow, storage=StorageResolver.single(storage), reader=reader
    )
    return env, storage, reader, uc


async def test_happy_path_promotes_final_video() -> None:
    gen_id = uuid4()
    asset_id = uuid4()
    env, storage, reader, uc = _wire(_video(gen_id, final_video_asset_id=asset_id))

    result = await uc.execute(
        generation_id=gen_id,
        project_id=env.project_id,
        tenant_id=env.tenant_id,
        owner_user_id=env.owner_user_id,
    )

    assert result.status == "promoted"
    assert result.generation_id == gen_id
    assert result.generation_asset_id == asset_id
    assert reader.calls == [gen_id]

    assets = list(env.media._media.values())
    assert len(assets) == 1
    asset = assets[0]
    assert asset.source == "generated"
    assert asset.kind == "video"
    assert asset.project_id == env.project_id
    assert asset.tenant_id == env.tenant_id
    assert asset.owner_user_id == env.owner_user_id
    assert asset.provider == "golden_provider"
    assert asset.width == 720 and asset.height == 1280
    assert asset.duration_seconds == 2.5
    # Copy, not reference: the checksum is recomputed from the copied bytes.
    assert asset.checksum_sha256 == hashlib.sha256(_VIDEO_BYTES).digest()
    assert asset.size_bytes == len(_VIDEO_BYTES)
    # Provenance carried in source_metadata.
    assert asset.source_metadata["origin"] == "generation_promotion"
    assert asset.source_metadata["generation_id"] == str(gen_id)
    assert asset.source_metadata["generation_asset_id"] == str(asset_id)
    assert asset.source_metadata["chosen_adapter"] == "golden_provider.video"
    assert asset.source_metadata["seed"] == 42

    # Deterministic media key (owner/project/generation/asset scoped), copied bytes.
    assert len(storage.put_keys) == 1
    key = storage.put_keys[0]
    assert key == (f"{env.tenant_id}/{env.project_id}/generation/{gen_id}/{asset_id}.mp4")
    assert storage.objects[key] == _VIDEO_BYTES


async def test_idempotent_replay_is_noop() -> None:
    gen_id = uuid4()
    env, storage, reader, uc = _wire(_video(gen_id, final_video_asset_id=uuid4()))

    first = await uc.execute(
        generation_id=gen_id,
        project_id=env.project_id,
        tenant_id=env.tenant_id,
        owner_user_id=env.owner_user_id,
    )
    second = await uc.execute(
        generation_id=gen_id,
        project_id=env.project_id,
        tenant_id=env.tenant_id,
        owner_user_id=env.owner_user_id,
    )

    assert first.status == "promoted"
    assert second.status == "noop"
    # The deterministic key collided → exactly one media asset persists.
    assert len(env.media._media) == 1
    # Both calls return the same asset identity.
    assert first.media.id == second.media.id


async def test_unknown_generation_is_404() -> None:
    gen_id = uuid4()
    env, _storage, _reader, uc = _wire(None)

    with pytest.raises(NotFoundError):
        await uc.execute(
            generation_id=gen_id,
            project_id=env.project_id,
            tenant_id=env.tenant_id,
            owner_user_id=env.owner_user_id,
        )
    assert len(env.media._media) == 0


async def test_incomplete_generation_is_422() -> None:
    gen_id = uuid4()
    env, _storage, _reader, uc = _wire(_video(gen_id, status="generating"))

    with pytest.raises(ValidationFailedError):
        await uc.execute(
            generation_id=gen_id,
            project_id=env.project_id,
            tenant_id=env.tenant_id,
            owner_user_id=env.owner_user_id,
        )
    assert len(env.media._media) == 0


async def test_no_final_video_is_422() -> None:
    gen_id = uuid4()
    video = PromotableGenerationVideo(
        generation_id=gen_id,
        status="completed",
        final_video_asset_id=None,
        chosen_provider=None,
        chosen_adapter=None,
        seed=None,
        title=None,
        target_platform=None,
        storage_backend=None,
        storage_bucket=None,
        storage_key=None,
        mime_type=None,
        size_bytes=None,
        checksum_sha256=None,
        width=None,
        height=None,
        duration_ms=None,
    )
    env, _storage, _reader, uc = _wire(video)

    with pytest.raises(ValidationFailedError):
        await uc.execute(
            generation_id=gen_id,
            project_id=env.project_id,
            tenant_id=env.tenant_id,
            owner_user_id=env.owner_user_id,
        )
    assert len(env.media._media) == 0


async def test_foreign_project_is_422() -> None:
    gen_id = uuid4()
    env, _storage, reader, uc = _wire(_video(gen_id, final_video_asset_id=uuid4()))

    with pytest.raises(ValidationFailedError):
        await uc.execute(
            generation_id=gen_id,
            project_id=uuid4(),  # not owned by the caller
            tenant_id=env.tenant_id,
            owner_user_id=env.owner_user_id,
        )
    # Ownership is checked before the generation is read.
    assert reader.calls == []
    assert len(env.media._media) == 0
