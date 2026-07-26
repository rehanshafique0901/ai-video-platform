"""Unit tests for the publishing fail-closed posture (α8.6a, ADR-0047 C2).

Production must refuse to boot without a master key; outside production, publishing is simply
unavailable (the credential store cannot be built) — never a plaintext fallback, never an
auto-generated key.
"""

from __future__ import annotations

import pytest

from app.core import container
from app.core.config import Settings

_DB_URL = "postgresql+psycopg://user:pass@localhost:5432/does_not_connect"
_JWT = "unit-test-jwt-secret-at-least-32-characters"


def _settings(*, environment: str, master_key: str | None) -> Settings:
    return Settings(  # type: ignore[call-arg]
        database_url=_DB_URL,
        jwt_secret=_JWT,  # type: ignore[arg-type]
        environment=environment,  # type: ignore[arg-type]
        publishing_credential_master_key=master_key,  # type: ignore[arg-type]
    )


def test_production_without_master_key_refuses_to_boot() -> None:
    container.reset()
    try:
        with pytest.raises(RuntimeError, match="publishing_credential_master_key"):
            container.init(_settings(environment="prod", master_key=None))
    finally:
        container.reset()


def test_production_with_master_key_boots() -> None:
    container.reset()
    try:
        container.init(_settings(environment="prod", master_key="a-production-master-key"))
        # The credential store builds when a key is present.
        assert container.get_social_credential_store() is not None
    finally:
        container.reset()


def test_local_without_master_key_boots_but_publishing_unavailable() -> None:
    container.reset()
    try:
        container.init(_settings(environment="local", master_key=None))
        with pytest.raises(RuntimeError, match="publishing master key is not configured"):
            container.get_social_credential_store()
    finally:
        container.reset()
