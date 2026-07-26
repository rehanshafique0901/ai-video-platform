"""``DestinationRegistry`` — resolve a platform key to its destination adapter (α8.6b).

A tiny in-code map from ``platform`` (free-text, OQ2) to its
:class:`app.application.interfaces.destination_publisher.IDestinationPublisher`. Wired at the
composition root; α8.6b registers only ``mock``. A YAML destination catalogue is deferred
until ≥2 real destinations justify it (contract §14). An unknown platform is a **permanent**
publish failure — a job for a destination the runtime cannot serve is never retried.
"""

from __future__ import annotations

from app.application.interfaces.destination_publisher import (
    DestinationError,
    IDestinationPublisher,
    IDestinationRegistry,
)


class DestinationRegistry(IDestinationRegistry):
    """Immutable platform → adapter lookup (populated at the composition root)."""

    def __init__(self, adapters: dict[str, IDestinationPublisher]) -> None:
        self._adapters = dict(adapters)

    def for_platform(self, platform: str) -> IDestinationPublisher:
        """Return the adapter for ``platform``, or raise a permanent ``DestinationError``."""
        adapter = self._adapters.get(platform)
        if adapter is None:
            raise DestinationError(
                f"no destination adapter registered for platform {platform!r}",
                retryable=False,
                code="unsupported_destination",
            )
        return adapter

    def supported_platforms(self) -> frozenset[str]:
        """The set of platform keys this registry can serve (create-time validation)."""
        return frozenset(self._adapters)


__all__ = ["DestinationRegistry"]
