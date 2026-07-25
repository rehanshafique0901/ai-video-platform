"""α8.5e.4 — runtime-state reader port.

The resolver reads operational state (health, quota, metrics) as an immutable
``RuntimeSnapshot`` and never mutates it (W8.5e.3). This port is the read-only boundary
over the α8.5e operational tables (migration 0011).
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.domain.resolver.models import RuntimeSnapshot


class IRuntimeStateReader(ABC):
    """Read-only accessor that builds a ``RuntimeSnapshot`` from the operational tables."""

    @abstractmethod
    async def load_snapshot(self) -> RuntimeSnapshot:
        """Return current operational state (empty maps when nothing is recorded yet)."""
        ...
