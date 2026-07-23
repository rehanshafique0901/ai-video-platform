"""``ThumbnailEnricher`` — wraps ``IThumbnailer`` (α8.4c behaviour, pipeline form)."""

from __future__ import annotations

from app.application.interfaces.thumbnailer import IThumbnailer
from app.application.use_cases.media.enrichers.base import DerivedArtifact, Enricher
from app.domain.media.media_asset import MediaAsset


class ThumbnailEnricher(Enricher):
    def __init__(self, thumbnailer: IThumbnailer, *, at_seconds: float = 1.0) -> None:
        self._thumbnailer = thumbnailer
        self._at_seconds = at_seconds

    @property
    def origin(self) -> str:
        return "thumbnail"

    async def produce(self, *, parent: MediaAsset, source_path: str) -> DerivedArtifact:
        thumb = await self._thumbnailer.thumbnail(
            source_path=source_path, at_seconds=self._at_seconds
        )
        # Deterministic key MUST match α8.4c so existing thumbnails recover idempotently.
        key = f"thumbnails/{parent.tenant_id}/{parent.id}.jpg"
        return DerivedArtifact(
            origin=self.origin,
            kind="image",
            data=thumb.image,
            mime_type=thumb.mime_type,
            storage_key=key,
            width=thumb.width,
            height=thumb.height,
            metadata={"bitrate": thumb.source_bitrate},
        )
