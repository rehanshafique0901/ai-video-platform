"""Port: slideshow renderer — assemble verified frames into a video.

The renderer takes an ordered list of image frames with per-frame durations and
produces a single video. It is provider-agnostic about where the frames came
from (the whole point of the image-generator seam). The concrete implementation
shells out to ffmpeg; tests use a fake that returns canned video bytes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


class SlideshowRenderError(RuntimeError):
    """Raised when the frames cannot be assembled into a video."""


@dataclass(frozen=True, slots=True)
class SlideshowFrame:
    """One frame of the slideshow: image bytes shown for ``duration_seconds``."""

    data: bytes
    duration_seconds: float


@dataclass(frozen=True, slots=True)
class SlideshowSpec:
    width: int
    height: int
    fps: int = 30
    container: str = "mp4"


@dataclass(frozen=True, slots=True)
class RenderedVideo:
    data: bytes
    content_type: str = "video/mp4"

    @property
    def size_bytes(self) -> int:
        return len(self.data)


class ISlideshowRenderer(ABC):
    @abstractmethod
    async def render(
        self, *, frames: tuple[SlideshowFrame, ...], spec: SlideshowSpec
    ) -> RenderedVideo:
        """Assemble ``frames`` (in order) into a single video per ``spec``."""
        ...
