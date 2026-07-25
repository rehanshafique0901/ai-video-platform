"""α8.5e.1 — provider operational-state tables (schema only).

See: docs/engineering/RESOLVER_RUNTIME_CONTRACT.md §7 (grounded operational tables)
     docs/engineering/PROVIDER_RUNTIME_DATA_MODEL.md §A.2 (operational ownership)
     docs/database/schema.md §39 (provider operational state)

**Slice α8.5e.1: structure only. Nothing reads or writes these tables yet.** They are
the runtime-owned counterpart to the α8.5d catalogue: the resolver *reads* them, the
Execution Runtime / Health Worker *write* them, and the catalogue never sees them
(W8.5d.10 — operational state is never catalogue metadata, and vice-versa).

Three signal tables stay deliberately independent (resolver contract §7):
`provider_health` (observational), `provider_quota_state` (operational), and
`adapter_runtime_metrics` (historical) — different cadence, retention, consumers; never
merged. `local_runtime_state` tracks device/model availability.
`generation_resolution_ledger` is a per-request provenance record (AR18 / W8.5e.5): it
stores the full ranked `candidate_list` — not just the winner — for complete replay, and
records catalogue provenance (`catalogue_version`, `manifest_digest`) as *values*, not
FKs, so a decision remains reproducible even after the catalogue changes.

Purely **additive**: new enums + new tables; FKs point *into* the α8.5d catalogue
(`providers`, `provider_adapters`, `device_profiles`), never the reverse. `generation_id`
carries no FK yet — the Generation Runtime that owns it lands in α8.5x. `downgrade`
drops every table + enum so the ci_gate upgrade→downgrade→upgrade roundtrip stays clean.

Revision ID: 0011_provider_operational_state
Revises: 0010_provider_catalogue
Create Date: 2026-07-25
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0011_provider_operational_state"
down_revision: str | None | Sequence[str] = "0010_provider_catalogue"
branch_labels: str | None | Sequence[str] = None
depends_on: str | None | Sequence[str] = None


# --------------------------------------------------------------------------- #
# New enums (routing_strategy is reused from 0010 — not redefined here)
# --------------------------------------------------------------------------- #
_ENUMS: dict[str, tuple[str, ...]] = {
    "quota_window": ("daily", "monthly"),
    "execution_result": ("success", "failure", "fallback", "none"),
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

    # ---- provider_health (observational; one row per provider) -------------
    op.execute(
        """
        CREATE TABLE provider_health (
            provider_id      text PRIMARY KEY REFERENCES providers(id) ON DELETE CASCADE,
            health_score     numeric(5,4) NOT NULL DEFAULT 1.0,
            last_success_at  timestamptz,
            last_failure_at  timestamptz,
            error_rate       numeric(5,4) NOT NULL DEFAULT 0,
            rate_limit_hits  integer NOT NULL DEFAULT 0,
            updated_at       timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_provider_health_score_bounded
                CHECK (health_score BETWEEN 0 AND 1),
            CONSTRAINT ck_provider_health_error_rate_bounded
                CHECK (error_rate BETWEEN 0 AND 1),
            CONSTRAINT ck_provider_health_rate_limit_hits_nonnegative
                CHECK (rate_limit_hits >= 0)
        )
        """
    )

    # ---- provider_quota_state (operational; per provider + window) ---------
    op.execute(
        """
        CREATE TABLE provider_quota_state (
            provider_id  text NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
            window       quota_window NOT NULL,
            used         integer NOT NULL DEFAULT 0,
            remaining    integer,
            resets_at    timestamptz,
            updated_at   timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT pk_provider_quota_state PRIMARY KEY (provider_id, window),
            CONSTRAINT ck_provider_quota_state_used_nonnegative CHECK (used >= 0),
            CONSTRAINT ck_provider_quota_state_remaining_nonnegative
                CHECK (remaining IS NULL OR remaining >= 0)
        )
        """
    )

    # ---- adapter_runtime_metrics (historical; per adapter) ----------------
    op.execute(
        """
        CREATE TABLE adapter_runtime_metrics (
            adapter_id           text PRIMARY KEY
                                     REFERENCES provider_adapters(id) ON DELETE CASCADE,
            avg_latency_ms       integer,
            p95_latency_ms       integer,
            success_rate         numeric(5,4),
            current_queue_depth  integer NOT NULL DEFAULT 0,
            sample_window        text,
            updated_at           timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_adapter_runtime_metrics_latency_nonnegative
                CHECK ((avg_latency_ms IS NULL OR avg_latency_ms >= 0)
                   AND (p95_latency_ms IS NULL OR p95_latency_ms >= 0)),
            CONSTRAINT ck_adapter_runtime_metrics_success_rate_bounded
                CHECK (success_rate IS NULL OR success_rate BETWEEN 0 AND 1),
            CONSTRAINT ck_adapter_runtime_metrics_queue_nonnegative
                CHECK (current_queue_depth >= 0)
        )
        """
    )

    # ---- local_runtime_state (operational; per device profile) ------------
    op.execute(
        """
        CREATE TABLE local_runtime_state (
            device_profile_id  text PRIMARY KEY
                                   REFERENCES device_profiles(id) ON DELETE CASCADE,
            gpu_available      boolean NOT NULL DEFAULT false,
            loaded_models      text[] NOT NULL DEFAULT '{}'::text[],
            free_vram_gb       numeric(6,2),
            updated_at         timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_local_runtime_state_vram_nonnegative
                CHECK (free_vram_gb IS NULL OR free_vram_gb >= 0)
        )
        """
    )

    # ---- generation_resolution_ledger (provenance; one row per request) ---
    # Catalogue provenance is stored as *values* (not FKs) so a resolution stays
    # reproducible after the catalogue changes; `chosen_adapter` is likewise a
    # historical snapshot value. `generation_id` gets its FK when the Generation
    # Runtime lands (α8.5x).
    op.execute(
        """
        CREATE TABLE generation_resolution_ledger (
            id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            generation_id     uuid NOT NULL,
            capability        text NOT NULL,
            catalogue_version text NOT NULL,
            manifest_digest   text NOT NULL,
            resolver_version  text NOT NULL,
            routing_strategy  routing_strategy NOT NULL,
            candidate_list    jsonb NOT NULL DEFAULT '[]'::jsonb,
            chosen_adapter    text,
            start_time        timestamptz,
            end_time          timestamptz,
            execution_result  execution_result,
            created_at        timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_generation_resolution_ledger_generation_id "
        "ON generation_resolution_ledger(generation_id)"
    )
    op.execute(
        "CREATE INDEX ix_generation_resolution_ledger_created_at "
        "ON generation_resolution_ledger(created_at)"
    )


# --------------------------------------------------------------------------- #
# downgrade
# --------------------------------------------------------------------------- #
def downgrade() -> None:
    for table in (
        "generation_resolution_ledger",
        "local_runtime_state",
        "adapter_runtime_metrics",
        "provider_quota_state",
        "provider_health",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    _drop_enums()
