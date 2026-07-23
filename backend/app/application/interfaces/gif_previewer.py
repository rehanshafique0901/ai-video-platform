"""Port: ``IGifPreviewer`` — derive an animated GIF preview from a video (α8.4d).

Neutral, engine-agnostic, configuration-blind (W8.1.1). Consumes only the parent
asset's bytes (W8.4c.2/W8.4c.3). C1 (α8.4d Fork C): one dedicated port per derived
artifact.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


class GifPreviewError(Exception):
    """The GIF preview could not be produced (bad input, engine failure, timeout)."""


@dataclass(frozen=True, slots=True)
class GifPreview:
    """A derived animated-GIF preview."""

    data: bytes
    mime_type: str = "image/gif"
    width: int | None = None
    height: int | None = None


class IGifPreviewer(ABC):
    """Produce a short animated GIF preview from a source video."""

    @abstractmethod
    async def gif(
        self, *, source_path: str, max_seconds: float, fps: int, max_width: int
    ) -> GifPreview:
        """Sample the first ``max_seconds`` at ``fps``, downscale to ``max_width``.

        Raises ``GifPreviewError`` on any engine failure, timeout, or invalid input.
        Must not partially succeed.
        """
        ...
