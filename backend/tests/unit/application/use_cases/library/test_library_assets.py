"""Unit tests for the Media Library asset use cases (Slice α9.2)."""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.application.use_cases.library.add_library_asset import AddLibraryAsset
from app.application.use_cases.library.create_library_folder import CreateLibraryFolder
from app.application.use_cases.library.delete_library_asset import DeleteLibraryAsset
from app.application.use_cases.library.delete_library_folder import DeleteLibraryFolder
from app.application.use_cases.library.get_library_asset import GetLibraryAsset
from app.application.use_cases.library.list_library_assets import ListLibraryAssets
from app.application.use_cases.library.record_library_asset_use import RecordLibraryAssetUse
from app.application.use_cases.library.update_library_asset import UpdateLibraryAsset
from app.core.errors import ConflictError, NotFoundError, VersionConflictError
from tests.unit.application.use_cases.library._fakes import (
    FakeLibraryRepository,
    FakeLibraryUnitOfWork,
    FakeMediaRepository,
    FakeProjectRepository,
    make_media_asset,
    make_project,
)

pytestmark = pytest.mark.unit


def _setup() -> (
    tuple[FakeLibraryUnitOfWork, FakeLibraryRepository, FakeMediaRepository, FakeProjectRepository]
):
    lib = FakeLibraryRepository()
    media = FakeMediaRepository()
    projects = FakeProjectRepository()
    uow = FakeLibraryUnitOfWork(library=lib, media=media, projects=projects)
    return uow, lib, media, projects


async def test_add_asset_happy_default_name_and_tag_normalization() -> None:
    uow, _lib, media, _proj = _setup()
    tenant, owner = uuid4(), uuid4()
    m = media.add(make_media_asset(tenant_id=tenant, owner_user_id=owner, kind="video"))

    asset = await AddLibraryAsset(uow).execute(
        tenant_id=tenant,
        owner_user_id=owner,
        media_asset_id=m.id,
        tags=["  Fun ", "fun", "Travel", ""],
    )
    assert asset.version == 1
    assert asset.media_asset_id == m.id
    # default name derived from media kind
    assert asset.name.startswith("video-")
    # normalized: lowercased, trimmed, de-duplicated, empties dropped
    assert asset.tags == ("fun", "travel")
    assert uow.committed is True


async def test_add_asset_uses_original_filename_when_present() -> None:
    uow, _lib, media, _proj = _setup()
    tenant, owner = uuid4(), uuid4()
    m = media.add(
        make_media_asset(
            tenant_id=tenant,
            owner_user_id=owner,
            source_metadata={"original_filename": "beach.mp4"},
        )
    )
    asset = await AddLibraryAsset(uow).execute(
        tenant_id=tenant, owner_user_id=owner, media_asset_id=m.id
    )
    assert asset.name == "beach.mp4"


async def test_add_asset_missing_media_is_404() -> None:
    uow, *_ = _setup()
    tenant, owner = uuid4(), uuid4()
    with pytest.raises(NotFoundError):
        await AddLibraryAsset(uow).execute(
            tenant_id=tenant, owner_user_id=owner, media_asset_id=uuid4()
        )


async def test_add_asset_foreign_media_is_404() -> None:
    uow, _lib, media, _proj = _setup()
    tenant, owner = uuid4(), uuid4()
    # media belongs to a different owner
    m = media.add(make_media_asset(tenant_id=tenant, owner_user_id=uuid4()))
    with pytest.raises(NotFoundError):
        await AddLibraryAsset(uow).execute(
            tenant_id=tenant, owner_user_id=owner, media_asset_id=m.id
        )


async def test_add_asset_missing_folder_is_404() -> None:
    uow, _lib, media, _proj = _setup()
    tenant, owner = uuid4(), uuid4()
    m = media.add(make_media_asset(tenant_id=tenant, owner_user_id=owner))
    with pytest.raises(NotFoundError):
        await AddLibraryAsset(uow).execute(
            tenant_id=tenant,
            owner_user_id=owner,
            media_asset_id=m.id,
            library_folder_id=uuid4(),
        )


async def test_add_asset_duplicate_media_is_409() -> None:
    uow, _lib, media, _proj = _setup()
    tenant, owner = uuid4(), uuid4()
    m = media.add(make_media_asset(tenant_id=tenant, owner_user_id=owner))
    await AddLibraryAsset(uow).execute(tenant_id=tenant, owner_user_id=owner, media_asset_id=m.id)
    with pytest.raises(ConflictError):
        await AddLibraryAsset(uow).execute(
            tenant_id=tenant, owner_user_id=owner, media_asset_id=m.id
        )


async def test_list_assets_keyset_folder_and_tag_filters() -> None:
    uow, lib, media, _proj = _setup()
    tenant, owner = uuid4(), uuid4()
    folder = await CreateLibraryFolder(uow).execute(tenant_id=tenant, owner_user_id=owner, name="F")
    filed = []
    for i in range(3):
        m = media.add(make_media_asset(tenant_id=tenant, owner_user_id=owner))
        a = await AddLibraryAsset(uow).execute(
            tenant_id=tenant,
            owner_user_id=owner,
            media_asset_id=m.id,
            library_folder_id=folder.id,
            tags=["keep"] if i == 0 else [],
        )
        filed.append(a)
    # one unfiled asset
    mu = media.add(make_media_asset(tenant_id=tenant, owner_user_id=owner))
    unfiled = await AddLibraryAsset(uow).execute(
        tenant_id=tenant, owner_user_id=owner, media_asset_id=mu.id
    )

    all_page = await ListLibraryAssets(uow).execute(tenant_id=tenant, owner_user_id=owner, limit=20)
    assert len(all_page.items) == 4

    in_folder = await ListLibraryAssets(uow).execute(
        tenant_id=tenant,
        owner_user_id=owner,
        limit=20,
        folder_id=folder.id,
        filter_by_folder=True,
    )
    assert {a.id for a in in_folder.items} == {a.id for a in filed}

    unfiled_page = await ListLibraryAssets(uow).execute(
        tenant_id=tenant,
        owner_user_id=owner,
        limit=20,
        folder_id=None,
        filter_by_folder=True,
    )
    assert [a.id for a in unfiled_page.items] == [unfiled.id]

    tagged = await ListLibraryAssets(uow).execute(
        tenant_id=tenant, owner_user_id=owner, limit=20, tags=("keep",)
    )
    assert [a.id for a in tagged.items] == [filed[0].id]


async def test_list_and_get_exclude_soft_deleted_media() -> None:
    uow, lib, media, _proj = _setup()
    tenant, owner = uuid4(), uuid4()
    m = media.add(make_media_asset(tenant_id=tenant, owner_user_id=owner))
    asset = await AddLibraryAsset(uow).execute(
        tenant_id=tenant, owner_user_id=owner, media_asset_id=m.id
    )
    # Simulate the underlying media being soft-deleted.
    lib.hidden_media.add(m.id)

    page = await ListLibraryAssets(uow).execute(tenant_id=tenant, owner_user_id=owner, limit=20)
    assert page.items == []
    with pytest.raises(NotFoundError):
        await GetLibraryAsset(uow).execute(asset_id=asset.id, tenant_id=tenant, owner_user_id=owner)


async def test_update_asset_happy_bumps_version() -> None:
    uow, _lib, media, _proj = _setup()
    tenant, owner = uuid4(), uuid4()
    m = media.add(make_media_asset(tenant_id=tenant, owner_user_id=owner))
    asset = await AddLibraryAsset(uow).execute(
        tenant_id=tenant, owner_user_id=owner, media_asset_id=m.id
    )
    updated = await UpdateLibraryAsset(uow).execute(
        asset_id=asset.id,
        tenant_id=tenant,
        owner_user_id=owner,
        expected_version=asset.version,
        changes={"name": "Renamed", "tags": ["A", "a", "B"]},
    )
    assert updated.name == "Renamed"
    assert updated.tags == ("a", "b")
    assert updated.version == asset.version + 1


async def test_update_asset_stale_version_is_412() -> None:
    uow, _lib, media, _proj = _setup()
    tenant, owner = uuid4(), uuid4()
    m = media.add(make_media_asset(tenant_id=tenant, owner_user_id=owner))
    asset = await AddLibraryAsset(uow).execute(
        tenant_id=tenant, owner_user_id=owner, media_asset_id=m.id
    )
    with pytest.raises(VersionConflictError):
        await UpdateLibraryAsset(uow).execute(
            asset_id=asset.id,
            tenant_id=tenant,
            owner_user_id=owner,
            expected_version=asset.version + 99,
            changes={"name": "X"},
        )


async def test_update_asset_missing_is_404_before_412() -> None:
    uow, *_ = _setup()
    tenant, owner = uuid4(), uuid4()
    with pytest.raises(NotFoundError):
        await UpdateLibraryAsset(uow).execute(
            asset_id=uuid4(),
            tenant_id=tenant,
            owner_user_id=owner,
            expected_version=1,
            changes={"name": "X"},
        )


async def test_update_asset_refile_missing_folder_is_404() -> None:
    uow, _lib, media, _proj = _setup()
    tenant, owner = uuid4(), uuid4()
    m = media.add(make_media_asset(tenant_id=tenant, owner_user_id=owner))
    asset = await AddLibraryAsset(uow).execute(
        tenant_id=tenant, owner_user_id=owner, media_asset_id=m.id
    )
    with pytest.raises(NotFoundError):
        await UpdateLibraryAsset(uow).execute(
            asset_id=asset.id,
            tenant_id=tenant,
            owner_user_id=owner,
            expected_version=asset.version,
            changes={"library_folder_id": uuid4()},
        )


async def test_delete_asset_idempotent_404() -> None:
    uow, _lib, media, _proj = _setup()
    tenant, owner = uuid4(), uuid4()
    m = media.add(make_media_asset(tenant_id=tenant, owner_user_id=owner))
    asset = await AddLibraryAsset(uow).execute(
        tenant_id=tenant, owner_user_id=owner, media_asset_id=m.id
    )
    await DeleteLibraryAsset(uow).execute(asset_id=asset.id, tenant_id=tenant, owner_user_id=owner)
    with pytest.raises(NotFoundError):
        await DeleteLibraryAsset(uow).execute(
            asset_id=asset.id, tenant_id=tenant, owner_user_id=owner
        )


async def test_delete_folder_detaches_assets_to_unfiled() -> None:
    uow, lib, media, _proj = _setup()
    tenant, owner = uuid4(), uuid4()
    folder = await CreateLibraryFolder(uow).execute(tenant_id=tenant, owner_user_id=owner, name="F")
    m = media.add(make_media_asset(tenant_id=tenant, owner_user_id=owner))
    asset = await AddLibraryAsset(uow).execute(
        tenant_id=tenant,
        owner_user_id=owner,
        media_asset_id=m.id,
        library_folder_id=folder.id,
    )
    await DeleteLibraryFolder(uow).execute(
        folder_id=folder.id, tenant_id=tenant, owner_user_id=owner
    )
    refetched = await GetLibraryAsset(uow).execute(
        asset_id=asset.id, tenant_id=tenant, owner_user_id=owner
    )
    assert refetched.library_folder_id is None


async def test_record_use_happy_and_idempotent() -> None:
    uow, _lib, media, projects = _setup()
    tenant, owner = uuid4(), uuid4()
    m = media.add(make_media_asset(tenant_id=tenant, owner_user_id=owner))
    asset = await AddLibraryAsset(uow).execute(
        tenant_id=tenant, owner_user_id=owner, media_asset_id=m.id
    )
    project = projects.add(make_project(tenant_id=tenant, owner_user_id=owner))

    once = await RecordLibraryAssetUse(uow).execute(
        asset_id=asset.id, tenant_id=tenant, owner_user_id=owner, project_id=project.id
    )
    assert once.usage_count == 1
    assert once.last_used_at is not None

    twice = await RecordLibraryAssetUse(uow).execute(
        asset_id=asset.id, tenant_id=tenant, owner_user_id=owner, project_id=project.id
    )
    # usage_count still advances, but the junction pair is recorded once.
    assert twice.usage_count == 2
    assert len(uow.library.junction) == 1  # type: ignore[attr-defined]


async def test_record_use_missing_project_is_404() -> None:
    uow, _lib, media, _proj = _setup()
    tenant, owner = uuid4(), uuid4()
    m = media.add(make_media_asset(tenant_id=tenant, owner_user_id=owner))
    asset = await AddLibraryAsset(uow).execute(
        tenant_id=tenant, owner_user_id=owner, media_asset_id=m.id
    )
    with pytest.raises(NotFoundError):
        await RecordLibraryAssetUse(uow).execute(
            asset_id=asset.id, tenant_id=tenant, owner_user_id=owner, project_id=uuid4()
        )


async def test_record_use_missing_asset_is_404() -> None:
    uow, _lib, _media, projects = _setup()
    tenant, owner = uuid4(), uuid4()
    project = projects.add(make_project(tenant_id=tenant, owner_user_id=owner))
    with pytest.raises(NotFoundError):
        await RecordLibraryAssetUse(uow).execute(
            asset_id=uuid4(), tenant_id=tenant, owner_user_id=owner, project_id=project.id
        )
