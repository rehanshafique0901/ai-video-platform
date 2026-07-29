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
        "FAL_API_KEY",
        "FAL_BASE_URL",
        "FAL_TIMEOUT_SECONDS",
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
def test_fal_settings_default_to_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    # α8.2: with no FAL_API_KEY the VIDEO capability stays mock; base URL + timeout
    # have sane defaults so the capability never fails to configure.
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@h:5432/d")
    monkeypatch.setenv("JWT_SECRET", _VALID_JWT_SECRET)

    s = Settings(_env_file=None)  # type: ignore[call-arg]

    assert s.fal_api_key is None
    assert s.fal_base_url == "https://queue.fal.run"
    assert s.fal_timeout_seconds == 60.0


@pytest.mark.unit
def test_fal_api_key_is_a_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    # The key is a SecretStr — it does not leak in repr and must be unwrapped
    # explicitly (the container does this once when building the shared client).
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@h:5432/d")
    monkeypatch.setenv("JWT_SECRET", _VALID_JWT_SECRET)
    monkeypatch.setenv("FAL_API_KEY", "fal-super-secret")
    monkeypatch.setenv("FAL_TIMEOUT_SECONDS", "12.5")

    s = Settings(_env_file=None)  # type: ignore[call-arg]

    assert s.fal_api_key is not None
    assert s.fal_api_key.get_secret_value() == "fal-super-secret"
    assert "fal-super-secret" not in repr(s.fal_api_key)
    assert s.fal_timeout_seconds == 12.5


@pytest.mark.unit
def test_fal_timeout_must_be_positive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@h:5432/d")
    monkeypatch.setenv("JWT_SECRET", _VALID_JWT_SECRET)
    monkeypatch.setenv("FAL_TIMEOUT_SECONDS", "0")
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


@pytest.mark.unit
def test_shipped_worker_defaults_satisfy_the_pf4_shutdown_principle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """α9.8 PF4 — batch size is chosen by shutdown semantics, not throughput.

    A pass must be able to finish inside its drain budget, or the host will routinely cut it off
    mid-batch on every deploy. For email that is not merely untidy: an interrupted pass has sent
    messages it never got to stamp, so ADR-0051's rare-duplicate window becomes an ordinary
    consequence of shipping. The shipped defaults are the ones that have to hold, so they are what
    this asserts.
    """
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@h:5432/d")
    monkeypatch.setenv("JWT_SECRET", _VALID_JWT_SECRET)
    s = Settings(_env_file=None)  # type: ignore[call-arg]

    # Email is the only worker that both batches and has a bounded per-item cost, so its
    # worst-case pass is computable — and must fit in the budget it is actually given.
    worst_case_email_pass = s.email_batch_size * s.email_send_timeout_seconds
    assert worst_case_email_pass <= s.worker_drain_budget_seconds, (
        f"an email pass can take {worst_case_email_pass}s but is drained after "
        f"{s.worker_drain_budget_seconds}s: sends would be cut off and redelivered every deploy"
    )

    # The long-running workers claim one item per pass for the same reason (PF4).
    assert s.generation_worker_batch_size == 1
    assert s.render_batch_size == 1
    assert s.export_batch_size == 1
    assert s.enrichment_batch_size == 1
    assert s.publish_batch_size == 1
