"""Port: ``IOAuthStateSigner`` — signed, stateless OAuth ``state`` tokens (α8.6a).

The OAuth ``state`` parameter is CSRF protection and the only thing that carries the
acting user across the provider redirect (the ``GET /callback`` request has no bearer). It
is a **short-lived, signed, stateless** token — no persistence table (OQ3). ``verify`` is
fail-closed: any missing / malformed / expired / tampered token raises
:class:`InvalidConnectionStateError`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from uuid import UUID


class InvalidConnectionStateError(Exception):
    """The OAuth ``state`` token is missing, malformed, expired, or tampered."""


@dataclass(frozen=True, slots=True)
class ConnectionState:
    """The identity + intent bound into an OAuth ``state`` token.

    ``tenant_id`` travels in the token so the callback can create the ``SocialAccount``
    with correct scoping without a second lookup (the callback is unauthenticated apart
    from this signed state).
    """

    user_id: UUID
    tenant_id: UUID
    platform: str


class IOAuthStateSigner(ABC):
    """Sign + verify the OAuth ``state`` token bound to one connection attempt."""

    @abstractmethod
    def sign(self, state: ConnectionState) -> str:
        """Return a signed, short-lived token encoding ``state``."""
        ...

    @abstractmethod
    def verify(self, token: str) -> ConnectionState:
        """Decode + validate a state token. Raises :class:`InvalidConnectionStateError`."""
        ...


__all__ = ["ConnectionState", "IOAuthStateSigner", "InvalidConnectionStateError"]
