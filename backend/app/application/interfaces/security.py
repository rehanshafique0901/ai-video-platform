"""Ports for security primitives — password hashing and token issuance.

Introduced by Slice α2a (approved improvement B). Use cases depend on
these ABCs rather than on concrete infrastructure so unit tests can
substitute deterministic in-memory fakes (crucial for the auth suite:
Argon2id with OWASP defaults is ~300 ms per verify, which would inflate
a 30-test unit run into a 10-second hashing marathon).

The two ports plus the ``IssuedTokens`` value object also decouple the
application layer from the JWT library. If a later phase switches to
PASETO or opaque tokens the swap is a single infrastructure module.

Rationale for the ``session_id`` (``sid``) claim in **both** access and
refresh tokens: without it, ``LogoutSession`` (Slice α2b) has no way to
identify which ``sessions`` row to revoke — the access token would only
carry ``sub`` and could at best log the user out of every device. The
``family_id`` (``fam``) claim on the access token is a small bonus for
future admin tooling ("terminate this family from every request that
inherits it"). The α1 ``JWTService`` already emits ``fam`` on refresh
tokens; α2a's ``AuthTokenIssuer`` extends both tokens with ``sid``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import NamedTuple
from uuid import UUID

from app.domain.identity.user import User


class IssuedTokens(NamedTuple):
    """One rotation's worth of tokens plus everything needed to persist a ``sessions`` row.

    ``refresh_token_hash`` is ``sha256(refresh_token)`` — the raw JWT is
    never stored. ``session_id`` is generated up-front (not database-
    assigned) so it can be embedded in the ``sid`` claim before the JWT
    is signed.
    """

    access_token: str
    refresh_token: str
    refresh_token_hash: str
    session_id: UUID
    family_id: UUID
    issued_at: datetime
    refresh_expires_at: datetime


class TokenClaims(NamedTuple):
    """Decoded, validated claims from an access or refresh token."""

    subject: UUID
    session_id: UUID
    family_id: UUID
    expires_at: datetime


class IPasswordHasher(ABC):
    """Password hashing + verification. Argon2id (ADR-0008) in production."""

    @abstractmethod
    def hash(self, password: str) -> str:
        """Hash a plaintext password. Returns an encoded digest."""
        ...

    @abstractmethod
    def verify(self, password: str, hashed: str) -> bool:
        """Return True iff ``password`` matches ``hashed``. Never raises."""
        ...

    @abstractmethod
    def needs_rehash(self, hashed: str) -> bool:
        """True iff ``hashed`` was computed with weaker-than-current parameters."""
        ...


class ITokenIssuer(ABC):
    """Bundle of "issue access + refresh tokens + hash + generate ids".

    Wraps the low-level JWT service so the use case never touches JWT
    encoding, SHA-256, or UUID minting for tokens directly.
    """

    @abstractmethod
    def issue_for_login(self, user: User) -> IssuedTokens:
        """Fresh ``family_id`` and fresh ``session_id`` — used by register + login."""
        ...

    @abstractmethod
    def issue_for_rotation(self, user: User, family_id: UUID) -> IssuedTokens:
        """Preserve ``family_id``, fresh ``session_id`` — used by α2b refresh."""
        ...

    @abstractmethod
    def verify_access(self, token: str) -> TokenClaims:
        """Decode + validate an access token. Raises ``UnauthorizedError`` on any failure."""
        ...

    @abstractmethod
    def verify_refresh(self, token: str) -> TokenClaims:
        """Decode + validate a refresh token. Raises ``UnauthorizedError`` on any failure."""
        ...
