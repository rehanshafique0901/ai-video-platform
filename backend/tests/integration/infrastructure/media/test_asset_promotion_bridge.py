"""α8.8 — Asset Promotion Bridge end-to-end against live PostgreSQL.

Proves the ADR-0046 **X8** seam works across the real transaction model: the
``PromoteGenerationAssets`` use case reads a *committed* execution-plane generation via
:class:`GenerationReader` (its own session), copies the finished bytes through the
storage resolver, and registers an owner-scoped ``media_assets(source='generated')`` row
via a real :class:`SqlAlchemyUnitOfWork` — then a re-promotion is an idempotent ``noop``
that collides on the ``media_assets`` storage-coordinate uniqueness (AP3, no migration).

Like the generation e2e slice, the use case *commits* (its UoW + reader own their
sessions), so this test seeds committed rows and cleans them up on teardown rather than
leaning on the SAVEPOINT ``session`` fixture.
"""

from __future__ import annotations

import hashlib
from uuid import UUID, uuid4

import pytest
from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.use_cases.media.promote_generation_assets import PromoteGenerationAssets
from app.infrastructure.db.models.identity import Tenant, User
from app.infrastructure.db.models.projects import Project as ProjectRow
from app.infrastructure.generation.generation_reader import GenerationReader
from app.infrastructure.storage import StorageResolver
from app.infrastructure.storage.local_object_storage import LocalObjectStorage
from app.infrastructure.uow.sqlalchemy_unit_of_work import SqlAlchemyUnitOfWork

pytestmark = pytest.mark.integration

_BUCKET = "media"
_SOURCE_KEY = "generations/final/fox.mp4"
_VIDEO_BYTES = b"FAKE-MP4-BYTES-FOR-PROMOTION"


_INSERT_GENERATION_SQL = text(
    """
    INSERT INTO generations (
        id, status, prompt, title, execution_mode, execution_tier,
        chosen_provider, chosen_adapter, seed, target_platform,
        final_video_asset_id, width, height
    ) VALUES (
        CAST(:id AS uuid), 'completed', :prompt, :title, 'automatic',
        CAST(:tier AS execution_tier), :provider, :adapter, :seed, :platform,
        CAST(:final_video_asset_id AS uuid), :width, :height
    )
    """
)

_INSERT_ASSET_SQL = text(
    """
    INSERT INTO generation_assets (
        id, generation_id, shot_number, asset_kind, storage_backend, storage_bucket,
        storage_key, mime_type, size_bytes, checksum_sha256, width, height, duration_ms
    ) VALUES (
        CAST(:id AS uuid), CAST(:generation_id AS uuid), NULL, 'video',
        CAST(:backend AS storage_backend), :bucket, :key, :mime,
        :size, :checksum, :width, :height, :duration_ms
    )
    """
)


async def _seed(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[UUID, UUID, UUID, UUID, UUID]:
    """Seed a committed tenant/user/project + a completed generation with a final video."""
    tenant_id = uuid4()
    user_id = uuid4()
    project_id = uuid4()
    generation_id = uuid4()
    asset_id = uuid4()
    async with session_factory() as s:
        await s.execute(insert(Tenant).values(id=tenant_id, name="AP", slug=f"ap-{tenant_id}"))
        await s.execute(
            insert(User).values(
                id=user_id,
                tenant_id=tenant_id,
                email=f"ap-{user_id}@example.com",
                display_name="AP Owner",
            )
        )
        await s.execute(
            insert(ProjectRow).values(
                id=project_id,
                tenant_id=tenant_id,
                owner_user_id=user_id,
                name=f"P {project_id}",
                aspect_ratio="vertical",
            )
        )
        await s.execute(
            _INSERT_GENERATION_SQL,
            {
                "id": str(generation_id),
                "prompt": "a fox in the snow",
                "title": "Fox",
                "tier": "free_remote",
                "provider": "golden_provider",
                "adapter": "golden_provider.video",
                "seed": 42,
                "platform": "youtube",
                "final_video_asset_id": str(asset_id),
                "width": 720,
                "height": 1280,
            },
        )
        await s.execute(
            _INSERT_ASSET_SQL,
            {
                "id": str(asset_id),
                "generation_id": str(generation_id),
                "backend": "local",
                "bucket": _BUCKET,
                "key": _SOURCE_KEY,
                "mime": "video/mp4",
                "size": len(_VIDEO_BYTES),
                "checksum": b"\x00" * 32,
                "width": 720,
                "height": 1280,
                "duration_ms": 2500,
            },
        )
        await s.commit()
    return tenant_id, user_id, project_id, generation_id, asset_id


async def _cleanup(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: UUID,
    user_id: UUID,
    project_id: UUID,
    generation_id: UUID,
) -> None:
    async with session_factory() as s:
        await s.execute(
            text("DELETE FROM media_assets WHERE tenant_id = CAST(:t AS uuid)"),
            {"t": str(tenant_id)},
        )
        # generation_assets cascade on this delete (FK ON DELETE CASCADE).
        await s.execute(
            text("DELETE FROM generations WHERE id = CAST(:g AS uuid)"),
            {"g": str(generation_id)},
        )
        await s.execute(
            text("DELETE FROM projects WHERE id = CAST(:p AS uuid)"), {"p": str(project_id)}
        )
        await s.execute(text("DELETE FROM users WHERE id = CAST(:u AS uuid)"), {"u": str(user_id)})
        await s.execute(
            text("DELETE FROM tenants WHERE id = CAST(:t AS uuid)"), {"t": str(tenant_id)}
        )
        await s.commit()


async def test_promote_and_idempotent_replay(
    session_factory: async_sessionmaker[AsyncSession], tmp_path
) -> None:
    storage = LocalObjectStorage(root=str(tmp_path), bucket=_BUCKET)
    # The "generation runtime" already stored the finished bytes in the shared store.
    await storage.put(key=_SOURCE_KEY, data=_VIDEO_BYTES, content_type="video/mp4")

    tenant_id, user_id, project_id, generation_id, asset_id = await _seed(session_factory)
    try:
        use_case = PromoteGenerationAssets(
            uow=SqlAlchemyUnitOfWork(session_factory),
            storage=StorageResolver(adapters={"local": storage}, active_backend="local"),
            reader=GenerationReader(session_factory),
        )

        # ---- First promotion ------------------------------------------------
        result = await use_case.execute(
            generation_id=generation_id,
            project_id=project_id,
            tenant_id=tenant_id,
            owner_user_id=user_id,
        )
        assert result.status == "promoted"
        assert result.generation_asset_id == asset_id
        media_id = result.media.id

        # Persistence: the media row exists, owned + generated, with copied bytes.
        async with session_factory() as s:
            row = (
                (
                    await s.execute(
                        text(
                            "SELECT tenant_id, owner_user_id, project_id, kind, source, "
                            "storage_backend, storage_key, mime_type, size_bytes, "
                            "checksum_sha256, width, height, duration_seconds, provider, "
                            "source_metadata FROM media_assets WHERE id = CAST(:m AS uuid)"
                        ),
                        {"m": str(media_id)},
                    )
                )
                .mappings()
                .one()
            )
        assert row["tenant_id"] == tenant_id
        assert row["owner_user_id"] == user_id
        assert row["project_id"] == project_id
        assert row["kind"] == "video"
        assert row["source"] == "generated"
        assert row["storage_backend"] == "local"
        assert row["mime_type"] == "video/mp4"
        assert row["size_bytes"] == len(_VIDEO_BYTES)
        assert bytes(row["checksum_sha256"]) == hashlib.sha256(_VIDEO_BYTES).digest()
        assert row["width"] == 720 and row["height"] == 1280
        assert float(row["duration_seconds"]) == 2.5
        assert row["provider"] == "golden_provider"
        meta = row["source_metadata"]
        assert meta["origin"] == "generation_promotion"
        assert meta["generation_id"] == str(generation_id)
        assert meta["generation_asset_id"] == str(asset_id)

        # AP4 — copy, not reference: the copied object exists under the media key and
        # is byte-identical, independent of the source generation object.
        copied = await storage.get(key=row["storage_key"])
        assert copied == _VIDEO_BYTES
        assert row["storage_key"] != _SOURCE_KEY

        # ---- Idempotent replay ---------------------------------------------
        replay = await use_case.execute(
            generation_id=generation_id,
            project_id=project_id,
            tenant_id=tenant_id,
            owner_user_id=user_id,
        )
        assert replay.status == "noop"
        assert replay.media.id == media_id

        async with session_factory() as s:
            count = (
                await s.execute(
                    text("SELECT count(*) FROM media_assets WHERE tenant_id = CAST(:t AS uuid)"),
                    {"t": str(tenant_id)},
                )
            ).scalar_one()
        assert count == 1
    finally:
        await _cleanup(
            session_factory,
            tenant_id=tenant_id,
            user_id=user_id,
            project_id=project_id,
            generation_id=generation_id,
        )
