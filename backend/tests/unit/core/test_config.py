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
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_TIMEOUT_SECONDS",
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
def test_openai_settings_default_to_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    # α8.1: with no OPENAI_API_KEY the provider stays mock; base URL + timeout have
    # sane defaults so the IMAGE capability never fails to configure.
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@h:5432/d")
    monkeypatch.setenv("JWT_SECRET", _VALID_JWT_SECRET)

    s = Settings(_env_file=None)  # type: ignore[call-arg]

    assert s.openai_api_key is None
    assert s.openai_base_url == "https://api.openai.com/v1"
    assert s.openai_timeout_seconds == 60.0


@pytest.mark.unit
def test_openai_api_key_is_a_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    # The key is a SecretStr — it does not leak in repr and must be unwrapped
    # explicitly (the container does this once when building the shared client).
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@h:5432/d")
    monkeypatch.setenv("JWT_SECRET", _VALID_JWT_SECRET)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-super-secret")
    monkeypatch.setenv("OPENAI_TIMEOUT_SECONDS", "12.5")

    s = Settings(_env_file=None)  # type: ignore[call-arg]

    assert s.openai_api_key is not None
    assert s.openai_api_key.get_secret_value() == "sk-super-secret"
    assert "sk-super-secret" not in repr(s.openai_api_key)
    assert s.openai_timeout_seconds == 12.5


@pytest.mark.unit
def test_openai_timeout_must_be_positive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@h:5432/d")
    monkeypatch.setenv("JWT_SECRET", _VALID_JWT_SECRET)
    monkeypatch.setenv("OPENAI_TIMEOUT_SECONDS", "0")
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
