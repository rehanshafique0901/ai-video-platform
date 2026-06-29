"""Centralised Postgres ENUM definitions.

Mirrors ``docs/database/schema.md`` §0.1. Adding a value requires:
  1. update this file,
  2. add a migration with ``ALTER TYPE ... ADD VALUE`` (NOT a recreate),
  3. update the matching Pydantic / domain value object.
"""

from __future__ import annotations

from sqlalchemy import Enum


def _pg_enum(name: str, *values: str) -> Enum:
    return Enum(*values, name=name, native_enum=True, create_type=True)


auth_role_enum = _pg_enum("auth_role", "user", "pro", "business", "enterprise", "admin")
version_reason_enum = _pg_enum(
    "version_reason", "manual_save", "autosave", "restore", "branch", "generated"
)
media_kind_enum = _pg_enum(
    "media_kind",
    "image",
    "video",
    "narration",
    "subtitle",
    "music",
    "sound_effect",
    "thumbnail",
)
media_source_enum = _pg_enum("media_source", "generated", "uploaded", "stock")
storage_backend_enum = _pg_enum("storage_backend", "local", "s3", "r2", "azure_blob", "gcs")
track_kind_enum = _pg_enum("track_kind", "video", "audio", "subtitle", "effect")
prompt_kind_enum = _pg_enum(
    "prompt_kind",
    "image",
    "video",
    "animation",
    "negative",
    "camera",
    "motion",
    "lighting",
    "style",
)
workflow_status_enum = _pg_enum(
    "workflow_status",
    "queued",
    "running",
    "paused",
    "succeeded",
    "failed",
    "canceled",
)
step_status_enum = _pg_enum(
    "step_status",
    "pending",
    "running",
    "succeeded",
    "failed",
    "skipped",
    "retrying",
)
render_status_enum = _pg_enum(
    "render_status", "queued", "running", "succeeded", "failed", "canceled"
)
export_status_enum = _pg_enum(
    "export_status", "queued", "running", "succeeded", "failed", "canceled"
)
export_format_enum = _pg_enum("export_format", "mp4", "mov", "gif", "webm")
export_quality_enum = _pg_enum("export_quality", "sd", "hd_1080p", "qhd_2k", "uhd_4k")
export_orientation_enum = _pg_enum("export_orientation", "horizontal", "vertical", "square")
plugin_kind_enum = _pg_enum("plugin_kind", "llm", "image", "video", "voice")
model_status_enum = _pg_enum("model_status", "available", "preview", "deprecated", "retired")
pricing_unit_enum = _pg_enum(
    "pricing_unit",
    "prompt_token",
    "completion_token",
    "image",
    "megapixel",
    "video_second",
    "audio_second",
    "embedding",
)
usage_status_enum = _pg_enum("usage_status", "success", "failed", "partial", "timeout")
subscription_status_enum = _pg_enum(
    "subscription_status",
    "active",
    "past_due",
    "canceled",
    "trialing",
    "expired",
)
invoice_status_enum = _pg_enum("invoice_status", "draft", "open", "paid", "void", "uncollectible")
billing_cycle_enum = _pg_enum("billing_cycle", "monthly", "yearly", "custom")
ledger_entry_type_enum = _pg_enum(
    "ledger_entry_type",
    "purchase",
    "grant",
    "consumption",
    "refund",
    "expiry",
    "adjustment",
)
flag_type_enum = _pg_enum("flag_type", "boolean", "percent_rollout", "multivariate")
flag_scope_enum = _pg_enum("flag_scope", "tenant", "user", "role")
idempotency_status_enum = _pg_enum("idempotency_status", "in_flight", "succeeded", "failed")
audit_actor_kind_enum = _pg_enum(
    "audit_actor_kind", "user", "system", "admin", "api_key", "webhook"
)
