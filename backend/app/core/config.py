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

    # ---- Generation · Pollinations image (Slice α8.6, Increment 3) -------
    # The free image provider for the generation runtime's ``IImageGenerator``.
    # No API key (Pollinations' simple GET endpoint is keyless); the container
    # injects base_url + timeout into the adapter's shared httpx client
    # (W8.1.1 — the adapter is configuration-blind). Network egress only happens
    # when the runtime actually resolves to this adapter.
    pollinations_base_url: str = Field(
        default="https://image.pollinations.ai",
        description="Base URL for the Pollinations image endpoint.",
        min_length=1,
    )
    pollinations_timeout_seconds: float = Field(
        default=120.0,
        description="Per-request timeout for a Pollinations image generation (one attempt).",
        gt=0,
    )
    pollinations_model: str = Field(
        default="flux",
        description="Default Pollinations model used for image generation.",
        min_length=1,
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

    # ---- Generated media ingestion (Slice α8.4a) ------------------------
    # After a run succeeds, a downstream subscriber downloads the provider's
    # produced artifact (image_ref/video_ref) and stores it via ``IObjectStorage``,
    # registering a ``MediaAsset(source='generated')``. α8.4a ships the local
    # filesystem backend; S3/R2/GCS adapters swap in later with no use-case change.
    media_storage_root: str = Field(
        default="./var/media",
        description="Filesystem root for the local object-storage adapter (α8.4a).",
        min_length=1,
    )
    media_storage_bucket: str = Field(
        default="generated",
        description="Logical bucket/container generated media is written into.",
        min_length=1,
    )
    media_download_timeout_seconds: float = Field(
        default=60.0,
        description="Per-request timeout for fetching a produced artifact (one attempt).",
        gt=0,
    )
    media_download_max_bytes: int = Field(
        default=512 * 1024 * 1024,
        description="Hard cap on a downloaded artifact's size (bytes); guards memory.",
        gt=0,
    )

    # ---- Storage backends & signed-URL delivery (Slice α8.5b.2) ---------
    # The single active write backend selects where *new* MediaAssets are persisted
    # (writers use ``StorageResolver.active()``). Reads/deletes/deliveries always resolve
    # by the artifact's persisted ``storage_backend`` — so changing this affects only
    # future writes, never existing assets (W8.5b.5). Exactly one active backend
    # (no preferred/fallback/mirror/replication — E2). ``s3``/``r2`` share one
    # S3-compatible client; R2 sets ``s3_endpoint_url`` (configuration-blind, W8.1.1).
    storage_active_backend: Literal["local", "s3", "r2"] = Field(
        default="local",
        description="Backend new MediaAssets are written to (local|s3|r2). E2, α8.5b.2.",
    )
    s3_bucket: str | None = Field(
        default=None,
        description="Bucket/container for the S3/R2 object-storage adapter (α8.5b.2).",
    )
    s3_region: str | None = Field(
        default=None,
        description="Region for the S3 client (e.g. us-east-1; 'auto' for R2).",
    )
    s3_endpoint_url: str | None = Field(
        default=None,
        description="Custom S3 endpoint (set for R2/MinIO; None = AWS default).",
    )
    s3_access_key_id: str | None = Field(
        default=None,
        description="Access key id for the S3/R2 client (injected, never fetched — W8.1.1).",
    )
    s3_secret_access_key: SecretStr | None = Field(
        default=None,
        description="Secret access key for the S3/R2 client (injected — W8.1.1).",
    )
    download_signed_url_ttl_seconds: int = Field(
        default=900,
        description="Fixed TTL for presigned download URLs (α8.5b.2 Fork F; no per-request TTL).",
        gt=0,
    )

    # --- Render engine (α8.4b) ------------------------------------------------
    # The render worker composes a project's Timeline into an output video via the
    # neutral ``IRenderer`` port (FFmpeg adapter in prod). Configuration-blind
    # (W8.1.1): binary paths + a timeout are injected, never fetched.
    render_ffmpeg_path: str = Field(
        default="ffmpeg",
        description="Path to the ffmpeg binary for the FFmpeg renderer (α8.4b).",
        min_length=1,
    )
    render_ffprobe_path: str = Field(
        default="ffprobe",
        description="Path to the ffprobe binary used to probe render output (α8.4b).",
        min_length=1,
    )
    render_timeout_seconds: float = Field(
        default=900.0,
        description="Hard timeout for a single ffmpeg/ffprobe invocation (seconds).",
        gt=0,
    )
    render_workspace_dir: str | None = Field(
        default=None,
        description="Parent dir for per-job render temp workspaces (None = system tmp).",
    )
    render_batch_size: int = Field(
        default=10,
        description="Max queued render jobs a single RenderWorker.run_once() claims.",
        gt=0,
    )

    # --- Media enrichment (α8.4c) ---------------------------------------------
    # The enrichment worker derives a thumbnail + scalar metadata for generated
    # videos via IThumbnailer (FFmpeg adapter, reusing the render binary config).
    enrichment_thumbnail_at_seconds: float = Field(
        default=1.0,
        description="Timestamp (seconds) the thumbnail frame is extracted from.",
        ge=0,
    )
    enrichment_batch_size: int = Field(
        default=10,
        description="Max generated videos a single MediaEnrichmentWorker.run_once() claims.",
        gt=0,
    )
    # α8.4d derived previews (preview clip / GIF / waveform). FFmpeg binary config reused.
    enrichment_preview_max_seconds: float = Field(
        default=5.0, description="Max duration of a derived preview clip.", gt=0
    )
    enrichment_preview_max_width: int = Field(
        default=640, description="Max width (px) of a derived preview clip; never upscales.", gt=0
    )
    enrichment_gif_max_seconds: float = Field(
        default=3.0, description="Max duration sampled into a derived GIF preview.", gt=0
    )
    enrichment_gif_fps: int = Field(
        default=10, description="Frame rate of a derived GIF preview.", gt=0
    )
    enrichment_gif_max_width: int = Field(
        default=480, description="Max width (px) of a derived GIF preview.", gt=0
    )
    enrichment_waveform_width: int = Field(
        default=640, description="Width (px) of a derived waveform image.", gt=0
    )
    enrichment_waveform_height: int = Field(
        default=120, description="Height (px) of a derived waveform image.", gt=0
    )

    # --- Export engine (α8.5a) ------------------------------------------------
    # The export worker transcodes a completed render's master into a delivery encoding via
    # IExporter (FFmpeg adapter, reusing the render binary + workspace + timeout config).
    export_batch_size: int = Field(
        default=10,
        description="Max queued export jobs a single ExportWorker.run_once() claims.",
        gt=0,
    )

    # --- Publishing credential ownership (α8.6a, ADR-0047) --------------------
    # The externally-managed master key that wraps per-record data keys for stored OAuth
    # credentials (envelope encryption, R2). Injected into the credential service; never
    # written to the database, never auto-generated. FAIL-CLOSED: production (environment ==
    # 'prod') requires this to be set or the process refuses to boot (see container.init);
    # dev/tests may inject a deterministic key. When unset outside production, publishing is
    # simply unavailable (no plaintext fallback).
    publishing_credential_master_key: SecretStr | None = Field(
        default=None,
        description="Master key wrapping stored OAuth credentials. Required in production.",
    )
    publishing_credential_key_version: str = Field(
        default="v1",
        description="Version label recorded with each encrypted credential (rotation key).",
        min_length=1,
    )
    # Base URL the destination OAuth provider redirects back to; the callback path is
    # appended to form the redirect_uri used in both authorize + code-exchange.
    publishing_oauth_redirect_base_url: str = Field(
        default="http://localhost:8000",
        description="Base URL for the OAuth callback (redirect_uri = base + callback path).",
        min_length=1,
    )
    publishing_oauth_state_ttl_seconds: int = Field(
        default=600,
        description="TTL for the signed, stateless OAuth 'state' token (CSRF window).",
        gt=0,
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide ``Settings`` singleton."""
    return Settings()
