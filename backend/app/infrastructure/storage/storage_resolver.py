"""``StorageResolver`` — backend → object-storage adapter registry (Slice α8.5b.2).

A concrete :class:`IStorageResolver`: a ``{backend → IObjectStorage}`` map built once at
composition-root init, plus the single configured *active write* backend. Writers persist new
artifacts via :meth:`active`; reads/deletes resolve an existing artifact's persisted
``storage_backend`` via :meth:`resolve` (W8.5b.4 / W8.5b.5).

Holds only :class:`IObjectStorage` ports — no cloud SDK import (Ruling D). Backend selection is
centralised here so no use case is backend-aware (Ruling A).
"""

from __future__ import annotations

from app.application.interfaces.object_storage import IObjectStorage, ObjectStorageError
from app.application.interfaces.storage_resolver import IStorageResolver


class StorageResolver(IStorageResolver):
    """Registry of object-storage adapters keyed by ``storage_backend``."""

    def __init__(self, *, adapters: dict[str, IObjectStorage], active_backend: str) -> None:
        if active_backend not in adapters:
            raise ValueError(
                f"active backend {active_backend!r} has no configured storage adapter "
                f"(configured: {sorted(adapters)})"
            )
        self._adapters = dict(adapters)
        self._active_backend = active_backend

    @classmethod
    def single(cls, storage: IObjectStorage) -> StorageResolver:
        """Build a resolver with one adapter serving as both the active + only backend.

        Convenience for the local-only default and for tests (mirrors the pre-α8.5b.2 single-
        instance behaviour): ``{storage.backend: storage}`` with ``storage.backend`` active.
        """
        return cls(adapters={storage.backend: storage}, active_backend=storage.backend)

    def active(self) -> IObjectStorage:
        return self._adapters[self._active_backend]

    def resolve(self, backend: str) -> IObjectStorage:
        try:
            return self._adapters[backend]
        except KeyError as exc:
            raise ObjectStorageError(
                f"no storage adapter configured for backend {backend!r}"
            ) from exc
