"""Port: ``IWaveformRenderer`` — derive a waveform image from a video/audio (α8.4d).

Neutral, engine-agnostic, configuration-blind (W8.1.1). Consumes only the parent
asset's bytes (W8.4c.2/W8.4c.3). C1 (α8.4d Fork C): one dedicated port per derived
artifact.

Applicability is data-dependent: a source with **no audio stream** yields ``None``
(*not applicable*, not a failure), so the enricher can skip it cleanly and still mark
the parent terminally enriched. Genuine engine failures raise ``WaveformError``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


class WaveformError(Exception):
    """The waveform could not be produced (engine failure, timeout, invalid input)."""


@dataclass(frozen=True, slots=True)
class Waveform:
    """A derived waveform image."""

    data: bytes
    mime_type: str = "image/png"
    width: int | None = None
    height: int | None = None


class IWaveformRenderer(ABC):
    """Render a waveform image from the source's audio track."""

    @abstractmethod
    async def waveform(self, *, source_path: str, width: int, height: int) -> Waveform | None:
        """Render a ``width`` × ``height`` waveform PNG from the source audio.

        Returns ``None`` when the source has **no audio stream** (not applicable).
        Raises ``WaveformError`` on any engine failure, timeout, or invalid input.
        """
        ...
