"""Local streaming ``IDownloadDelivery`` adapter (Slice α8.5b.1).

Streams a delivery artifact's bytes through the API from :class:`IObjectStorage`. This is the
single α8.5b.1 delivery mechanism — appropriate for local / single-node deployments. Cloud
adapters (signed-URL :class:`RedirectDelivery`) are α8.5b.2 and add **no** endpoint change.

W8.5b.2 (pure transfer): this adapter never encodes, transcodes, resizes, or otherwise mutates
the bytes — it reads the already-final object and yields it in fixed-size chunks.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from app.application.interfaces.download_delivery import (
    DownloadDeliveryError,
    DownloadRequest,
    IDownloadDelivery,
    StreamDelivery,
)
from app.application.interfaces.object_storage import IObjectStorage

# 64 KiB — a conventional streaming chunk; the object is read once from storage and yielded
# in slices so the response is emitted incrementally rather than as one large frame.
_CHUNK_SIZE = 1 << 16


class LocalStreamDelivery(IDownloadDelivery):
    """Deliver an artifact by streaming its bytes from object storage through the API."""

    def __init__(self, storage: IObjectStorage, *, chunk_size: int = _CHUNK_SIZE) -> None:
        self._storage = storage
        self._chunk_size = chunk_size

    async def deliver(self, request: DownloadRequest) -> StreamDelivery:
        # This adapter only serves objects that live in its own storage backend/bucket.
        # A mismatch means the artifact belongs to a cloud backend that needs the α8.5b.2
        # redirect adapter — surface it explicitly rather than 404-ing on a missing key.
        if (
            request.storage_backend != self._storage.backend
            or request.storage_bucket != self._storage.bucket
        ):
            raise DownloadDeliveryError(
                "artifact is not served by the local delivery adapter "
                f"({request.storage_backend}/{request.storage_bucket})"
            )

        # ``IObjectStorage.get`` returns the full object; α8.5b.1 targets local/single-node,
        # so a read-then-chunk stream is acceptable (see the pre-flight Fork A trade-off).
        # Read eagerly here (raising ``ObjectStorageError`` on a missing object *before* any
        # download accounting) and yield in chunks.
        data = await self._storage.get(key=request.storage_key)

        async def _chunks() -> AsyncIterator[bytes]:
            for start in range(0, len(data), self._chunk_size):
                yield data[start : start + self._chunk_size]

        return StreamDelivery(
            chunks=_chunks(),
            media_type=request.media_type,
            filename=request.filename,
            content_length=len(data),
        )
