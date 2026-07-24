"""Unit tests for ``S3ObjectStorage`` (Slice α8.5b.2).

Drives the S3/R2 adapter against a stub boto3 client (no network, no real AWS). Pins:

* ``put`` records backend/bucket/key + content-type and returns the ``StoredObject`` triple.
* ``get`` reads the object body back as bytes.
* ``exists`` maps a 404 ``ClientError`` to ``False`` (present → ``True``).
* ``delete`` removes the object.
* SDK/transport errors are mapped to the neutral ``ObjectStorageError`` (no SDK leak).

The ``backend`` label is injected (``s3`` / ``r2``) — the same adapter serves both.
"""

from __future__ import annotations

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from app.application.interfaces.object_storage import ObjectStorageError
from app.infrastructure.storage import S3ObjectStorage

pytestmark = pytest.mark.unit


class _StubS3Client:
    """Minimal in-memory stand-in for a boto3 S3 client."""

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], tuple[bytes, str | None]] = {}
        self.put_calls: list[dict[str, object]] = []

    def put_object(self, **kwargs: object) -> dict[str, object]:
        self.put_calls.append(kwargs)
        bucket = str(kwargs["Bucket"])
        key = str(kwargs["Key"])
        body = kwargs["Body"]
        assert isinstance(body, bytes)
        self.store[(bucket, key)] = (body, kwargs.get("ContentType"))  # type: ignore[assignment]
        return {}

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, object]:  # noqa: N803
        try:
            body, _ = self.store[(Bucket, Key)]
        except KeyError as exc:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject") from exc
        return {"Body": _Body(body)}

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, object]:  # noqa: N803
        if (Bucket, Key) not in self.store:
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
        return {}

    def delete_object(self, *, Bucket: str, Key: str) -> dict[str, object]:  # noqa: N803
        self.store.pop((Bucket, Key), None)
        return {}


class _Body:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


def _adapter(client: _StubS3Client, *, backend: str = "s3") -> S3ObjectStorage:
    return S3ObjectStorage(backend=backend, bucket="media", client=client)


async def test_put_stores_and_returns_triple() -> None:
    client = _StubS3Client()
    adapter = _adapter(client, backend="r2")

    stored = await adapter.put(key="a/b.mp4", data=b"BYTES", content_type="video/mp4")

    assert (stored.backend, stored.bucket, stored.key) == ("r2", "media", "a/b.mp4")
    assert client.store[("media", "a/b.mp4")] == (b"BYTES", "video/mp4")
    assert client.put_calls[0]["ContentType"] == "video/mp4"


async def test_get_reads_body_back() -> None:
    client = _StubS3Client()
    adapter = _adapter(client)
    await adapter.put(key="k", data=b"HELLO", content_type=None)

    assert await adapter.get(key="k") == b"HELLO"


async def test_exists_true_then_false() -> None:
    client = _StubS3Client()
    adapter = _adapter(client)
    await adapter.put(key="k", data=b"x", content_type=None)

    assert await adapter.exists(key="k") is True
    assert await adapter.exists(key="missing") is False


async def test_delete_removes_object() -> None:
    client = _StubS3Client()
    adapter = _adapter(client)
    await adapter.put(key="k", data=b"x", content_type=None)

    await adapter.delete(key="k")

    assert await adapter.exists(key="k") is False


async def test_get_missing_maps_to_object_storage_error() -> None:
    client = _StubS3Client()
    adapter = _adapter(client)

    with pytest.raises(ObjectStorageError):
        await adapter.get(key="nope")


async def test_transport_error_maps_to_object_storage_error() -> None:
    class _BrokenClient(_StubS3Client):
        def put_object(self, **kwargs: object) -> dict[str, object]:
            raise EndpointConnectionError(endpoint_url="https://s3.example")

    adapter = _adapter(_BrokenClient())

    with pytest.raises(ObjectStorageError):
        await adapter.put(key="k", data=b"x", content_type=None)
