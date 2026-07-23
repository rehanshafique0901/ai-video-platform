"""``Enricher`` protocol + ``DerivedArtifact`` DTO for the α8.4d pipeline.

An enricher is a pure `parent MediaAsset (+ its bytes) → one derived artifact`
transform (W8.4c.3). It owns applicability and its deterministic storage key; the
use case owns leasing, materialization, registration, and the versioned marker.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from app.domain.media.media_asset import MediaAsset


@dataclass(frozen=True, slots=True)
class DerivedArtifact:
    """One derived artifact produced from a parent asset's bytes."""

    origin: str  # "thumbnail" | "preview" | "gif" | "waveform" — provenance + marker key
    kind: str  # media_kind: "image" | "video"
    data: bytes
    mime_type: str
    storage_key: str  # deterministic in (tenant, parent) — idempotency
    width: int | None = None
    height: int | None = None
    duration_seconds: float | None = None
    # Scalar contributions merged into the parent's enrichment marker (e.g. bitrate).
    metadata: dict[str, object] = field(default_factory=dict)


class Enricher(ABC):
    """A single deterministic derived-media transform of a parent ``MediaAsset``."""

    @property
    @abstractmethod
    def origin(self) -> str:
        """Stable provenance tag (also the ``<origin>_media_asset_id`` marker key)."""
        ...

    @abstractmethod
    async def produce(self, *, parent: MediaAsset, source_path: str) -> DerivedArtifact | None:
        """Produce the derived artifact from the materialized source.

        Returns ``None`` when the enricher does not apply to this parent (e.g. a
        waveform for a silent video) — a clean skip, not a failure. Raises the wrapped
        port's neutral error on a genuine engine failure.
        """
        ...
