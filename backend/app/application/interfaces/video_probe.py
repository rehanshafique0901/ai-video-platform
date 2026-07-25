"""Port: video probe — measure a rendered video for the video verifier.

Mirrors the image feature extractor but for the assembled video: it reports the
observed duration and dimensions so the use case can verify the render matches the
plan (right length, right aspect). The concrete implementation shells out to
ffprobe; tests use a fake.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ObservedVideo:
    """Measured properties of a rendered video (``None`` == not measured)."""

    duration_seconds: float | None = None
    width: int | None = None
    height: int | None = None


class IVideoProbe(ABC):
    @abstractmethod
    async def probe(self, video: bytes) -> ObservedVideo:
        """Measure ``video`` into an :class:`ObservedVideo`."""
        ...
