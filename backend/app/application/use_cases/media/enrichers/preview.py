"""``PreviewEnricher`` — wraps ``IPreviewClipper`` (α8.4d)."""

from __future__ import annotations

from app.application.interfaces.preview_clipper import IPreviewClipper
from app.application.use_cases.media.enrichers.base import DerivedArtifact, Enricher
from app.domain.media.media_asset import MediaAsset


class PreviewEnricher(Enricher):
    def __init__(
        self, clipper: IPreviewClipper, *, max_seconds: float = 5.0, max_width: int = 640
    ) -> None:
        self._clipper = clipper
        self._max_seconds = max_seconds
        self._max_width = max_width

    @property
    def origin(self) -> str:
        return "preview"

    async def produce(self, *, parent: MediaAsset, source_path: str) -> DerivedArtifact:
        clip = await self._clipper.preview(
            source_path=source_path,
            max_seconds=self._max_seconds,
            max_width=self._max_width,
        )
        key = f"previews/{parent.tenant_id}/{parent.id}.mp4"
        return DerivedArtifact(
            origin=self.origin,
            kind="video",
            data=clip.data,
            mime_type=clip.mime_type,
            storage_key=key,
            width=clip.width,
            height=clip.height,
            duration_seconds=clip.duration_seconds,
        )
