"""Local-filesystem ``IObjectStorage`` adapter (Slice α8.4a).

Persists objects under ``<root>/<bucket>/<key>``. The ``storage_backend`` value is
``"local"`` (matching the DB enum). Intended for development and single-node
deployments; the port lets S3/R2/GCS adapters replace it with no use-case change.

Configuration-blind (W8.1.1): the root directory + bucket are injected at
construction. File I/O runs in a worker thread (``asyncio.to_thread``) so the event
loop is never blocked. Keys are ``/``-delimited and confined to the bucket root
(traversal outside the root is rejected).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from app.application.interfaces.object_storage import (
    IObjectStorage,
    ObjectStorageError,
    StoredObject,
)

_BACKEND = "local"


class LocalObjectStorage(IObjectStorage):
    """Store objects on the local filesystem under an injected root + bucket."""

    def __init__(self, *, root: str, bucket: str) -> None:
        self._root = Path(root).resolve()
        self._bucket = bucket
        self._bucket_root = (self._root / bucket).resolve()

    @property
    def backend(self) -> str:
        return _BACKEND

    @property
    def bucket(self) -> str:
        return self._bucket

    def _resolve(self, key: str) -> Path:
        # Confine the key to the bucket root — reject traversal (``..``) escapes.
        target = (self._bucket_root / key).resolve()
        if target != self._bucket_root and self._bucket_root not in target.parents:
            raise ObjectStorageError(f"storage key escapes bucket root: {key!r}")
        return target

    async def put(self, *, key: str, data: bytes, content_type: str | None) -> StoredObject:
        target = self._resolve(key)

        def _write() -> None:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)

        try:
            await asyncio.to_thread(_write)
        except OSError as exc:
            raise ObjectStorageError(f"failed to write object {key!r}: {exc}") from exc
        return StoredObject(backend=_BACKEND, bucket=self._bucket, key=key)

    async def get(self, *, key: str) -> bytes:
        target = self._resolve(key)
        try:
            return await asyncio.to_thread(target.read_bytes)
        except OSError as exc:
            raise ObjectStorageError(f"failed to read object {key!r}: {exc}") from exc

    async def exists(self, *, key: str) -> bool:
        return await asyncio.to_thread(self._resolve(key).is_file)

    async def delete(self, *, key: str) -> None:
        target = self._resolve(key)

        def _unlink() -> None:
            target.unlink(missing_ok=True)

        try:
            await asyncio.to_thread(_unlink)
        except OSError as exc:
            raise ObjectStorageError(f"failed to delete object {key!r}: {exc}") from exc
