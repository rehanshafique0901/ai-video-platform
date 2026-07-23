"""``EnrichGeneratedMedia`` — derive a thumbnail + scalar metadata for a video (α8.4c).

The worker body behind :class:`MediaEnrichmentWorker`. For a single generated video
``MediaAsset`` it:

1. acquires the ``media_enrichment:<id>`` lease (exactly-once, mirrors the render/
   completion workers);
2. re-reads the parent asset (must still be a live, un-enriched generated video);
3. **materializes** the source bytes from ``IObjectStorage``;
4. extracts one thumbnail + probes bitrate via the neutral ``IThumbnailer``;
5. stores the thumbnail under a **deterministic** key and registers a derived
   ``MediaAsset(kind="image", source="generated")`` cross-linked to the parent;
6. augments the parent's ``source_metadata`` with an ``enrichment`` marker
   (``{thumbnail_media_asset_id, bitrate, enriched_at}``) — which also removes it
   from the claim scan.

Invariants:
* **W8.4c.1** — observational + downstream: it derives artifacts and augments the
  owning asset's ``source_metadata``, but never mutates orchestration state,
  checkpoints, provider state, workflow/render lifecycle, Timeline definitions, or
  renderer inputs.
* **W8.4c.2** — consumes only ``MediaAsset`` bytes + identifiers.
* **W8.4c.3** — a **pure function of the parent** ``MediaAsset``: it reads only the
  parent (by id) + its bytes; it never touches render-job history, checkpoints,
  Timeline, or provider payloads. Thumbnails are reproducible from the parent alone.

Idempotency: the thumbnail key is deterministic in the parent id, so a re-run writes
identical bytes and the ``media_assets`` uniqueness raises ``ConflictError`` — caught
and resolved to the existing derived asset; the parent ``update_owned`` is naturally
idempotent. FFmpeg + all I/O happen **outside** any DB transaction.
"""

from __future__ import annotations

import asyncio
import hashlib
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import structlog

from app.application.interfaces.object_storage import IObjectStorage, ObjectStorageError
from app.application.interfaces.thumbnailer import IThumbnailer, ThumbnailError
from app.application.interfaces.unit_of_work import IUnitOfWork
from app.core.errors import ConflictError
from app.domain.media.media_asset import MediaAsset

_LOGGER = structlog.get_logger(__name__)

_ENRICHMENT_KEY = "enrichment"
_DEFAULT_LEASE = timedelta(seconds=300)
_DEFAULT_THUMB_AT = 1.0


@dataclass(frozen=True, slots=True)
class EnrichGeneratedMediaResult:
    """Outcome of one enrichment pass over a single asset."""

    media_asset_id: UUID
    status: str  # "enriched" | "skipped" | "noop" | "failed"
    thumbnail_media_asset_id: UUID | None = None
    reason: str | None = None


class EnrichGeneratedMedia:
    """Thumbnail + metadata enrichment for one generated video asset (leased)."""

    def __init__(
        self,
        uow: IUnitOfWork,
        storage: IObjectStorage,
        thumbnailer: IThumbnailer,
        *,
        thumbnail_at_seconds: float = _DEFAULT_THUMB_AT,
        workspace_dir: str | None = None,
        owner: str | None = None,
        lease: timedelta = _DEFAULT_LEASE,
    ) -> None:
        self._uow = uow
        self._storage = storage
        self._thumbnailer = thumbnailer
        self._thumbnail_at = thumbnail_at_seconds
        self._workspace_dir = workspace_dir
        self._owner = owner or f"enrichment-worker:{uuid4()}"
        self._lease = lease

    async def execute(self, *, asset: MediaAsset) -> EnrichGeneratedMediaResult:
        lock_key = f"media_enrichment:{asset.id}"
        async with self._uow:
            lease = await self._uow.locks.acquire(
                key=lock_key, owner=self._owner, lease=self._lease
            )
            await self._uow.commit()
        if lease is None:
            _LOGGER.info("enrichment.locked", media_asset_id=str(asset.id))
            return EnrichGeneratedMediaResult(
                media_asset_id=asset.id, status="skipped", reason="locked"
            )

        try:
            # Phase 1 — re-read the parent (W8.4c.3: parent is the sole source of truth).
            async with self._uow:
                fresh = await self._uow.media.get_owned(
                    asset.id, asset.tenant_id, asset.owner_user_id
                )
                if fresh is None or fresh.kind != "video" or fresh.source != "generated":
                    return EnrichGeneratedMediaResult(
                        media_asset_id=asset.id, status="noop", reason="not_target"
                    )
                if (
                    isinstance(fresh.source_metadata, dict)
                    and _ENRICHMENT_KEY in fresh.source_metadata
                ):
                    return EnrichGeneratedMediaResult(
                        media_asset_id=asset.id, status="noop", reason="already_enriched"
                    )
                if (
                    fresh.storage_backend != self._storage.backend
                    or fresh.storage_bucket != self._storage.bucket
                ):
                    return EnrichGeneratedMediaResult(
                        media_asset_id=asset.id, status="noop", reason="unsupported_storage"
                    )

            try:
                return await self._enrich(fresh)
            except (ThumbnailError, ObjectStorageError) as exc:
                # No job row to settle; leave un-enriched so a later scan retries a
                # transient failure. Permanent-failure backoff is α8.4d.
                _LOGGER.warning(
                    "enrichment.failed", media_asset_id=str(asset.id), error=str(exc)[:500]
                )
                return EnrichGeneratedMediaResult(
                    media_asset_id=asset.id, status="failed", reason=str(exc)[:500]
                )
        finally:
            async with self._uow:
                await self._uow.locks.release(lease)
                await self._uow.commit()

    async def _enrich(self, parent: MediaAsset) -> EnrichGeneratedMediaResult:
        # Phase 2 — materialize + thumbnail, OUTSIDE any DB transaction.
        data = await self._storage.get(key=parent.storage_key)
        with tempfile.TemporaryDirectory(dir=self._workspace_dir) as tmp:
            src = Path(tmp) / "source"
            await asyncio.to_thread(src.write_bytes, data)
            thumb = await self._thumbnailer.thumbnail(
                source_path=str(src), at_seconds=self._thumbnail_at
            )

        thumb_key = f"thumbnails/{parent.tenant_id}/{parent.id}.jpg"
        stored = await self._storage.put(
            key=thumb_key, data=thumb.image, content_type=thumb.mime_type
        )
        checksum = hashlib.sha256(thumb.image).digest()

        # Phase 3 — register derived thumbnail + mark the parent enriched.
        async with self._uow:
            thumbnail_id = await self._register_thumbnail(
                parent=parent,
                stored_backend=stored.backend,
                stored_bucket=stored.bucket,
                stored_key=stored.key,
                mime_type=thumb.mime_type,
                size_bytes=len(thumb.image),
                checksum=checksum,
                width=thumb.width,
                height=thumb.height,
            )
            enrichment = {
                "thumbnail_media_asset_id": str(thumbnail_id),
                "bitrate": thumb.source_bitrate,
                "enriched_at": datetime.now(UTC).isoformat(),
            }
            base = dict(parent.source_metadata) if isinstance(parent.source_metadata, dict) else {}
            base[_ENRICHMENT_KEY] = enrichment
            await self._uow.media.update_owned(
                parent.id,
                parent.tenant_id,
                parent.owner_user_id,
                {"source_metadata": base},
            )
            await self._uow.commit()

        _LOGGER.info(
            "enrichment.done",
            media_asset_id=str(parent.id),
            thumbnail_media_asset_id=str(thumbnail_id),
        )
        return EnrichGeneratedMediaResult(
            media_asset_id=parent.id,
            status="enriched",
            thumbnail_media_asset_id=thumbnail_id,
        )

    async def _register_thumbnail(
        self,
        *,
        parent: MediaAsset,
        stored_backend: str,
        stored_bucket: str,
        stored_key: str,
        mime_type: str,
        size_bytes: int,
        checksum: bytes,
        width: int | None,
        height: int | None,
    ) -> UUID:
        try:
            thumb = await self._uow.media.add(
                tenant_id=parent.tenant_id,
                owner_user_id=parent.owner_user_id,
                kind="image",
                source="generated",
                storage_backend=stored_backend,
                storage_bucket=stored_bucket,
                storage_key=stored_key,
                mime_type=mime_type,
                size_bytes=size_bytes,
                checksum_sha256=checksum,
                project_id=parent.project_id,
                scene_id=None,
                prompt_id=None,
                model_id=None,
                provider=None,
                width=width,
                height=height,
                duration_seconds=None,
                source_metadata={
                    "origin": "thumbnail",
                    "parent_media_asset_id": str(parent.id),
                },
            )
            return thumb.id
        except ConflictError:
            existing = await self._uow.media.get_by_storage_coords(
                storage_backend=stored_backend,
                storage_bucket=stored_bucket,
                storage_key=stored_key,
            )
            if existing is None:  # pragma: no cover - constraint says it must exist
                raise
            return existing.id
