"""baseline schema for Phase 2 Step B

Revision ID: 0001_baseline
Revises:
Create Date: 2026-06-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_baseline"
down_revision: str | None | Sequence[str] = None
branch_labels: str | None | Sequence[str] = None
depends_on: str | None | Sequence[str] = None


# ---------------------------------------------------------------------------
# Extensions, ENUMs, helper functions
# ---------------------------------------------------------------------------
_EXTENSIONS = ("pgcrypto", "citext", "pg_trgm", "vector", "btree_gin")

_ENUMS = {
    "auth_role": ("user", "pro", "business", "enterprise", "admin"),
    "version_reason": ("manual_save", "autosave", "restore", "branch", "generated"),
    "media_kind": (
        "image", "video", "narration", "subtitle", "music", "sound_effect", "thumbnail",
    ),
    "media_source": ("generated", "uploaded", "stock"),
    "storage_backend": ("local", "s3", "r2", "azure_blob", "gcs"),
    "track_kind": ("video", "audio", "subtitle", "effect"),
    "prompt_kind": (
        "image", "video", "animation", "negative", "camera", "motion", "lighting", "style",
    ),
    "workflow_status": ("queued", "running", "paused", "succeeded", "failed", "canceled"),
    "step_status": ("pending", "running", "succeeded", "failed", "skipped", "retrying"),
    "render_status": ("queued", "running", "succeeded", "failed", "canceled"),
    "export_status": ("queued", "running", "succeeded", "failed", "canceled"),
    "export_format": ("mp4", "mov", "gif", "webm"),
    "export_quality": ("sd", "hd_1080p", "qhd_2k", "uhd_4k"),
    "export_orientation": ("horizontal", "vertical", "square"),
    "plugin_kind": ("llm", "image", "video", "voice"),
    "model_status": ("available", "preview", "deprecated", "retired"),
    "pricing_unit": (
        "prompt_token", "completion_token", "image", "megapixel",
        "video_second", "audio_second", "embedding",
    ),
    "usage_status": ("success", "failed", "partial", "timeout"),
    "subscription_status": ("active", "past_due", "canceled", "trialing", "expired"),
    "invoice_status": ("draft", "open", "paid", "void", "uncollectible"),
    "billing_cycle": ("monthly", "yearly", "custom"),
    "ledger_entry_type": (
        "purchase", "grant", "consumption", "refund", "expiry", "adjustment",
    ),
    "flag_type": ("boolean", "percent_rollout", "multivariate"),
    "flag_scope": ("tenant", "user", "role"),
    "idempotency_status": ("in_flight", "succeeded", "failed"),
    "audit_actor_kind": ("user", "system", "admin", "api_key", "webhook"),
}


def _create_extensions() -> None:
    for ext in _EXTENSIONS:
        op.execute(f'CREATE EXTENSION IF NOT EXISTS "{ext}"')


def _drop_extensions() -> None:
    # Intentionally do not drop extensions on downgrade — they may be shared by
    # other databases on the same cluster. This is consistent with industry
    # practice for Alembic baselines.
    pass


def _create_enums() -> None:
    for name, values in _ENUMS.items():
        values_sql = ", ".join(f"'{v}'" for v in values)
        op.execute(f"CREATE TYPE {name} AS ENUM ({values_sql})")


def _drop_enums() -> None:
    for name in reversed(list(_ENUMS.keys())):
        op.execute(f"DROP TYPE IF EXISTS {name}")


def _create_helper_functions() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION touch_updated_at() RETURNS trigger AS $$
        BEGIN
            NEW.updated_at = now();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION bump_version() RETURNS trigger AS $$
        BEGIN
            IF NEW.version = OLD.version THEN
                NEW.version = OLD.version + 1;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION reject_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'Table % is immutable; mutations are not permitted', TG_TABLE_NAME;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION enforce_credit_ledger_balance() RETURNS trigger AS $$
        DECLARE
            prev NUMERIC(18,4);
        BEGIN
            SELECT balance_after INTO prev
              FROM credit_ledger
             WHERE tenant_id = NEW.tenant_id
             ORDER BY created_at DESC, id DESC
             LIMIT 1;
            IF prev IS NULL THEN prev := 0; END IF;
            IF ROUND(prev + NEW.amount, 4) <> ROUND(NEW.balance_after, 4) THEN
                RAISE EXCEPTION 'credit_ledger balance mismatch: % + % <> %',
                    prev, NEW.amount, NEW.balance_after;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )


def _drop_helper_functions() -> None:
    for fn in (
        "enforce_credit_ledger_balance()",
        "reject_mutation()",
        "bump_version()",
        "touch_updated_at()",
    ):
        op.execute(f"DROP FUNCTION IF EXISTS {fn}")


# ---------------------------------------------------------------------------
# Partition helpers
# ---------------------------------------------------------------------------
_PARTITIONED_TABLES = ("usage_records", "analytics_events", "event_log", "audit_log")


def _create_initial_partitions() -> None:
    """Create partitions for current + next 24 months, plus a default catch-all."""
    op.execute(
        """
        DO $$
        DECLARE
            tbl text;
            mo  date;
            partition_name text;
            partition_start text;
            partition_end text;
        BEGIN
            FOREACH tbl IN ARRAY ARRAY['usage_records','analytics_events','event_log','audit_log']
            LOOP
                FOR mo IN
                    SELECT (date_trunc('month', now()) + (i || ' month')::interval)::date
                    FROM generate_series(-1, 24) AS s(i)
                LOOP
                    partition_name := tbl || '_y' || to_char(mo, 'YYYY') || 'm' || to_char(mo, 'MM');
                    partition_start := to_char(mo, 'YYYY-MM-DD');
                    partition_end := to_char((mo + interval '1 month')::date, 'YYYY-MM-DD');
                    EXECUTE format(
                        'CREATE TABLE IF NOT EXISTS %I PARTITION OF %I '
                        'FOR VALUES FROM (%L) TO (%L)',
                        partition_name, tbl, partition_start, partition_end
                    );
                END LOOP;
                EXECUTE format(
                    'CREATE TABLE IF NOT EXISTS %I PARTITION OF %I DEFAULT',
                    tbl || '_default', tbl
                );
            END LOOP;
        END $$;
        """
    )


def _drop_partitions() -> None:
    op.execute(
        """
        DO $$
        DECLARE
            r record;
        BEGIN
            FOR r IN
                SELECT inhrelid::regclass::text AS child
                  FROM pg_inherits
                 WHERE inhparent::regclass::text IN (
                    'usage_records','analytics_events','event_log','audit_log'
                 )
            LOOP
                EXECUTE format('DROP TABLE IF EXISTS %s CASCADE', r.child);
            END LOOP;
        END $$;
        """
    )


# ---------------------------------------------------------------------------
# Upgrade
# ---------------------------------------------------------------------------
def upgrade() -> None:  # noqa: PLR0915 - large baseline by necessity
    _create_extensions()
    _create_enums()
    _create_helper_functions()

    # ---- tenants -----------------------------------------------------------
    op.execute(
        """
        CREATE TABLE tenants (
            id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            name        text        NOT NULL,
            slug        text        NOT NULL,
            plan_tier   text        NOT NULL DEFAULT 'free',
            created_at  timestamptz NOT NULL DEFAULT now(),
            updated_at  timestamptz NOT NULL DEFAULT now(),
            deleted_at  timestamptz
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_tenants_slug ON tenants(slug) WHERE deleted_at IS NULL"
    )

    # ---- users -------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE users (
            id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id       uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
            email           citext NOT NULL,
            password_hash   text,
            display_name    text NOT NULL,
            email_verified_at timestamptz,
            last_login_at   timestamptz,
            extra           jsonb NOT NULL DEFAULT '{}'::jsonb,
            version         integer NOT NULL DEFAULT 1,
            created_at      timestamptz NOT NULL DEFAULT now(),
            updated_at      timestamptz NOT NULL DEFAULT now(),
            deleted_at      timestamptz
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_users_tenant_id_email "
        "ON users(tenant_id, email) WHERE deleted_at IS NULL"
    )
    op.execute("CREATE INDEX ix_users_tenant_id ON users(tenant_id)")
    op.execute("CREATE INDEX ix_users_last_login_at ON users(last_login_at)")

    # ---- oauth_identities --------------------------------------------------
    op.execute(
        """
        CREATE TABLE oauth_identities (
            id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id     uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            provider    text NOT NULL,
            subject     text NOT NULL,
            linked_at   timestamptz NOT NULL DEFAULT now(),
            created_at  timestamptz NOT NULL DEFAULT now(),
            updated_at  timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_oauth_identities_provider_subject UNIQUE (provider, subject),
            CONSTRAINT uq_oauth_identities_user_id_provider UNIQUE (user_id, provider)
        )
        """
    )

    # ---- sessions ----------------------------------------------------------
    op.execute(
        """
        CREATE TABLE sessions (
            id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id      uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            family_id    uuid NOT NULL,
            token_hash   text NOT NULL,
            ip           inet,
            user_agent   text,
            issued_at    timestamptz NOT NULL,
            last_used_at timestamptz NOT NULL,
            revoked_at   timestamptz,
            expires_at   timestamptz NOT NULL,
            CONSTRAINT uq_sessions_token_hash UNIQUE (token_hash)
        )
        """
    )
    op.execute("CREATE INDEX ix_sessions_user_id_family_id ON sessions(user_id, family_id)")
    op.execute(
        "CREATE INDEX ix_sessions_expires_at "
        "ON sessions(expires_at) WHERE revoked_at IS NULL"
    )

    # ---- roles + roles_users ----------------------------------------------
    op.execute(
        """
        CREATE TABLE roles (
            id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            code        text NOT NULL,
            description text,
            CONSTRAINT uq_roles_code UNIQUE (code)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE roles_users (
            role_id              uuid NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
            user_id              uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            granted_at           timestamptz NOT NULL DEFAULT now(),
            granted_by_user_id   uuid REFERENCES users(id) ON DELETE SET NULL,
            PRIMARY KEY (role_id, user_id)
        )
        """
    )

    # ---- folders, tags ----------------------------------------------------
    op.execute(
        """
        CREATE TABLE folders (
            id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id         uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
            owner_user_id     uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
            parent_folder_id  uuid REFERENCES folders(id) ON DELETE CASCADE,
            name              text NOT NULL,
            created_at        timestamptz NOT NULL DEFAULT now(),
            updated_at        timestamptz NOT NULL DEFAULT now(),
            deleted_at        timestamptz,
            CONSTRAINT ck_folders_no_self_parent CHECK (id <> parent_folder_id)
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_folders_parent_folder_id_name "
        "ON folders(parent_folder_id, name) WHERE deleted_at IS NULL"
    )
    op.execute(
        "CREATE INDEX ix_folders_tenant_id_parent_folder_id "
        "ON folders(tenant_id, parent_folder_id)"
    )

    op.execute(
        """
        CREATE TABLE tags (
            id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id   uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
            name        text NOT NULL,
            created_at  timestamptz NOT NULL DEFAULT now(),
            updated_at  timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_tags_tenant_id_name UNIQUE (tenant_id, name)
        )
        """
    )

    # ---- projects + project_versions + project_tags -----------------------
    op.execute(
        """
        CREATE TABLE projects (
            id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id           uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
            owner_user_id       uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
            folder_id           uuid REFERENCES folders(id) ON DELETE SET NULL,
            current_version_id  uuid,  -- FK added after project_versions exists
            name                text NOT NULL,
            description         text,
            aspect_ratio        text NOT NULL,
            duration_seconds    numeric(10,3),
            language            varchar(8) NOT NULL DEFAULT 'en',
            style               text,
            settings            jsonb NOT NULL DEFAULT '{}'::jsonb,
            version             integer NOT NULL DEFAULT 1,
            created_at          timestamptz NOT NULL DEFAULT now(),
            updated_at          timestamptz NOT NULL DEFAULT now(),
            deleted_at          timestamptz,
            CONSTRAINT ck_projects_aspect_ratio
                CHECK (aspect_ratio IN ('horizontal','vertical','square'))
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_projects_tenant_id_owner_user_id_name "
        "ON projects(tenant_id, owner_user_id, name) WHERE deleted_at IS NULL"
    )
    op.execute(
        "CREATE INDEX ix_projects_tenant_id_owner_user_id ON projects(tenant_id, owner_user_id)"
    )
    op.execute(
        "CREATE INDEX ix_projects_folder_id ON projects(folder_id) WHERE deleted_at IS NULL"
    )

    op.execute(
        """
        CREATE TABLE project_versions (
            id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            project_id          uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            version_number      integer NOT NULL,
            parent_version_id   uuid REFERENCES project_versions(id) ON DELETE RESTRICT,
            created_by_user_id  uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
            reason              version_reason NOT NULL,
            snapshot            jsonb NOT NULL,
            diff_summary        jsonb,
            created_at          timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_project_versions_project_id_version_number
                UNIQUE (project_id, version_number),
            CONSTRAINT ck_project_versions_no_self_parent CHECK (id <> parent_version_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_project_versions_project_id_created_at "
        "ON project_versions(project_id, created_at)"
    )
    op.execute(
        "CREATE INDEX ix_project_versions_parent_version_id "
        "ON project_versions(parent_version_id)"
    )
    op.execute(
        """
        ALTER TABLE projects
            ADD CONSTRAINT fk_projects_current_version_id__project_versions
            FOREIGN KEY (current_version_id) REFERENCES project_versions(id)
            ON DELETE SET NULL
        """
    )

    op.execute(
        """
        CREATE TABLE project_tags (
            project_id  uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            tag_id      uuid NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
            tagged_at   timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (project_id, tag_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_project_tags_tag_id_project_id ON project_tags(tag_id, project_id)"
    )

    # ---- storyboards, scenes, prompts -------------------------------------
    op.execute(
        """
        CREATE TABLE storyboards (
            id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            project_id          uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            project_version_id  uuid REFERENCES project_versions(id) ON DELETE SET NULL,
            generated_by        text NOT NULL,
            generated_at        timestamptz NOT NULL DEFAULT now(),
            version             integer NOT NULL DEFAULT 1,
            created_at          timestamptz NOT NULL DEFAULT now(),
            updated_at          timestamptz NOT NULL DEFAULT now(),
            deleted_at          timestamptz,
            CONSTRAINT ck_storyboards_generated_by_valid
                CHECK (generated_by IN ('system','user'))
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_storyboards_project_id_created_at "
        "ON storyboards(project_id, created_at)"
    )

    op.execute(
        """
        CREATE TABLE scenes (
            id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            storyboard_id     uuid NOT NULL REFERENCES storyboards(id) ON DELETE CASCADE,
            scene_number      integer NOT NULL,
            title             text NOT NULL,
            duration_seconds  numeric(8,3) NOT NULL,
            narration         text,
            subtitle          text,
            emotion           text,
            camera_angle      text,
            camera_motion     text,
            lens              text,
            lighting          text,
            weather           text,
            location          text,
            animation         text,
            transition_in     text,
            music_mood        text,
            extra             jsonb NOT NULL DEFAULT '{}'::jsonb,
            version           integer NOT NULL DEFAULT 1,
            created_at        timestamptz NOT NULL DEFAULT now(),
            updated_at        timestamptz NOT NULL DEFAULT now(),
            deleted_at        timestamptz,
            CONSTRAINT ck_scenes_duration_positive CHECK (duration_seconds > 0)
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_scenes_storyboard_id_scene_number "
        "ON scenes(storyboard_id, scene_number) WHERE deleted_at IS NULL"
    )
    op.execute("CREATE INDEX ix_scenes_storyboard_id ON scenes(storyboard_id)")

    # ai_models is created before prompts so prompts.model_id FK resolves
    op.execute(
        """
        CREATE TABLE ai_models (
            id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            model_key           text NOT NULL,
            provider            text NOT NULL,
            vendor_model_id     text NOT NULL,
            kind                plugin_kind NOT NULL,
            capabilities        text[] NOT NULL DEFAULT '{}'::text[],
            modalities          text[] NOT NULL DEFAULT '{}'::text[],
            context_window      integer,
            max_output_tokens   integer,
            max_output_pixels   bigint,
            max_output_seconds  integer,
            status              model_status NOT NULL DEFAULT 'available',
            released_at         date,
            deprecated_at       date,
            retires_at          date,
            successor_model_id  uuid REFERENCES ai_models(id) ON DELETE SET NULL,
            tags                text[] NOT NULL DEFAULT '{}'::text[],
            extra               jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at          timestamptz NOT NULL DEFAULT now(),
            updated_at          timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_ai_models_model_key UNIQUE (model_key),
            CONSTRAINT ck_ai_models_deprecated_after_release
                CHECK (deprecated_at IS NULL OR released_at IS NULL OR deprecated_at >= released_at),
            CONSTRAINT ck_ai_models_retires_after_deprecation
                CHECK (retires_at IS NULL OR deprecated_at IS NULL OR retires_at >= deprecated_at),
            CONSTRAINT ck_ai_models_no_self_successor CHECK (id <> successor_model_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_ai_models_provider_kind_status "
        "ON ai_models(provider, kind, status)"
    )
    op.execute(
        "CREATE INDEX ix_ai_models_successor_model_id ON ai_models(successor_model_id)"
    )
    op.execute(
        "CREATE INDEX ix_ai_models_capabilities_gin ON ai_models USING gin (capabilities)"
    )

    op.execute(
        """
        CREATE TABLE prompts (
            id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            project_id          uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            scene_id            uuid REFERENCES scenes(id) ON DELETE SET NULL,
            kind                prompt_kind NOT NULL,
            text_content        text NOT NULL,
            model_id            uuid REFERENCES ai_models(id) ON DELETE SET NULL,
            generated_by_agent  text,
            extra               jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at          timestamptz NOT NULL DEFAULT now(),
            updated_at          timestamptz NOT NULL DEFAULT now(),
            deleted_at          timestamptz
        )
        """
    )
    op.execute("CREATE INDEX ix_prompts_project_id_kind ON prompts(project_id, kind)")
    op.execute("CREATE INDEX ix_prompts_scene_id ON prompts(scene_id)")
    op.execute("CREATE INDEX ix_prompts_model_id ON prompts(model_id)")

    # ---- ai_model_pricing + provider_plugin_registrations -----------------
    op.execute(
        """
        CREATE TABLE ai_model_pricing (
            id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            model_id        uuid NOT NULL REFERENCES ai_models(id) ON DELETE RESTRICT,
            effective_from  timestamptz NOT NULL,
            effective_to    timestamptz,
            unit            pricing_unit NOT NULL,
            price_per_unit  numeric(18,8) NOT NULL,
            currency        varchar(3) NOT NULL,
            created_at      timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_ai_model_pricing_price_nonnegative CHECK (price_per_unit >= 0),
            CONSTRAINT ck_ai_model_pricing_effective_to_after_from
                CHECK (effective_to IS NULL OR effective_to > effective_from)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_ai_model_pricing_model_id_effective_from "
        "ON ai_model_pricing(model_id, effective_from)"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_ai_model_pricing_model_id_unit "
        "ON ai_model_pricing(model_id, unit) WHERE effective_to IS NULL"
    )

    op.execute(
        """
        CREATE TABLE provider_plugin_registrations (
            id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            name                text NOT NULL,
            version             text NOT NULL,
            kind                plugin_kind NOT NULL,
            capabilities        text[] NOT NULL DEFAULT '{}'::text[],
            enabled             boolean NOT NULL DEFAULT true,
            last_health_status  text,
            last_health_at      timestamptz,
            extra               jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at          timestamptz NOT NULL DEFAULT now(),
            updated_at          timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_provider_plugin_registrations_name_version UNIQUE (name, version)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_provider_plugin_registrations_kind_enabled "
        "ON provider_plugin_registrations(kind, enabled)"
    )

    # ---- media_assets, library_folders, library_assets, joins -------------
    op.execute(
        """
        CREATE TABLE media_assets (
            id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id           uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
            owner_user_id       uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
            kind                media_kind NOT NULL,
            project_id          uuid REFERENCES projects(id) ON DELETE SET NULL,
            scene_id            uuid REFERENCES scenes(id) ON DELETE SET NULL,
            prompt_id           uuid REFERENCES prompts(id) ON DELETE SET NULL,
            model_id            uuid REFERENCES ai_models(id) ON DELETE RESTRICT,
            provider            text,
            storage_backend     storage_backend NOT NULL,
            storage_bucket      text NOT NULL,
            storage_key         text NOT NULL,
            mime_type           text NOT NULL,
            size_bytes          bigint NOT NULL,
            width               integer,
            height              integer,
            duration_seconds    numeric(10,3),
            checksum_sha256     bytea NOT NULL,
            source              media_source NOT NULL,
            source_metadata     jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at          timestamptz NOT NULL DEFAULT now(),
            updated_at          timestamptz NOT NULL DEFAULT now(),
            deleted_at          timestamptz,
            CONSTRAINT ck_media_assets_size_nonnegative CHECK (size_bytes >= 0),
            CONSTRAINT uq_media_assets_storage_backend_storage_bucket_storage_key
                UNIQUE (storage_backend, storage_bucket, storage_key)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_media_assets_tenant_id_kind_created_at "
        "ON media_assets(tenant_id, kind, created_at)"
    )
    op.execute("CREATE INDEX ix_media_assets_project_id ON media_assets(project_id)")
    op.execute("CREATE INDEX ix_media_assets_prompt_id ON media_assets(prompt_id)")
    op.execute("CREATE INDEX ix_media_assets_checksum_sha256 ON media_assets(checksum_sha256)")

    op.execute(
        """
        CREATE TABLE library_folders (
            id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id         uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
            owner_user_id     uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
            parent_folder_id  uuid REFERENCES library_folders(id) ON DELETE CASCADE,
            name              text NOT NULL,
            created_at        timestamptz NOT NULL DEFAULT now(),
            updated_at        timestamptz NOT NULL DEFAULT now(),
            deleted_at        timestamptz,
            CONSTRAINT ck_library_folders_no_self_parent CHECK (id <> parent_folder_id)
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_library_folders_parent_folder_id_name "
        "ON library_folders(parent_folder_id, name) WHERE deleted_at IS NULL"
    )
    op.execute(
        "CREATE INDEX ix_library_folders_tenant_id_parent_folder_id "
        "ON library_folders(tenant_id, parent_folder_id)"
    )

    op.execute(
        """
        CREATE TABLE library_assets (
            id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id           uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
            owner_user_id       uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
            media_asset_id      uuid NOT NULL REFERENCES media_assets(id) ON DELETE RESTRICT,
            library_folder_id   uuid REFERENCES library_folders(id) ON DELETE SET NULL,
            name                text NOT NULL,
            description         text,
            tags                text[] NOT NULL DEFAULT '{}'::text[],
            embedding           vector(1536),
            usage_count         integer NOT NULL DEFAULT 0,
            last_used_at        timestamptz,
            version             integer NOT NULL DEFAULT 1,
            created_at          timestamptz NOT NULL DEFAULT now(),
            updated_at          timestamptz NOT NULL DEFAULT now(),
            deleted_at          timestamptz,
            CONSTRAINT uq_library_assets_media_asset_id UNIQUE (media_asset_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_library_assets_tenant_id_owner_user_id "
        "ON library_assets(tenant_id, owner_user_id)"
    )
    op.execute(
        "CREATE INDEX ix_library_assets_last_used_at "
        "ON library_assets(last_used_at) WHERE deleted_at IS NULL"
    )
    op.execute("CREATE INDEX ix_library_assets_tags_gin ON library_assets USING gin (tags)")
    op.execute(
        "CREATE INDEX ix_library_assets_embedding_hnsw "
        "ON library_assets USING hnsw (embedding vector_cosine_ops)"
    )

    op.execute(
        """
        CREATE TABLE library_asset_projects (
            library_asset_id  uuid NOT NULL REFERENCES library_assets(id) ON DELETE CASCADE,
            project_id        uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            first_used_at     timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (library_asset_id, project_id)
        )
        """
    )

    # ---- transitions + timelines + tracks + clips -------------------------
    op.execute(
        """
        CREATE TABLE transitions (
            id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            name              text NOT NULL,
            kind              text NOT NULL,
            duration_seconds  numeric(6,3) NOT NULL DEFAULT 0.5,
            params            jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at        timestamptz NOT NULL DEFAULT now(),
            updated_at        timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE timelines (
            id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            project_id          uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            project_version_id  uuid REFERENCES project_versions(id) ON DELETE SET NULL,
            duration_seconds    numeric(10,3) NOT NULL DEFAULT 0,
            aspect_ratio        text NOT NULL,
            frame_rate          integer NOT NULL DEFAULT 30,
            background_color    text NOT NULL DEFAULT '#000000',
            version             integer NOT NULL DEFAULT 1,
            created_at          timestamptz NOT NULL DEFAULT now(),
            updated_at          timestamptz NOT NULL DEFAULT now(),
            deleted_at          timestamptz,
            CONSTRAINT ck_timelines_frame_rate_range CHECK (frame_rate BETWEEN 1 AND 240)
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_timelines_project_id "
        "ON timelines(project_id) WHERE deleted_at IS NULL"
    )

    op.execute(
        """
        CREATE TABLE tracks (
            id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            timeline_id  uuid NOT NULL REFERENCES timelines(id) ON DELETE CASCADE,
            kind         track_kind NOT NULL,
            z_index      integer NOT NULL,
            locked       boolean NOT NULL DEFAULT false,
            muted        boolean NOT NULL DEFAULT false,
            name         text NOT NULL,
            created_at   timestamptz NOT NULL DEFAULT now(),
            updated_at   timestamptz NOT NULL DEFAULT now(),
            deleted_at   timestamptz
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_tracks_timeline_id_z_index "
        "ON tracks(timeline_id, z_index) WHERE deleted_at IS NULL"
    )
    op.execute("CREATE INDEX ix_tracks_timeline_id_kind ON tracks(timeline_id, kind)")

    op.execute(
        """
        CREATE TABLE clips (
            id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            track_id              uuid NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
            media_asset_id        uuid REFERENCES media_assets(id) ON DELETE SET NULL,
            start_seconds         numeric(10,3) NOT NULL,
            end_seconds           numeric(10,3) NOT NULL,
            source_start_seconds  numeric(10,3) NOT NULL DEFAULT 0,
            source_end_seconds    numeric(10,3) NOT NULL DEFAULT 0,
            transition_in_id      uuid REFERENCES transitions(id) ON DELETE SET NULL,
            transition_out_id     uuid REFERENCES transitions(id) ON DELETE SET NULL,
            effects               jsonb NOT NULL DEFAULT '[]'::jsonb,
            volume                numeric(4,2) NOT NULL DEFAULT 1.00,
            locked                boolean NOT NULL DEFAULT false,
            created_at            timestamptz NOT NULL DEFAULT now(),
            updated_at            timestamptz NOT NULL DEFAULT now(),
            deleted_at            timestamptz,
            CONSTRAINT ck_clips_start_nonnegative CHECK (start_seconds >= 0),
            CONSTRAINT ck_clips_end_after_start CHECK (end_seconds > start_seconds),
            CONSTRAINT ck_clips_volume_range CHECK (volume BETWEEN 0 AND 4)
        )
        """
    )
    op.execute("CREATE INDEX ix_clips_track_id_start_seconds ON clips(track_id, start_seconds)")
    op.execute("CREATE INDEX ix_clips_media_asset_id ON clips(media_asset_id)")

    # ---- workflows --------------------------------------------------------
    op.execute(
        """
        CREATE TABLE workflow_runs (
            id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            project_id            uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            workflow_key          text NOT NULL,
            workflow_version      text NOT NULL,
            status                workflow_status NOT NULL,
            started_at            timestamptz,
            finished_at           timestamptz,
            triggered_by_user_id  uuid REFERENCES users(id) ON DELETE SET NULL,
            idempotency_key       text,
            input_snapshot        jsonb NOT NULL,
            output_summary        jsonb,
            error                 jsonb,
            created_at            timestamptz NOT NULL DEFAULT now(),
            updated_at            timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_workflow_runs_project_id_idempotency_key
                UNIQUE (project_id, idempotency_key)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_workflow_runs_project_id_status "
        "ON workflow_runs(project_id, status)"
    )
    op.execute(
        "CREATE INDEX ix_workflow_runs_workflow_key_workflow_version "
        "ON workflow_runs(workflow_key, workflow_version)"
    )

    op.execute(
        """
        CREATE TABLE workflow_steps (
            id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            workflow_run_id  uuid NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
            step_index       integer NOT NULL,
            step_name        text NOT NULL,
            status           step_status NOT NULL,
            started_at       timestamptz,
            finished_at      timestamptz,
            retries          integer NOT NULL DEFAULT 0,
            input            jsonb,
            output           jsonb,
            error            jsonb,
            created_at       timestamptz NOT NULL DEFAULT now(),
            updated_at       timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_workflow_steps_workflow_run_id_step_index
                UNIQUE (workflow_run_id, step_index)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_workflow_steps_workflow_run_id_status "
        "ON workflow_steps(workflow_run_id, status)"
    )

    op.execute(
        """
        CREATE TABLE workflow_checkpoints (
            id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            workflow_run_id  uuid NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
            step_index       integer NOT NULL,
            state            jsonb NOT NULL,
            created_at       timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_workflow_checkpoints_workflow_run_id_step_index "
        "ON workflow_checkpoints(workflow_run_id, step_index)"
    )

    # ---- render_jobs + export_jobs ----------------------------------------
    op.execute(
        """
        CREATE TABLE render_jobs (
            id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            project_id              uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            timeline_id             uuid NOT NULL REFERENCES timelines(id) ON DELETE RESTRICT,
            workflow_run_id         uuid REFERENCES workflow_runs(id) ON DELETE SET NULL,
            pipeline                text NOT NULL,
            pipeline_version        text NOT NULL,
            queue                   text NOT NULL,
            priority                integer NOT NULL DEFAULT 0,
            status                  render_status NOT NULL,
            started_at              timestamptz,
            finished_at             timestamptz,
            progress                text NOT NULL DEFAULT '0.00',
            error                   jsonb,
            output_media_asset_id   uuid REFERENCES media_assets(id) ON DELETE SET NULL,
            idempotency_key         text,
            version                 integer NOT NULL DEFAULT 1,
            created_at              timestamptz NOT NULL DEFAULT now(),
            updated_at              timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_render_jobs_queue_valid
                CHECK (queue IN ('critical','high','normal','low','background')),
            CONSTRAINT uq_render_jobs_project_id_idempotency_key
                UNIQUE (project_id, idempotency_key)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_render_jobs_status_priority_created_at "
        "ON render_jobs(status, priority, created_at)"
    )
    op.execute(
        "CREATE INDEX ix_render_jobs_project_id_status ON render_jobs(project_id, status)"
    )

    op.execute(
        """
        CREATE TABLE export_jobs (
            id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            render_job_id           uuid NOT NULL REFERENCES render_jobs(id) ON DELETE CASCADE,
            requested_by_user_id    uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
            format                  export_format NOT NULL,
            quality                 export_quality NOT NULL,
            orientation             export_orientation NOT NULL,
            status                  export_status NOT NULL,
            output_media_asset_id   uuid REFERENCES media_assets(id) ON DELETE SET NULL,
            download_count          integer NOT NULL DEFAULT 0,
            last_downloaded_at      timestamptz,
            file_size_bytes         bigint,
            finished_at             timestamptz,
            version                 integer NOT NULL DEFAULT 1,
            created_at              timestamptz NOT NULL DEFAULT now(),
            updated_at              timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ix_export_jobs_render_job_id ON export_jobs(render_job_id)")
    op.execute(
        "CREATE INDEX ix_export_jobs_requested_by_user_id_created_at "
        "ON export_jobs(requested_by_user_id, created_at)"
    )

    # ---- usage_records (partitioned) + cost_reconciliations ---------------
    op.execute(
        """
        CREATE TABLE usage_records (
            id                  uuid NOT NULL DEFAULT gen_random_uuid(),
            tenant_id           uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
            user_id             uuid REFERENCES users(id) ON DELETE SET NULL,
            project_id          uuid REFERENCES projects(id) ON DELETE SET NULL,
            scene_id            uuid REFERENCES scenes(id) ON DELETE SET NULL,
            prompt_id           uuid REFERENCES prompts(id) ON DELETE SET NULL,
            workflow_run_id     uuid REFERENCES workflow_runs(id) ON DELETE SET NULL,
            workflow_step_id    uuid REFERENCES workflow_steps(id) ON DELETE SET NULL,
            model_id            uuid NOT NULL REFERENCES ai_models(id) ON DELETE RESTRICT,
            pricing_id          uuid REFERENCES ai_model_pricing(id) ON DELETE SET NULL,
            request_id          text,
            unit                pricing_unit NOT NULL,
            unit_count          numeric(18,4) NOT NULL,
            tokens_prompt       integer,
            tokens_completion   integer,
            images_count        integer,
            seconds_generated   numeric(10,3),
            credits_consumed    numeric(18,4) NOT NULL DEFAULT 0,
            estimated_cost      numeric(18,8) NOT NULL DEFAULT 0,
            actual_cost         numeric(18,8),
            currency            varchar(3) NOT NULL,
            status              usage_status NOT NULL,
            latency_ms          integer,
            error_code          text,
            extra               jsonb NOT NULL DEFAULT '{}'::jsonb,
            occurred_at         timestamptz NOT NULL DEFAULT now(),
            created_at          timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT pk_usage_records PRIMARY KEY (id, occurred_at),
            CONSTRAINT ck_usage_records_credits_nonnegative CHECK (credits_consumed >= 0),
            CONSTRAINT ck_usage_records_estimated_cost_nonnegative CHECK (estimated_cost >= 0),
            CONSTRAINT ck_usage_records_actual_cost_nonnegative
                CHECK (actual_cost IS NULL OR actual_cost >= 0)
        ) PARTITION BY RANGE (occurred_at)
        """
    )
    op.execute(
        "CREATE INDEX ix_usage_records_tenant_id_occurred_at "
        "ON usage_records(tenant_id, occurred_at)"
    )
    op.execute(
        "CREATE INDEX ix_usage_records_model_id_occurred_at "
        "ON usage_records(model_id, occurred_at)"
    )
    op.execute("CREATE INDEX ix_usage_records_workflow_run_id ON usage_records(workflow_run_id)")
    op.execute("CREATE INDEX ix_usage_records_request_id ON usage_records(request_id)")

    op.execute(
        """
        CREATE TABLE cost_reconciliations (
            id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id           uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
            model_id            uuid NOT NULL REFERENCES ai_models(id) ON DELETE RESTRICT,
            period_start        timestamptz NOT NULL,
            period_end          timestamptz NOT NULL,
            invoiced_amount     numeric(18,4) NOT NULL,
            estimated_amount    numeric(18,4) NOT NULL,
            variance            numeric(18,4) NOT NULL,
            currency            varchar(3) NOT NULL,
            notes               text,
            created_at          timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_cost_reconciliations_period_valid CHECK (period_end > period_start)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_cost_reconciliations_tenant_id_period_start "
        "ON cost_reconciliations(tenant_id, period_start)"
    )

    # ---- billing: plans, subscriptions, invoices, credit_ledger -----------
    op.execute(
        """
        CREATE TABLE plans (
            id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            code            text NOT NULL,
            name            text NOT NULL,
            description     text,
            cycle           billing_cycle NOT NULL,
            monthly_credits numeric(18,4) NOT NULL DEFAULT 0,
            monthly_price   numeric(18,4) NOT NULL DEFAULT 0,
            currency        varchar(3) NOT NULL DEFAULT 'USD',
            features        jsonb NOT NULL DEFAULT '{}'::jsonb,
            active          boolean NOT NULL DEFAULT true,
            created_at      timestamptz NOT NULL DEFAULT now(),
            updated_at      timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_plans_code UNIQUE (code)
        )
        """
    )

    op.execute(
        """
        CREATE TABLE subscriptions (
            id                       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id                uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
            plan_id                  uuid NOT NULL REFERENCES plans(id) ON DELETE RESTRICT,
            status                   subscription_status NOT NULL,
            started_at               timestamptz NOT NULL,
            renews_at                timestamptz,
            canceled_at              timestamptz,
            trial_ends_at            timestamptz,
            payment_provider         text NOT NULL,
            external_customer_id     text,
            external_subscription_id text,
            version                  integer NOT NULL DEFAULT 1,
            created_at               timestamptz NOT NULL DEFAULT now(),
            updated_at               timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_subscriptions_tenant_id_active "
        "ON subscriptions(tenant_id) WHERE status IN ('active','trialing','past_due')"
    )
    op.execute(
        "CREATE INDEX ix_subscriptions_status_renews_at ON subscriptions(status, renews_at)"
    )

    op.execute(
        """
        CREATE TABLE invoices (
            id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            subscription_id     uuid NOT NULL REFERENCES subscriptions(id) ON DELETE RESTRICT,
            number              text NOT NULL,
            status              invoice_status NOT NULL,
            amount_due          numeric(18,4) NOT NULL,
            amount_paid         numeric(18,4) NOT NULL DEFAULT 0,
            currency            varchar(3) NOT NULL,
            period_start        timestamptz NOT NULL,
            period_end          timestamptz NOT NULL,
            issued_at           timestamptz NOT NULL,
            paid_at             timestamptz,
            external_invoice_id text,
            created_at          timestamptz NOT NULL DEFAULT now(),
            updated_at          timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_invoices_number UNIQUE (number),
            CONSTRAINT ck_invoices_period_valid CHECK (period_end > period_start)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_invoices_subscription_id_period_start "
        "ON invoices(subscription_id, period_start)"
    )
    op.execute("CREATE INDEX ix_invoices_status ON invoices(status)")

    op.execute(
        """
        CREATE TABLE credit_ledger (
            id                       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id                uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
            user_id                  uuid REFERENCES users(id) ON DELETE SET NULL,
            entry_type               ledger_entry_type NOT NULL,
            amount                   numeric(18,4) NOT NULL,
            balance_after            numeric(18,4) NOT NULL,
            related_invoice_id       uuid REFERENCES invoices(id) ON DELETE SET NULL,
            related_usage_record_id  uuid,
            idempotency_key          text NOT NULL,
            description              text,
            created_at               timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_credit_ledger_balance_nonnegative CHECK (balance_after >= 0),
            CONSTRAINT uq_credit_ledger_tenant_id_idempotency_key
                UNIQUE (tenant_id, idempotency_key)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_credit_ledger_tenant_id_created_at "
        "ON credit_ledger(tenant_id, created_at)"
    )

    # ---- feature_flags ----------------------------------------------------
    op.execute(
        """
        CREATE TABLE feature_flags (
            id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            key                text NOT NULL,
            description        text,
            flag_type          flag_type NOT NULL,
            default_value      jsonb NOT NULL DEFAULT 'false'::jsonb,
            rollout_percent    integer,
            variants           jsonb,
            archived           boolean NOT NULL DEFAULT false,
            version            integer NOT NULL DEFAULT 1,
            created_at         timestamptz NOT NULL DEFAULT now(),
            updated_at         timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_feature_flags_key UNIQUE (key),
            CONSTRAINT ck_feature_flags_rollout_percent_range
                CHECK (rollout_percent IS NULL OR rollout_percent BETWEEN 0 AND 100)
        )
        """
    )

    op.execute(
        """
        CREATE TABLE feature_flag_overrides (
            id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            feature_flag_id   uuid NOT NULL REFERENCES feature_flags(id) ON DELETE CASCADE,
            scope             flag_scope NOT NULL,
            scope_id          uuid NOT NULL,
            value             jsonb NOT NULL,
            expires_at        timestamptz,
            created_at        timestamptz NOT NULL DEFAULT now(),
            updated_at        timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_feature_flag_overrides_feature_flag_id_scope_scope_id
                UNIQUE (feature_flag_id, scope, scope_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_feature_flag_overrides_scope_scope_id "
        "ON feature_flag_overrides(scope, scope_id)"
    )

    # ---- notifications, analytics_events ---------------------------------
    op.execute(
        """
        CREATE TABLE notifications (
            id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id               uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            kind                  text NOT NULL,
            title                 text NOT NULL,
            body                  text,
            payload               jsonb NOT NULL DEFAULT '{}'::jsonb,
            delivered_in_app_at   timestamptz,
            delivered_email_at    timestamptz,
            read_at               timestamptz,
            archived              boolean NOT NULL DEFAULT false,
            created_at            timestamptz NOT NULL DEFAULT now(),
            updated_at            timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_notifications_user_id_unread "
        "ON notifications(user_id, created_at) "
        "WHERE read_at IS NULL AND archived = false"
    )
    op.execute(
        "CREATE INDEX ix_notifications_user_id_created_at "
        "ON notifications(user_id, created_at)"
    )

    op.execute(
        """
        CREATE TABLE analytics_events (
            id           uuid NOT NULL DEFAULT gen_random_uuid(),
            tenant_id    uuid REFERENCES tenants(id) ON DELETE SET NULL,
            user_id      uuid REFERENCES users(id) ON DELETE SET NULL,
            session_id   uuid,
            event_name   text NOT NULL,
            properties   jsonb NOT NULL DEFAULT '{}'::jsonb,
            ip           inet,
            user_agent   text,
            occurred_at  timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT pk_analytics_events PRIMARY KEY (id, occurred_at)
        ) PARTITION BY RANGE (occurred_at)
        """
    )
    op.execute(
        "CREATE INDEX ix_analytics_events_tenant_id_occurred_at "
        "ON analytics_events(tenant_id, occurred_at)"
    )
    op.execute(
        "CREATE INDEX ix_analytics_events_event_name_occurred_at "
        "ON analytics_events(event_name, occurred_at)"
    )
    op.execute(
        "CREATE INDEX ix_analytics_events_properties_gin "
        "ON analytics_events USING gin (properties)"
    )

    # ---- event_outbox + event_log (partitioned) ---------------------------
    op.execute(
        """
        CREATE TABLE event_outbox (
            id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            aggregate_type  text NOT NULL,
            aggregate_id    uuid NOT NULL,
            event_type      text NOT NULL,
            event_version   text NOT NULL DEFAULT '1.0',
            payload         jsonb NOT NULL,
            metadata        jsonb NOT NULL DEFAULT '{}'::jsonb,
            occurred_at     timestamptz NOT NULL,
            published_at    timestamptz,
            attempts        integer NOT NULL DEFAULT 0,
            last_error      text,
            created_at      timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_event_outbox_unpublished_occurred_at "
        "ON event_outbox(occurred_at) WHERE published_at IS NULL"
    )
    op.execute(
        "CREATE INDEX ix_event_outbox_aggregate_type_aggregate_id "
        "ON event_outbox(aggregate_type, aggregate_id)"
    )

    op.execute(
        """
        CREATE TABLE event_log (
            id                 uuid NOT NULL DEFAULT gen_random_uuid(),
            aggregate_type     text NOT NULL,
            aggregate_id       uuid NOT NULL,
            aggregate_version  bigint NOT NULL,
            event_type         text NOT NULL,
            event_version      text NOT NULL DEFAULT '1.0',
            payload            jsonb NOT NULL,
            metadata           jsonb NOT NULL DEFAULT '{}'::jsonb,
            occurred_at        timestamptz NOT NULL,
            CONSTRAINT pk_event_log PRIMARY KEY (id, occurred_at),
            CONSTRAINT uq_event_log_aggregate
                UNIQUE (aggregate_type, aggregate_id, aggregate_version, occurred_at)
        ) PARTITION BY RANGE (occurred_at)
        """
    )
    op.execute(
        "CREATE INDEX ix_event_log_event_type_occurred_at ON event_log(event_type, occurred_at)"
    )

    # ---- configuration tables --------------------------------------------
    op.execute(
        """
        CREATE TABLE system_settings (
            id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            key                 text NOT NULL,
            value               jsonb NOT NULL,
            description         text,
            is_secret           boolean NOT NULL DEFAULT false,
            updated_by_user_id  uuid REFERENCES users(id) ON DELETE SET NULL,
            version             integer NOT NULL DEFAULT 1,
            created_at          timestamptz NOT NULL DEFAULT now(),
            updated_at          timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_system_settings_key UNIQUE (key)
        )
        """
    )

    op.execute(
        """
        CREATE TABLE tenant_settings (
            id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id           uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            key                 text NOT NULL,
            value               jsonb NOT NULL,
            description         text,
            is_secret           boolean NOT NULL DEFAULT false,
            updated_by_user_id  uuid REFERENCES users(id) ON DELETE SET NULL,
            version             integer NOT NULL DEFAULT 1,
            created_at          timestamptz NOT NULL DEFAULT now(),
            updated_at          timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_tenant_settings_tenant_id_key UNIQUE (tenant_id, key)
        )
        """
    )
    op.execute("CREATE INDEX ix_tenant_settings_tenant_id ON tenant_settings(tenant_id)")

    op.execute(
        """
        CREATE TABLE provider_settings (
            id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            provider            text NOT NULL,
            tenant_id           uuid REFERENCES tenants(id) ON DELETE CASCADE,
            key                 text NOT NULL,
            value               jsonb NOT NULL,
            is_secret           boolean NOT NULL DEFAULT false,
            updated_by_user_id  uuid REFERENCES users(id) ON DELETE SET NULL,
            version             integer NOT NULL DEFAULT 1,
            created_at          timestamptz NOT NULL DEFAULT now(),
            updated_at          timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_provider_settings_global_provider_key "
        "ON provider_settings(provider, key) WHERE tenant_id IS NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_provider_settings_tenant_provider_key "
        "ON provider_settings(tenant_id, provider, key) WHERE tenant_id IS NOT NULL"
    )
    op.execute("CREATE INDEX ix_provider_settings_provider ON provider_settings(provider)")

    # ---- templates, webhook_deliveries -----------------------------------
    op.execute(
        """
        CREATE TABLE templates (
            id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id       uuid REFERENCES tenants(id) ON DELETE RESTRICT,
            owner_user_id   uuid REFERENCES users(id) ON DELETE RESTRICT,
            name            text NOT NULL,
            description     text,
            category        text NOT NULL,
            tags            text[] NOT NULL DEFAULT '{}'::text[],
            body            jsonb NOT NULL,
            is_public       boolean NOT NULL DEFAULT false,
            is_system       boolean NOT NULL DEFAULT false,
            version         integer NOT NULL DEFAULT 1,
            created_at      timestamptz NOT NULL DEFAULT now(),
            updated_at      timestamptz NOT NULL DEFAULT now(),
            deleted_at      timestamptz
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_templates_tenant_id_owner_user_id_name "
        "ON templates(tenant_id, owner_user_id, name) WHERE deleted_at IS NULL"
    )
    op.execute(
        "CREATE INDEX ix_templates_category_is_public ON templates(category, is_public)"
    )

    op.execute(
        """
        CREATE TABLE webhook_deliveries (
            id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id         uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            destination_url   text NOT NULL,
            event_type        text NOT NULL,
            source_event_id   text NOT NULL,
            payload           jsonb NOT NULL,
            attempts          integer NOT NULL DEFAULT 0,
            last_attempt_at   timestamptz,
            next_attempt_at   timestamptz,
            delivered_at      timestamptz,
            last_status_code  integer,
            last_error        text,
            created_at        timestamptz NOT NULL DEFAULT now(),
            updated_at        timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_webhook_deliveries_tenant_id_source_event_id
                UNIQUE (tenant_id, source_event_id),
            CONSTRAINT ck_webhook_deliveries_attempts_nonnegative CHECK (attempts >= 0)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_webhook_deliveries_next_attempt_at "
        "ON webhook_deliveries(next_attempt_at) WHERE delivered_at IS NULL"
    )
    op.execute(
        "CREATE INDEX ix_webhook_deliveries_tenant_id_event_type "
        "ON webhook_deliveries(tenant_id, event_type)"
    )

    # ---- operational tables: idempotency_keys, distributed_locks ----------
    op.execute(
        """
        CREATE TABLE idempotency_keys (
            id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id         uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            key               text NOT NULL,
            resource_type     text NOT NULL,
            resource_id       uuid,
            request_hash      text NOT NULL,
            response_hash     text,
            response_payload  jsonb,
            status            idempotency_status NOT NULL,
            http_status       text,
            expires_at        timestamptz NOT NULL,
            created_at        timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_idempotency_keys_tenant_id_key_resource_type
                UNIQUE (tenant_id, key, resource_type)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_idempotency_keys_expires_at "
        "ON idempotency_keys(expires_at) WHERE status <> 'in_flight'"
    )
    op.execute(
        "CREATE INDEX ix_idempotency_keys_resource_type_resource_id "
        "ON idempotency_keys(resource_type, resource_id)"
    )

    op.execute(
        """
        CREATE TABLE distributed_locks (
            lock_key      text PRIMARY KEY,
            owner         text NOT NULL,
            lease_until   timestamptz NOT NULL,
            heartbeat_at  timestamptz NOT NULL,
            acquired_at   timestamptz NOT NULL DEFAULT now(),
            metadata      jsonb NOT NULL DEFAULT '{}'::jsonb
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_distributed_locks_lease_until ON distributed_locks(lease_until)"
    )

    # ---- audit_log (partitioned, immutable) -------------------------------
    op.execute(
        """
        CREATE TABLE audit_log (
            id              uuid NOT NULL DEFAULT gen_random_uuid(),
            tenant_id       uuid REFERENCES tenants(id) ON DELETE SET NULL,
            actor_kind      audit_actor_kind NOT NULL,
            actor_user_id   uuid REFERENCES users(id) ON DELETE SET NULL,
            actor_label     text,
            entity_type     text NOT NULL,
            entity_id       uuid,
            action          text NOT NULL,
            before_json     jsonb,
            after_json      jsonb,
            correlation_id  uuid,
            request_id      text,
            ip              inet,
            user_agent      text,
            occurred_at     timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT pk_audit_log PRIMARY KEY (id, occurred_at)
        ) PARTITION BY RANGE (occurred_at)
        """
    )
    op.execute(
        "CREATE INDEX ix_audit_log_tenant_id_occurred_at "
        "ON audit_log(tenant_id, occurred_at)"
    )
    op.execute(
        "CREATE INDEX ix_audit_log_entity_type_entity_id_occurred_at "
        "ON audit_log(entity_type, entity_id, occurred_at)"
    )
    op.execute(
        "CREATE INDEX ix_audit_log_actor_user_id_occurred_at "
        "ON audit_log(actor_user_id, occurred_at)"
    )
    op.execute("CREATE INDEX ix_audit_log_action_occurred_at ON audit_log(action, occurred_at)")

    # ---- agent_memory -----------------------------------------------------
    op.execute(
        """
        CREATE TABLE agent_memory (
            id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id          uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            user_id            uuid REFERENCES users(id) ON DELETE SET NULL,
            project_id         uuid REFERENCES projects(id) ON DELETE SET NULL,
            agent_key          text NOT NULL,
            kind               text NOT NULL,
            content            text NOT NULL,
            embedding          vector(1536),
            salience           numeric(4,3) NOT NULL DEFAULT 0.5,
            last_accessed_at   timestamptz,
            metadata           jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at         timestamptz NOT NULL DEFAULT now(),
            updated_at         timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_agent_memory_tenant_id_agent_key_kind "
        "ON agent_memory(tenant_id, agent_key, kind)"
    )
    op.execute("CREATE INDEX ix_agent_memory_project_id ON agent_memory(project_id)")
    op.execute(
        "CREATE INDEX ix_agent_memory_embedding_hnsw "
        "ON agent_memory USING hnsw (embedding vector_cosine_ops)"
    )

    # ---- _backup_sentinel -------------------------------------------------
    op.execute(
        """
        CREATE TABLE _backup_sentinel (
            id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            inserted_at  timestamptz NOT NULL DEFAULT now(),
            label        text NOT NULL,
            notes        text
        )
        """
    )

    # ---- partitions --------------------------------------------------------
    _create_initial_partitions()

    # ---- triggers ----------------------------------------------------------
    _UPDATED_AT_TABLES = (
        "tenants", "users", "oauth_identities", "folders", "tags",
        "projects", "storyboards", "scenes", "prompts",
        "ai_models", "provider_plugin_registrations",
        "media_assets", "library_folders", "library_assets", "transitions",
        "timelines", "tracks", "clips",
        "workflow_runs", "workflow_steps",
        "render_jobs", "export_jobs",
        "plans", "subscriptions", "invoices",
        "feature_flags", "feature_flag_overrides",
        "notifications",
        "system_settings", "tenant_settings", "provider_settings",
        "templates", "webhook_deliveries", "agent_memory",
    )
    for tbl in _UPDATED_AT_TABLES:
        op.execute(
            f"CREATE TRIGGER tg_{tbl}_biu_touch_updated_at "
            f"BEFORE UPDATE ON {tbl} FOR EACH ROW EXECUTE FUNCTION touch_updated_at()"
        )

    _VERSION_BUMP_TABLES = (
        "users", "projects", "storyboards", "scenes",
        "library_assets", "timelines",
        "render_jobs", "export_jobs",
        "subscriptions",
        "feature_flags",
        "system_settings", "tenant_settings", "provider_settings",
        "templates",
    )
    for tbl in _VERSION_BUMP_TABLES:
        op.execute(
            f"CREATE TRIGGER tg_{tbl}_biu_version_bump "
            f"BEFORE UPDATE ON {tbl} FOR EACH ROW EXECUTE FUNCTION bump_version()"
        )

    _IMMUTABLE_TABLES = (
        "project_versions",
        "ai_model_pricing",
        "workflow_checkpoints",
        "usage_records",
        "credit_ledger",
        "analytics_events",
        "event_log",
        "audit_log",
    )
    for tbl in _IMMUTABLE_TABLES:
        op.execute(
            f"CREATE TRIGGER tg_{tbl}_bud_reject_mutation "
            f"BEFORE UPDATE OR DELETE ON {tbl} "
            f"FOR EACH ROW EXECUTE FUNCTION reject_mutation()"
        )

    op.execute(
        "CREATE TRIGGER tg_credit_ledger_bi_enforce_balance "
        "BEFORE INSERT ON credit_ledger "
        "FOR EACH ROW EXECUTE FUNCTION enforce_credit_ledger_balance()"
    )


# ---------------------------------------------------------------------------
# Downgrade
# ---------------------------------------------------------------------------
def downgrade() -> None:
    # Order matters: drop dependents first, then partition children, then enums.
    _drop_partitions()
    _DROP_ORDER = [
        "_backup_sentinel",
        "agent_memory",
        "audit_log",
        "distributed_locks",
        "idempotency_keys",
        "webhook_deliveries",
        "templates",
        "provider_settings",
        "tenant_settings",
        "system_settings",
        "event_log",
        "event_outbox",
        "analytics_events",
        "notifications",
        "feature_flag_overrides",
        "feature_flags",
        "credit_ledger",
        "invoices",
        "subscriptions",
        "plans",
        "cost_reconciliations",
        "usage_records",
        "export_jobs",
        "render_jobs",
        "workflow_checkpoints",
        "workflow_steps",
        "workflow_runs",
        "clips",
        "tracks",
        "timelines",
        "transitions",
        "library_asset_projects",
        "library_assets",
        "library_folders",
        "media_assets",
        "provider_plugin_registrations",
        "ai_model_pricing",
        "prompts",
        "ai_models",
        "scenes",
        "storyboards",
        "project_tags",
    ]
    for tbl in _DROP_ORDER:
        op.execute(f"DROP TABLE IF EXISTS {tbl} CASCADE")

    # projects ↔ project_versions has a circular FK; drop one side first
    op.execute(
        "ALTER TABLE IF EXISTS projects "
        "DROP CONSTRAINT IF EXISTS fk_projects_current_version_id__project_versions"
    )
    op.execute("DROP TABLE IF EXISTS project_versions CASCADE")
    op.execute("DROP TABLE IF EXISTS projects CASCADE")
    op.execute("DROP TABLE IF EXISTS folders CASCADE")
    op.execute("DROP TABLE IF EXISTS tags CASCADE")
    op.execute("DROP TABLE IF EXISTS roles_users CASCADE")
    op.execute("DROP TABLE IF EXISTS roles CASCADE")
    op.execute("DROP TABLE IF EXISTS sessions CASCADE")
    op.execute("DROP TABLE IF EXISTS oauth_identities CASCADE")
    op.execute("DROP TABLE IF EXISTS users CASCADE")
    op.execute("DROP TABLE IF EXISTS tenants CASCADE")

    _drop_helper_functions()
    _drop_enums()
    _drop_extensions()
