"""Master-key provider — the externally-managed root of the envelope (ADR-0047 R2).

The master key wraps per-record data encryption keys; it is **never** stored in the
database. α8.6a injects it from configuration (managed externally by the deployment's
secret store) behind :class:`IMasterKeyProvider`. A future cloud-KMS provider (where the
key never leaves the KMS) is a drop-in implementation of the same seam — the envelope model
and the credential service do not change when that swap happens (OQ5).

Production is fail-closed: the provider is only constructed when a master key is present;
there is **no** auto-generation (see ``app.core.container``).
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod


class IMasterKeyProvider(ABC):
    """Supplies the 32-byte master key used to wrap/unwrap per-record data keys."""

    @abstractmethod
    def current(self) -> tuple[str, bytes]:
        """Return ``(key_version, key_bytes)`` for **new** encryptions."""
        ...

    @abstractmethod
    def key_for(self, version: str) -> bytes:
        """Return the 32-byte key for a given ``version`` (for decrypting older records).

        Raises :class:`KeyError` for an unknown version (a rotated-out key not configured).
        """
        ...


class EnvMasterKeyProvider(IMasterKeyProvider):
    """Master key derived from an injected, externally-managed secret string.

    The provided secret is mapped to a 32-byte AES-256 key via SHA-256 so any
    sufficiently-random master secret yields a valid key length deterministically. The raw
    secret is never persisted and never leaves this object.
    """

    def __init__(self, *, version: str, secret: str) -> None:
        if not version:
            raise ValueError("master key version must be non-empty")
        if not secret:
            raise ValueError("master key secret must be non-empty")
        self._version = version
        self._key = hashlib.sha256(secret.encode("utf-8")).digest()

    def current(self) -> tuple[str, bytes]:
        return self._version, self._key

    def key_for(self, version: str) -> bytes:
        if version != self._version:
            raise KeyError(f"master key version {version!r} is not configured")
        return self._key


__all__ = ["EnvMasterKeyProvider", "IMasterKeyProvider"]
