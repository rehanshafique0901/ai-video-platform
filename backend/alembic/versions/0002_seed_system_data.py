"""seed system data — plans, feature flags, AI providers/models, roles, system settings

Revision ID: 0002_seed_system_data
Revises: 0001_baseline
Create Date: 2026-06-28

Idempotent: every INSERT uses ``ON CONFLICT DO NOTHING`` so re-running this
migration (e.g. on a brand-new environment after downgrade/upgrade) is safe.

Per CONTRIBUTING.md §Migrations: only *required system data* is seeded here.
Sample data (demo tenants, fake users, test projects) lives in
``scripts/seed_demo.py`` and is never run in migrations.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_seed_system_data"
down_revision: str | None | Sequence[str] = "0001_baseline"
branch_labels: str | None | Sequence[str] = None
depends_on: str | None | Sequence[str] = None


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------
_PLANS: list[dict] = [
    {
        "code": "free",
        "name": "Free",
        "description": "Starter plan with monthly free credits.",
        "cycle": "monthly",
        "monthly_credits": 100,
        "monthly_price": 0,
        "currency": "USD",
        "features": {
            "max_projects": 3,
            "max_render_seconds": 30,
            "watermark": True,
            "queues": ["low", "background"],
        },
        "active": True,
    },
    {
        "code": "pro",
        "name": "Pro",
        "description": "For individual creators.",
        "cycle": "monthly",
        "monthly_credits": 2500,
        "monthly_price": 29,
        "currency": "USD",
        "features": {
            "max_projects": 50,
            "max_render_seconds": 120,
            "watermark": False,
            "queues": ["normal", "high"],
        },
        "active": True,
    },
    {
        "code": "business",
        "name": "Business",
        "description": "For teams and agencies.",
        "cycle": "monthly",
        "monthly_credits": 12000,
        "monthly_price": 99,
        "currency": "USD",
        "features": {
            "max_projects": 500,
            "max_render_seconds": 300,
            "watermark": False,
            "seats": 5,
            "queues": ["normal", "high", "critical"],
            "team_collab": True,
        },
        "active": True,
    },
    {
        "code": "enterprise",
        "name": "Enterprise",
        "description": "Custom contract.",
        "cycle": "custom",
        "monthly_credits": 0,
        "monthly_price": 0,
        "currency": "USD",
        "features": {
            "max_projects": None,
            "watermark": False,
            "seats": None,
            "queues": ["normal", "high", "critical"],
            "sso": True,
            "audit_export": True,
            "dedicated_support": True,
        },
        "active": True,
    },
]


# ---------------------------------------------------------------------------
# Feature flags (ADR-0009)
# ---------------------------------------------------------------------------
_FEATURE_FLAGS: list[dict] = [
    {
        "key": "ai.model_registry.enabled",
        "description": "Master toggle for the AI model registry (CR-11).",
        "flag_type": "boolean",
        "default_value": True,
    },
    {
        "key": "rendering.pipeline.ffmpeg.enabled",
        "description": "Allow FFmpeg rendering pipeline.",
        "flag_type": "boolean",
        "default_value": True,
    },
    {
        "key": "rendering.pipeline.moviepy.enabled",
        "description": "Allow MoviePy rendering pipeline.",
        "flag_type": "boolean",
        "default_value": True,
    },
    {
        "key": "rendering.pipeline.opencv.enabled",
        "description": "Allow OpenCV rendering pipeline.",
        "flag_type": "boolean",
        "default_value": False,
    },
    {
        "key": "asset_library.enabled",
        "description": "Master toggle for the Asset Library (CR-8).",
        "flag_type": "boolean",
        "default_value": True,
    },
    {
        "key": "workflows.resumable.enabled",
        "description": "Allow resuming workflows from checkpoints (CR-7).",
        "flag_type": "boolean",
        "default_value": True,
    },
    {
        "key": "billing.cost_tracking.enabled",
        "description": "Enable per-call AI cost tracking (CR-12).",
        "flag_type": "boolean",
        "default_value": True,
    },
    {
        "key": "queues.priorities.enabled",
        "description": "Honour queue priorities for Celery tasks (CR-13).",
        "flag_type": "boolean",
        "default_value": True,
    },
    {
        "key": "audit_log.enabled",
        "description": "Write to the immutable audit log (CR-DB-3).",
        "flag_type": "boolean",
        "default_value": True,
    },
    {
        "key": "experimental.canvas_editor",
        "description": "Show the canvas-style editor preview.",
        "flag_type": "percent_rollout",
        "default_value": False,
        "rollout_percent": 10,
    },
]


# ---------------------------------------------------------------------------
# Provider plugin registrations
# ---------------------------------------------------------------------------
_PROVIDER_PLUGINS: list[dict] = [
    {"name": "openai", "version": "1.0.0", "kind": "llm",
     "capabilities": ["text", "embedding", "vision"], "enabled": True},
    {"name": "anthropic", "version": "1.0.0", "kind": "llm",
     "capabilities": ["text", "vision"], "enabled": True},
    {"name": "google_gemini", "version": "1.0.0", "kind": "llm",
     "capabilities": ["text", "vision", "embedding"], "enabled": True},
    {"name": "openai_dalle", "version": "1.0.0", "kind": "image",
     "capabilities": ["text_to_image"], "enabled": True},
    {"name": "flux", "version": "1.0.0", "kind": "image",
     "capabilities": ["text_to_image", "image_to_image"], "enabled": True},
    {"name": "sdxl", "version": "1.0.0", "kind": "image",
     "capabilities": ["text_to_image", "image_to_image", "inpainting"], "enabled": True},
    {"name": "google_veo", "version": "1.0.0", "kind": "video",
     "capabilities": ["text_to_video", "image_to_video"], "enabled": True},
    {"name": "runway", "version": "1.0.0", "kind": "video",
     "capabilities": ["text_to_video", "image_to_video"], "enabled": True},
    {"name": "elevenlabs", "version": "1.0.0", "kind": "voice",
     "capabilities": ["tts", "voice_clone"], "enabled": True},
]


# ---------------------------------------------------------------------------
# AI models (initial catalogue — see ARCHITECTURE.md §8j)
# ---------------------------------------------------------------------------
_AI_MODELS: list[dict] = [
    # OpenAI
    {"model_key": "openai/gpt-5", "provider": "openai", "vendor_model_id": "gpt-5",
     "kind": "llm", "capabilities": ["text", "function_calling", "vision"],
     "modalities": ["text", "image"], "context_window": 256000,
     "max_output_tokens": 8192, "status": "available"},
    {"model_key": "openai/gpt-4.1", "provider": "openai", "vendor_model_id": "gpt-4.1",
     "kind": "llm", "capabilities": ["text", "function_calling"],
     "modalities": ["text"], "context_window": 128000,
     "max_output_tokens": 8192, "status": "available"},
    {"model_key": "openai/dalle-3", "provider": "openai_dalle", "vendor_model_id": "dall-e-3",
     "kind": "image", "capabilities": ["text_to_image"],
     "modalities": ["image"], "max_output_pixels": 1024 * 1024, "status": "available"},
    # Anthropic
    {"model_key": "anthropic/claude-opus-4", "provider": "anthropic",
     "vendor_model_id": "claude-opus-4", "kind": "llm",
     "capabilities": ["text", "vision", "function_calling"],
     "modalities": ["text", "image"], "context_window": 200000,
     "max_output_tokens": 8192, "status": "available"},
    {"model_key": "anthropic/claude-sonnet-4", "provider": "anthropic",
     "vendor_model_id": "claude-sonnet-4", "kind": "llm",
     "capabilities": ["text", "vision"], "modalities": ["text", "image"],
     "context_window": 200000, "max_output_tokens": 8192, "status": "available"},
    # Google
    {"model_key": "google/gemini-2.5-pro", "provider": "google_gemini",
     "vendor_model_id": "gemini-2.5-pro", "kind": "llm",
     "capabilities": ["text", "vision", "function_calling"],
     "modalities": ["text", "image"], "context_window": 2000000,
     "max_output_tokens": 8192, "status": "available"},
    {"model_key": "google/veo-3", "provider": "google_veo",
     "vendor_model_id": "veo-3", "kind": "video",
     "capabilities": ["text_to_video", "image_to_video"],
     "modalities": ["video"], "max_output_seconds": 30, "status": "available"},
    {"model_key": "google/veo-2", "provider": "google_veo",
     "vendor_model_id": "veo-2", "kind": "video",
     "capabilities": ["text_to_video"], "modalities": ["video"],
     "max_output_seconds": 30, "status": "deprecated"},
    # Image
    {"model_key": "flux/flux-pro", "provider": "flux", "vendor_model_id": "flux-pro",
     "kind": "image", "capabilities": ["text_to_image", "image_to_image"],
     "modalities": ["image"], "max_output_pixels": 4 * 1024 * 1024,
     "status": "available"},
    {"model_key": "flux/flux-dev", "provider": "flux", "vendor_model_id": "flux-dev",
     "kind": "image", "capabilities": ["text_to_image"],
     "modalities": ["image"], "max_output_pixels": 1024 * 1024,
     "status": "preview"},
    {"model_key": "stability/sdxl-1.0", "provider": "sdxl",
     "vendor_model_id": "sdxl-1.0", "kind": "image",
     "capabilities": ["text_to_image", "image_to_image", "inpainting"],
     "modalities": ["image"], "max_output_pixels": 1024 * 1024,
     "status": "available"},
    # Video
    {"model_key": "runway/gen-4", "provider": "runway", "vendor_model_id": "gen-4",
     "kind": "video", "capabilities": ["text_to_video", "image_to_video"],
     "modalities": ["video"], "max_output_seconds": 16, "status": "available"},
    # Voice
    {"model_key": "elevenlabs/v2-multilingual", "provider": "elevenlabs",
     "vendor_model_id": "eleven_multilingual_v2", "kind": "voice",
     "capabilities": ["tts"], "modalities": ["audio"], "status": "available"},
]


# ---------------------------------------------------------------------------
# Roles (RBAC)
# ---------------------------------------------------------------------------
_ROLES: list[dict] = [
    {"code": "owner", "description": "Tenant owner — full administrative authority."},
    {"code": "admin", "description": "Workspace administrator."},
    {"code": "editor", "description": "Can create and edit projects."},
    {"code": "viewer", "description": "Read-only access."},
    {"code": "billing", "description": "Manage billing and invoices."},
    {"code": "support", "description": "Internal support engineer (impersonation gated)."},
]


# ---------------------------------------------------------------------------
# System settings (CR-DB-4)
# ---------------------------------------------------------------------------
_SYSTEM_SETTINGS: list[dict] = [
    {"key": "billing.default_currency", "value": "USD",
     "description": "Default currency for new tenants.", "is_secret": False},
    {"key": "ai.default_models",
     "value": {"image": "flux/flux-pro", "video": "google/veo-3",
               "llm": "openai/gpt-5", "voice": "elevenlabs/v2-multilingual"},
     "description": "Default AI model selections used when a project does not pin one.",
     "is_secret": False},
    {"key": "rendering.default_pipeline", "value": "ffmpeg",
     "description": "Default rendering pipeline.", "is_secret": False},
    {"key": "queues.default_queue", "value": "normal",
     "description": "Default Celery queue for new render jobs.", "is_secret": False},
    {"key": "audit_log.retention_years", "value": 7,
     "description": "Audit log retention in years (compliance).", "is_secret": False},
    {"key": "idempotency.default_ttl_seconds", "value": 86400,
     "description": "Default TTL for idempotency keys.", "is_secret": False},
    {"key": "feature_flag.cache_ttl_seconds", "value": 30,
     "description": "Cache TTL for feature flag evaluations in the API process.",
     "is_secret": False},
]


def _json(value: object) -> str:
    import json
    return json.dumps(value)


def upgrade() -> None:
    bind = op.get_bind()

    # Plans
    for p in _PLANS:
        bind.execute(
            sa.text(
                """
                INSERT INTO plans
                    (code, name, description, cycle, monthly_credits,
                     monthly_price, currency, features, active)
                VALUES
                    (:code, :name, :description, :cycle, :monthly_credits,
                     :monthly_price, :currency, CAST(:features AS jsonb), :active)
                ON CONFLICT (code) DO NOTHING
                """
            ),
            {**p, "features": _json(p["features"])},
        )

    # Feature flags
    for f in _FEATURE_FLAGS:
        bind.execute(
            sa.text(
                """
                INSERT INTO feature_flags
                    (key, description, flag_type, default_value, rollout_percent)
                VALUES
                    (:key, :description, :flag_type, CAST(:default_value AS jsonb),
                     :rollout_percent)
                ON CONFLICT (key) DO NOTHING
                """
            ),
            {
                "key": f["key"],
                "description": f["description"],
                "flag_type": f["flag_type"],
                "default_value": _json(f["default_value"]),
                "rollout_percent": f.get("rollout_percent"),
            },
        )

    # Provider plugins
    for pl in _PROVIDER_PLUGINS:
        bind.execute(
            sa.text(
                """
                INSERT INTO provider_plugin_registrations
                    (name, version, kind, capabilities, enabled)
                VALUES (:name, :version, :kind, :capabilities, :enabled)
                ON CONFLICT (name, version) DO NOTHING
                """
            ),
            {
                "name": pl["name"],
                "version": pl["version"],
                "kind": pl["kind"],
                "capabilities": pl["capabilities"],
                "enabled": pl["enabled"],
            },
        )

    # AI models
    for m in _AI_MODELS:
        bind.execute(
            sa.text(
                """
                INSERT INTO ai_models
                    (model_key, provider, vendor_model_id, kind, capabilities,
                     modalities, context_window, max_output_tokens,
                     max_output_pixels, max_output_seconds, status)
                VALUES
                    (:model_key, :provider, :vendor_model_id, :kind,
                     :capabilities, :modalities,
                     :context_window, :max_output_tokens,
                     :max_output_pixels, :max_output_seconds, :status)
                ON CONFLICT (model_key) DO NOTHING
                """
            ),
            {
                "model_key": m["model_key"],
                "provider": m["provider"],
                "vendor_model_id": m["vendor_model_id"],
                "kind": m["kind"],
                "capabilities": m["capabilities"],
                "modalities": m["modalities"],
                "context_window": m.get("context_window"),
                "max_output_tokens": m.get("max_output_tokens"),
                "max_output_pixels": m.get("max_output_pixels"),
                "max_output_seconds": m.get("max_output_seconds"),
                "status": m["status"],
            },
        )

    # Roles
    for r in _ROLES:
        bind.execute(
            sa.text(
                "INSERT INTO roles (code, description) VALUES (:code, :description) "
                "ON CONFLICT (code) DO NOTHING"
            ),
            r,
        )

    # System settings
    for s in _SYSTEM_SETTINGS:
        bind.execute(
            sa.text(
                """
                INSERT INTO system_settings (key, value, description, is_secret)
                VALUES (:key, CAST(:value AS jsonb), :description, :is_secret)
                ON CONFLICT (key) DO NOTHING
                """
            ),
            {
                "key": s["key"],
                "value": _json(s["value"]),
                "description": s["description"],
                "is_secret": s["is_secret"],
            },
        )


def downgrade() -> None:
    bind = op.get_bind()

    bind.execute(
        sa.text("DELETE FROM system_settings WHERE key = ANY(:keys)"),
        {"keys": [s["key"] for s in _SYSTEM_SETTINGS]},
    )
    bind.execute(
        sa.text("DELETE FROM roles WHERE code = ANY(:codes)"),
        {"codes": [r["code"] for r in _ROLES]},
    )
    bind.execute(
        sa.text("DELETE FROM ai_models WHERE model_key = ANY(:keys)"),
        {"keys": [m["model_key"] for m in _AI_MODELS]},
    )
    for pl in _PROVIDER_PLUGINS:
        bind.execute(
            sa.text(
                "DELETE FROM provider_plugin_registrations "
                "WHERE name = :name AND version = :version"
            ),
            {"name": pl["name"], "version": pl["version"]},
        )
    bind.execute(
        sa.text("DELETE FROM feature_flags WHERE key = ANY(:keys)"),
        {"keys": [f["key"] for f in _FEATURE_FLAGS]},
    )
    bind.execute(
        sa.text("DELETE FROM plans WHERE code = ANY(:codes)"),
        {"codes": [p["code"] for p in _PLANS]},
    )
