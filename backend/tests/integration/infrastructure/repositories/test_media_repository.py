"""Integration tests for ``MediaRepository`` (Slice α6.2).

Runs against the live database; each test is wrapped in a SAVEPOINT that rolls
back on teardown, so no rows persist. Covers owner-scoped register (owner-level +
fully-linked), newest-first listing with soft-delete exclusion + kind/source/
project/scene filters, owner isolation on read, the no-OCC update (``updated_at``
advances, no ``version`` column exists), soft delete + idempotency,
``model_is_linkable`` (exists + not-retired), the storage-coordinate uniqueness
(``409``), and the load-bearing **F5** schema-interaction test: a media asset's
``project_id`` / ``scene_id`` / ``prompt_id`` links SURVIVE a parent
*soft-delete* (the ``ON DELETE SET NULL`` FKs fire only on a hard parent delete).

Coverage map (α6.2 pre-flight §5.2):

* R1 — ``add`` owner-level (all links NULL) + fully-linked.
* R2 — ``list_owned`` newest-first, excludes soft-deleted.
* R3 — ``list_owned`` kind + source + project + scene filters (combined AND).
* R4 — ``get_owned`` owner isolation → ``None``.
* R5 — ``update_owned`` real change: ``updated_at`` advances (no ``version``).
* R6 — ``update_owned`` foreign owner / soft-deleted → ``None``.
* R7 — ``soft_delete_owned`` happy / already-deleted → ``False`` / wrong owner.
* R8 — ``model_is_linkable``: available/deprecated True, retired/unknown False.
* R9 — duplicate ``(backend, bucket, key)`` → ``ConflictError`` (409).
* R10 — **F5** ``project_id`` / ``scene_id`` / ``prompt_id`` survive parent soft-delete.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError
from app.domain.media.media_asset import MediaAsset as MediaEntity
from app.infrastructure.db.models.ai_models import AIModel as AIModelRow
from app.infrastructure.db.models.identity import Tenant, User
from app.infrastructure.db.models.media import MediaAsset as MediaRow
from app.infrastructure.db.models.projects import Project as ProjectRow
from app.infrastructure.db.models.scenes import (
    Prompt as PromptRow,
    Scene as SceneRow,
    Storyboard as StoryboardRow,
)
from app.infrastructure.repositories.media_repository import MediaRepository

_CHECKSUM = bytes.fromhex("ab" * 32)


async def _seed_owner(session: AsyncSession) -> tuple[UUID, UUID]:
    tenant_id = uuid4()
    await session.execute(
        insert(Tenant).values(id=tenant_id, name="MR Test", slug=f"mr-{tenant_id}")
    )
    user_id = uuid4()
    await session.execute(
        insert(User).values(
            id=user_id,
            tenant_id=tenant_id,
            email=f"mr-{user_id}@example.com",
            display_name="MR Owner",
        )
    )
    return tenant_id, user_id


async def _insert_project(session: AsyncSession, *, tenant_id: UUID, owner_user_id: UUID) -> UUID:
    project_id = uuid4()
    await session.execute(
        insert(ProjectRow).values(
            id=project_id,
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            name=f"P {project_id}",
            aspect_ratio="horizontal",
        )
    )
    return project_id


async def _insert_scene(session: AsyncSession, *, project_id: UUID) -> UUID:
    storyboard_id = uuid4()
    await session.execute(
        insert(StoryboardRow).values(id=storyboard_id, project_id=project_id, generated_by="system")
    )
    scene_id = uuid4()
    await session.execute(
        insert(SceneRow).values(
            id=scene_id,
            storyboard_id=storyboard_id,
            scene_number=1000,
            title="S1",
            duration_seconds=1.0,
        )
    )
    await session.flush()
    return scene_id


async def _insert_prompt(session: AsyncSession, *, project_id: UUID) -> UUID:
    prompt_id = uuid4()
    await session.execute(
        insert(PromptRow).values(
            id=prompt_id,
            project_id=project_id,
            kind="image",
            text_content="x",
        )
    )
    await session.flush()
    return prompt_id


async def _insert_model(session: AsyncSession, *, status: str = "available") -> UUID:
    model_id = uuid4()
    await session.execute(
        insert(AIModelRow).values(
            id=model_id,
            model_key=f"mk-{model_id}",
            provider="test",
            vendor_model_id="v1",
            kind="image",
            status=status,
        )
    )
    await session.flush()
    return model_id


async def _add(
    repo: MediaRepository,
    *,
    tenant_id: UUID,
    owner_user_id: UUID,
    kind: str = "image",
    source: str = "uploaded",
    storage_backend: str = "s3",
    storage_bucket: str = "assets",
    storage_key: str | None = None,
    mime_type: str = "image/png",
    size_bytes: int = 2048,
    checksum_sha256: bytes = _CHECKSUM,
    project_id: UUID | None = None,
    scene_id: UUID | None = None,
    prompt_id: UUID | None = None,
    model_id: UUID | None = None,
    provider: str | None = None,
    width: int | None = None,
    height: int | None = None,
    duration_seconds: float | None = None,
    source_metadata: dict[str, object] | None = None,
) -> MediaEntity:
    """Thin ``repo.add`` wrapper with defaults, to keep call sites short."""
    return await repo.add(
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        kind=kind,
        source=source,
        storage_backend=storage_backend,
        storage_bucket=storage_bucket,
        storage_key=storage_key or f"uploads/{uuid4()}.png",
        mime_type=mime_type,
        size_bytes=size_bytes,
        checksum_sha256=checksum_sha256,
        project_id=project_id,
        scene_id=scene_id,
        prompt_id=prompt_id,
        model_id=model_id,
        provider=provider,
        width=width,
        height=height,
        duration_seconds=duration_seconds,
        source_metadata=source_metadata or {},
    )


# ---- R1 — add owner-level + fully-linked ------------------------------


@pytest.mark.integration
async def test_r1_add_owner_level_and_fully_linked(session: AsyncSession) -> None:
    tenant_id, owner_id = await _seed_owner(session)
    project_id = await _insert_project(session, tenant_id=tenant_id, owner_user_id=owner_id)
    scene_id = await _insert_scene(session, project_id=project_id)
    prompt_id = await _insert_prompt(session, project_id=project_id)
    model_id = await _insert_model(session)
    repo = MediaRepository(session)

    plain = await _add(repo, tenant_id=tenant_id, owner_user_id=owner_id, source="stock")
    assert plain.project_id is None
    assert plain.scene_id is None
    assert plain.prompt_id is None
    assert plain.model_id is None
    assert plain.source == "stock"

    linked = await _add(
        repo,
        tenant_id=tenant_id,
        owner_user_id=owner_id,
        kind="video",
        project_id=project_id,
        scene_id=scene_id,
        prompt_id=prompt_id,
        model_id=model_id,
        width=1920,
        height=1080,
        duration_seconds=12.5,
        source_metadata={"origin": "unsplash"},
    )
    assert linked.project_id == project_id
    assert linked.scene_id == scene_id
    assert linked.prompt_id == prompt_id
    assert linked.model_id == model_id
    assert linked.width == 1920
    assert linked.duration_seconds == 12.5
    assert linked.source_metadata == {"origin": "unsplash"}
    assert linked.checksum_sha256 == _CHECKSUM


# ---- R2 — list newest-first, excludes soft-deleted --------------------


@pytest.mark.integration
async def test_r2_list_newest_first_excludes_soft_deleted(session: AsyncSession) -> None:
    tenant_id, owner_id = await _seed_owner(session)
    repo = MediaRepository(session)

    m1 = await _add(repo, tenant_id=tenant_id, owner_user_id=owner_id)
    m2 = await _add(repo, tenant_id=tenant_id, owner_user_id=owner_id)
    await repo.soft_delete_owned(m1.id, tenant_id, owner_id)

    listed = await repo.list_owned(tenant_id, owner_id)
    assert [m.id for m in listed] == [m2.id]


# ---- R3 — list filters -------------------------------------------------


@pytest.mark.integration
async def test_r3_list_filters(session: AsyncSession) -> None:
    tenant_id, owner_id = await _seed_owner(session)
    project_id = await _insert_project(session, tenant_id=tenant_id, owner_user_id=owner_id)
    scene_id = await _insert_scene(session, project_id=project_id)
    repo = MediaRepository(session)

    await _add(repo, tenant_id=tenant_id, owner_user_id=owner_id, kind="image", source="uploaded")
    vid = await _add(
        repo, tenant_id=tenant_id, owner_user_id=owner_id, kind="video", source="stock"
    )
    scoped = await _add(
        repo,
        tenant_id=tenant_id,
        owner_user_id=owner_id,
        kind="video",
        source="stock",
        project_id=project_id,
        scene_id=scene_id,
    )

    # ``created_at`` defaults to ``now()``, which Postgres holds CONSTANT for the
    # whole transaction — so ``vid`` and ``scoped`` tie and the ``created_at DESC,
    # id DESC`` order would fall back to a nondeterministic ``uuid4`` tiebreak.
    # Stamp ``scoped`` strictly newer so the ordered "stock" filter below is
    # deterministic (media assets carry no ``version`` column, so no bump trigger).
    await session.execute(
        update(MediaRow).where(MediaRow.id == vid.id).values(created_at=datetime.now(UTC))
    )
    await session.execute(
        update(MediaRow)
        .where(MediaRow.id == scoped.id)
        .values(created_at=datetime.now(UTC) + timedelta(seconds=1))
    )
    await session.flush()

    assert {m.id for m in await repo.list_owned(tenant_id, owner_id, kind="video")} == {
        vid.id,
        scoped.id,
    }
    assert [m.id for m in await repo.list_owned(tenant_id, owner_id, source="stock")] == [
        scoped.id,
        vid.id,
    ]
    assert [m.id for m in await repo.list_owned(tenant_id, owner_id, project_id=project_id)] == [
        scoped.id
    ]
    # combined AND
    assert [
        m.id for m in await repo.list_owned(tenant_id, owner_id, kind="video", scene_id=scene_id)
    ] == [scoped.id]


# ---- R4 — get owner isolation -----------------------------------------


@pytest.mark.integration
async def test_r4_get_owned_owner_isolation(session: AsyncSession) -> None:
    tenant_id, owner_id = await _seed_owner(session)
    _other_tenant, other_owner = await _seed_owner(session)
    repo = MediaRepository(session)

    media = await _add(repo, tenant_id=tenant_id, owner_user_id=owner_id)
    assert await repo.get_owned(media.id, tenant_id, owner_id) is not None
    # Same media id, wrong owner → invisible.
    assert await repo.get_owned(media.id, tenant_id, other_owner) is None


# ---- R5 — update real change (no version column) ----------------------


@pytest.mark.integration
async def test_r5_update_owned_changes_metadata(session: AsyncSession) -> None:
    tenant_id, owner_id = await _seed_owner(session)
    repo = MediaRepository(session)

    media = await _add(repo, tenant_id=tenant_id, owner_user_id=owner_id)
    updated = await repo.update_owned(
        media.id, tenant_id, owner_id, {"source_metadata": {"k": "v"}}
    )
    assert updated is not None
    assert updated.source_metadata == {"k": "v"}
    # No version column on media (ADR-0037) — the entity carries none.
    assert not hasattr(updated, "version")


# ---- R6 — update foreign / soft-deleted → None ------------------------


@pytest.mark.integration
async def test_r6_update_owned_foreign_or_deleted_returns_none(session: AsyncSession) -> None:
    tenant_id, owner_id = await _seed_owner(session)
    _other_tenant, other_owner = await _seed_owner(session)
    repo = MediaRepository(session)

    media = await _add(repo, tenant_id=tenant_id, owner_user_id=owner_id)
    # Foreign owner scope → no match.
    assert await repo.update_owned(media.id, tenant_id, other_owner, {"provider": "x"}) is None
    # Soft-deleted → no match.
    await repo.soft_delete_owned(media.id, tenant_id, owner_id)
    assert await repo.update_owned(media.id, tenant_id, owner_id, {"provider": "x"}) is None


# ---- R7 — soft delete happy / idempotent / wrong owner ----------------


@pytest.mark.integration
async def test_r7_soft_delete_owned(session: AsyncSession) -> None:
    tenant_id, owner_id = await _seed_owner(session)
    _other_tenant, other_owner = await _seed_owner(session)
    repo = MediaRepository(session)

    media = await _add(repo, tenant_id=tenant_id, owner_user_id=owner_id)
    # Wrong owner → False, no delete.
    assert await repo.soft_delete_owned(media.id, tenant_id, other_owner) is False
    # Happy → True.
    assert await repo.soft_delete_owned(media.id, tenant_id, owner_id) is True
    # Idempotent: already deleted → False.
    assert await repo.soft_delete_owned(media.id, tenant_id, owner_id) is False


# ---- R8 — model_is_linkable -------------------------------------------


@pytest.mark.integration
async def test_r8_model_is_linkable(session: AsyncSession) -> None:
    repo = MediaRepository(session)
    available = await _insert_model(session, status="available")
    deprecated = await _insert_model(session, status="deprecated")
    retired = await _insert_model(session, status="retired")

    assert await repo.model_is_linkable(available) is True
    assert await repo.model_is_linkable(deprecated) is True
    assert await repo.model_is_linkable(retired) is False
    assert await repo.model_is_linkable(uuid4()) is False


# ---- R9 — duplicate storage coordinates → ConflictError ---------------


@pytest.mark.integration
async def test_r9_duplicate_storage_coords_raises_conflict(session: AsyncSession) -> None:
    tenant_id, owner_id = await _seed_owner(session)
    repo = MediaRepository(session)

    await _add(
        repo,
        tenant_id=tenant_id,
        owner_user_id=owner_id,
        storage_backend="s3",
        storage_bucket="assets",
        storage_key="dup/key.png",
    )
    with pytest.raises(ConflictError):
        await _add(
            repo,
            tenant_id=tenant_id,
            owner_user_id=owner_id,
            storage_backend="s3",
            storage_bucket="assets",
            storage_key="dup/key.png",  # same coords
        )


# ---- R10 — F5: links survive parent soft-delete -----------------------


@pytest.mark.integration
async def test_r10_links_survive_parent_soft_delete(session: AsyncSession) -> None:
    tenant_id, owner_id = await _seed_owner(session)
    project_id = await _insert_project(session, tenant_id=tenant_id, owner_user_id=owner_id)
    scene_id = await _insert_scene(session, project_id=project_id)
    prompt_id = await _insert_prompt(session, project_id=project_id)
    repo = MediaRepository(session)

    media = await _add(
        repo,
        tenant_id=tenant_id,
        owner_user_id=owner_id,
        project_id=project_id,
        scene_id=scene_id,
        prompt_id=prompt_id,
    )

    # Soft-delete all three parents (set deleted_at, NOT a hard DELETE). The
    # ON DELETE SET NULL FKs fire only on a hard parent delete, so the links
    # must SURVIVE (F5 — load-bearing schema interaction).
    await session.execute(
        update(ProjectRow).where(ProjectRow.id == project_id).values(deleted_at=func.now())
    )
    await session.execute(
        update(SceneRow).where(SceneRow.id == scene_id).values(deleted_at=func.now())
    )
    await session.execute(
        update(PromptRow).where(PromptRow.id == prompt_id).values(deleted_at=func.now())
    )
    await session.flush()

    still = await repo.get_owned(media.id, tenant_id, owner_id)
    assert still is not None
    assert still.project_id == project_id
    assert still.scene_id == scene_id
    assert still.prompt_id == prompt_id

    # Confirm at the row level too (defence in depth).
    row = (
        await session.execute(
            select(MediaRow.project_id, MediaRow.scene_id, MediaRow.prompt_id).where(
                MediaRow.id == media.id
            )
        )
    ).one()
    assert row.project_id == project_id
    assert row.scene_id == scene_id
    assert row.prompt_id == prompt_id
