"""Object-storage adapters (Slice α8.4a).

α8.4a ships one adapter — :class:`LocalObjectStorage` (filesystem) — behind the
neutral ``IObjectStorage`` port. S3 / R2 / GCS / Azure / MinIO adapters plug in
later without changing any use case.
"""

from __future__ import annotations

from app.infrastructure.storage.local_object_storage import LocalObjectStorage

__all__ = ["LocalObjectStorage"]
