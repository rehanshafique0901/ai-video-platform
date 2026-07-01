"""Port: ``IClock`` — process-wide time source.

Introduced by Slice α2b. Use cases depend on this ABC rather than
``datetime.now(UTC)`` so unit tests can freeze or fast-forward time
without monkey-patching stdlib. A single method (``now``) is enough
for every current caller; a richer surface (monotonic, sleep, tz
conversions) can be added in a later slice if a use case actually
needs it — YAGNI keeps the port small.

Scope note: ``JWTService`` and ``AuthTokenIssuer`` continue to call
``datetime.now(UTC)`` directly. Refactoring them to accept an
``IClock`` would thread the port through every JWT call site for
marginal benefit; unit tests already control token issuance via the
``ITokenIssuer`` fake. See the α2b pre-flight §1 Non-goal #9.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime


class IClock(ABC):
    """Return the current wall-clock time. Implementations MUST return tz-aware UTC."""

    @abstractmethod
    def now(self) -> datetime:
        """Return the current time as a tz-aware UTC ``datetime``."""
        ...
