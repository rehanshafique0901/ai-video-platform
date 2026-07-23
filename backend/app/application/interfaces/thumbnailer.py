"""Port: ``IThumbnailer`` — derive a still thumbnail + probe scalars from a video (α8.4c).

The media-enrichment worker hands a produced video's **local bytes** to this neutral
port and gets back one thumbnail frame (image bytes + dimensions) plus a couple of
probed scalars (bitrate). The port is engine-agnostic: the α8.4c adapter shells out
to FFmpeg/ffprobe, but the use case never knows that.

Kept **separate from `IRenderer`** (α8.4c Fork D): rendering is a `Timeline → Video`
transform; thumbnailing is a `Video → Image` transform — different capabilities.
This also keeps W8.4b.2 intact (the renderer consumes only Timeline data + MediaAsset
ids). Per W8.4c.2/W8.4c.3 the thumbnailer consumes only the parent asset's bytes —
never provider payloads, checkpoints, Timeline state, or render-job history — so
``MediaAsset → Thumbnail`` is a pure function of the parent.

Adapters are configuration-blind (W8.1.1): binary paths + timeout are injected.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


class ThumbnailError(Exception):
    """The thumbnail could not be produced (bad input, engine failure, timeout)."""


@dataclass(frozen=True, slots=True)
class Thumbnail:
    """One derived still frame + the scalar facts α8.4c persists on the parent."""

    image: bytes
    mime_type: str  # e.g. "image/jpeg"
    width: int | None = None
    height: int | None = None
    source_bitrate: int | None = None  # probed off the source video (bits/second)


class IThumbnailer(ABC):
    """Extract a still thumbnail (and probe a couple of scalars) from a video."""

    @abstractmethod
    async def thumbnail(self, *, source_path: str, at_seconds: float) -> Thumbnail:
        """Extract one frame at ``at_seconds`` from ``source_path``; return it + probe.

        Raises ``ThumbnailError`` on any engine failure, timeout, or invalid input.
        Must not partially succeed: either a full :class:`Thumbnail` is returned or
        ``ThumbnailError`` is raised.
        """
        ...
