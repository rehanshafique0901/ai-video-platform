"""Port: ``IRenderer`` — compose a Timeline's resolved media into one video (α8.4b).

The render worker resolves a ``RenderJob``'s Timeline into an ordered list of
**local source files** (already materialized from storage) plus their trim
windows, and hands them to this neutral port. The port is renderer-agnostic: the
α8.4b adapter shells out to FFmpeg, but the use case never knows that — a
different engine could replace it with no use-case change.

Keeping this seam separate from the AI provider ports is deliberate (α8.4b Fork B,
invariant **W8.4b.2**): the renderer consumes only Timeline data + `MediaAsset`
bytes, **never** provider outputs, URLs, checkpoints, request IDs, provider job
IDs, or webhook payloads. It is completely provider-agnostic.

Adapters are configuration-blind (W8.1.1): binary paths + timeouts are injected.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


class RenderError(Exception):
    """The render could not be produced (bad inputs, engine failure, timeout).

    Raised so the render worker settles the job ``failed`` — never a partial
    ``succeeded``.
    """


@dataclass(frozen=True, slots=True)
class RenderInput:
    """One ordered source segment of the composition (a resolved, trimmed clip)."""

    path: str  # local filesystem path to the materialized source media
    source_start_seconds: float  # trim window start into the source (>= 0)
    source_end_seconds: float  # trim window end (> start)


@dataclass(frozen=True, slots=True)
class RenderSpec:
    """The neutral render request: ordered inputs → one output file."""

    inputs: tuple[RenderInput, ...]
    output_path: str  # local filesystem path the engine must write
    container: str = "mp4"


@dataclass(frozen=True, slots=True)
class RenderResult:
    """The produced output file + the basic probed facts α8.4b persists.

    Duration/dimensions/codec are cheap to read off the freshly-produced output
    (unlike α8.4a, which had no probe step). Richer metadata / thumbnails / previews
    are α8.4c.
    """

    output_path: str
    size_bytes: int
    duration_seconds: float | None = None
    width: int | None = None
    height: int | None = None
    codec: str | None = None


class IRenderer(ABC):
    """Compose ordered source segments into a single output video."""

    @abstractmethod
    async def render(self, spec: RenderSpec) -> RenderResult:
        """Produce ``spec.output_path`` from ``spec.inputs``; return the probed result.

        Raises ``RenderError`` on any engine failure, timeout, or invalid input.
        Must not partially succeed: either the output file exists and is returned,
        or ``RenderError`` is raised.
        """
        ...
