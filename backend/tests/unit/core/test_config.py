"""Unit tests for ``app.core.config.Settings``."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings

_VALID_JWT_SECRET = "test-secret-do-not-use-in-production-32chars"


@pytest.fixture(autouse=True)
def _clean_settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip every Settings field's env var so each test starts blank."""
    for key in (
        "DATABASE_URL",
        "JWT_SECRET",
        "JWT_ALGORITHM",
        "JWT_ACCESS_TTL_SECONDS",
        "JWT_REFRESH_TTL_SECONDS",
        "LOG_LEVEL",
        "ENVIRONMENT",
    ):
        monkeypatch.delenv(key, raising=False)


@pytest.mark.unit
def test_settings_loads_required_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@h:5432/d")
    monkeypatch.setenv("JWT_SECRET", _VALID_JWT_SECRET)

    s = Settings(_env_file=None)  # type: ignore[call-arg]

    assert s.database_url.startswith("postgresql+psycopg://")
    assert s.jwt_secret.get_secret_value() == _VALID_JWT_SECRET
    assert s.jwt_algorithm == "HS256"
    assert s.jwt_access_ttl_seconds == 900
    assert s.jwt_refresh_ttl_seconds == 2_592_000
    assert s.log_level == "INFO"
    assert s.environment == "local"


@pytest.mark.unit
def test_settings_rejects_missing_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JWT_SECRET", _VALID_JWT_SECRET)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]


@pytest.mark.unit
def test_settings_rejects_short_jwt_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@h:5432/d")
    monkeypatch.setenv("JWT_SECRET", "too-short")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]


@pytest.mark.unit
def test_settings_rejects_invalid_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@h:5432/d")
    monkeypatch.setenv("JWT_SECRET", _VALID_JWT_SECRET)
    monkeypatch.setenv("ENVIRONMENT", "production")  # not in the Literal
    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]


@pytest.mark.unit
def test_settings_rejects_non_positive_ttls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@h:5432/d")
    monkeypatch.setenv("JWT_SECRET", _VALID_JWT_SECRET)
    monkeypatch.setenv("JWT_ACCESS_TTL_SECONDS", "0")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]
