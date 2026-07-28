"""Unit tests for the Media Library folder use cases (Slice α9.2)."""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.application.use_cases.library.create_library_folder import CreateLibraryFolder
from app.application.use_cases.library.delete_library_folder import DeleteLibraryFolder
from app.application.use_cases.library.get_library_folder import GetLibraryFolder
from app.application.use_cases.library.list_library_folders import ListLibraryFolders
from app.application.use_cases.library.update_library_folder import UpdateLibraryFolder
from app.core.errors import ConflictError, NotFoundError, ValidationFailedError
from tests.unit.application.use_cases.library._fakes import (
    FakeLibraryRepository,
    FakeLibraryUnitOfWork,
)

pytestmark = pytest.mark.unit


def _uow() -> FakeLibraryUnitOfWork:
    return FakeLibraryUnitOfWork(library=FakeLibraryRepository())


async def test_create_folder_happy() -> None:
    uow = _uow()
    tenant, owner = uuid4(), uuid4()
    folder = await CreateLibraryFolder(uow).execute(
        tenant_id=tenant, owner_user_id=owner, name="Clips"
    )
    assert folder.name == "Clips"
    assert folder.parent_folder_id is None
    assert uow.committed is True


async def test_create_folder_with_missing_parent_is_404() -> None:
    uow = _uow()
    tenant, owner = uuid4(), uuid4()
    with pytest.raises(NotFoundError):
        await CreateLibraryFolder(uow).execute(
            tenant_id=tenant,
            owner_user_id=owner,
            name="Sub",
            parent_folder_id=uuid4(),
        )


async def test_create_folder_duplicate_name_is_409() -> None:
    uow = _uow()
    tenant, owner = uuid4(), uuid4()
    await CreateLibraryFolder(uow).execute(tenant_id=tenant, owner_user_id=owner, name="Dup")
    with pytest.raises(ConflictError):
        await CreateLibraryFolder(uow).execute(tenant_id=tenant, owner_user_id=owner, name="Dup")


async def test_create_child_under_owned_parent_and_same_name_different_parent_ok() -> None:
    uow = _uow()
    tenant, owner = uuid4(), uuid4()
    parent = await CreateLibraryFolder(uow).execute(
        tenant_id=tenant, owner_user_id=owner, name="Parent"
    )
    # Same name "X" is allowed under different parents (root vs parent).
    await CreateLibraryFolder(uow).execute(tenant_id=tenant, owner_user_id=owner, name="X")
    child = await CreateLibraryFolder(uow).execute(
        tenant_id=tenant, owner_user_id=owner, name="X", parent_folder_id=parent.id
    )
    assert child.parent_folder_id == parent.id


async def test_get_folder_404_for_foreign_owner() -> None:
    uow = _uow()
    tenant, owner = uuid4(), uuid4()
    folder = await CreateLibraryFolder(uow).execute(
        tenant_id=tenant, owner_user_id=owner, name="Mine"
    )
    with pytest.raises(NotFoundError):
        await GetLibraryFolder(uow).execute(
            folder_id=folder.id, tenant_id=tenant, owner_user_id=uuid4()
        )


async def test_list_folders_keyset_walk_and_owner_isolation() -> None:
    repo = FakeLibraryRepository()
    uow = FakeLibraryUnitOfWork(library=repo)
    tenant, owner = uuid4(), uuid4()
    created = [
        await CreateLibraryFolder(uow).execute(tenant_id=tenant, owner_user_id=owner, name=f"F{i}")
        for i in range(5)
    ]
    # A foreign owner's folder must not appear.
    await CreateLibraryFolder(uow).execute(tenant_id=tenant, owner_user_id=uuid4(), name="foreign")

    collected: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        page = await ListLibraryFolders(uow).execute(
            tenant_id=tenant, owner_user_id=owner, limit=2, cursor_token=cursor
        )
        collected.extend(f.name for f in page.items)
        pages += 1
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
        assert pages < 10
    # Newest-first, every owned folder exactly once, no foreign row.
    assert collected == [f.name for f in reversed(created)]
    assert pages == 3


async def test_list_folders_parent_filter_roots_only() -> None:
    repo = FakeLibraryRepository()
    uow = FakeLibraryUnitOfWork(library=repo)
    tenant, owner = uuid4(), uuid4()
    root = await CreateLibraryFolder(uow).execute(
        tenant_id=tenant, owner_user_id=owner, name="Root"
    )
    await CreateLibraryFolder(uow).execute(
        tenant_id=tenant, owner_user_id=owner, name="Child", parent_folder_id=root.id
    )
    roots = await ListLibraryFolders(uow).execute(
        tenant_id=tenant,
        owner_user_id=owner,
        limit=20,
        filter_by_parent=True,
        parent_folder_id=None,
    )
    assert [f.name for f in roots.items] == ["Root"]

    children = await ListLibraryFolders(uow).execute(
        tenant_id=tenant,
        owner_user_id=owner,
        limit=20,
        filter_by_parent=True,
        parent_folder_id=root.id,
    )
    assert [f.name for f in children.items] == ["Child"]


async def test_update_folder_rename_and_noop() -> None:
    uow = _uow()
    tenant, owner = uuid4(), uuid4()
    folder = await CreateLibraryFolder(uow).execute(
        tenant_id=tenant, owner_user_id=owner, name="Old"
    )
    renamed = await UpdateLibraryFolder(uow).execute(
        folder_id=folder.id,
        tenant_id=tenant,
        owner_user_id=owner,
        changes={"name": "New"},
    )
    assert renamed.name == "New"
    # Same-value no-op returns unchanged row.
    noop = await UpdateLibraryFolder(uow).execute(
        folder_id=folder.id,
        tenant_id=tenant,
        owner_user_id=owner,
        changes={"name": "New"},
    )
    assert noop.name == "New"


async def test_update_folder_move_happy() -> None:
    uow = _uow()
    tenant, owner = uuid4(), uuid4()
    a = await CreateLibraryFolder(uow).execute(tenant_id=tenant, owner_user_id=owner, name="A")
    b = await CreateLibraryFolder(uow).execute(tenant_id=tenant, owner_user_id=owner, name="B")
    moved = await UpdateLibraryFolder(uow).execute(
        folder_id=b.id,
        tenant_id=tenant,
        owner_user_id=owner,
        changes={"parent_folder_id": a.id},
    )
    assert moved.parent_folder_id == a.id


async def test_update_folder_move_into_self_is_422() -> None:
    uow = _uow()
    tenant, owner = uuid4(), uuid4()
    a = await CreateLibraryFolder(uow).execute(tenant_id=tenant, owner_user_id=owner, name="A")
    with pytest.raises(ValidationFailedError):
        await UpdateLibraryFolder(uow).execute(
            folder_id=a.id,
            tenant_id=tenant,
            owner_user_id=owner,
            changes={"parent_folder_id": a.id},
        )


async def test_update_folder_move_into_descendant_is_422() -> None:
    uow = _uow()
    tenant, owner = uuid4(), uuid4()
    a = await CreateLibraryFolder(uow).execute(tenant_id=tenant, owner_user_id=owner, name="A")
    b = await CreateLibraryFolder(uow).execute(
        tenant_id=tenant, owner_user_id=owner, name="B", parent_folder_id=a.id
    )
    # Moving A under its child B forms a cycle.
    with pytest.raises(ValidationFailedError):
        await UpdateLibraryFolder(uow).execute(
            folder_id=a.id,
            tenant_id=tenant,
            owner_user_id=owner,
            changes={"parent_folder_id": b.id},
        )


async def test_update_folder_move_under_missing_parent_is_404() -> None:
    uow = _uow()
    tenant, owner = uuid4(), uuid4()
    a = await CreateLibraryFolder(uow).execute(tenant_id=tenant, owner_user_id=owner, name="A")
    with pytest.raises(NotFoundError):
        await UpdateLibraryFolder(uow).execute(
            folder_id=a.id,
            tenant_id=tenant,
            owner_user_id=owner,
            changes={"parent_folder_id": uuid4()},
        )


async def test_delete_folder_happy_and_idempotent_404() -> None:
    uow = _uow()
    tenant, owner = uuid4(), uuid4()
    f = await CreateLibraryFolder(uow).execute(tenant_id=tenant, owner_user_id=owner, name="Z")
    await DeleteLibraryFolder(uow).execute(folder_id=f.id, tenant_id=tenant, owner_user_id=owner)
    with pytest.raises(NotFoundError):
        await DeleteLibraryFolder(uow).execute(
            folder_id=f.id, tenant_id=tenant, owner_user_id=owner
        )


async def test_delete_folder_with_subfolders_is_409() -> None:
    uow = _uow()
    tenant, owner = uuid4(), uuid4()
    parent = await CreateLibraryFolder(uow).execute(tenant_id=tenant, owner_user_id=owner, name="P")
    await CreateLibraryFolder(uow).execute(
        tenant_id=tenant, owner_user_id=owner, name="C", parent_folder_id=parent.id
    )
    with pytest.raises(ConflictError):
        await DeleteLibraryFolder(uow).execute(
            folder_id=parent.id, tenant_id=tenant, owner_user_id=owner
        )
