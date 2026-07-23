"""Port: ``IPreviewClipper`` — derive a short preview clip from a video (α8.4d).

A derived-preview enricher hands a produced video's **local bytes** to this neutral
port and gets back a short, downscaled preview clip (video bytes + dimensions). The
port is engine-agnostic (the α8.4d adapter shells out to FFmpeg, but the enricher
never knows that) and configuration-blind (W8.1.1 — binary paths + limits injected).

Per W8.4c.2/W8.4c.3 the clipper consumes only the parent asset's bytes — never
provider payloads, checkpoints, Timeline state, or render-job history — so the preview
is a pure function of the parent. C1 (α8.4d Fork C): one dedicated port per derived
artifact, never a "God" `IMediaEnricher`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


class PreviewClipError(Exception):
    """The preview clip could not be produced (bad input, engine failure, timeout)."""


@dataclass(frozen=True, slots=True)
class PreviewClip:
    """A short derived preview clip."""

    data: bytes
    mime_type: str  # e.g. "video/mp4"
    width: int | None = None
    height: int | None = None
    duration_seconds: float | None = None


class IPreviewClipper(ABC):
    """Produce a short, downscaled preview clip from a source video."""

    @abstractmethod
    async def preview(self, *, source_path: str, max_seconds: float, max_width: int) -> PreviewClip:
        """Trim the source to ``max_seconds`` and downscale to ``max_width``.

        Raises ``PreviewClipError`` on any engine failure, timeout, or invalid input.
        Must not partially succeed.
        """
        ...
