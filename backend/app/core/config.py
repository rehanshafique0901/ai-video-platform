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

    # ---- Providers · OpenAI Images (Slice α8.1) -------------------------
    # Optional. Present → the process wires the real synchronous
    # ``OpenAIImageProvider`` for ``Capability.IMAGE``; absent → the IMAGE
    # capability stays on ``MockImageProvider`` (LLM/VIDEO/VOICE always mock).
    # The container injects this key into the provider's shared httpx client;
    # the provider never reads it (W8.1.1 — adapters are configuration-blind).
    openai_api_key: SecretStr | None = Field(
        default=None,
        description="OpenAI API key. When unset, IMAGE stays on the mock provider.",
    )
    openai_base_url: str = Field(
        default="https://api.openai.com/v1",
        description="Base URL for the OpenAI REST API.",
        min_length=1,
    )
    openai_timeout_seconds: float = Field(
        default=60.0,
        description="Per-request timeout for the OpenAI image call (one attempt).",
        gt=0,
    )

    # ---- Providers · Fal.ai Video (Slice α8.2) --------------------------
    # Optional. Present → the process wires the real **async** ``FalVideoProvider``
    # for ``Capability.VIDEO`` (submit → ``IN_PROGRESS`` + job id → the runner
    # pauses; α8.3 owns completion); absent → the VIDEO capability stays on
    # ``MockVideoProvider``. Independent of the OpenAI key (IMAGE and VIDEO are
    # composed separately). The container injects this key into the provider's
    # shared httpx client (``Authorization: Key …``); the provider never reads it
    # (W8.1.1 — adapters are configuration-blind; W8.2.3 — the adapter never
    # mutates orchestration state).
    fal_api_key: SecretStr | None = Field(
        default=None,
        description="Fal.ai API key. When unset, VIDEO stays on the mock provider.",
    )
    fal_base_url: str = Field(
        default="https://queue.fal.run",
        description="Base URL for the Fal.ai queue (submit) API.",
        min_length=1,
    )
    fal_timeout_seconds: float = Field(
        default=60.0,
        description="Per-request timeout for the Fal.ai submit call (one attempt).",
        gt=0,
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide ``Settings`` singleton."""
    return Settings()
