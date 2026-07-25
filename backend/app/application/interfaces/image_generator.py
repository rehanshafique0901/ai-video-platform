"""Port: image generator — produce an image for a prompt via a chosen adapter.

This is the provider-agnostic seam the renderer sits behind: Pollinations,
ComfyUI, Stability, fal, or a local-folder fixture all implement the same port,
and nothing downstream knows or cares which produced the bytes. The use case
passes the resolver-chosen ``adapter_id`` so a dispatcher implementation can route
to the right provider.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


class ImageGenerationError(RuntimeError):
    """Raised when an adapter fails to produce an image (network, quota, etc.)."""


@dataclass(frozen=True, slots=True)
class GeneratedImage:
    """Raw image bytes plus the adapter/provider that produced them."""

    data: bytes
    content_type: str
    adapter_id: str
    provider_id: str | None = None
    model: str | None = None


class IImageGenerator(ABC):
    @abstractmethod
    async def generate(
        self,
        *,
        adapter_id: str,
        prompt: str,
        seed: int,
        width: int,
        height: int,
        negative_prompt: str | None = None,
        reference_image_refs: tuple[str, ...] = (),
        local_model_path: str | None = None,
    ) -> GeneratedImage:
        """Generate one image. ``seed`` biases toward reproducibility/consistency.

        ``negative_prompt`` and ``reference_image_refs`` come from the Reference
        Asset Store; an adapter consumes them only if it supports negative prompts
        / image conditioning and ignores them otherwise. ``local_model_path`` is
        supplied only for local-tier adapters after the Model Cache has ensured the
        weights are present; remote adapters ignore it.
        """
        ...
