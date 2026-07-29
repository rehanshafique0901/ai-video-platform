"""α8.8 — read-only generation reader (raw SQL, ORM-less).

Implements :class:`IGenerationReader` for the Asset Promotion Bridge. Reads the
``generations`` aggregate and its final ``generation_assets`` video row (migration
``0012``) so ``PromoteGenerationAssets`` can copy the finished output into the media
library. Read-only by construction: it issues a single ``SELECT`` in a short session
and never writes, emits events, or advances the state machine (W8.6.7). Same raw-SQL
+ ``async_sessionmaker`` shape as :class:`SqlExecutionRuntimeStore` (ADR-0045 F4/F5);
the execution write seam is untouched (X8 — promotion is the only bridge).
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.interfaces.generation_reader import (
    IGenerationReader,
    PromotableGenerationVideo,
)

# LEFT JOIN so the generation head is returned even when no final video exists yet
# (lets the use case tell "unknown generation" 404 from "nothing promotable" 422).
_LOAD_FINAL_VIDEO_SQL = text(
    """
    SELECT
        g.id                AS generation_id,
        g.status            AS status,
        g.final_video_asset_id AS final_video_asset_id,
        g.chosen_provider   AS chosen_provider,
        g.chosen_adapter    AS chosen_adapter,
        g.seed              AS seed,
        g.title             AS title,
        g.target_platform   AS target_platform,
        a.storage_backend   AS storage_backend,
        a.storage_bucket    AS storage_bucket,
        a.storage_key       AS storage_key,
        a.mime_type         AS mime_type,
        a.size_bytes        AS size_bytes,
        a.checksum_sha256   AS checksum_sha256,
        a.width             AS width,
        a.height            AS height,
        a.duration_ms       AS duration_ms
    FROM generations g
    LEFT JOIN generation_assets a ON a.id = g.final_video_asset_id
    WHERE g.id = CAST(:generation_id AS uuid)
      AND g.tenant_id = CAST(:tenant_id AS uuid)
      AND g.owner_user_id = CAST(:owner_user_id AS uuid)
    """
)


class GenerationReader(IGenerationReader):
    """Raw-SQL reader for a generation's final rendered video artefact."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def load_final_video(
        self, *, generation_id: UUID, tenant_id: UUID, owner_user_id: UUID
    ) -> PromotableGenerationVideo | None:
        async with self._session_factory() as session:
            row = (
                (
                    await session.execute(
                        _LOAD_FINAL_VIDEO_SQL,
                        {
                            "generation_id": str(generation_id),
                            "tenant_id": str(tenant_id),
                            "owner_user_id": str(owner_user_id),
                        },
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            return None

        checksum = row["checksum_sha256"]
        return PromotableGenerationVideo(
            generation_id=row["generation_id"],
            status=str(row["status"]),
            final_video_asset_id=row["final_video_asset_id"],
            chosen_provider=row["chosen_provider"],
            chosen_adapter=row["chosen_adapter"],
            seed=row["seed"],
            title=row["title"],
            target_platform=row["target_platform"],
            storage_backend=row["storage_backend"],
            storage_bucket=row["storage_bucket"],
            storage_key=row["storage_key"],
            mime_type=row["mime_type"],
            size_bytes=row["size_bytes"],
            checksum_sha256=bytes(checksum) if checksum is not None else None,
            width=row["width"],
            height=row["height"],
            duration_ms=row["duration_ms"],
        )


__all__ = ["GenerationReader"]
