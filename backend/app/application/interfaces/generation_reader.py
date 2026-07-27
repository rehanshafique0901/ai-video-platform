"""Port: ``IGenerationReader`` — read-only view of a generation's final output (α8.8).

The Asset Promotion Bridge (`PromoteGenerationAssets`, the ADR-0046 **X8**
`PublishGenerationAssets` seam) needs to *read* an execution-plane generation and its
final rendered video so it can copy the bytes into the platform media library. No
existing use-case-facing port exposes ``generations`` / ``generation_assets``: the UoW
carries no generation repository, and :class:`IExecutionRuntimeStore` is **write-only**
(``begin``/``set_status``/``register_asset``/``record_shot``/``complete``/``fail``).

Rather than broaden that frozen write seam, α8.8 adds this **separate, additive,
read-only** port. It reads only; it never writes, emits events, or advances the state
machine (W8.6.7). The Execution Runtime (`GenerateVideo`, the store, the generation
repositories) is left untouched — promotion is the only bridge, and it consumes this
port. Concrete implementation: ``app/infrastructure/generation/generation_reader.py``
(raw SQL, ORM-less, ADR-0045 F4/F5).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class PromotableGenerationVideo:
    """A generation head + (if present) its final rendered video artefact.

    Returned by :meth:`IGenerationReader.load_final_video`. When the generation exists
    but has no final video yet (``final_video_asset_id IS NULL`` — e.g. it is not
    ``completed``), the ``final_video_asset_id`` and the physical ``storage_*`` /
    ``mime_type`` fields are ``None`` and the caller rejects the promotion (422). The
    physical fields describe the **execution-owned** object to be copied — never a
    ``media_assets`` row (X8).
    """

    generation_id: UUID
    status: str
    final_video_asset_id: UUID | None
    # Provenance carried onto the promoted media asset (all nullable on the row).
    chosen_provider: str | None
    chosen_adapter: str | None
    seed: int | None
    title: str | None
    target_platform: str | None
    # The final video artefact's physical coordinates + descriptors (None when absent).
    storage_backend: str | None
    storage_bucket: str | None
    storage_key: str | None
    mime_type: str | None
    size_bytes: int | None
    checksum_sha256: bytes | None
    width: int | None
    height: int | None
    duration_ms: int | None

    @property
    def has_final_video(self) -> bool:
        """True iff a final video artefact is registered + physically addressable."""
        return (
            self.final_video_asset_id is not None
            and self.storage_backend is not None
            and self.storage_bucket is not None
            and self.storage_key is not None
            and self.mime_type is not None
        )


class IGenerationReader(ABC):
    """Read-only access to an execution-plane generation's final rendered video."""

    @abstractmethod
    async def load_final_video(self, generation_id: UUID) -> PromotableGenerationVideo | None:
        """Return the generation head + its final video artefact, or ``None``.

        ``None`` means no ``generations`` row exists for ``generation_id``. A row that
        exists but has no ``final_video_asset_id`` is returned with the physical fields
        set to ``None`` (``has_final_video`` is ``False``) so the caller can distinguish
        "unknown generation" (404) from "nothing promotable yet" (422). Side-effect-free.
        """
        ...


__all__ = ["IGenerationReader", "PromotableGenerationVideo"]
