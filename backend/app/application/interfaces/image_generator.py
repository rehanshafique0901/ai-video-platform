"""Port: image generator — produce an image for a prompt via a chosen adapter.

This is the provider-agnostic seam the renderer sits behind: Pollinations,
ComfyUI, Stability, fal, or a local-folder fixture all implement the same port,
and nothing downstream knows or cares which produced the bytes.

``IImageAdapterRegistry`` is the dispatch seam above it (ADR-0054): the use case asks the
registry for the resolver-chosen ``adapter_id`` and invokes what it gets back. The
registry key — not anything the adapter reports — is the authority on which adapter
produced an artefact (DISP-2).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


class ImageGenerationError(RuntimeError):
    """Raised when an adapter fails to produce an image (network, quota, etc.)."""


class AdapterNotRegisteredError(RuntimeError):
    """Raised when no adapter is registered under a requested ``adapter_id``.

    Permanent and terminal: a deployment that cannot construct the adapter will not be
    able to on a retry. Under ADR-0054 DISP-1 the resolver cannot return a non-executable
    adapter, so this is a fail-closed assertion against non-conformant wiring.
    """

    def __init__(self, adapter_id: str) -> None:
        super().__init__(f"no image adapter registered for {adapter_id!r}")
        self.adapter_id = adapter_id
        self.retryable = False
        self.code = "unknown_adapter"


@dataclass(frozen=True, slots=True)
class GeneratedImage:
    """Raw image bytes plus the adapter/provider the *adapter* reports.

    ``adapter_id`` is an echo of the requested id, not an observation, so it is never a
    provenance source (ADR-0054 DISP-2). Producer identity comes from the registry key the
    caller dispatched on.
    """

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


class IImageAdapterRegistry(ABC):
    """Adapter-id → generator lookup: the dispatch seam of ADR-0054."""

    @abstractmethod
    def for_adapter(self, adapter_id: str) -> IImageGenerator:
        """Return the generator registered under ``adapter_id``.

        Raises :class:`AdapterNotRegisteredError` if none is — never a fallback, and
        never a default adapter, which would silently break the binding that provenance
        is asserted from.
        """
        ...

    @abstractmethod
    def supported_adapters(self) -> frozenset[str]:
        """The adapter ids this deployment can construct (the executable set, D1)."""
        ...
