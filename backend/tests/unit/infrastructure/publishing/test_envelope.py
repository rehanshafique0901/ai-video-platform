"""Unit tests for the envelope cipher (α8.6a, ADR-0047 R2).

Proves the credential-encryption primitive in isolation (no DB): round-trip fidelity,
authenticated-encryption tamper detection, key isolation, nonce uniqueness, and rotation
version handling.
"""

from __future__ import annotations

import pytest

from app.application.interfaces.social_credential_store import CredentialDecryptionError
from app.infrastructure.publishing.credentials.envelope import EnvelopeCipher
from app.infrastructure.publishing.credentials.master_key import EnvMasterKeyProvider


def _cipher(secret: str = "master-secret-alpha", version: str = "v1") -> EnvelopeCipher:
    return EnvelopeCipher(EnvMasterKeyProvider(version=version, secret=secret))


def test_round_trip_recovers_plaintext() -> None:
    cipher = _cipher()
    plaintext = b'{"access_token": "abc", "refresh_token": "xyz"}'
    blob = cipher.encrypt(plaintext)
    assert cipher.decrypt(blob) == plaintext


def test_ciphertext_is_not_plaintext_and_carries_metadata() -> None:
    cipher = _cipher(version="v7")
    plaintext = b"super-secret-access-token"
    blob = cipher.encrypt(plaintext)
    assert plaintext not in blob.ciphertext
    assert blob.key_version == "v7"
    assert blob.algorithm == "AES-256-GCM"
    assert len(blob.nonce) == 12
    # wrapped_dek = 12-byte wrap nonce + wrapped key + GCM tag (never the bare 32-byte DEK).
    assert len(blob.wrapped_dek) > 32


def test_nonce_and_ciphertext_differ_across_encryptions() -> None:
    cipher = _cipher()
    plaintext = b"same-input"
    a = cipher.encrypt(plaintext)
    b = cipher.encrypt(plaintext)
    assert a.nonce != b.nonce
    assert a.ciphertext != b.ciphertext
    assert a.wrapped_dek != b.wrapped_dek


def test_tampered_ciphertext_is_rejected() -> None:
    cipher = _cipher()
    blob = cipher.encrypt(b"authentic")
    tampered = type(blob)(
        ciphertext=bytes(blob.ciphertext[:-1] + bytes([blob.ciphertext[-1] ^ 0x01])),
        nonce=blob.nonce,
        wrapped_dek=blob.wrapped_dek,
        key_version=blob.key_version,
        algorithm=blob.algorithm,
    )
    with pytest.raises(CredentialDecryptionError):
        cipher.decrypt(tampered)


def test_wrong_master_key_cannot_decrypt() -> None:
    blob = _cipher(secret="key-one").encrypt(b"secret")
    with pytest.raises(CredentialDecryptionError):
        _cipher(secret="key-two").decrypt(blob)


def test_unknown_key_version_is_rejected() -> None:
    blob = _cipher(version="v1").encrypt(b"secret")
    # A provider configured only with v2 cannot supply the v1 key.
    other = EnvelopeCipher(EnvMasterKeyProvider(version="v2", secret="master-secret-alpha"))
    with pytest.raises(CredentialDecryptionError):
        other.decrypt(blob)
