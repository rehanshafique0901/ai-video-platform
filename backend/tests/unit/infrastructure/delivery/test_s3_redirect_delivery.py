"""Unit tests for ``S3RedirectDelivery`` (Slice α8.5b.2).

Fixed-TTL presigned-URL delivery for S3/R2 (Fork F). Pins:

* returns a ``RedirectDelivery`` to the presigned URL with ``expires_at`` populated;
* the fixed TTL is passed to the signer (no per-request customization);
* a backend/bucket mismatch (resolver misroute) raises ``DownloadDeliveryError``;
* a signer/transport error maps to the neutral ``DownloadDeliveryError`` (no SDK leak).

The presign is offline — the stub signer performs no network I/O, mirroring
``botocore.generate_presigned_url``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from botocore.exceptions import BotoCoreError

from app.application.interfaces.download_delivery import (
    DownloadDeliveryError,
    DownloadRequest,
    RedirectDelivery,
)
from app.infrastructure.delivery import S3RedirectDelivery

pytestmark = pytest.mark.unit


class _StubSigner:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def generate_presigned_url(
        self, op: str, *, Params: dict[str, str], ExpiresIn: int  # noqa: N803
    ) -> str:
        self.calls.append({"op": op, "Params": Params, "ExpiresIn": ExpiresIn})
        return f"https://s3.example/{Params['Bucket']}/{Params['Key']}?exp={ExpiresIn}"


def _delivery(client: object, *, backend: str = "s3", ttl: int = 900) -> S3RedirectDelivery:
    return S3RedirectDelivery(backend=backend, bucket="media", client=client, ttl_seconds=ttl)


def _request(*, backend: str = "s3", bucket: str = "media") -> DownloadRequest:
    return DownloadRequest(
        storage_backend=backend,
        storage_bucket=bucket,
        storage_key="exports/final.mp4",
        media_type="video/mp4",
        filename="export.mp4",
        content_length=10,
    )


async def test_returns_redirect_with_presigned_url_and_expiry() -> None:
    signer = _StubSigner()
    delivery = _delivery(signer, ttl=600)

    before = datetime.now(UTC)
    decision = await delivery.deliver(_request())

    assert isinstance(decision, RedirectDelivery)
    assert decision.url == "https://s3.example/media/exports/final.mp4?exp=600"
    assert decision.expires_at is not None
    delta = (decision.expires_at - before).total_seconds()
    assert 595 <= delta <= 620  # ~ now + fixed TTL
    assert signer.calls[0]["ExpiresIn"] == 600


async def test_backend_mismatch_raises() -> None:
    delivery = _delivery(_StubSigner(), backend="s3")

    with pytest.raises(DownloadDeliveryError):
        await delivery.deliver(_request(backend="r2"))


async def test_bucket_mismatch_raises() -> None:
    delivery = _delivery(_StubSigner())

    with pytest.raises(DownloadDeliveryError):
        await delivery.deliver(_request(bucket="other-bucket"))


async def test_signer_error_maps_to_delivery_error() -> None:
    class _BrokenSigner:
        def generate_presigned_url(self, *args: object, **kwargs: object) -> str:
            raise _SomeBotoError()

    class _SomeBotoError(BotoCoreError):
        fmt = "boom"

    delivery = _delivery(_BrokenSigner())

    with pytest.raises(DownloadDeliveryError):
        await delivery.deliver(_request())
