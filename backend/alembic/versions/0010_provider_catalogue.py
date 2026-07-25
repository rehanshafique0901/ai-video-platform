"""Phase 3 Slice α8.5d — provider capability-catalogue tables (schema only).

See: docs/engineering/PHASE3_ALPHA8_5d_PREFLIGHT.md §5 (target schema)
     docs/engineering/PROVIDER_RUNTIME_DATA_MODEL.md (ownership + flow)
     docs/database/schema.md §38 (provider catalogue)

**Phase 1 of α8.5d: structure only. No data, no seeding, no runtime behaviour.**
The seeder (`scripts/seed_providers.py`, Phase 2) is the *only* writer of these
tables; the runtime never reads YAML (W8.5c.2 / W8.5d.1) and never mutates
catalogue rows (W8.5d.10). Operational state (health, latency, quota-remaining,
success rate, queue depth) is deliberately **absent** here — it belongs to
operational tables owned by the runtime, never the catalogue (W8.5d.10).

This migration is purely **additive**: it creates new enums + new tables and
touches no existing table (Freeze Gate 1 = No, Gate 2 = N/A). The catalogue
coexists with — and never duplicates — `ai_models`/`ai_model_pricing` (model
catalogue + authoritative pricing) and `provider_plugin_registrations` (loaded
code plugins + live health). `kind` reuses the existing `plugin_kind` enum.

Postgres enum values mirror the Pydantic StrEnums in `scripts/provider_manifest.py`
one-to-one (anti-drift). D-B storage tiers: stable⇒typed columns,
semi-structured⇒JSONB, highly-variable⇒own tables (capability_dependencies,
adapter_fallbacks). `downgrade` drops every table + type so the ci_gate
upgrade→downgrade→upgrade roundtrip (stages 5–7) runs on a clean slate.

Revision ID: 0010_provider_catalogue
Revises: 0009_notifications_source_event_id
Create Date: 2026-07-25
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0010_provider_catalogue"
down_revision: str | None | Sequence[str] = "0009_notifications_source_event_id"
branch_labels: str | None | Sequence[str] = None
depends_on: str | None | Sequence[str] = None


# --------------------------------------------------------------------------- #
# New enums (mirror scripts/provider_manifest.py StrEnums; `kind` reuses plugin_kind)
# --------------------------------------------------------------------------- #
_ENUMS: dict[str, tuple[str, ...]] = {
    "provider_pricing": ("free", "freemium", "paid"),
    "provider_auth": ("none", "api_key", "oauth", "token"),
    "adapter_status": ("planned", "implemented"),
    "adapter_execution_mode": ("local", "cloud", "hybrid"),
    "cost_unit": ("image", "second", "minute", "token", "character", "request"),
    "cost_source": ("declared", "derived", "unknown"),
    "routing_strategy": (
        "free_first",
        "lowest_cost",
        "highest_quality",
        "fastest",
        "balanced",
        "offline_only",
        "privacy_first",
        "commercial_only",
        "free_only",
    ),
    "fallback_mode": ("automatic", "none"),
    "selection_mode": ("best_available", "first_available"),
    "gpu_backend": ("metal", "cuda", "rocm", "cpu"),
    "generation_mode": ("quick", "balanced", "quality", "ultra"),
    "capability_dep_kind": ("requires", "optional"),
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

    # ---- capabilities (the vocabulary) ------------------------------------
    op.execute(
        """
        CREATE TABLE capabilities (
            id          text PRIMARY KEY,
            kind        plugin_kind NOT NULL,
            inputs      text[] NOT NULL DEFAULT '{}'::text[],
            outputs     text[] NOT NULL DEFAULT '{}'::text[],
            requires    text[] NOT NULL DEFAULT '{}'::text[],
            optional    text[] NOT NULL DEFAULT '{}'::text[],
            created_at  timestamptz NOT NULL DEFAULT now(),
            updated_at  timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ix_capabilities_kind ON capabilities(kind)")

    # ---- capability_dependencies (capability -> capability; own table) ----
    op.execute(
        """
        CREATE TABLE capability_dependencies (
            capability_id  text NOT NULL REFERENCES capabilities(id) ON DELETE CASCADE,
            depends_on_id  text NOT NULL REFERENCES capabilities(id) ON DELETE RESTRICT,
            kind           capability_dep_kind NOT NULL,
            created_at     timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT pk_capability_dependencies PRIMARY KEY (capability_id, depends_on_id, kind),
            CONSTRAINT ck_capability_dependencies_no_self CHECK (capability_id <> depends_on_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_capability_dependencies_depends_on_id "
        "ON capability_dependencies(depends_on_id)"
    )

    # ---- providers --------------------------------------------------------
    op.execute(
        """
        CREATE TABLE providers (
            id                 text PRIMARY KEY,
            name               text NOT NULL,
            homepage           text,
            license            text,
            commercial         boolean NOT NULL DEFAULT false,
            authentication     provider_auth NOT NULL DEFAULT 'none',
            requires_login     boolean NOT NULL DEFAULT false,
            pricing            provider_pricing NOT NULL,
            quota_daily        integer,
            quota_monthly      integer,
            config_keys        text[] NOT NULL DEFAULT '{}'::text[],
            score_quality      smallint NOT NULL,
            score_cost         smallint NOT NULL,
            score_speed        smallint NOT NULL,
            score_reliability  smallint NOT NULL,
            enabled            boolean NOT NULL DEFAULT true,
            extra              jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at         timestamptz NOT NULL DEFAULT now(),
            updated_at         timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_providers_quota_daily_positive
                CHECK (quota_daily IS NULL OR quota_daily > 0),
            CONSTRAINT ck_providers_quota_monthly_positive
                CHECK (quota_monthly IS NULL OR quota_monthly > 0),
            CONSTRAINT ck_providers_scores_bounded CHECK (
                score_quality BETWEEN 0 AND 100 AND
                score_cost BETWEEN 0 AND 100 AND
                score_speed BETWEEN 0 AND 100 AND
                score_reliability BETWEEN 0 AND 100
            )
        )
        """
    )
    op.execute("CREATE INDEX ix_providers_pricing_enabled ON providers(pricing, enabled)")

    # ---- provider_adapters (the runtime-loadable unit; D-B tiers) ---------
    op.execute(
        """
        CREATE TABLE provider_adapters (
            id                         text PRIMARY KEY,
            provider_id                text NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
            capability_id              text NOT NULL REFERENCES capabilities(id) ON DELETE RESTRICT,
            status                     adapter_status NOT NULL DEFAULT 'planned',
            execution_mode             adapter_execution_mode,
            implemented                boolean NOT NULL DEFAULT false,
            enabled                    boolean NOT NULL DEFAULT true,
            import_path                text,
            cost_unit                  cost_unit,
            cost_amount                numeric(18,8),
            cost_currency              varchar(3),
            cost_source                cost_source,
            estimated_generation_cost  numeric(18,8),
            estimated_download_cost    numeric(18,8),
            estimated_gpu_minutes      numeric(12,4),
            supports                   jsonb NOT NULL DEFAULT '{}'::jsonb,
            runtime                    jsonb NOT NULL DEFAULT '{}'::jsonb,
            features                   jsonb NOT NULL DEFAULT '[]'::jsonb,
            outputs                    jsonb NOT NULL DEFAULT '{}'::jsonb,
            extra                      jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at                 timestamptz NOT NULL DEFAULT now(),
            updated_at                 timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_provider_adapters_provider_capability
                UNIQUE (provider_id, capability_id),
            CONSTRAINT ck_provider_adapters_status_implemented
                CHECK ((status = 'implemented') = implemented),
            CONSTRAINT ck_provider_adapters_cost_amount_nonnegative
                CHECK (cost_amount IS NULL OR cost_amount >= 0)
        )
        """
    )
    op.execute("CREATE INDEX ix_provider_adapters_capability_id ON provider_adapters(capability_id)")
    op.execute("CREATE INDEX ix_provider_adapters_provider_id ON provider_adapters(provider_id)")
    op.execute(
        "CREATE INDEX ix_provider_adapters_status_enabled ON provider_adapters(status, enabled)"
    )

    # ---- adapter_fallbacks (join table; D-A2) -----------------------------
    op.execute(
        """
        CREATE TABLE adapter_fallbacks (
            adapter_id           text NOT NULL REFERENCES provider_adapters(id) ON DELETE CASCADE,
            fallback_adapter_id  text NOT NULL REFERENCES provider_adapters(id) ON DELETE RESTRICT,
            reason               text,
            ordinal              integer NOT NULL DEFAULT 0,
            created_at           timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT pk_adapter_fallbacks PRIMARY KEY (adapter_id, fallback_adapter_id),
            CONSTRAINT ck_adapter_fallbacks_no_self CHECK (adapter_id <> fallback_adapter_id),
            CONSTRAINT uq_adapter_fallbacks_adapter_ordinal UNIQUE (adapter_id, ordinal)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_adapter_fallbacks_fallback_adapter_id "
        "ON adapter_fallbacks(fallback_adapter_id)"
    )

    # ---- routing_policies -------------------------------------------------
    op.execute(
        """
        CREATE TABLE routing_policies (
            scope       text PRIMARY KEY,
            strategy    routing_strategy NOT NULL,
            fallback    fallback_mode NOT NULL,
            selection   selection_mode NOT NULL,
            created_at  timestamptz NOT NULL DEFAULT now(),
            updated_at  timestamptz NOT NULL DEFAULT now()
        )
        """
    )

    # ---- device_profiles (curated reference data) -------------------------
    op.execute(
        """
        CREATE TABLE device_profiles (
            id              text PRIMARY KEY,
            ram_gb          integer,
            gpu             text,
            backend         gpu_backend NOT NULL,
            unified_memory  boolean NOT NULL DEFAULT false,
            preferred_mode  generation_mode NOT NULL DEFAULT 'balanced',
            extra           jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at      timestamptz NOT NULL DEFAULT now(),
            updated_at      timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_device_profiles_ram_positive CHECK (ram_gb IS NULL OR ram_gb > 0)
        )
        """
    )

    # ---- provider_registry_meta (singleton: digest + provenance) ----------
    op.execute(
        """
        CREATE TABLE provider_registry_meta (
            id                 boolean PRIMARY KEY DEFAULT true,
            manifest_digest    text NOT NULL,
            manifest_revision  integer NOT NULL DEFAULT 1,
            catalogue_version  text NOT NULL,
            generator_version  text NOT NULL,
            generated_at       timestamptz NOT NULL,
            seeded_at          timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_provider_registry_meta_singleton CHECK (id IS TRUE),
            CONSTRAINT ck_provider_registry_meta_revision_positive CHECK (manifest_revision > 0)
        )
        """
    )


# --------------------------------------------------------------------------- #
# downgrade
# --------------------------------------------------------------------------- #
def downgrade() -> None:
    for table in (
        "provider_registry_meta",
        "device_profiles",
        "routing_policies",
        "adapter_fallbacks",
        "provider_adapters",
        "providers",
        "capability_dependencies",
        "capabilities",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    _drop_enums()
