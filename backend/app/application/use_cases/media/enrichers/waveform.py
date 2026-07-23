"""``WaveformEnricher`` — wraps ``IWaveformRenderer`` (α8.4d).

Returns ``None`` for a silent source (the port yields ``None`` when there is no audio
stream) — a clean, terminal skip, not a failure.
"""

from __future__ import annotations

from app.application.interfaces.waveform_renderer import IWaveformRenderer
from app.application.use_cases.media.enrichers.base import DerivedArtifact, Enricher
from app.domain.media.media_asset import MediaAsset


class WaveformEnricher(Enricher):
    def __init__(self, renderer: IWaveformRenderer, *, width: int = 640, height: int = 120) -> None:
        self._renderer = renderer
        self._width = width
        self._height = height

    @property
    def origin(self) -> str:
        return "waveform"

    async def produce(self, *, parent: MediaAsset, source_path: str) -> DerivedArtifact | None:
        wave = await self._renderer.waveform(
            source_path=source_path, width=self._width, height=self._height
        )
        if wave is None:
            return None  # silent source — not applicable
        key = f"waveforms/{parent.tenant_id}/{parent.id}.png"
        return DerivedArtifact(
            origin=self.origin,
            kind="image",
            data=wave.data,
            mime_type=wave.mime_type,
            storage_key=key,
            width=wave.width,
            height=wave.height,
        )
