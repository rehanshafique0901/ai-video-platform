"""Port: ``IStorageResolver`` — pick the object-storage adapter for a backend (Slice α8.5b.2).

Multi-backend storage centralises **backend selection** so no use case is aware of which
concrete adapter serves a given artifact (α8.5b.2 Ruling A — registry). Two selection modes:

* **`active()`** — the single configured *write* backend (``storage_active_backend``). Writers
  persist *new* artifacts here; changing it affects only future writes (W8.5b.5).
* **`resolve(backend)`** — the adapter for an *existing* artifact's persisted
  ``MediaAsset.storage_backend``. Reads / deletes always resolve by the persisted value, never
  the active one, so an artifact is always accessed where it actually lives (W8.5b.5).

The resolver depends only on the :class:`IObjectStorage` port — the concrete cloud SDK stays in
the infrastructure adapters (Ruling D). Implemented as a registry in
``app/infrastructure/storage/storage_resolver.py``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.application.interfaces.object_storage import IObjectStorage


class IStorageResolver(ABC):
    """Resolve a ``storage_backend`` value to the object-storage adapter that serves it."""

    @abstractmethod
    def active(self) -> IObjectStorage:
        """Return the adapter for the single configured *active write* backend (E2)."""
        ...

    @abstractmethod
    def resolve(self, backend: str) -> IObjectStorage:
        """Return the adapter for ``backend`` (an artifact's persisted ``storage_backend``).

        Raises :class:`ObjectStorageError` when no adapter is configured for ``backend`` — the
        caller surfaces that as a clean not-found/failed outcome (never a backend leak).
        """
        ...
