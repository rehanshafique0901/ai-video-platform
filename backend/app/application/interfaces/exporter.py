"""Port: ``IExporter`` — transcode a rendered master into a delivery encoding (α8.5a).

Export is a **distinct domain from rendering** (α8.5a Fork C): the renderer composes a
Timeline into the canonical master ``MediaAsset``; the exporter takes that finished master
and produces a *replaceable delivery artifact* in a requested ``(format, quality)`` — a
pure, deterministic transcode (RC6/RP9). The renderer never learns about delivery codecs.

    Timeline → IRenderer → Master MediaAsset → IExporter → Delivery MediaAsset

Contract & boundaries:

* **Delivery-only, same presentation (α8.5a Fork F, tightened).** The exporter changes
  container / codec / bitrate / resolution **within the master's own orientation**. It never
  letterboxes, pillarboxes, crops, or reframes — those change *presentation* semantics and
  are out of scope. The caller (``CreateExportJob``) rejects cross-orientation requests
  before a job is ever created; the exporter assumes ``spec.orientation`` matches the source.
* **Configuration-blind (W8.1.1).** Concrete adapters take binary paths + a timeout by
  injection; nothing here reads env / DB / secrets.
* **Neutral failure.** Any adapter failure raises :class:`ExportError` with a short message;
  no subprocess / provider detail leaks past this boundary (mirror of ``RenderError``).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


class ExportError(Exception):
    """Raised when an export transcode cannot be produced (neutral, no infra detail)."""


# Delivery-format → stored-object facts, shared by the use case (MediaAsset registration)
# and the adapter (probe/result). ``gif`` is an animated image; the rest are video. The file
# extension equals the format value. Keyed by the validated ``export_format`` enum value.
EXPORT_FORMAT_MIME: dict[str, str] = {
    "mp4": "video/mp4",
    "mov": "video/quicktime",
    "webm": "video/webm",
    "gif": "image/gif",
}
EXPORT_FORMAT_KIND: dict[str, str] = {
    "mp4": "video",
    "mov": "video",
    "webm": "video",
    "gif": "image",
}


@dataclass(frozen=True, slots=True)
class ExportSpec:
    """A single delivery-encoding request against one finished master file.

    ``format`` / ``quality`` / ``orientation`` are the validated ``export_*`` enum values.
    ``source_path`` is the materialized master render; ``output_path`` is where the adapter
    must write the delivery artifact. All fields are pure inputs — the same spec always
    yields a functionally-equivalent artifact (RC6/RP9).
    """

    source_path: str
    output_path: str
    format: str
    quality: str
    orientation: str


@dataclass(frozen=True, slots=True)
class ExportResult:
    """The produced delivery artifact's stored-object facts (for ``MediaAsset`` registration)."""

    output_path: str
    size_bytes: int
    mime_type: str
    duration_seconds: float | None
    width: int | None
    height: int | None


class IExporter(ABC):
    """Transcode a finished master render into one delivery encoding."""

    @abstractmethod
    async def export(self, spec: ExportSpec) -> ExportResult:
        """Produce ``spec.output_path`` from ``spec.source_path`` for the requested encoding.

        Deterministic and side-effect-free beyond writing the output file. Raises
        :class:`ExportError` on any failure (bad input, non-zero exit, timeout, missing
        output). Never recomposes or mutates the source (W8.5.1).
        """
        ...
