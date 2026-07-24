"""S3-compatible ``IObjectStorage`` adapter (Slice α8.5b.2).

One adapter serves both **AWS S3** and **Cloudflare R2** — R2 is S3-API-compatible, differing
only by the injected endpoint URL + credentials (α8.5b.2 Ruling C). The ``backend`` label it
persists as (``"s3"`` / ``"r2"``) is injected so a written ``MediaAsset`` records the correct
value and reads later resolve back to this adapter (W8.5b.4 / W8.5b.5).

Configuration-blind (W8.1.1): the boto3 client + bucket + backend label are injected at
construction; nothing is fetched at runtime. The boto3 client is **synchronous**, so blocking
calls run in a worker thread (``asyncio.to_thread``) to keep the event loop free — the same
discipline as the local adapter.

**SDK isolation (Ruling D):** ``boto3`` is imported here (an infrastructure leaf) only; no SDK
type crosses the :class:`IObjectStorage` / :class:`IStorageResolver` seams. :func:`build_s3_client`
is the single construction point the composition root calls.
"""

from __future__ import annotations

import asyncio
from typing import Any

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

from app.application.interfaces.object_storage import (
    IObjectStorage,
    ObjectStorageError,
    StoredObject,
)


def build_s3_client(
    *,
    region: str | None,
    endpoint_url: str | None,
    access_key_id: str | None,
    secret_access_key: str | None,
) -> Any:
    """Build a boto3 S3 client (AWS S3 or an S3-compatible endpoint such as R2).

    Signature-version ``s3v4`` is pinned so presigned URLs are valid across S3 and R2.
    Credentials are injected (W8.1.1) — never read from ambient config here.
    """
    return boto3.client(
        "s3",
        region_name=region,
        endpoint_url=endpoint_url,
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_access_key,
        config=BotoConfig(signature_version="s3v4"),
    )


class S3ObjectStorage(IObjectStorage):
    """Store objects in an S3-compatible bucket (AWS S3 or Cloudflare R2)."""

    def __init__(self, *, backend: str, bucket: str, client: Any) -> None:
        self._backend = backend
        self._bucket = bucket
        self._client = client

    @property
    def backend(self) -> str:
        return self._backend

    @property
    def bucket(self) -> str:
        return self._bucket

    async def put(self, *, key: str, data: bytes, content_type: str | None) -> StoredObject:
        kwargs: dict[str, Any] = {"Bucket": self._bucket, "Key": key, "Body": data}
        if content_type is not None:
            kwargs["ContentType"] = content_type
        try:
            await asyncio.to_thread(lambda: self._client.put_object(**kwargs))
        except (BotoCoreError, ClientError) as exc:
            raise ObjectStorageError(f"failed to write object {key!r}: {exc}") from exc
        return StoredObject(backend=self._backend, bucket=self._bucket, key=key)

    async def get(self, *, key: str) -> bytes:
        def _read() -> bytes:
            resp = self._client.get_object(Bucket=self._bucket, Key=key)
            body = resp["Body"].read()
            return bytes(body)

        try:
            return await asyncio.to_thread(_read)
        except (BotoCoreError, ClientError) as exc:
            raise ObjectStorageError(f"failed to read object {key!r}: {exc}") from exc

    async def exists(self, *, key: str) -> bool:
        def _head() -> bool:
            try:
                self._client.head_object(Bucket=self._bucket, Key=key)
                return True
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code")
                if code in ("404", "NoSuchKey", "NotFound"):
                    return False
                raise

        try:
            return await asyncio.to_thread(_head)
        except (BotoCoreError, ClientError) as exc:
            raise ObjectStorageError(f"failed to stat object {key!r}: {exc}") from exc

    async def delete(self, *, key: str) -> None:
        try:
            await asyncio.to_thread(
                lambda: self._client.delete_object(Bucket=self._bucket, Key=key)
            )
        except (BotoCoreError, ClientError) as exc:
            raise ObjectStorageError(f"failed to delete object {key!r}: {exc}") from exc
