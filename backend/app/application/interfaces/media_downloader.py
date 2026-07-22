"""Port: ``IMediaDownloader`` — fetch a provider's produced artifact (Slice α8.4a).

A provider job resolves to a *URL* (`image_ref` / `video_ref`) — the bytes live
on the provider's/CDN's storage. Ingestion fetches those bytes through this
neutral port before persisting them via :mod:`object_storage`. Keeping the
downloader separate from the provider adapters is deliberate (α8.4a Fork B): the
provider already resolved the job; downloading the result is generic
infrastructure, not a provider-protocol concern, so we do **not** expand the
frozen provider ports.

Adapters are configuration-blind (W8.1.1): the HTTP client (timeout, limits) is
injected at construction.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


class MediaDownloadError(Exception):
    """The artifact could not be fetched (HTTP error, timeout, or size-cap exceeded).

    Raised so the ingestion subscriber surfaces it to the relay as a failed publish
    — the at-least-once relay then retries the delivery on a later pass.
    """


@dataclass(frozen=True, slots=True)
class DownloadedMedia:
    """The fetched bytes plus the minimum metadata α8.4a persists (no probing)."""

    content: bytes
    mime_type: str | None
    size_bytes: int


class IMediaDownloader(ABC):
    """Fetch the bytes of a produced artifact by URL."""

    @abstractmethod
    async def download(self, url: str) -> DownloadedMedia:
        """Fetch ``url`` and return its bytes + content type + size.

        Raises ``MediaDownloadError`` on any non-success status, transport error,
        timeout, or if the response exceeds the adapter's configured size cap.
        """
        ...
