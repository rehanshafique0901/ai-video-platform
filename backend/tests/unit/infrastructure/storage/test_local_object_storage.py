"""Unit tests for the local-filesystem object-storage adapter (Slice α8.4a)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.application.interfaces.object_storage import ObjectStorageError
from app.infrastructure.storage import LocalObjectStorage

pytestmark = pytest.mark.unit


def _storage(tmp_path: Path, bucket: str = "generated") -> LocalObjectStorage:
    return LocalObjectStorage(root=str(tmp_path), bucket=bucket)


async def test_put_get_roundtrip_and_coordinates(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    stored = await storage.put(key="a/b/c.png", data=b"pixels", content_type="image/png")

    assert stored.backend == "local"
    assert stored.bucket == "generated"
    assert stored.key == "a/b/c.png"
    assert await storage.get(key="a/b/c.png") == b"pixels"
    # Physically under <root>/<bucket>/<key>.
    assert (tmp_path / "generated" / "a" / "b" / "c.png").read_bytes() == b"pixels"


async def test_exists_and_idempotent_delete(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    assert await storage.exists(key="x.bin") is False
    await storage.put(key="x.bin", data=b"1", content_type=None)
    assert await storage.exists(key="x.bin") is True

    await storage.delete(key="x.bin")
    assert await storage.exists(key="x.bin") is False
    # Deleting a missing object is a no-op, not an error.
    await storage.delete(key="x.bin")


async def test_put_overwrites_idempotently(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    await storage.put(key="k", data=b"first", content_type=None)
    await storage.put(key="k", data=b"first", content_type=None)
    assert await storage.get(key="k") == b"first"


async def test_get_missing_raises(tmp_path: Path) -> None:
    with pytest.raises(ObjectStorageError):
        await _storage(tmp_path).get(key="nope")


async def test_path_traversal_is_rejected(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    with pytest.raises(ObjectStorageError):
        await storage.put(key="../escape", data=b"x", content_type=None)
