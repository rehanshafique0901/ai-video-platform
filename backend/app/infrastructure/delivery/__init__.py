"""Download-delivery adapters (Slice α8.5b.1; multi-backend α8.5b.2).

α8.5b.1 shipped the local streaming adapter. α8.5b.2 adds :class:`S3RedirectDelivery` (fixed-TTL
presigned URLs for S3/R2) and the :class:`DeliveryResolver` registry that dispatches per persisted
backend — all behind the same
:class:`~app.application.interfaces.download_delivery.IDownloadDelivery` port.
"""

from __future__ import annotations

from app.infrastructure.delivery.delivery_resolver import DeliveryResolver
from app.infrastructure.delivery.local_stream_delivery import LocalStreamDelivery
from app.infrastructure.delivery.s3_redirect_delivery import S3RedirectDelivery

__all__ = [
    "DeliveryResolver",
    "LocalStreamDelivery",
    "S3RedirectDelivery",
]
