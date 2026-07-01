"""``SystemClock`` — concrete ``IClock`` returning process wall-clock time.

Deliberately trivial. Kept in its own module so the composition root
(``app.core.container``) can construct one without pulling any wider
infrastructure surface into the port import graph.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.application.interfaces.clock import IClock


class SystemClock(IClock):
    """Returns ``datetime.now(UTC)``. Tz-aware by construction."""

    def now(self) -> datetime:
        return datetime.now(UTC)
