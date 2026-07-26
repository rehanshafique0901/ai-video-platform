"""Envelope encryption for stored OAuth tokens (ADR-0047 R2) — AES-256-GCM.

Two-layer envelope, using only ``cryptography``'s AEAD primitives (no bespoke crypto):

    master key (externally managed)  ── wraps ──▶  per-record DEK  ── encrypts ──▶  tokens

Each :meth:`encrypt` mints a fresh random 256-bit data encryption key (DEK) and a fresh
96-bit nonce (nonce uniqueness), encrypts the plaintext with ``AES-256-GCM`` (authenticated
— tamper is detected on decrypt), then wraps the DEK under the current master key with its
own AES-GCM nonce. The database stores only ``ciphertext`` + ``nonce`` + ``wrapped_dek``
(``wrap_nonce || wrapped``) + ``key_version`` — never the DEK, never the master key, never
plaintext (C1/C2). Rotation is a re-encrypt under a new ``key_version``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.application.interfaces.social_credential_store import CredentialDecryptionError
from app.infrastructure.publishing.credentials.master_key import IMasterKeyProvider

_NONCE_BYTES = 12  # 96-bit GCM nonce (recommended)
_DEK_BITS = 256


@dataclass(frozen=True, slots=True)
class EncryptedBlob:
    """The persisted envelope fields for one encrypted payload."""

    ciphertext: bytes
    nonce: bytes
    wrapped_dek: bytes
    key_version: str
    algorithm: str


class EnvelopeCipher:
    """Encrypt/decrypt token payloads under a master-key-wrapped per-record DEK."""

    ALGORITHM = "AES-256-GCM"

    def __init__(self, master_keys: IMasterKeyProvider) -> None:
        self._master_keys = master_keys

    def encrypt(self, plaintext: bytes) -> EncryptedBlob:
        version, master = self._master_keys.current()
        dek = AESGCM.generate_key(bit_length=_DEK_BITS)
        nonce = os.urandom(_NONCE_BYTES)
        ciphertext = AESGCM(dek).encrypt(nonce, plaintext, None)
        wrap_nonce = os.urandom(_NONCE_BYTES)
        wrapped = AESGCM(master).encrypt(wrap_nonce, dek, None)
        return EncryptedBlob(
            ciphertext=ciphertext,
            nonce=nonce,
            wrapped_dek=wrap_nonce + wrapped,
            key_version=version,
            algorithm=self.ALGORITHM,
        )

    def decrypt(self, blob: EncryptedBlob) -> bytes:
        try:
            master = self._master_keys.key_for(blob.key_version)
        except KeyError as e:
            raise CredentialDecryptionError(
                f"master key version {blob.key_version!r} unavailable"
            ) from e
        wrap_nonce, wrapped = blob.wrapped_dek[:_NONCE_BYTES], blob.wrapped_dek[_NONCE_BYTES:]
        try:
            dek = AESGCM(master).decrypt(wrap_nonce, wrapped, None)
            return AESGCM(dek).decrypt(blob.nonce, blob.ciphertext, None)
        except InvalidTag as e:
            raise CredentialDecryptionError("stored credential failed integrity check") from e


__all__ = ["EncryptedBlob", "EnvelopeCipher"]
