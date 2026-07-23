"""SQLAlchemy implementation of ``IMediaRepository`` (Slice α6.2).

Media assets are **generation outputs** — registered pointers to concrete
stored objects (``MEDIA_AGGREGATE.md`` / ADR-0037). This adapter is close in
shape to the prompt repository (no ordering key, **no version column**, **no
per-row optimistic-concurrency fence** — mutations are last-writer-wins and do
**not** bump ``projects.version``), with two α6.2-specific twists:

* **Owner-scoped, not project-scoped.** ``media_assets`` carries its own
  ``tenant_id`` + ``owner_user_id``; every read/write filters on both (plus
  ``deleted_at IS NULL``) so another owner's asset is invisible
  (anti-enumeration). ``project_id`` is an optional link/filter, never the
  access key.
* **Storage identity is unique.** ``(storage_backend, storage_bucket,
  storage_key)`` is a UNIQUE constraint; ``add`` maps its violation to
  ``ConflictError`` (the use case surfaces ``409``) — the DB constraint is the
  race-safe backstop behind the use case's pre-check.

:meth:`model_is_linkable` is the app-level gate for a client-supplied
``model_id`` (exists + not ``retired``); the use case calls it before writing
and maps ``False`` to ``422`` (α6.2 Q5). ``updated_at`` is trigger-owned; the
repository never hand-sets it (there is no ``version`` to co-set).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.interfaces.repositories import IMediaRepository
from app.core.errors import ConflictError
from app.domain.media.media_asset import MediaAsset as MediaAssetEntity
from app.infrastructure.db.models.ai_models import AIModel as AIModelRow
from app.infrastructure.db.models.media import MediaAsset as MediaAssetRow


class MediaRepository(IMediaRepository):
    """Media persistence adapter. Soft-deleted rows are excluded."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ---- create --------------------------------------------------------

    async def add(
        self,
        *,
        tenant_id: UUID,
        owner_user_id: UUID,
        kind: str,
        source: str,
        storage_backend: str,
        storage_bucket: str,
        storage_key: str,
        mime_type: str,
        size_bytes: int,
        checksum_sha256: bytes,
        project_id: UUID | None,
        scene_id: UUID | None,
        prompt_id: UUID | None,
        model_id: UUID | None,
        provider: str | None,
        width: int | None,
        height: int | None,
        duration_seconds: float | None,
        source_metadata: dict[str, Any],
    ) -> MediaAssetEntity:
        row = MediaAssetRow(
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            kind=kind,
            source=source,
            storage_backend=storage_backend,
            storage_bucket=storage_bucket,
            storage_key=storage_key,
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
            source_metadata=source_metadata,
        )
        self._session.add(row)
        try:
            await self._session.flush()
        except IntegrityError as e:
            # 23505 on ``uq_media_assets_storage_backend_storage_bucket_storage_key``
            # → an asset with these storage coordinates already exists. Surface as
            # ConflictError so the use case maps it to 409 (α6.2 Q6/F4). The unique
            # constraint is the race-safe backstop behind the use case's pre-check.
            raise ConflictError(
                "media asset already exists for these storage coordinates",
                details={"constraint": _extract_constraint_name(e) or "unknown"},
            ) from e
        await self._session.refresh(row)
        return _row_to_entity(row)

    # ---- reads ---------------------------------------------------------

    async def list_owned(
        self,
        tenant_id: UUID,
        owner_user_id: UUID,
        *,
        kind: str | None = None,
        source: str | None = None,
        project_id: UUID | None = None,
        scene_id: UUID | None = None,
    ) -> list[MediaAssetEntity]:
        stmt = (
            select(MediaAssetRow)
            .where(MediaAssetRow.tenant_id == tenant_id)
            .where(MediaAssetRow.owner_user_id == owner_user_id)
            .where(MediaAssetRow.deleted_at.is_(None))
        )
        if kind is not None:
            stmt = stmt.where(MediaAssetRow.kind == kind)
        if source is not None:
            stmt = stmt.where(MediaAssetRow.source == source)
        if project_id is not None:
            stmt = stmt.where(MediaAssetRow.project_id == project_id)
        if scene_id is not None:
            stmt = stmt.where(MediaAssetRow.scene_id == scene_id)
        # Total order (created_at, id) DESC → stable newest-first, no dupes/skips
        # under timestamp ties (mirrors the prompt/project repositories).
        stmt = stmt.order_by(MediaAssetRow.created_at.desc(), MediaAssetRow.id.desc())
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_row_to_entity(r) for r in rows]

    async def get_owned(
        self,
        media_id: UUID,
        tenant_id: UUID,
        owner_user_id: UUID,
    ) -> MediaAssetEntity | None:
        stmt = (
            select(MediaAssetRow)
            .where(MediaAssetRow.id == media_id)
            .where(MediaAssetRow.tenant_id == tenant_id)
            .where(MediaAssetRow.owner_user_id == owner_user_id)
            .where(MediaAssetRow.deleted_at.is_(None))
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _row_to_entity(row) if row is not None else None

    async def list_unenriched_generated_videos(self, *, limit: int) -> list[MediaAssetEntity]:
        # α8.4c enrichment claim scan: live generated videos without the
        # ``enrichment`` marker in source_metadata (the JSONB ``?`` key test), oldest
        # first. Owner-agnostic (server-side worker); the set shrinks as assets are
        # marked enriched. Total order (created_at, id) ASC.
        stmt = (
            select(MediaAssetRow)
            .where(MediaAssetRow.kind == "video")
            .where(MediaAssetRow.source == "generated")
            .where(MediaAssetRow.deleted_at.is_(None))
            .where(~MediaAssetRow.source_metadata.has_key("enrichment"))
            .order_by(MediaAssetRow.created_at.asc(), MediaAssetRow.id.asc())
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_row_to_entity(r) for r in rows]

    async def get_by_storage_coords(
        self,
        *,
        storage_backend: str,
        storage_bucket: str,
        storage_key: str,
    ) -> MediaAssetEntity | None:
        # Owner-agnostic idempotent-recovery lookup (α8.4b): the physical-object
        # columns are immutable and unique per deterministic-key artifact, so this
        # returns the single live asset a deterministic producer registered there.
        stmt = (
            select(MediaAssetRow)
            .where(MediaAssetRow.storage_backend == storage_backend)
            .where(MediaAssetRow.storage_bucket == storage_bucket)
            .where(MediaAssetRow.storage_key == storage_key)
            .where(MediaAssetRow.deleted_at.is_(None))
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _row_to_entity(row) if row is not None else None

    # ---- mutations -----------------------------------------------------

    async def update_owned(
        self,
        media_id: UUID,
        tenant_id: UUID,
        owner_user_id: UUID,
        changes: Mapping[str, Any],
    ) -> MediaAssetEntity | None:
        # No version fence (ADR-0037 — media has no OCC column). Scoped to the
        # owner + live rows. ``updated_at`` is trigger-owned; we do not hand-set
        # it (there is no ``version`` to co-bump). Only mutable columns reach
        # here (the use case rejects immutable fields with 422 upstream).
        assert changes, "update_owned requires at least one changed column"
        upd = (
            update(MediaAssetRow)
            .where(MediaAssetRow.id == media_id)
            .where(MediaAssetRow.tenant_id == tenant_id)
            .where(MediaAssetRow.owner_user_id == owner_user_id)
            .where(MediaAssetRow.deleted_at.is_(None))
            .values(**changes)
            .returning(MediaAssetRow)
        )
        row = (await self._session.execute(upd)).scalar_one_or_none()
        return _row_to_entity(row) if row is not None else None

    async def soft_delete_owned(
        self,
        media_id: UUID,
        tenant_id: UUID,
        owner_user_id: UUID,
    ) -> bool:
        stmt = (
            update(MediaAssetRow)
            .where(MediaAssetRow.id == media_id)
            .where(MediaAssetRow.tenant_id == tenant_id)
            .where(MediaAssetRow.owner_user_id == owner_user_id)
            .where(MediaAssetRow.deleted_at.is_(None))
            .values(deleted_at=func.now())
            .returning(MediaAssetRow.id)
        )
        marked = (await self._session.execute(stmt)).scalar_one_or_none()
        return marked is not None

    # ---- validation helper --------------------------------------------

    async def model_is_linkable(self, model_id: UUID) -> bool:
        # Linkable iff the ai_models row exists and is not 'retired' (α6.2 Q5,
        # mirrors α6.1). ai_models is a system registry with no soft-delete.
        stmt = (
            select(AIModelRow.id)
            .where(AIModelRow.id == model_id)
            .where(AIModelRow.status != "retired")
        )
        return (await self._session.execute(stmt)).scalar_one_or_none() is not None


def _row_to_entity(row: MediaAssetRow) -> MediaAssetEntity:
    return MediaAssetEntity(
        id=row.id,
        tenant_id=row.tenant_id,
        owner_user_id=row.owner_user_id,
        kind=row.kind,
        project_id=row.project_id,
        scene_id=row.scene_id,
        prompt_id=row.prompt_id,
        model_id=row.model_id,
        provider=row.provider,
        storage_backend=row.storage_backend,
        storage_bucket=row.storage_bucket,
        storage_key=row.storage_key,
        mime_type=row.mime_type,
        size_bytes=row.size_bytes,
        width=row.width,
        height=row.height,
        # ``duration_seconds`` is ``Numeric(10,3)`` → psycopg returns a
        # ``Decimal``; the domain models it as ``float | None``.
        duration_seconds=(
            float(row.duration_seconds) if row.duration_seconds is not None else None
        ),
        checksum_sha256=bytes(row.checksum_sha256),
        source=row.source,
        source_metadata=dict(row.source_metadata),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _extract_constraint_name(exc: IntegrityError) -> str | None:
    """Best-effort extraction of the failed constraint name from psycopg.

    Mirrors the helper in ``project_repository.py`` / ``user_repository.py``.
    Duplicated rather than shared because a one-line ``getattr`` chain is
    cheaper to read inline than a shared infra-utility import.
    """
    orig = getattr(exc, "orig", None)
    diag = getattr(orig, "diag", None)
    name = getattr(diag, "constraint_name", None)
    return str(name) if name else None
