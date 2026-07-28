"""SQLAlchemy implementation of ``ILibraryRepository`` (Slice α9.2 — Media Library).

The Media Library is the Asset Library reserved by ADR-0037 CR-8: ``library_assets`` /
``library_folders`` / ``library_asset_projects`` built **over** registered
``media_assets``. This adapter mirrors :class:`ProjectRepository` /
:class:`MediaRepository`:

* **Owner-scoped, live-only.** Every read/write filters ``tenant_id`` +
  ``owner_user_id`` (+ ``deleted_at IS NULL``) so another owner's rows are invisible
  (anti-enumeration).
* **Sibling over Media.** ``library_assets`` references a ``media_asset`` by id; this
  adapter never writes ``media_assets``. Reads (``get_asset`` / ``list_assets``) join
  ``media_assets`` and exclude entries whose underlying asset is soft-deleted (α9.2
  §7.2) so the library never surfaces a dangling entry.
* **OCC on assets.** ``update_asset`` is a version-fenced CAS (``library_assets``
  carries ``VersionMixin`` — ADR-0037 CR-8), hand-setting ``version = version + 1`` so
  the guarded ``tg_library_assets_biu_version_bump`` trigger no-ops (net +1), identical
  to :meth:`ProjectRepository.update_owned`. Folders are last-writer-wins (no version).
* **Uniqueness backstops.** ``uq_library_folders_parent_folder_id_name`` (live rows) and
  ``uq_library_assets_media_asset_id`` surface as ``ConflictError`` → ``409`` — the DB
  constraint is the race-safe arbiter (no pre-check SELECT).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.interfaces.repositories import ILibraryRepository
from app.core.errors import ConflictError
from app.domain.library.library_asset import LibraryAsset as LibraryAssetEntity
from app.domain.library.library_folder import LibraryFolder as LibraryFolderEntity
from app.infrastructure.db.models.media import (
    LibraryAsset as LibraryAssetRow,
    LibraryAssetProject as LibraryAssetProjectRow,
    LibraryFolder as LibraryFolderRow,
    MediaAsset as MediaAssetRow,
)


class LibraryRepository(ILibraryRepository):
    """Media Library persistence adapter. Soft-deleted rows are excluded."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ---- folders -------------------------------------------------------

    async def add_folder(
        self,
        *,
        tenant_id: UUID,
        owner_user_id: UUID,
        parent_folder_id: UUID | None,
        name: str,
    ) -> LibraryFolderEntity:
        row = LibraryFolderRow(
            id=uuid4(),
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            parent_folder_id=parent_folder_id,
            name=name,
        )
        self._session.add(row)
        try:
            await self._session.flush()
        except IntegrityError as e:
            # 23505 on the live-row partial-unique index
            # ``uq_library_folders_parent_folder_id_name`` → a folder with this
            # name already exists under the same parent for this owner.
            raise ConflictError(
                "library folder already exists",
                details={"constraint": _extract_constraint_name(e) or "unknown"},
            ) from e
        await self._session.refresh(row)
        return _folder_to_entity(row)

    async def get_folder(
        self, folder_id: UUID, tenant_id: UUID, owner_user_id: UUID
    ) -> LibraryFolderEntity | None:
        stmt = (
            select(LibraryFolderRow)
            .where(LibraryFolderRow.id == folder_id)
            .where(LibraryFolderRow.tenant_id == tenant_id)
            .where(LibraryFolderRow.owner_user_id == owner_user_id)
            .where(LibraryFolderRow.deleted_at.is_(None))
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _folder_to_entity(row) if row is not None else None

    async def folder_name_conflicts(
        self,
        *,
        tenant_id: UUID,
        owner_user_id: UUID,
        parent_folder_id: UUID | None,
        name: str,
        exclude_folder_id: UUID | None = None,
    ) -> bool:
        stmt = (
            select(LibraryFolderRow.id)
            .where(LibraryFolderRow.tenant_id == tenant_id)
            .where(LibraryFolderRow.owner_user_id == owner_user_id)
            .where(LibraryFolderRow.name == name)
            .where(LibraryFolderRow.deleted_at.is_(None))
        )
        if parent_folder_id is None:
            stmt = stmt.where(LibraryFolderRow.parent_folder_id.is_(None))
        else:
            stmt = stmt.where(LibraryFolderRow.parent_folder_id == parent_folder_id)
        if exclude_folder_id is not None:
            stmt = stmt.where(LibraryFolderRow.id != exclude_folder_id)
        stmt = stmt.limit(1)
        return (await self._session.execute(stmt)).scalar_one_or_none() is not None

    async def list_folders(
        self,
        tenant_id: UUID,
        owner_user_id: UUID,
        *,
        parent_folder_id: UUID | None,
        filter_by_parent: bool,
        limit: int,
        after: tuple[datetime, UUID] | None = None,
    ) -> list[LibraryFolderEntity]:
        stmt = (
            select(LibraryFolderRow)
            .where(LibraryFolderRow.tenant_id == tenant_id)
            .where(LibraryFolderRow.owner_user_id == owner_user_id)
            .where(LibraryFolderRow.deleted_at.is_(None))
        )
        if filter_by_parent:
            if parent_folder_id is None:
                stmt = stmt.where(LibraryFolderRow.parent_folder_id.is_(None))
            else:
                stmt = stmt.where(LibraryFolderRow.parent_folder_id == parent_folder_id)
        if after is not None:
            after_created_at, after_id = after
            stmt = stmt.where(
                tuple_(LibraryFolderRow.created_at, LibraryFolderRow.id)
                < (after_created_at, after_id)
            )
        stmt = stmt.order_by(LibraryFolderRow.created_at.desc(), LibraryFolderRow.id.desc()).limit(
            limit
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_folder_to_entity(r) for r in rows]

    async def update_folder(
        self,
        folder_id: UUID,
        tenant_id: UUID,
        owner_user_id: UUID,
        changes: Mapping[str, Any],
    ) -> LibraryFolderEntity | None:
        # Last-writer-wins (no version column). ``updated_at`` is trigger-owned
        # (``tg_library_folders_biu_touch_updated_at``); we never hand-set it.
        assert changes, "update_folder requires at least one changed column"
        upd = (
            update(LibraryFolderRow)
            .where(LibraryFolderRow.id == folder_id)
            .where(LibraryFolderRow.tenant_id == tenant_id)
            .where(LibraryFolderRow.owner_user_id == owner_user_id)
            .where(LibraryFolderRow.deleted_at.is_(None))
            .values(**changes)
            .returning(LibraryFolderRow)
        )
        try:
            row = (await self._session.execute(upd)).scalar_one_or_none()
        except IntegrityError as e:
            raise ConflictError(
                "library folder already exists",
                details={"constraint": _extract_constraint_name(e) or "unknown"},
            ) from e
        return _folder_to_entity(row) if row is not None else None

    async def soft_delete_folder(
        self, folder_id: UUID, tenant_id: UUID, owner_user_id: UUID
    ) -> bool:
        stmt = (
            update(LibraryFolderRow)
            .where(LibraryFolderRow.id == folder_id)
            .where(LibraryFolderRow.tenant_id == tenant_id)
            .where(LibraryFolderRow.owner_user_id == owner_user_id)
            .where(LibraryFolderRow.deleted_at.is_(None))
            .values(deleted_at=func.now())
            .returning(LibraryFolderRow.id)
        )
        marked = (await self._session.execute(stmt)).scalar_one_or_none()
        return marked is not None

    async def folder_has_children(
        self, folder_id: UUID, tenant_id: UUID, owner_user_id: UUID
    ) -> bool:
        stmt = (
            select(LibraryFolderRow.id)
            .where(LibraryFolderRow.parent_folder_id == folder_id)
            .where(LibraryFolderRow.tenant_id == tenant_id)
            .where(LibraryFolderRow.owner_user_id == owner_user_id)
            .where(LibraryFolderRow.deleted_at.is_(None))
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none() is not None

    async def detach_assets_from_folder(
        self, folder_id: UUID, tenant_id: UUID, owner_user_id: UUID
    ) -> int:
        # Detach (not delete) contained assets so folder deletion never orphans
        # a library entry (α9.2 §7.4). Hand-set ``version = version + 1`` so the
        # guarded version-bump trigger no-ops (net +1) — a folder move is a real
        # edit to each asset's OCC token.
        upd = (
            update(LibraryAssetRow)
            .where(LibraryAssetRow.library_folder_id == folder_id)
            .where(LibraryAssetRow.tenant_id == tenant_id)
            .where(LibraryAssetRow.owner_user_id == owner_user_id)
            .where(LibraryAssetRow.deleted_at.is_(None))
            .values(
                library_folder_id=None,
                version=LibraryAssetRow.version + 1,
                updated_at=func.now(),
            )
            .returning(LibraryAssetRow.id)
        )
        rows = (await self._session.execute(upd)).scalars().all()
        return len(rows)

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
    ) -> LibraryAssetEntity:
        row = LibraryAssetRow(
            id=uuid4(),
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            media_asset_id=media_asset_id,
            library_folder_id=library_folder_id,
            name=name,
            description=description,
            tags=list(tags),
        )
        self._session.add(row)
        try:
            await self._session.flush()
        except IntegrityError as e:
            # 23505 on ``uq_library_assets_media_asset_id`` → this media asset is
            # already in the library. The unique constraint is the race-safe
            # backstop behind the use case's pre-check.
            raise ConflictError(
                "media asset already in library",
                details={"constraint": _extract_constraint_name(e) or "unknown"},
            ) from e
        await self._session.refresh(row)
        return _asset_to_entity(row)

    async def get_asset(
        self, asset_id: UUID, tenant_id: UUID, owner_user_id: UUID
    ) -> LibraryAssetEntity | None:
        # Join media_assets and require it live — a library entry whose underlying
        # media was soft-deleted is treated as absent (α9.2 §7.2).
        stmt = (
            select(LibraryAssetRow)
            .join(MediaAssetRow, LibraryAssetRow.media_asset_id == MediaAssetRow.id)
            .where(LibraryAssetRow.id == asset_id)
            .where(LibraryAssetRow.tenant_id == tenant_id)
            .where(LibraryAssetRow.owner_user_id == owner_user_id)
            .where(LibraryAssetRow.deleted_at.is_(None))
            .where(MediaAssetRow.deleted_at.is_(None))
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _asset_to_entity(row) if row is not None else None

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
    ) -> list[LibraryAssetEntity]:
        stmt = (
            select(LibraryAssetRow)
            .join(MediaAssetRow, LibraryAssetRow.media_asset_id == MediaAssetRow.id)
            .where(LibraryAssetRow.tenant_id == tenant_id)
            .where(LibraryAssetRow.owner_user_id == owner_user_id)
            .where(LibraryAssetRow.deleted_at.is_(None))
            .where(MediaAssetRow.deleted_at.is_(None))
        )
        if filter_by_folder:
            if folder_id is None:
                stmt = stmt.where(LibraryAssetRow.library_folder_id.is_(None))
            else:
                stmt = stmt.where(LibraryAssetRow.library_folder_id == folder_id)
        if tags:
            # ANY-of match over the ``ix_library_assets_tags_gin`` index
            # (array overlap ``tags && :tags``).
            stmt = stmt.where(LibraryAssetRow.tags.overlap(list(tags)))
        if after is not None:
            after_created_at, after_id = after
            stmt = stmt.where(
                tuple_(LibraryAssetRow.created_at, LibraryAssetRow.id)
                < (after_created_at, after_id)
            )
        stmt = stmt.order_by(LibraryAssetRow.created_at.desc(), LibraryAssetRow.id.desc()).limit(
            limit
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_asset_to_entity(r) for r in rows]

    async def update_asset(
        self,
        asset_id: UUID,
        tenant_id: UUID,
        owner_user_id: UUID,
        *,
        expected_version: int,
        changes: Mapping[str, Any],
    ) -> LibraryAssetEntity | None:
        # Version-fenced CAS — mirrors ProjectRepository.update_owned. Hand-set
        # ``version + 1`` so the guarded ``tg_library_assets_biu_version_bump``
        # trigger no-ops (net +1). ``tags`` (when present) arrives as a tuple →
        # coerce to list for the ARRAY column.
        assert changes, "update_asset requires at least one changed column"
        values = dict(changes)
        if "tags" in values and values["tags"] is not None:
            values["tags"] = list(values["tags"])
        upd = (
            update(LibraryAssetRow)
            .where(LibraryAssetRow.id == asset_id)
            .where(LibraryAssetRow.tenant_id == tenant_id)
            .where(LibraryAssetRow.owner_user_id == owner_user_id)
            .where(LibraryAssetRow.version == expected_version)
            .where(LibraryAssetRow.deleted_at.is_(None))
            .values(**values, version=LibraryAssetRow.version + 1, updated_at=func.now())
            .returning(LibraryAssetRow)
        )
        row = (await self._session.execute(upd)).scalar_one_or_none()
        return _asset_to_entity(row) if row is not None else None

    async def soft_delete_asset(self, asset_id: UUID, tenant_id: UUID, owner_user_id: UUID) -> bool:
        stmt = (
            update(LibraryAssetRow)
            .where(LibraryAssetRow.id == asset_id)
            .where(LibraryAssetRow.tenant_id == tenant_id)
            .where(LibraryAssetRow.owner_user_id == owner_user_id)
            .where(LibraryAssetRow.deleted_at.is_(None))
            .values(deleted_at=func.now())
            .returning(LibraryAssetRow.id)
        )
        marked = (await self._session.execute(stmt)).scalar_one_or_none()
        return marked is not None

    async def record_use(
        self,
        asset_id: UUID,
        tenant_id: UUID,
        owner_user_id: UUID,
        *,
        project_id: UUID,
    ) -> LibraryAssetEntity | None:
        # Advance reuse counters on the owner's live asset. The version-bump
        # trigger fires (usage is a genuine mutation) → the OCC token advances by
        # 1; ``updated_at`` is trigger-owned. Returns None if no live owned row
        # matched → 404 at the use case.
        upd = (
            update(LibraryAssetRow)
            .where(LibraryAssetRow.id == asset_id)
            .where(LibraryAssetRow.tenant_id == tenant_id)
            .where(LibraryAssetRow.owner_user_id == owner_user_id)
            .where(LibraryAssetRow.deleted_at.is_(None))
            .values(
                usage_count=LibraryAssetRow.usage_count + 1,
                last_used_at=func.now(),
            )
            .returning(LibraryAssetRow)
        )
        row = (await self._session.execute(upd)).scalar_one_or_none()
        if row is None:
            return None
        # Idempotent per (asset, project): the junction PK collapses repeats.
        ins = (
            pg_insert(LibraryAssetProjectRow)
            .values(library_asset_id=asset_id, project_id=project_id)
            .on_conflict_do_nothing(index_elements=["library_asset_id", "project_id"])
        )
        await self._session.execute(ins)
        return _asset_to_entity(row)


def _folder_to_entity(row: LibraryFolderRow) -> LibraryFolderEntity:
    return LibraryFolderEntity(
        id=row.id,
        tenant_id=row.tenant_id,
        owner_user_id=row.owner_user_id,
        parent_folder_id=row.parent_folder_id,
        name=row.name,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _asset_to_entity(row: LibraryAssetRow) -> LibraryAssetEntity:
    return LibraryAssetEntity(
        id=row.id,
        tenant_id=row.tenant_id,
        owner_user_id=row.owner_user_id,
        media_asset_id=row.media_asset_id,
        library_folder_id=row.library_folder_id,
        name=row.name,
        description=row.description,
        tags=tuple(row.tags),
        usage_count=row.usage_count,
        last_used_at=row.last_used_at,
        version=row.version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _extract_constraint_name(exc: IntegrityError) -> str | None:
    """Best-effort extraction of the failed constraint name from psycopg.

    Mirrors the helper in ``project_repository.py`` / ``media_repository.py``.
    """
    orig = getattr(exc, "orig", None)
    diag = getattr(orig, "diag", None)
    name = getattr(diag, "constraint_name", None)
    return str(name) if name else None
