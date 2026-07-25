"""α8.6 Increment 4 — generation asset registry repository (raw SQL, ORM-less).

Writes the canonical execution artefact registry (``generation_assets``, migration
``0012``). Every frame/reference/mask/audio/video/thumbnail/metadata blob produced
by the Execution Runtime is registered here; ``parent_asset_id`` turns repair into
a lineage graph (contract §3). Execution-owned — never linked to ``media_assets``
(W8.6.8). Transaction scoping is the caller's.
"""

from __future__ import annotations

import json
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.interfaces.execution_runtime_store import NewGenerationAsset

_INSERT_ASSET_SQL = text(
    """
    INSERT INTO generation_assets (
        generation_id, shot_number, asset_kind, storage_backend, storage_bucket,
        storage_key, mime_type, size_bytes, checksum_sha256, width, height,
        duration_ms, parent_asset_id, metadata
    ) VALUES (
        CAST(:generation_id AS uuid), :shot_number,
        CAST(:asset_kind AS generation_asset_kind),
        CAST(:storage_backend AS storage_backend), :storage_bucket, :storage_key,
        :mime_type, :size_bytes, :checksum_sha256, :width, :height, :duration_ms,
        CAST(:parent_asset_id AS uuid), CAST(:metadata AS jsonb)
    )
    RETURNING id
    """
)


class GenerationAssetRepository:
    """Raw-SQL writer for the ``generation_assets`` artefact registry."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def insert(self, asset: NewGenerationAsset) -> UUID:
        row = (
            await self._session.execute(
                _INSERT_ASSET_SQL,
                {
                    "generation_id": str(asset.generation_id),
                    "shot_number": asset.shot_number,
                    "asset_kind": asset.asset_kind.value,
                    "storage_backend": asset.storage_backend,
                    "storage_bucket": asset.storage_bucket,
                    "storage_key": asset.storage_key,
                    "mime_type": asset.mime_type,
                    "size_bytes": asset.size_bytes,
                    "checksum_sha256": asset.checksum_sha256,
                    "width": asset.width,
                    "height": asset.height,
                    "duration_ms": asset.duration_ms,
                    "parent_asset_id": (
                        str(asset.parent_asset_id) if asset.parent_asset_id else None
                    ),
                    "metadata": json.dumps(asset.metadata),
                },
            )
        ).one()
        return row[0]  # type: ignore[no-any-return]
