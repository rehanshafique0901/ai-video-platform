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

    # ---- Completion engine · async job completion (Slice α8.3) ----------
    # The α8.3 completion engine resolves in-flight provider jobs for ``paused``
    # runs and resumes the terminal ones. Library-only: a test loop / trigger
    # drives ``poll_once``; there is no daemon, Celery, or Redis. The per-run
    # lease (``workflow_run:<id>``) serialises ingresses; the resume CAS is the
    # exactly-once backstop.
    completion_lock_owner: str = Field(
        default="completion-engine",
        description="Fencing identity for the completion engine's per-run lock lease.",
        min_length=1,
    )
    completion_lease_seconds: float = Field(
        default=60.0,
        description="Per-run completion lock lease (seconds); longer than one resolve.",
        gt=0,
    )

    # ---- Webhook ingress · Fal.ai completion callbacks (Slice α8.3b) -----
    # The webhook is a *trigger* only (W8.3b.1): it verifies the signature,
    # finds the paused run by ``provider_job_id``, and calls the SAME frozen
    # ``CompletionEngine.complete()`` — the payload never mutates state. Fal
    # signs callbacks with ED25519; we verify against the **public** keys from
    # its JWKS endpoint (a configuration-independent trust anchor — the W8.1.1
    # "configuration-blind" invariant governs *credentials*, not public
    # verification keys). No secret is injected here.
    fal_webhook_jwks_url: str = Field(
        default="https://rest.fal.ai/.well-known/jwks.json",
        description="Fal.ai JWKS endpoint (ED25519 public keys for webhook verification).",
        min_length=1,
    )
    fal_webhook_timestamp_tolerance_seconds: int = Field(
        default=300,
        description="Max |now - X-Fal-Webhook-Timestamp| accepted (replay guard).",
        gt=0,
    )
    fal_webhook_jwks_cache_seconds: float = Field(
        default=3600.0,
        description="JWKS cache TTL (seconds); Fal rotates keys — do not exceed 24h.",
        gt=0,
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide ``Settings`` singleton."""
    return Settings()
