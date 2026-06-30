"""Runtime configuration loaded from the process environment.

``Settings`` mirrors the schema of ``backend/.env.validation`` so the
scripts (``ci_gate``, ``validate_schema``, ``regenerate_erd`` via
``scripts/_load_env``) and the runtime application read from a single
source of truth.

``get_settings()`` is ``@lru_cache``-decorated; subsequent calls return
the same instance.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["local", "staging", "prod"]


class Settings(BaseSettings):
    """Process-wide runtime settings.

    Loaded from ``backend/.env.validation`` first, then merged with the
    process environment (env vars override the file). Extra variables
    in the file (e.g. ``SUPABASE_*``) are ignored.
    """

    model_config = SettingsConfigDict(
        env_file=".env.validation",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---- Database (Phase 2 baseline) -------------------------------------
    database_url: str = Field(
        ...,
        description="SQLAlchemy URL using the psycopg3 async driver.",
        min_length=1,
    )

    # ---- JWT (ADR-0008) --------------------------------------------------
    jwt_secret: SecretStr = Field(
        ...,
        description="Symmetric signing key (HS256). Minimum 32 characters.",
        min_length=32,
    )
    jwt_algorithm: str = Field(default="HS256")
    jwt_access_ttl_seconds: int = Field(default=900, gt=0)
    jwt_refresh_ttl_seconds: int = Field(default=2_592_000, gt=0)

    # ---- Observability + environment ------------------------------------
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    environment: Environment = "local"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide ``Settings`` singleton."""
    return Settings()
