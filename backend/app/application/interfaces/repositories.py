"""Port: repository ABCs.

Slice α1 ships only ``IUserRepository`` with two read-only methods so
the architecture wiring (FastAPI dep → repository → session → DB) can
be proven end-to-end without inventing domain logic. Slice α2 extends
this interface with ``get_by_email``, ``add``, ``update``, etc., and
introduces a ``User`` domain entity to return from those methods.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from uuid import UUID


class IUserRepository(ABC):
    """Read-only User repository surface for α1.

    α2 extends this with mutation methods (``add``, ``update``) and
    entity-returning queries (``get_by_email``) once the ``User``
    domain entity exists.
    """

    @abstractmethod
    async def count(self) -> int:
        """Return the number of non-soft-deleted users."""
        ...

    @abstractmethod
    async def exists_by_id(self, user_id: UUID) -> bool:
        """True iff a non-soft-deleted user row exists with this id."""
        ...
