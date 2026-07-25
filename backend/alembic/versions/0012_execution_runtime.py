"""α8.6 Increment 4 — Execution Runtime & Provenance tables (schema only).

See: docs/engineering/EXECUTION_RUNTIME_CONTRACT.md (invariants W8.6.1–8, state machine)
     docs/decisions/ADR-0045-ai-runtime-core-freeze.md (three planes; F4/F5 raw-SQL)

**Structure only. Repositories/use cases wire in later phases of Increment 4.** These
tables make the Execution plane persistent while keeping it a *consumer* of immutable
plans (Planner) and ordered candidates (Resolver):

- `generations`        — the execution aggregate: state machine + provenance head + the
                         final render pointer (a logical ref to `generation_assets`).
- `generation_shots`   — per-shot prompt, attempts, verification, repair, accepted asset.
- `generation_assets`  — the canonical execution artefact registry (frame/reference/mask/
                         audio/video/thumbnail/metadata) with a `parent_asset_id` lineage
                         graph so repair is a graph, not an overwrite (Q1 ruling A).
- `model_cache`        — persistent local-model registry (no downloader until Increment 6).

Execution-owned by design (W8.6.8): these never touch the platform's `media_assets`
library — promotion is a future explicit use case (`PublishGenerationAssets`). Raw-SQL +
ORM-less like 0010/0011 (Q2 ruling A / ADR-0045 F4/F5); allowlisted in validate_schema.py
and documented in the ERD (Cluster 12). The α8.5e `generation_resolution_ledger` is
*reused* (its `generation_id` stays a logical reference — a hard FK would be destructive
to pre-Increment-4 rows; the runtime guarantees the `generations` row exists first).

Purely **additive**: new enums + new tables; `storage_backend` is reused from 0001.
`downgrade` drops every table + enum so the ci_gate upgrade→downgrade→upgrade roundtrip
stays clean.

Revision ID: 0012_execution_runtime
Revises: 0011_provider_operational_state
Create Date: 2026-07-25
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0012_execution_runtime"
down_revision: str | None | Sequence[str] = "0011_provider_operational_state"
branch_labels: str | None | Sequence[str] = None
depends_on: str | None | Sequence[str] = None


# --------------------------------------------------------------------------- #
# New enums (storage_backend is reused from 0001 — not redefined here)
# --------------------------------------------------------------------------- #
_ENUMS: dict[str, tuple[str, ...]] = {
    "generation_status": (
        "queued",
        "planning",
        "resolving",
        "generating",
        "verifying",
        "repairing",
        "rendering",
        "exporting",
        "completed",
        "failed",
        "cancelled",
    ),
    "generation_asset_kind": (
        "frame",
        "reference",
        "mask",
        "audio",
        "video",
        "thumbnail",
        "metadata",
    ),
    "execution_tier": ("local", "free_remote", "commercial"),
}


def _create_enums() -> None:
    for name, values in _ENUMS.items():
        values_sql = ", ".join(f"'{v}'" for v in values)
        op.execute(f"CREATE TYPE {name} AS ENUM ({values_sql})")


def _drop_enums() -> None:
    for name in reversed(list(_ENUMS.keys())):
        op.execute(f"DROP TYPE IF EXISTS {name}")


# --------------------------------------------------------------------------- #
# upgrade
# --------------------------------------------------------------------------- #
def upgrade() -> None:
    _create_enums()

    # ---- generations (execution aggregate + state machine + provenance) ----
    # `final_video_asset_id` is a *logical* pointer to generation_assets(id); it
    # carries no FK to avoid a circular dependency (generation_assets FKs back to
    # generations). The runtime sets it after the final render is registered.
    op.execute(
        """
        CREATE TABLE generations (
            id                     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            status                 generation_status NOT NULL DEFAULT 'queued',
            prompt                 text NOT NULL,
            title                  text,
            identity_id            text,
            execution_mode         text NOT NULL,
            execution_tier         execution_tier,
            chosen_provider        text,
            chosen_adapter         text,
            seed                   bigint,
            aspect_ratio           text,
            target_platform        text,
            width                  integer,
            height                 integer,
            fps                    integer,
            shot_count             integer,
            planner_version        text,
            storyboard_version     text,
            prompt_builder_version text,
            resolver_version       text,
            verifier_version       text,
            repair_version         text,
            renderer_version       text,
            score_schema_version   integer,
            catalogue_version      text,
            manifest_digest        text,
            final_video_asset_id   uuid,
            video_backend          storage_backend,
            video_bucket           text,
            video_key              text,
            duration_seconds       numeric(10,3),
            failure_reason         text,
            provenance             jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at             timestamptz NOT NULL DEFAULT now(),
            started_at            timestamptz,
            finished_at           timestamptz,
            updated_at            timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_generations_shot_count_nonnegative
                CHECK (shot_count IS NULL OR shot_count >= 0),
            CONSTRAINT ck_generations_dims_positive
                CHECK ((width IS NULL OR width > 0) AND (height IS NULL OR height > 0))
        )
        """
    )
    op.execute("CREATE INDEX ix_generations_status ON generations(status)")
    op.execute("CREATE INDEX ix_generations_created_at ON generations(created_at)")

    # ---- generation_assets (canonical execution artefact registry) ---------
    # parent_asset_id is a self-reference so repair/upscale/face-fix history is a
    # graph rather than an overwrite (Q1 ruling A). storage_uri = backend+bucket+key.
    op.execute(
        """
        CREATE TABLE generation_assets (
            id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            generation_id     uuid NOT NULL REFERENCES generations(id) ON DELETE CASCADE,
            shot_number       integer,
            asset_kind        generation_asset_kind NOT NULL,
            storage_backend   storage_backend NOT NULL,
            storage_bucket    text NOT NULL,
            storage_key       text NOT NULL,
            mime_type         text NOT NULL,
            size_bytes        bigint,
            checksum_sha256   bytea,
            width             integer,
            height            integer,
            duration_ms       integer,
            parent_asset_id   uuid REFERENCES generation_assets(id) ON DELETE SET NULL,
            metadata          jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at        timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_generation_assets_storage
                UNIQUE (storage_backend, storage_bucket, storage_key),
            CONSTRAINT ck_generation_assets_size_nonnegative
                CHECK (size_bytes IS NULL OR size_bytes >= 0),
            CONSTRAINT ck_generation_assets_duration_nonnegative
                CHECK (duration_ms IS NULL OR duration_ms >= 0)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_generation_assets_generation_id " "ON generation_assets(generation_id)"
    )
    op.execute("CREATE INDEX ix_generation_assets_parent ON generation_assets(parent_asset_id)")

    # ---- generation_shots (per-shot execution record) ----------------------
    op.execute(
        """
        CREATE TABLE generation_shots (
            id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            generation_id     uuid NOT NULL REFERENCES generations(id) ON DELETE CASCADE,
            shot_number       integer NOT NULL,
            prompt            text NOT NULL,
            negative_prompt   text,
            reference_images  jsonb NOT NULL DEFAULT '[]'::jsonb,
            adapter_used      text,
            seed              bigint,
            accepted          boolean NOT NULL DEFAULT false,
            verification      jsonb NOT NULL DEFAULT '{}'::jsonb,
            attempts          jsonb NOT NULL DEFAULT '[]'::jsonb,
            repair_count      integer NOT NULL DEFAULT 0,
            asset_id          uuid REFERENCES generation_assets(id) ON DELETE SET NULL,
            reason            text,
            created_at        timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_generation_shots_generation_shot
                UNIQUE (generation_id, shot_number),
            CONSTRAINT ck_generation_shots_repair_count_nonnegative
                CHECK (repair_count >= 0)
        )
        """
    )

    # ---- model_cache (persistent local-model registry; no downloader yet) --
    op.execute(
        """
        CREATE TABLE model_cache (
            id                     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            model_ref              text NOT NULL UNIQUE,
            version                text,
            sha256                 bytea,
            size_bytes             bigint,
            backend                text,
            execution_tier         execution_tier,
            supported_capabilities text[] NOT NULL DEFAULT '{}'::text[],
            local_path             text,
            status                 text NOT NULL DEFAULT 'registered',
            downloaded_at          timestamptz,
            last_used_at           timestamptz,
            created_at             timestamptz NOT NULL DEFAULT now(),
            updated_at             timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_model_cache_size_nonnegative
                CHECK (size_bytes IS NULL OR size_bytes >= 0)
        )
        """
    )


# --------------------------------------------------------------------------- #
# downgrade
# --------------------------------------------------------------------------- #
def downgrade() -> None:
    for table in (
        "model_cache",
        "generation_shots",
        "generation_assets",
        "generations",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    _drop_enums()
