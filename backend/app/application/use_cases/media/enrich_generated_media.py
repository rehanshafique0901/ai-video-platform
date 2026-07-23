"""``EnrichGeneratedMedia`` — derive preview artifacts + metadata for a video (α8.4c/d).

The worker body behind :class:`MediaEnrichmentWorker`. For a single generated video
``MediaAsset`` it runs a **pipeline of independent enrichers** (α8.4d): thumbnail
(α8.4c) + preview clip + GIF + waveform. Each enricher is a pure `parent (+ bytes) →
one derived artifact` transform; the use case owns leasing, a single materialization,
registration, and the **versioned** enrichment marker.

Flow:

1. acquire the ``media_enrichment:<id>`` lease (exactly-once; mirrors render/completion);
2. re-read the parent (a live, **primary** generated video whose enrichment version is
   below the current target — W8.4d.1: derived assets are never enriched);
3. **materialize** the source bytes once from ``IObjectStorage``;
4. run each applicable enricher (FFmpeg, **outside any DB transaction**), storing each
   derived artifact under its **deterministic** key;
5. register each derived ``MediaAsset`` (idempotent) + augment the parent's
   ``source_metadata.enrichment`` with the ids, scalars, and — iff every applicable
   enricher succeeded — ``version = CURRENT_ENRICHMENT_VERSION`` (which drops the asset
   out of the claim scan).

Invariants: **W8.4c.1** (observational + downstream), **W8.4c.2** (consumes only
`MediaAsset` bytes + ids), **W8.4c.3** (pure function of the parent), **W8.4d.1**
(derived media is terminal). Idempotent via deterministic keys + `ConflictError`
recovery. Per-artifact failure isolation: a transient enricher/storage failure leaves
the version un-bumped so a later pass retries (recovering already-registered artifacts).
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID, uuid4

import structlog

from app.application.interfaces.gif_previewer import GifPreviewError
from app.application.interfaces.object_storage import (
    IObjectStorage,
    ObjectStorageError,
    StoredObject,
)
from app.application.interfaces.preview_clipper import PreviewClipError
from app.application.interfaces.thumbnailer import ThumbnailError
from app.application.interfaces.unit_of_work import IUnitOfWork
from app.application.interfaces.waveform_renderer import WaveformError
from app.application.use_cases.media.enrichers.base import DerivedArtifact, Enricher
from app.core.errors import ConflictError
from app.domain.media.media_asset import MediaAsset

_LOGGER = structlog.get_logger(__name__)

# Bump when the set of derived artifacts changes so already-enriched assets are
# re-claimed and backfilled (α8.4d Fork D). α8.4c markers carry no version → 0.
#   1 = thumbnail (α8.4c)   2 = + preview + gif + waveform (α8.4d)
CURRENT_ENRICHMENT_VERSION = 2

_ENRICHMENT_KEY = "enrichment"
_PARENT_KEY = "parent_media_asset_id"
_DEFAULT_LEASE = timedelta(seconds=300)

# Neutral per-port errors an enricher may raise on a genuine engine failure.
_ENRICH_ERRORS = (ThumbnailError, PreviewClipError, GifPreviewError, WaveformError)


@dataclass(frozen=True, slots=True)
class EnrichGeneratedMediaResult:
    """Outcome of one enrichment pass over a single asset."""

    media_asset_id: UUID
    status: str  # "enriched" | "partial" | "failed" | "noop" | "skipped"
    derived_media_ids: dict[str, UUID] = field(default_factory=dict)  # origin -> id
    failed: tuple[str, ...] = ()
    reason: str | None = None


class EnrichGeneratedMedia:
    """Derived-preview enrichment for one generated video asset (leased pipeline)."""

    def __init__(
        self,
        uow: IUnitOfWork,
        storage: IObjectStorage,
        enrichers: Sequence[Enricher],
        *,
        workspace_dir: str | None = None,
        owner: str | None = None,
        lease: timedelta = _DEFAULT_LEASE,
    ) -> None:
        self._uow = uow
        self._storage = storage
        self._enrichers = list(enrichers)
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
                # W8.4d.1 — derived media is terminal; never enrich a derived asset.
                if isinstance(fresh.source_metadata, dict) and _PARENT_KEY in fresh.source_metadata:
                    return EnrichGeneratedMediaResult(
                        media_asset_id=asset.id, status="noop", reason="derived"
                    )
                if _marker_version(fresh.source_metadata) >= CURRENT_ENRICHMENT_VERSION:
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

            return await self._run_pipeline(fresh)
        finally:
            async with self._uow:
                await self._uow.locks.release(lease)
                await self._uow.commit()

    async def _run_pipeline(self, parent: MediaAsset) -> EnrichGeneratedMediaResult:
        # Phase 2 — materialize once + run each enricher, OUTSIDE any DB transaction.
        try:
            data = await self._storage.get(key=parent.storage_key)
        except ObjectStorageError as exc:
            _LOGGER.warning(
                "enrichment.materialize_failed", media_asset_id=str(parent.id), error=str(exc)[:500]
            )
            return EnrichGeneratedMediaResult(
                media_asset_id=parent.id, status="failed", reason=str(exc)[:500]
            )

        produced: list[tuple[DerivedArtifact, StoredObject]] = []
        failed: list[str] = []
        with TemporaryDirectory(dir=self._workspace_dir) as tmp:
            src = Path(tmp) / "source"
            await asyncio.to_thread(src.write_bytes, data)
            for enricher in self._enrichers:
                try:
                    artifact = await enricher.produce(parent=parent, source_path=str(src))
                except _ENRICH_ERRORS as exc:
                    _LOGGER.warning(
                        "enrichment.artifact_failed",
                        media_asset_id=str(parent.id),
                        artifact=enricher.origin,
                        error=str(exc)[:500],
                    )
                    failed.append(enricher.origin)
                    continue
                if artifact is None:
                    continue  # not applicable (e.g. waveform, no audio) — clean skip
                try:
                    stored = await self._storage.put(
                        key=artifact.storage_key,
                        data=artifact.data,
                        content_type=artifact.mime_type,
                    )
                except ObjectStorageError as exc:
                    _LOGGER.warning(
                        "enrichment.store_failed",
                        media_asset_id=str(parent.id),
                        artifact=enricher.origin,
                        error=str(exc)[:500],
                    )
                    failed.append(enricher.origin)
                    continue
                produced.append((artifact, stored))

        # Phase 3 — register derived assets + write the (versioned) marker in one txn.
        derived_ids: dict[str, UUID] = {}
        async with self._uow:
            enrichment: dict[str, object] = {}
            for artifact, stored in produced:
                derived_id = await self._register_derived(parent, artifact, stored)
                derived_ids[artifact.origin] = derived_id
                enrichment[f"{artifact.origin}_media_asset_id"] = str(derived_id)
                enrichment.update(artifact.metadata)
            enrichment["enriched_at"] = datetime.now(UTC).isoformat()
            # Only mark terminally enriched when every applicable enricher succeeded.
            if not failed:
                enrichment["version"] = CURRENT_ENRICHMENT_VERSION
            base = dict(parent.source_metadata) if isinstance(parent.source_metadata, dict) else {}
            base[_ENRICHMENT_KEY] = enrichment
            await self._uow.media.update_owned(
                parent.id, parent.tenant_id, parent.owner_user_id, {"source_metadata": base}
            )
            await self._uow.commit()

        status = "enriched" if not failed else ("failed" if not produced else "partial")
        _LOGGER.info(
            "enrichment.done",
            media_asset_id=str(parent.id),
            status=status,
            derived=list(derived_ids),
            failed=failed,
        )
        return EnrichGeneratedMediaResult(
            media_asset_id=parent.id,
            status=status,
            derived_media_ids=derived_ids,
            failed=tuple(failed),
        )

    async def _register_derived(
        self, parent: MediaAsset, artifact: DerivedArtifact, stored: StoredObject
    ) -> UUID:
        try:
            row = await self._uow.media.add(
                tenant_id=parent.tenant_id,
                owner_user_id=parent.owner_user_id,
                kind=artifact.kind,
                source="generated",
                storage_backend=stored.backend,
                storage_bucket=stored.bucket,
                storage_key=stored.key,
                mime_type=artifact.mime_type,
                size_bytes=len(artifact.data),
                checksum_sha256=hashlib.sha256(artifact.data).digest(),
                project_id=parent.project_id,
                scene_id=None,
                prompt_id=None,
                model_id=None,
                provider=None,
                width=artifact.width,
                height=artifact.height,
                duration_seconds=artifact.duration_seconds,
                source_metadata={"origin": artifact.origin, _PARENT_KEY: str(parent.id)},
            )
            return row.id
        except ConflictError:
            existing = await self._uow.media.get_by_storage_coords(
                storage_backend=stored.backend,
                storage_bucket=stored.bucket,
                storage_key=stored.key,
            )
            if existing is None:  # pragma: no cover - constraint says it must exist
                raise
            return existing.id


def _marker_version(source_metadata: object) -> int:
    """Current enrichment version of a parent asset (absent/α8.4c markers → 0)."""
    if not isinstance(source_metadata, dict):
        return 0
    enrichment = source_metadata.get(_ENRICHMENT_KEY)
    if not isinstance(enrichment, dict):
        return 0
    version = enrichment.get("version")
    return version if isinstance(version, int) else 0
