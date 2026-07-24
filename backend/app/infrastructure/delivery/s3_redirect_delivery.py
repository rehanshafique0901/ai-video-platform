"""S3-compatible signed-URL ``IDownloadDelivery`` adapter (Slice α8.5b.2).

Delivers a cloud-stored artifact by handing the client a short-lived **presigned URL** and
letting the object store serve the bytes — keeping large transfers off the API workers. Serves
both AWS S3 and Cloudflare R2 (S3-API-compatible; α8.5b.2 Ruling C).

Signing lives **here, in the delivery adapter** (Ruling B) — storage owns persistence, delivery
owns transport — so :class:`IObjectStorage` never grows a ``signed_url()`` method. The presign is
**offline**: ``botocore.generate_presigned_url`` computes a signature locally with no network
call on the request path.

Fixed, centrally-configured TTL (Ruling F): the same ``ttl_seconds`` applies to every URL (no
per-request customization); ``RedirectDelivery.expires_at`` is populated so callers can observe
the expiry. Pure transfer (W8.5b.2): a redirect re-encodes nothing.

**SDK isolation (Ruling D):** ``botocore`` is imported here only; no SDK type crosses the
:class:`IDownloadDelivery` seam.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from app.application.interfaces.download_delivery import (
    DownloadDeliveryError,
    DownloadRequest,
    IDownloadDelivery,
    RedirectDelivery,
)


class S3RedirectDelivery(IDownloadDelivery):
    """Deliver a cloud artifact via a fixed-TTL presigned GET URL (302 redirect)."""

    def __init__(self, *, backend: str, bucket: str, client: Any, ttl_seconds: int) -> None:
        self._backend = backend
        self._bucket = bucket
        self._client = client
        self._ttl_seconds = ttl_seconds

    async def deliver(self, request: DownloadRequest) -> RedirectDelivery:
        # This adapter only serves objects in its own backend/bucket; a mismatch means the
        # resolver mapped the wrong adapter — surface it explicitly rather than signing a URL
        # for an object that is not here.
        if request.storage_backend != self._backend or request.storage_bucket != self._bucket:
            raise DownloadDeliveryError(
                "artifact is not served by this S3 delivery adapter "
                f"({request.storage_backend}/{request.storage_bucket})"
            )
        try:
            url = self._client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self._bucket, "Key": request.storage_key},
                ExpiresIn=self._ttl_seconds,
            )
        except (BotoCoreError, ClientError) as exc:
            raise DownloadDeliveryError(
                f"failed to presign object {request.storage_key!r}: {exc}"
            ) from exc
        expires_at = datetime.now(UTC) + timedelta(seconds=self._ttl_seconds)
        return RedirectDelivery(url=url, expires_at=expires_at)
