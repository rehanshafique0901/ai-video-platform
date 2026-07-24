"""Object-storage adapters (Slice α8.4a; multi-backend α8.5b.2).

α8.4a shipped :class:`LocalObjectStorage` (filesystem) behind the neutral ``IObjectStorage``
port. α8.5b.2 adds :class:`S3ObjectStorage` (AWS S3 / Cloudflare R2) and the
:class:`StorageResolver` registry that selects the adapter per backend — no use case is
backend-aware. GCS / Azure / MinIO adapters plug in later the same way.
"""

from __future__ import annotations

from app.infrastructure.storage.local_object_storage import LocalObjectStorage
from app.infrastructure.storage.s3_object_storage import S3ObjectStorage, build_s3_client
from app.infrastructure.storage.storage_resolver import StorageResolver

__all__ = [
    "LocalObjectStorage",
    "S3ObjectStorage",
    "StorageResolver",
    "build_s3_client",
]
