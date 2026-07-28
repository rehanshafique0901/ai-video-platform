"""α9.2 — Media Library end-to-end against live PostgreSQL (ADR-0037 CR-8).

Proves the real :class:`LibraryRepository` + library use cases honour their DB-enforced
invariants against a live database: ``(parent, name)`` / ``media_asset_id`` uniqueness,
version-fenced OCC, keyset pagination, the tag GIN-backed ANY-of browse, hiding entries
whose underlying media is soft-deleted, folder-move cycle rejection, folder-delete asset
detachment, and idempotent reuse recording. A final group drives the real
``/api/v1/library/*`` endpoints for an authenticated owner.

DB-semantics tests seed identity + media directly (committed, like the other runtime
slices) and delete everything they created on teardown — library tables are mutable, so
no immutable-guard is touched. HTTP-contract tests register a user and seed committed
media/projects, exercise the SAVEPOINT-rolled-back ``client`` fixture, and clean up the
committed seed afterwards.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.use_cases.library.add_library_asset import AddLibraryAsset
from app.application.use_cases.library.create_library_folder import CreateLibraryFolder
from app.application.use_cases.library.delete_library_folder import DeleteLibraryFolder
from app.application.use_cases.library.get_library_asset import GetLibraryAsset
from app.application.use_cases.library.list_library_assets import ListLibraryAssets
from app.application.use_cases.library.record_library_asset_use import RecordLibraryAssetUse
from app.application.use_cases.library.update_library_asset import UpdateLibraryAsset
from app.application.use_cases.library.update_library_folder import UpdateLibraryFolder
from app.core import container
from app.core.errors import (
    ConflictError,
    NotFoundError,
    ValidationFailedError,
    VersionConflictError,
)
from app.infrastructure.db.models.identity import Tenant, User
from app.infrastructure.db.models.media import MediaAsset as MediaAssetRow
from app.infrastructure.db.models.projects import Project as ProjectRow
from app.infrastructure.uow.sqlalchemy_unit_of_work import SqlAlchemyUnitOfWork

pytestmark = pytest.mark.integration


@dataclass
class _Owner:
    tenant_id: UUID
    user_id: UUID
    media_ids: list[UUID] = field(default_factory=list)
    project_id: UUID | None = None


async def _insert_identity(
    session_factory: async_sessionmaker[AsyncSession], tenant_id: UUID, user_id: UUID
) -> None:
    async with session_factory() as s:
        await s.execute(insert(Tenant).values(id=tenant_id, name="LIB", slug=f"lib-{tenant_id}"))
        await s.execute(
            insert(User).values(
                id=user_id,
                tenant_id=tenant_id,
                email=f"lib-{user_id}@example.com",
                display_name="LIB Owner",
            )
        )
        await s.commit()


async def _insert_media(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: UUID,
    user_id: UUID,
    count: int,
) -> list[UUID]:
    ids: list[UUID] = []
    async with session_factory() as s:
        for _ in range(count):
            mid = uuid4()
            await s.execute(
                insert(MediaAssetRow).values(
                    id=mid,
                    tenant_id=tenant_id,
                    owner_user_id=user_id,
                    kind="video",
                    storage_backend="local",
                    storage_bucket="media",
                    storage_key=f"lib/{mid}.mp4",
                    mime_type="video/mp4",
                    size_bytes=1024,
                    checksum_sha256=b"\x00" * 32,
                    source="generated",
                )
            )
            ids.append(mid)
        await s.commit()
    return ids


async def _insert_project(
    session_factory: async_sessionmaker[AsyncSession], *, tenant_id: UUID, user_id: UUID
) -> UUID:
    pid = uuid4()
    async with session_factory() as s:
        await s.execute(
            insert(ProjectRow).values(
                id=pid,
                tenant_id=tenant_id,
                owner_user_id=user_id,
                name=f"lib-proj-{pid}",
                aspect_ratio="horizontal",
            )
        )
        await s.commit()
    return pid


async def _soft_delete_media(
    session_factory: async_sessionmaker[AsyncSession], media_id: UUID
) -> None:
    async with session_factory() as s:
        await s.execute(
            text("UPDATE media_assets SET deleted_at = now() WHERE id = :i"),
            {"i": str(media_id)},
        )
        await s.commit()


async def _cleanup(
    session_factory: async_sessionmaker[AsyncSession],
    user_id: UUID,
    tenant_id: UUID,
    *,
    drop_identity: bool,
) -> None:
    async with session_factory() as s:
        await s.execute(
            text(
                "DELETE FROM library_asset_projects WHERE library_asset_id IN "
                "(SELECT id FROM library_assets WHERE owner_user_id = :u)"
            ),
            {"u": str(user_id)},
        )
        await s.execute(
            text("DELETE FROM library_assets WHERE owner_user_id = :u"), {"u": str(user_id)}
        )
        await s.execute(
            text("DELETE FROM library_folders WHERE owner_user_id = :u"), {"u": str(user_id)}
        )
        await s.execute(text("DELETE FROM projects WHERE owner_user_id = :u"), {"u": str(user_id)})
        await s.execute(
            text("DELETE FROM media_assets WHERE owner_user_id = :u"), {"u": str(user_id)}
        )
        if drop_identity:
            await s.execute(text("DELETE FROM users WHERE id = :i"), {"i": str(user_id)})
            await s.execute(text("DELETE FROM tenants WHERE id = :i"), {"i": str(tenant_id)})
        await s.commit()


@asynccontextmanager
async def _owner(
    session_factory: async_sessionmaker[AsyncSession], *, media: int = 0
) -> AsyncIterator[_Owner]:
    tenant_id, user_id = uuid4(), uuid4()
    await _insert_identity(session_factory, tenant_id, user_id)
    owner = _Owner(tenant_id=tenant_id, user_id=user_id)
    if media:
        owner.media_ids = await _insert_media(
            session_factory, tenant_id=tenant_id, user_id=user_id, count=media
        )
    try:
        yield owner
    finally:
        await _cleanup(session_factory, user_id, tenant_id, drop_identity=True)


# --------------------------------------------------------------------------- #
# DB semantics via the real repository / use cases                            #
# --------------------------------------------------------------------------- #
async def test_folder_uniqueness_and_owner_isolation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with _owner(session_factory) as o1, _owner(session_factory) as o2:
        uc = CreateLibraryFolder(SqlAlchemyUnitOfWork(session_factory))
        await uc.execute(tenant_id=o1.tenant_id, owner_user_id=o1.user_id, name="Shared")
        # Same (parent=None, name) for the same owner → 409.
        with pytest.raises(ConflictError):
            await uc.execute(tenant_id=o1.tenant_id, owner_user_id=o1.user_id, name="Shared")
        # A different owner may reuse the name.
        other = await uc.execute(tenant_id=o2.tenant_id, owner_user_id=o2.user_id, name="Shared")
        assert other.name == "Shared"


async def test_folder_move_cycle_rejected(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with _owner(session_factory) as o:
        create = CreateLibraryFolder(SqlAlchemyUnitOfWork(session_factory))
        a = await create.execute(tenant_id=o.tenant_id, owner_user_id=o.user_id, name="A")
        b = await create.execute(
            tenant_id=o.tenant_id, owner_user_id=o.user_id, name="B", parent_folder_id=a.id
        )
        update = UpdateLibraryFolder(SqlAlchemyUnitOfWork(session_factory))
        with pytest.raises(ValidationFailedError):
            await update.execute(
                folder_id=a.id,
                tenant_id=o.tenant_id,
                owner_user_id=o.user_id,
                changes={"parent_folder_id": b.id},
            )


async def test_delete_folder_detaches_assets(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with _owner(session_factory, media=1) as o:
        folder = await CreateLibraryFolder(SqlAlchemyUnitOfWork(session_factory)).execute(
            tenant_id=o.tenant_id, owner_user_id=o.user_id, name="F"
        )
        asset = await AddLibraryAsset(SqlAlchemyUnitOfWork(session_factory)).execute(
            tenant_id=o.tenant_id,
            owner_user_id=o.user_id,
            media_asset_id=o.media_ids[0],
            library_folder_id=folder.id,
        )
        await DeleteLibraryFolder(SqlAlchemyUnitOfWork(session_factory)).execute(
            folder_id=folder.id, tenant_id=o.tenant_id, owner_user_id=o.user_id
        )
        refetched = await GetLibraryAsset(SqlAlchemyUnitOfWork(session_factory)).execute(
            asset_id=asset.id, tenant_id=o.tenant_id, owner_user_id=o.user_id
        )
        assert refetched.library_folder_id is None


async def test_asset_unique_media(session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with _owner(session_factory, media=1) as o:
        add = AddLibraryAsset(SqlAlchemyUnitOfWork(session_factory))
        first = await add.execute(
            tenant_id=o.tenant_id, owner_user_id=o.user_id, media_asset_id=o.media_ids[0]
        )
        assert first.version == 1
        with pytest.raises(ConflictError):
            await add.execute(
                tenant_id=o.tenant_id, owner_user_id=o.user_id, media_asset_id=o.media_ids[0]
            )


async def test_asset_occ_update_and_stale_version(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with _owner(session_factory, media=1) as o:
        asset = await AddLibraryAsset(SqlAlchemyUnitOfWork(session_factory)).execute(
            tenant_id=o.tenant_id, owner_user_id=o.user_id, media_asset_id=o.media_ids[0]
        )
        update = UpdateLibraryAsset(SqlAlchemyUnitOfWork(session_factory))
        updated = await update.execute(
            asset_id=asset.id,
            tenant_id=o.tenant_id,
            owner_user_id=o.user_id,
            expected_version=asset.version,
            changes={"name": "Renamed", "tags": ["Alpha", "alpha", "Beta"]},
        )
        assert updated.version == asset.version + 1
        assert updated.tags == ("alpha", "beta")
        # Replaying the stale version is a 412.
        with pytest.raises(VersionConflictError):
            await update.execute(
                asset_id=asset.id,
                tenant_id=o.tenant_id,
                owner_user_id=o.user_id,
                expected_version=asset.version,
                changes={"name": "Again"},
            )


async def test_tag_overlap_browse(session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with _owner(session_factory, media=3) as o:
        add = AddLibraryAsset(SqlAlchemyUnitOfWork(session_factory))
        a_travel = await add.execute(
            tenant_id=o.tenant_id,
            owner_user_id=o.user_id,
            media_asset_id=o.media_ids[0],
            tags=["travel", "beach"],
        )
        await add.execute(
            tenant_id=o.tenant_id,
            owner_user_id=o.user_id,
            media_asset_id=o.media_ids[1],
            tags=["cooking"],
        )
        await add.execute(
            tenant_id=o.tenant_id, owner_user_id=o.user_id, media_asset_id=o.media_ids[2]
        )
        page = await ListLibraryAssets(SqlAlchemyUnitOfWork(session_factory)).execute(
            tenant_id=o.tenant_id, owner_user_id=o.user_id, limit=20, tags=("beach",)
        )
        assert [a.id for a in page.items] == [a_travel.id]


async def test_soft_deleted_media_hidden_from_browse_and_get(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with _owner(session_factory, media=1) as o:
        asset = await AddLibraryAsset(SqlAlchemyUnitOfWork(session_factory)).execute(
            tenant_id=o.tenant_id, owner_user_id=o.user_id, media_asset_id=o.media_ids[0]
        )
        await _soft_delete_media(session_factory, o.media_ids[0])
        page = await ListLibraryAssets(SqlAlchemyUnitOfWork(session_factory)).execute(
            tenant_id=o.tenant_id, owner_user_id=o.user_id, limit=20
        )
        assert page.items == []
        with pytest.raises(NotFoundError):
            await GetLibraryAsset(SqlAlchemyUnitOfWork(session_factory)).execute(
                asset_id=asset.id, tenant_id=o.tenant_id, owner_user_id=o.user_id
            )


async def test_keyset_pagination_covers_all(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with _owner(session_factory, media=5) as o:
        add = AddLibraryAsset(SqlAlchemyUnitOfWork(session_factory))
        created = []
        for mid in o.media_ids:
            created.append(
                await add.execute(
                    tenant_id=o.tenant_id, owner_user_id=o.user_id, media_asset_id=mid
                )
            )
        lister = ListLibraryAssets(SqlAlchemyUnitOfWork(session_factory))
        seen: list[UUID] = []
        cursor: str | None = None
        pages = 0
        while True:
            page = await lister.execute(
                tenant_id=o.tenant_id, owner_user_id=o.user_id, limit=2, cursor_token=cursor
            )
            seen.extend(a.id for a in page.items)
            pages += 1
            if page.next_cursor is None:
                break
            cursor = page.next_cursor
            assert pages < 10
        assert set(seen) == {a.id for a in created}
        assert len(seen) == 5


async def test_record_use_increments_and_junction_idempotent(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with _owner(session_factory, media=1) as o:
        o.project_id = await _insert_project(
            session_factory, tenant_id=o.tenant_id, user_id=o.user_id
        )
        asset = await AddLibraryAsset(SqlAlchemyUnitOfWork(session_factory)).execute(
            tenant_id=o.tenant_id, owner_user_id=o.user_id, media_asset_id=o.media_ids[0]
        )
        rec = RecordLibraryAssetUse(SqlAlchemyUnitOfWork(session_factory))
        once = await rec.execute(
            asset_id=asset.id,
            tenant_id=o.tenant_id,
            owner_user_id=o.user_id,
            project_id=o.project_id,
        )
        assert once.usage_count == 1
        assert once.last_used_at is not None
        twice = await rec.execute(
            asset_id=asset.id,
            tenant_id=o.tenant_id,
            owner_user_id=o.user_id,
            project_id=o.project_id,
        )
        assert twice.usage_count == 2
        # The junction pair is recorded exactly once (idempotent upsert).
        async with session_factory() as s:
            n = (
                await s.execute(
                    text(
                        "SELECT count(*) FROM library_asset_projects "
                        "WHERE library_asset_id = :a AND project_id = :p"
                    ),
                    {"a": str(asset.id), "p": str(o.project_id)},
                )
            ).scalar_one()
        assert n == 1


# --------------------------------------------------------------------------- #
# HTTP surface (through the real endpoints)                                    #
# --------------------------------------------------------------------------- #
async def test_endpoint_folder_and_asset_crud_for_owner(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    reg = container.get_register_user_use_case()
    result = await reg.execute(
        email=f"lib-api-{uuid4()}@example.com",
        password="correct horse battery staple",
        name="LIB API",
    )
    auth = {"Authorization": f"Bearer {result.tokens.access_token}"}
    tenant_id, user_id = result.user.tenant_id, result.user.id
    media_ids = await _insert_media(session_factory, tenant_id=tenant_id, user_id=user_id, count=1)
    project_id = await _insert_project(session_factory, tenant_id=tenant_id, user_id=user_id)
    try:
        # Create a folder.
        rf = await client.post("/api/v1/library/folders", headers=auth, json={"name": "Vlogs"})
        assert rf.status_code == 201, rf.text
        folder_id = rf.json()["data"]["id"]

        # Add an asset into the folder.
        ra = await client.post(
            "/api/v1/library/assets",
            headers=auth,
            json={
                "media_asset_id": str(media_ids[0]),
                "library_folder_id": folder_id,
                "name": "Clip One",
                "tags": ["Fun", "fun", "Travel"],
            },
        )
        assert ra.status_code == 201, ra.text
        asset = ra.json()["data"]
        assert asset["tags"] == ["fun", "travel"]
        assert asset["version"] == 1
        asset_id = asset["id"]

        # List (browse) shows it.
        rl = await client.get("/api/v1/library/assets", headers=auth)
        assert rl.status_code == 200
        assert any(a["id"] == asset_id for a in rl.json()["data"])

        # Tag filter narrows.
        rt = await client.get("/api/v1/library/assets?tags=travel", headers=auth)
        assert [a["id"] for a in rt.json()["data"]] == [asset_id]

        # OCC update.
        ru = await client.patch(
            f"/api/v1/library/assets/{asset_id}",
            headers=auth,
            json={"version": 1, "name": "Clip Renamed"},
        )
        assert ru.status_code == 200, ru.text
        assert ru.json()["data"]["name"] == "Clip Renamed"
        assert ru.json()["data"]["version"] == 2

        # Stale version → 412.
        rstale = await client.patch(
            f"/api/v1/library/assets/{asset_id}",
            headers=auth,
            json={"version": 1, "name": "Nope"},
        )
        assert rstale.status_code == 412

        # Record a reuse.
        rr = await client.post(
            f"/api/v1/library/assets/{asset_id}/uses",
            headers=auth,
            json={"project_id": str(project_id)},
        )
        assert rr.status_code == 200, rr.text
        assert rr.json()["data"]["usage_count"] == 1

        # Delete the asset → then 404.
        rd = await client.delete(f"/api/v1/library/assets/{asset_id}", headers=auth)
        assert rd.status_code == 204
        assert (
            await client.get(f"/api/v1/library/assets/{asset_id}", headers=auth)
        ).status_code == 404
    finally:
        await _cleanup(session_factory, user_id, tenant_id, drop_identity=False)


async def test_endpoint_requires_auth(client: AsyncClient) -> None:
    assert (await client.get("/api/v1/library/assets")).status_code == 401
    assert (await client.post("/api/v1/library/folders", json={"name": "X"})).status_code == 401


async def test_endpoint_unknown_asset_is_404(client: AsyncClient) -> None:
    reg = container.get_register_user_use_case()
    result = await reg.execute(
        email=f"lib-404-{uuid4()}@example.com",
        password="correct horse battery staple",
        name="LIB 404",
    )
    auth = {"Authorization": f"Bearer {result.tokens.access_token}"}
    r = await client.get(f"/api/v1/library/assets/{uuid4()}", headers=auth)
    assert r.status_code == 404, r.text


async def test_endpoint_conflicting_folder_filters_is_422(client: AsyncClient) -> None:
    reg = container.get_register_user_use_case()
    result = await reg.execute(
        email=f"lib-422-{uuid4()}@example.com",
        password="correct horse battery staple",
        name="LIB 422",
    )
    auth = {"Authorization": f"Bearer {result.tokens.access_token}"}
    r = await client.get(f"/api/v1/library/assets?unfiled=true&folder_id={uuid4()}", headers=auth)
    assert r.status_code == 422, r.text
