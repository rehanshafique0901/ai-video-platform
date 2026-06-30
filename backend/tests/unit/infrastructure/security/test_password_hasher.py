"""Unit tests for ``app.infrastructure.security.password_hasher.PasswordHasher``."""

from __future__ import annotations

import pytest

from app.infrastructure.security.password_hasher import PasswordHasher


@pytest.mark.unit
def test_hash_then_verify_roundtrip() -> None:
    hasher = PasswordHasher()
    digest = hasher.hash("correct horse battery staple")
    assert hasher.verify("correct horse battery staple", digest) is True


@pytest.mark.unit
def test_verify_rejects_wrong_password() -> None:
    hasher = PasswordHasher()
    digest = hasher.hash("right")
    assert hasher.verify("wrong", digest) is False


@pytest.mark.unit
def test_verify_rejects_malformed_hash_without_raising() -> None:
    hasher = PasswordHasher()
    assert hasher.verify("anything", "not-an-argon2-hash") is False


@pytest.mark.unit
def test_hash_outputs_argon2id_prefix() -> None:
    hasher = PasswordHasher()
    digest = hasher.hash("anything")
    assert digest.startswith("$argon2id$"), digest


@pytest.mark.unit
def test_needs_rehash_returns_bool() -> None:
    hasher = PasswordHasher()
    digest = hasher.hash("password")
    assert isinstance(hasher.needs_rehash(digest), bool)


@pytest.mark.unit
def test_hash_is_non_deterministic() -> None:
    hasher = PasswordHasher()
    a = hasher.hash("same-password")
    b = hasher.hash("same-password")
    assert a != b
