"""Internal enricher pipeline for α8.4d derived-preview enrichment.

`EnrichGeneratedMedia` iterates a list of independent :class:`Enricher`s rather than
hard-coding each artifact type. Each enricher wraps one neutral FFmpeg port and owns
its applicability, deterministic storage key, and derived-asset + metadata
contribution. This is an **implementation detail** of the media use-case layer — not a
new platform abstraction, worker, or port (α8.4d additional sign-off).
"""

from __future__ import annotations

from app.application.use_cases.media.enrichers.base import DerivedArtifact, Enricher
from app.application.use_cases.media.enrichers.gif import GifEnricher
from app.application.use_cases.media.enrichers.preview import PreviewEnricher
from app.application.use_cases.media.enrichers.thumbnail import ThumbnailEnricher
from app.application.use_cases.media.enrichers.waveform import WaveformEnricher

__all__ = [
    "DerivedArtifact",
    "Enricher",
    "GifEnricher",
    "PreviewEnricher",
    "ThumbnailEnricher",
    "WaveformEnricher",
]
