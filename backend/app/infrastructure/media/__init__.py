"""Media infrastructure adapters (Slice α8.4a).

α8.4a ships :class:`HttpMediaDownloader` behind the neutral ``IMediaDownloader``
port — fetches a provider's produced artifact (``image_ref`` / ``video_ref``) by
URL so ingestion can persist it via ``IObjectStorage``.
"""

from __future__ import annotations

from app.infrastructure.media.http_media_downloader import HttpMediaDownloader

__all__ = ["HttpMediaDownloader"]
