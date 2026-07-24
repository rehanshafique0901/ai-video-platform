"""Unit tests for ``StorageResolver`` (Slice α8.5b.2).

The backend → object-storage registry that centralises write/read selection (E2). Pins:

* ``active()`` returns the configured active-write adapter; ``resolve(backend)`` returns the
  adapter for an *existing* artifact's persisted backend (W8.5b.4 / W8.5b.5).
* An unconfigured backend raises ``ObjectStorageError`` (never a leak / KeyError).
* The active backend must be one of the registered adapters (fail-fast config error).
* ``single()`` mirrors the pre-α8.5b.2 single-instance behaviour.
"""

from __future__ import annotations

import pytest

from app.application.interfaces.object_storage import (
    IObjectStorage,
    ObjectStorageError,
    StoredObject,
)
from app.infrastructure.storage import StorageResolver

pytestmark = pytest.mark.unit


class _FakeStorage(IObjectStorage):
    def __init__(self, *, backend: str, bucket: str) -> None:
        self._backend = backend
        self._bucket = bucket

    @property
    def backend(self) -> str:
        return self._backend

    @property
    def bucket(self) -> str:
        return self._bucket

    async def put(self, *, key: str, data: bytes, content_type: str | None) -> StoredObject:
        return StoredObject(backend=self._backend, bucket=self._bucket, key=key)

    async def get(self, *, key: str) -> bytes:
        return b""

    async def exists(self, *, key: str) -> bool:
        return False

    async def delete(self, *, key: str) -> None:
        return None


def test_active_returns_configured_active_backend() -> None:
    local = _FakeStorage(backend="local", bucket="generated")
    s3 = _FakeStorage(backend="s3", bucket="media")
    resolver = StorageResolver(adapters={"local": local, "s3": s3}, active_backend="s3")

    assert resolver.active() is s3


def test_resolve_returns_adapter_for_persisted_backend() -> None:
    local = _FakeStorage(backend="local", bucket="generated")
    s3 = _FakeStorage(backend="s3", bucket="media")
    # Active is s3, but an existing local artifact still resolves to local (W8.5b.5).
    resolver = StorageResolver(adapters={"local": local, "s3": s3}, active_backend="s3")

    assert resolver.resolve("local") is local
    assert resolver.resolve("s3") is s3


def test_resolve_unknown_backend_raises_object_storage_error() -> None:
    local = _FakeStorage(backend="local", bucket="generated")
    resolver = StorageResolver(adapters={"local": local}, active_backend="local")

    with pytest.raises(ObjectStorageError):
        resolver.resolve("r2")


def test_active_backend_must_be_registered() -> None:
    local = _FakeStorage(backend="local", bucket="generated")

    with pytest.raises(ValueError, match="active backend"):
        StorageResolver(adapters={"local": local}, active_backend="s3")


def test_single_wraps_one_adapter_as_active_and_only_backend() -> None:
    local = _FakeStorage(backend="local", bucket="generated")
    resolver = StorageResolver.single(local)

    assert resolver.active() is local
    assert resolver.resolve("local") is local
    with pytest.raises(ObjectStorageError):
        resolver.resolve("s3")
