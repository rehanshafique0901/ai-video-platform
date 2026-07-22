"""Port: ``IObjectStorage`` — durable blob storage for produced media (Slice α8.4a).

The first storage abstraction in the platform. Generated-media ingestion writes
downloaded provider bytes through this port and records the returned coordinates
on a ``MediaAsset`` (``storage_backend`` / ``storage_bucket`` / ``storage_key``).
The port is backend-neutral so the local-filesystem adapter shipped in α8.4a can
be swapped for S3 / R2 / GCS / Azure / MinIO later **without touching any use
case** — the ``storage_backend`` enum already enumerates those targets.

Keys are opaque, ``/``-delimited paths chosen by the caller (α8.4a uses a
deterministic key so re-ingestion is idempotent). Adapters are configuration-blind
(W8.1.1): credentials/roots are injected at construction, never read here.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


class ObjectStorageError(Exception):
    """A storage operation failed (I/O, permissions, missing object)."""


@dataclass(frozen=True, slots=True)
class StoredObject:
    """The durable coordinates of a written object — mirrors the ``MediaAsset`` triple."""

    backend: str
    bucket: str
    key: str


class IObjectStorage(ABC):
    """Backend-neutral object store: put/get/exists/delete by opaque key."""

    @property
    @abstractmethod
    def backend(self) -> str:
        """The ``storage_backend`` enum value this adapter persists as (e.g. ``"local"``)."""
        ...

    @property
    @abstractmethod
    def bucket(self) -> str:
        """The logical bucket/container objects are written into."""
        ...

    @abstractmethod
    async def put(self, *, key: str, data: bytes, content_type: str | None) -> StoredObject:
        """Write ``data`` at ``key`` (creating intermediate paths) and return its coordinates.

        Overwrite semantics are **idempotent**: writing the same key with the same
        bytes is a safe no-op-equivalent (α8.4a derives a deterministic key, so a
        retried ingestion re-writes identical content). Raises ``ObjectStorageError``
        on an I/O failure.
        """
        ...

    @abstractmethod
    async def get(self, *, key: str) -> bytes:
        """Return the bytes stored at ``key``. Raises ``ObjectStorageError`` if absent."""
        ...

    @abstractmethod
    async def exists(self, *, key: str) -> bool:
        """Return whether an object exists at ``key``."""
        ...

    @abstractmethod
    async def delete(self, *, key: str) -> None:
        """Remove the object at ``key`` if present (idempotent — missing is not an error)."""
        ...
