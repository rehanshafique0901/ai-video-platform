"""α8.5e.3 — catalogue reader port.

The Decision plane (resolver) consumes an immutable ``CatalogueSnapshot`` rather than
touching the database or YAML directly (W8.5e.6). This port is the read-only boundary
that materialises that snapshot from the α8.5d catalogue tables (migration 0010).
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.domain.resolver.models import CatalogueSnapshot


class ICatalogueReader(ABC):
    """Read-only accessor that builds a ``CatalogueSnapshot`` from the catalogue tables."""

    @abstractmethod
    async def load_snapshot(self) -> CatalogueSnapshot | None:
        """Return the current catalogue as a single immutable snapshot.

        Returns ``None`` when the catalogue has not been seeded yet (no
        ``provider_registry_meta`` row) so callers can fail fast rather than
        resolve against an empty catalogue.
        """
        ...
