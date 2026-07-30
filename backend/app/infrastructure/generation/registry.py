"""``ImageAdapterRegistry`` — resolve an adapter id to its image generator (α9.9).

An in-code map from the catalogue's ``adapter_id`` to its
:class:`app.application.interfaces.image_generator.IImageGenerator`, populated at the
composition root and immutable thereafter. Deliberately the same shape as
``DestinationRegistry``: a closed table, no dynamic ``import_path`` loading, and an
unknown key is a permanent failure rather than a fallback (ADR-0054 D1/D4).

The registry is also the deployment's answer to "what can this build execute?" —
``supported_adapters()`` is what becomes ``ExecutableAdapters`` for the resolver.
"""

from __future__ import annotations

from app.application.interfaces.image_generator import (
    AdapterNotRegisteredError,
    IImageAdapterRegistry,
    IImageGenerator,
)
from app.infrastructure.generation.pollinations_image_generator import (
    ADAPTER_ID as POLLINATIONS_ADAPTER_ID,
)

# The image adapters that exist in this build. The composition root constructs exactly
# these keys (asserted by a container unit test), and provider validation asserts each one
# is a real catalogue adapter id — the two halves of keeping code and manifest honest.
IMPLEMENTED_IMAGE_ADAPTER_IDS: frozenset[str] = frozenset({POLLINATIONS_ADAPTER_ID})


class ImageAdapterRegistry(IImageAdapterRegistry):
    """Immutable adapter id → generator lookup (populated at the composition root)."""

    def __init__(self, adapters: dict[str, IImageGenerator]) -> None:
        self._adapters = dict(adapters)

    def for_adapter(self, adapter_id: str) -> IImageGenerator:
        adapter = self._adapters.get(adapter_id)
        if adapter is None:
            raise AdapterNotRegisteredError(adapter_id)
        return adapter

    def supported_adapters(self) -> frozenset[str]:
        return frozenset(self._adapters)


__all__ = ["IMPLEMENTED_IMAGE_ADAPTER_IDS", "ImageAdapterRegistry"]
