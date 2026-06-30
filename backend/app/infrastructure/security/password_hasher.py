"""Argon2id password hashing (ADR-0008).

Thin wrapper around ``argon2-cffi``. OWASP-default parameters; the
``needs_rehash`` helper enables opportunistic upgrades on next login
when those parameters tighten in a future release of argon2-cffi.
"""

from __future__ import annotations

from argon2 import PasswordHasher as _Argon2Hasher
from argon2.exceptions import InvalidHash, VerifyMismatchError


class PasswordHasher:
    """Argon2id hasher using argon2-cffi's OWASP-aligned defaults."""

    def __init__(self) -> None:
        self._hasher = _Argon2Hasher()

    def hash(self, password: str) -> str:
        """Hash a plaintext password. Returns the encoded Argon2id digest."""
        return self._hasher.hash(password)

    def verify(self, password: str, hashed: str) -> bool:
        """Return True iff ``password`` matches ``hashed``.

        Returns False for any of: wrong password, malformed hash, or
        unknown algorithm. Never raises.
        """
        try:
            return self._hasher.verify(hashed, password)
        except (VerifyMismatchError, InvalidHash):
            return False

    def needs_rehash(self, hashed: str) -> bool:
        """True iff ``hashed`` was computed with weaker-than-current parameters.

        Callers should re-hash and persist the new digest on next
        successful login when this returns True.
        """
        return self._hasher.check_needs_rehash(hashed)
