"""HTTP ``IMediaDownloader`` adapter (Slice α8.4a).

Fetches a produced artifact by URL with a single GET (one attempt — retries are
the ingestion subscriber's via the at-least-once relay, not the downloader's). The
``httpx.AsyncClient`` is injected (configuration-blind, W8.1.1); a byte cap guards
against unexpectedly large responses. Any non-success status, transport error,
timeout, or cap breach maps to a neutral ``MediaDownloadError`` — nothing HTTP
leaks upward.
"""

from __future__ import annotations

import httpx

from app.application.interfaces.media_downloader import (
    DownloadedMedia,
    IMediaDownloader,
    MediaDownloadError,
)


class HttpMediaDownloader(IMediaDownloader):
    """Download artifact bytes over HTTP with an injected client + size cap."""

    def __init__(self, *, client: httpx.AsyncClient, max_bytes: int) -> None:
        self._client = client
        self._max_bytes = max_bytes

    async def download(self, url: str) -> DownloadedMedia:
        try:
            response = await self._client.get(url)
        except httpx.HTTPError as exc:
            raise MediaDownloadError(f"download transport error for {url!r}: {exc}") from exc

        if response.status_code != httpx.codes.OK:
            raise MediaDownloadError(f"download for {url!r} returned HTTP {response.status_code}")

        content = response.content
        if len(content) > self._max_bytes:
            raise MediaDownloadError(
                f"download for {url!r} exceeded size cap "
                f"({len(content)} > {self._max_bytes} bytes)"
            )

        # Content-Type header is the artifact's declared mime (strip any ``; charset``).
        raw_type = response.headers.get("content-type")
        mime_type = raw_type.split(";", 1)[0].strip() if raw_type else None
        return DownloadedMedia(
            content=content, mime_type=mime_type or None, size_bytes=len(content)
        )
