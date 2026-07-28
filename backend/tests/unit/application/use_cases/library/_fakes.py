"""In-memory fakes for the Media Library use-case unit tests (Slice α9.2).

Faithful, dependency-free reimplementations of the persistence semantics the real
``LibraryRepository`` relies on (owner scoping, live-only reads, ``(parent, name)`` /
``media_asset_id`` uniqueness, keyset ordering, OCC version bump, soft-deleted-media
hiding, idempotent reuse). None of these leak into integration tests — those exercise
the real repository against a live database.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Any, Self, cast
from uuid import UUID, uuid4

from app.application.interfaces.repositories import (
    ILibraryRepository,
    IMediaRepository,
    IProjectRepository,
)
from app.application.interfaces.unit_of_work import IUnitOfWork
from app.core.errors import ConflictError
from app.domain.library.library_asset import LibraryAsset
from app.domain.library.library_folder import LibraryFolder
from app.domain.media.media_asset import MediaAsset
from app.domain.projects.project import Project

_BASE = datetime(2026, 1, 1, tzinfo=UTC)


def make_media_asset(
    *,
    tenant_id: UUID,
    owner_user_id: UUID,
    kind: str = "video",
    source_metadata: dict[str, Any] | None = None,
) -> MediaAsset:
    mid = uuid4()
    return MediaAsset(
        id=mid,
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        kind=kind,
        project_id=None,
        scene_id=None,
        prompt_id=None,
        model_id=None,
        provider=None,
        storage_backend="s3",
        storage_bucket="b",
        storage_key=f"k/{mid}",
        mime_type="video/mp4",
        size_bytes=1,
        width=None,
        height=None,
        duration_seconds=None,
        checksum_sha256=b"\x00" * 32,
        source="generated",
        source_metadata=source_metadata or {},
        created_at=_BASE,
        updated_at=_BASE,
    )


def make_project(*, tenant_id: UUID, owner_user_id: UUID) -> Project:
    return Project(
        id=uuid4(),
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        folder_id=None,
        current_version_id=None,
        name=f"proj-{uuid4().hex[:6]}",
        description=None,
        aspect_ratio="horizontal",
        duration_seconds=None,
        language="en",
        style=None,
        settings={},
        created_at=_BASE,
        updated_at=_BASE,
        version=1,
    )


class FakeMediaRepository:
    """Only the surface the library use cases touch: ``get_owned``."""

    def __init__(self) -> None:
        self.rows: dict[UUID, MediaAsset] = {}

    def add(self, media: MediaAsset) -> MediaAsset:
        self.rows[media.id] = media
        return media

    async def get_owned(
        self, media_id: UUID, tenant_id: UUID, owner_user_id: UUID
    ) -> MediaAsset | None:
        m = self.rows.get(media_id)
        if m is None or m.tenant_id != tenant_id or m.owner_user_id != owner_user_id:
            return None
        return m


class FakeProjectRepository:
    """Only the surface the library use cases touch: ``get_owned``."""

    def __init__(self) -> None:
        self.rows: dict[UUID, Project] = {}

    def add(self, project: Project) -> Project:
        self.rows[project.id] = project
        return project

    async def get_owned(
        self, project_id: UUID, tenant_id: UUID, owner_user_id: UUID
    ) -> Project | None:
        p = self.rows.get(project_id)
        if p is None or p.tenant_id != tenant_id or p.owner_user_id != owner_user_id:
            return None
        return p


class FakeLibraryRepository(ILibraryRepository):
    """In-memory ``ILibraryRepository`` with faithful semantics."""

    def __init__(self) -> None:
        self.folders: dict[UUID, LibraryFolder] = {}
        self.assets: dict[UUID, LibraryAsset] = {}
        self.junction: set[tuple[UUID, UUID]] = set()
        # media_asset_ids whose underlying media is soft-deleted → entry hidden.
        self.hidden_media: set[UUID] = set()
        self._seq = 0

    def _next_ts(self) -> datetime:
        self._seq += 1
        return _BASE + timedelta(seconds=self._seq)

    # ---- folders -------------------------------------------------------

    async def add_folder(
        self,
        *,
        tenant_id: UUID,
        owner_user_id: UUID,
        parent_folder_id: UUID | None,
        name: str,
    ) -> LibraryFolder:
        for f in self.folders.values():
            if (
                f.owner_user_id == owner_user_id
                and f.parent_folder_id == parent_folder_id
                and f.name == name
            ):
                raise ConflictError("library folder already exists")
        ts = self._next_ts()
        folder = LibraryFolder(
            id=uuid4(),
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            parent_folder_id=parent_folder_id,
            name=name,
            created_at=ts,
            updated_at=ts,
        )
        self.folders[folder.id] = folder
        return folder

    async def get_folder(
        self, folder_id: UUID, tenant_id: UUID, owner_user_id: UUID
    ) -> LibraryFolder | None:
        f = self.folders.get(folder_id)
        if f is None or f.tenant_id != tenant_id or f.owner_user_id != owner_user_id:
            return None
        return f

    async def folder_name_conflicts(
        self,
        *,
        tenant_id: UUID,
        owner_user_id: UUID,
        parent_folder_id: UUID | None,
        name: str,
        exclude_folder_id: UUID | None = None,
    ) -> bool:
        return any(
            f.tenant_id == tenant_id
            and f.owner_user_id == owner_user_id
            and f.parent_folder_id == parent_folder_id
            and f.name == name
            and f.id != exclude_folder_id
            for f in self.folders.values()
        )

    async def list_folders(
        self,
        tenant_id: UUID,
        owner_user_id: UUID,
        *,
        parent_folder_id: UUID | None,
        filter_by_parent: bool,
        limit: int,
        after: tuple[datetime, UUID] | None = None,
    ) -> list[LibraryFolder]:
        rows = [
            f
            for f in self.folders.values()
            if f.tenant_id == tenant_id and f.owner_user_id == owner_user_id
        ]
        if filter_by_parent:
            rows = [f for f in rows if f.parent_folder_id == parent_folder_id]
        rows.sort(key=lambda f: (f.created_at, f.id), reverse=True)
        if after is not None:
            rows = [f for f in rows if (f.created_at, f.id) < after]
        return rows[:limit]

    async def update_folder(
        self,
        folder_id: UUID,
        tenant_id: UUID,
        owner_user_id: UUID,
        changes: Mapping[str, Any],
    ) -> LibraryFolder | None:
        f = await self.get_folder(folder_id, tenant_id, owner_user_id)
        if f is None:
            return None
        new_name = changes.get("name", f.name)
        new_parent = changes.get("parent_folder_id", f.parent_folder_id)
        for other in self.folders.values():
            if (
                other.id != folder_id
                and other.owner_user_id == owner_user_id
                and other.parent_folder_id == new_parent
                and other.name == new_name
            ):
                raise ConflictError("library folder already exists")
        updated = replace(f, **changes, updated_at=self._next_ts())
        self.folders[folder_id] = updated
        return updated

    async def soft_delete_folder(
        self, folder_id: UUID, tenant_id: UUID, owner_user_id: UUID
    ) -> bool:
        f = await self.get_folder(folder_id, tenant_id, owner_user_id)
        if f is None:
            return False
        del self.folders[folder_id]
        return True

    async def folder_has_children(
        self, folder_id: UUID, tenant_id: UUID, owner_user_id: UUID
    ) -> bool:
        return any(
            f.parent_folder_id == folder_id
            and f.tenant_id == tenant_id
            and f.owner_user_id == owner_user_id
            for f in self.folders.values()
        )

    async def detach_assets_from_folder(
        self, folder_id: UUID, tenant_id: UUID, owner_user_id: UUID
    ) -> int:
        count = 0
        for aid, a in list(self.assets.items()):
            if (
                a.library_folder_id == folder_id
                and a.tenant_id == tenant_id
                and a.owner_user_id == owner_user_id
            ):
                self.assets[aid] = replace(
                    a,
                    library_folder_id=None,
                    version=a.version + 1,
                    updated_at=self._next_ts(),
                )
                count += 1
        return count

    # ---- assets --------------------------------------------------------

    async def add_asset(
        self,
        *,
        tenant_id: UUID,
        owner_user_id: UUID,
        media_asset_id: UUID,
        library_folder_id: UUID | None,
        name: str,
        description: str | None,
        tags: tuple[str, ...],
    ) -> LibraryAsset:
        for a in self.assets.values():
            if a.media_asset_id == media_asset_id:
                raise ConflictError("media asset already in library")
        ts = self._next_ts()
        asset = LibraryAsset(
            id=uuid4(),
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            media_asset_id=media_asset_id,
            library_folder_id=library_folder_id,
            name=name,
            description=description,
            tags=tuple(tags),
            usage_count=0,
            last_used_at=None,
            version=1,
            created_at=ts,
            updated_at=ts,
        )
        self.assets[asset.id] = asset
        return asset

    async def get_asset(
        self, asset_id: UUID, tenant_id: UUID, owner_user_id: UUID
    ) -> LibraryAsset | None:
        a = self.assets.get(asset_id)
        if a is None or a.tenant_id != tenant_id or a.owner_user_id != owner_user_id:
            return None
        if a.media_asset_id in self.hidden_media:
            return None
        return a

    async def list_assets(
        self,
        tenant_id: UUID,
        owner_user_id: UUID,
        *,
        folder_id: UUID | None,
        filter_by_folder: bool,
        tags: tuple[str, ...] | None,
        limit: int,
        after: tuple[datetime, UUID] | None = None,
    ) -> list[LibraryAsset]:
        rows = [
            a
            for a in self.assets.values()
            if a.tenant_id == tenant_id
            and a.owner_user_id == owner_user_id
            and a.media_asset_id not in self.hidden_media
        ]
        if filter_by_folder:
            rows = [a for a in rows if a.library_folder_id == folder_id]
        if tags:
            wanted = set(tags)
            rows = [a for a in rows if wanted & set(a.tags)]
        rows.sort(key=lambda a: (a.created_at, a.id), reverse=True)
        if after is not None:
            rows = [a for a in rows if (a.created_at, a.id) < after]
        return rows[:limit]

    async def update_asset(
        self,
        asset_id: UUID,
        tenant_id: UUID,
        owner_user_id: UUID,
        *,
        expected_version: int,
        changes: Mapping[str, Any],
    ) -> LibraryAsset | None:
        a = self.assets.get(asset_id)
        if a is None or a.tenant_id != tenant_id or a.owner_user_id != owner_user_id:
            return None
        if a.version != expected_version:
            return None
        normalized = dict(changes)
        if "tags" in normalized and normalized["tags"] is not None:
            normalized["tags"] = tuple(normalized["tags"])
        updated = replace(a, **normalized, version=a.version + 1, updated_at=self._next_ts())
        self.assets[asset_id] = updated
        return updated

    async def soft_delete_asset(self, asset_id: UUID, tenant_id: UUID, owner_user_id: UUID) -> bool:
        a = self.assets.get(asset_id)
        if a is None or a.tenant_id != tenant_id or a.owner_user_id != owner_user_id:
            return False
        del self.assets[asset_id]
        return True

    async def record_use(
        self,
        asset_id: UUID,
        tenant_id: UUID,
        owner_user_id: UUID,
        *,
        project_id: UUID,
    ) -> LibraryAsset | None:
        a = self.assets.get(asset_id)
        if a is None or a.tenant_id != tenant_id or a.owner_user_id != owner_user_id:
            return None
        updated = replace(
            a,
            usage_count=a.usage_count + 1,
            last_used_at=self._next_ts(),
            version=a.version + 1,
            updated_at=self._next_ts(),
        )
        self.assets[asset_id] = updated
        self.junction.add((asset_id, project_id))
        return updated


class FakeLibraryUnitOfWork(IUnitOfWork):
    """Minimal UoW exposing the ports the library use cases touch."""

    def __init__(
        self,
        *,
        library: FakeLibraryRepository | None = None,
        media: FakeMediaRepository | None = None,
        projects: FakeProjectRepository | None = None,
    ) -> None:
        self.library = cast(ILibraryRepository, library or FakeLibraryRepository())
        self.media = cast(IMediaRepository, media or FakeMediaRepository())
        self.projects = cast(IProjectRepository, projects or FakeProjectRepository())
        self.committed = False

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        return None
