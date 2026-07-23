"""``GifEnricher`` — wraps ``IGifPreviewer`` (α8.4d)."""

from __future__ import annotations

from app.application.interfaces.gif_previewer import IGifPreviewer
from app.application.use_cases.media.enrichers.base import DerivedArtifact, Enricher
from app.domain.media.media_asset import MediaAsset


class GifEnricher(Enricher):
    def __init__(
        self,
        previewer: IGifPreviewer,
        *,
        max_seconds: float = 3.0,
        fps: int = 10,
        max_width: int = 480,
    ) -> None:
        self._previewer = previewer
        self._max_seconds = max_seconds
        self._fps = fps
        self._max_width = max_width

    @property
    def origin(self) -> str:
        return "gif"

    async def produce(self, *, parent: MediaAsset, source_path: str) -> DerivedArtifact:
        gif = await self._previewer.gif(
            source_path=source_path,
            max_seconds=self._max_seconds,
            fps=self._fps,
            max_width=self._max_width,
        )
        key = f"gifs/{parent.tenant_id}/{parent.id}.gif"
        return DerivedArtifact(
            origin=self.origin,
            kind="image",
            data=gif.data,
            mime_type=gif.mime_type,
            storage_key=key,
            width=gif.width,
            height=gif.height,
        )
