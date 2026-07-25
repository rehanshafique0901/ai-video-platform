"""Port: image feature extractor — turn raw bytes into observed features.

The verification *policy* (``app.domain.generation.verification``) never touches
bytes; this port is the infrastructure side that measures them (dimensions,
blank/near-uniform detection, watermark heuristic, and a perceptual similarity to
an optional reference frame for cross-shot consistency). The concrete
implementation uses Pillow; tests use a fake that returns canned features.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.domain.generation.verification import ObservedImage


class IImageFeatureExtractor(ABC):
    @abstractmethod
    async def extract(self, image: bytes, *, reference: bytes | None = None) -> ObservedImage:
        """Measure ``image`` into an :class:`ObservedImage`.

        When ``reference`` is given, ``similarity_to_reference`` is populated
        (0..1, perceptual) so the consistency check can run; otherwise it stays
        ``None`` (first shot has no reference) and the check is SKIPPED.
        """
        ...
