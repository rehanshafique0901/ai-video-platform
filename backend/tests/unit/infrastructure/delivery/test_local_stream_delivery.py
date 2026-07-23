"""Unit tests for ``LocalStreamDelivery`` (Slice α8.5b.1).

The local streaming adapter reads a stored object once and yields it in chunks, preserving the
bytes exactly (W8.5b.2 — pure transfer). It refuses objects that do not live in its own
backend/bucket (that is the α8.5b.2 cloud adapters' job) and surfaces a missing object as
``ObjectStorageError``.
"""

from __future__ import annotations

import pytest

from app.application.interfaces.download_delivery import (
    DownloadDeliveryError,
    DownloadRequest,
    StreamDelivery,
)
from app.application.interfaces.object_storage import ObjectStorageError
from app.infrastructure.delivery.local_stream_delivery import LocalStreamDelivery
from tests.unit.application.use_cases.export._helpers import FakeObjectStorage

pytestmark = pytest.mark.unit


def _request(storage: FakeObjectStorage, *, key: str, backend: str, bucket: str) -> DownloadRequest:
    return DownloadRequest(
        storage_backend=backend,
        storage_bucket=bucket,
        storage_key=key,
        media_type="video/mp4",
        filename="out.mp4",
        content_length=None,
    )


async def test_streams_object_in_chunks_preserving_bytes() -> None:
    storage = FakeObjectStorage()
    payload = b"0123456789ABCDEF"
    storage.objects["k.mp4"] = payload
    delivery = LocalStreamDelivery(storage, chunk_size=5)

    decision = await delivery.deliver(
        _request(storage, key="k.mp4", backend=storage.backend, bucket=storage.bucket)
    )

    assert isinstance(decision, StreamDelivery)
    chunks = [c async for c in decision.chunks]
    assert chunks == [b"01234", b"56789", b"ABCDE", b"F"]  # chunked
    assert b"".join(chunks) == payload  # byte-identical
    assert decision.content_length == len(payload)
    assert decision.media_type == "video/mp4"
    assert decision.filename == "out.mp4"


async def test_foreign_backend_rejected() -> None:
    storage = FakeObjectStorage()
    storage.objects["k.mp4"] = b"x"
    delivery = LocalStreamDelivery(storage)

    with pytest.raises(DownloadDeliveryError):
        await delivery.deliver(_request(storage, key="k.mp4", backend="s3", bucket=storage.bucket))


async def test_foreign_bucket_rejected() -> None:
    storage = FakeObjectStorage()
    storage.objects["k.mp4"] = b"x"
    delivery = LocalStreamDelivery(storage)

    with pytest.raises(DownloadDeliveryError):
        await delivery.deliver(
            _request(storage, key="k.mp4", backend=storage.backend, bucket="other")
        )


async def test_missing_object_raises_storage_error() -> None:
    storage = FakeObjectStorage()
    delivery = LocalStreamDelivery(storage)

    with pytest.raises(ObjectStorageError):
        await delivery.deliver(
            _request(storage, key="absent.mp4", backend=storage.backend, bucket=storage.bucket)
        )
