# Database Naming Conventions

> Authoritative for Phase 2 Step A schema design, Step B ORM models, and all future migrations.
> Governed by `rule.md`, `ARCHITECTURE.md`, and `CONTRIBUTING.md` §3 "Database".

---

## 1. Object Naming

| Object | Rule | Example |
|---|---|---|
| Table | `snake_case`, **plural** | `projects`, `library_assets` |
| Column | `snake_case`, **singular** | `owner_user_id`, `started_at` |
| Primary key | always `id` | `projects.id` |
| Foreign key column | `<referenced_table_singular>_id` | `projects.owner_user_id → users.id` |
| Self-referencing FK | `parent_<table_singular>_id` | `folders.parent_folder_id` |
| Junction table | `<a_plural>_<b_plural>` alphabetised | `project_tags`, `roles_users` |
| Domain enum (Postgres `ENUM`) | `<context>_<concept>` | `workflow_status`, `media_kind` |
| Sequence | auto (`<table>_<col>_seq`) — but we use UUIDs, so rare | n/a |
| Partition child | `<parent>_y<YYYY>m<MM>` (monthly) or `_y<YYYY>w<WW>` (weekly) | `usage_records_y2026m07` |
| View | `vw_<purpose>` | `vw_user_credit_balance` |
| Materialised view | `mv_<purpose>` | `mv_daily_usage_summary` |
| Function | `fn_<verb>_<noun>` | `fn_compute_credit_balance` |
| Trigger | `tg_<table>_<event>_<purpose>` | `tg_projects_biu_touch_updated_at` |

`biu` = before insert/update; `aiu` = after insert/update; `bid` = before delete; etc.

## 2. Constraint Naming

| Constraint | Rule | Example |
|---|---|---|
| Primary key | `pk_<table>` | `pk_projects` |
| Foreign key | `fk_<table>_<col>__<referenced_table>` | `fk_projects_owner_user_id__users` |
| Unique | `uq_<table>_<col1>_<col2>…` | `uq_project_versions_project_id_version_number` |
| Check | `ck_<table>_<rule>` | `ck_credit_ledger_amount_nonzero` |
| Exclusion | `ex_<table>_<rule>` | `ex_clips_track_time_no_overlap` |
| Default value (named, when explicit) | `df_<table>_<col>` | rare |

Constraint names live in the migration — never auto-generated. This keeps Alembic diffs deterministic across Postgres versions.

## 3. Index Naming

| Index kind | Rule | Example |
|---|---|---|
| B-tree, single column | `ix_<table>_<col>` | `ix_users_email_normalized` |
| Composite | `ix_<table>_<col1>_<col2>…` (order = index order) | `ix_usage_records_tenant_id_started_at` |
| Unique index (not a constraint) | `uix_<table>_<cols>` | `uix_sessions_token_hash` |
| Partial | `ix_<table>_<cols>__where_<predicate_short>` | `ix_event_outbox_unpublished__where_published_at_null` |
| GIN | `gin_<table>_<col>` | `gin_library_assets_tags` |
| GIN trigram (text search) | `gin_<table>_<col>_trgm` | `gin_projects_name_trgm` |
| BRIN | `brin_<table>_<col>` | `brin_event_log_received_at` |
| pgvector HNSW | `hnsw_<table>_<col>` | `hnsw_library_assets_embedding` |
| pgvector IVF | `ivfflat_<table>_<col>` | (alternative; HNSW preferred for our scale) |

All non-PK indexes are declared **explicitly** in migrations — no implicit indexes from FK definitions (Postgres does **not** auto-index FKs, and we want the catalog name to match the convention).

## 4. ENUM Naming and Policy

- Postgres ENUMs only for **stable, exhaustive, low-cardinality** value sets that we control (e.g. `workflow_status`).
- Vendor-driven or product-driven value sets that can grow (model `kind`, provider name, plan code) live in **lookup tables**, not ENUMs.
- ENUM type name: `<context>_<concept>` — example: `media_kind`, `workflow_status`, `ledger_entry_type`.
- Adding an enum value requires a migration with `ALTER TYPE … ADD VALUE`; removal requires recreate (acknowledged cost).

| Use ENUM | Use lookup table |
|---|---|
| `workflow_status`, `step_status`, `render_status`, `export_status`, `notification_kind`, `subscription_status`, `invoice_status`, `media_kind`, `ledger_entry_type`, `version_reason`, `auth_role`, `tier_code`, `pipeline_kind` | `ai_models` (CR-11), `plans`, `feature_flags`, `provider_plugin_registrations`, `tags`, `library_folders` |

## 5. Audit Columns

Every table (except immutable ledger / append-only tables) has:

| Column | Type | Default | Notes |
|---|---|---|---|
| `id` | `uuid` | `gen_random_uuid()` | primary key, UUIDv4 |
| `created_at` | `timestamptz` | `now()` | NOT NULL, immutable after insert |
| `updated_at` | `timestamptz` | `now()` | NOT NULL, maintained by `tg_<table>_biu_touch_updated_at` trigger |
| `deleted_at` | `timestamptz` | `NULL` | soft delete marker; partial indexes use `WHERE deleted_at IS NULL` |

Immutable / append-only tables omit `updated_at` and `deleted_at` entirely:
- `project_versions`
- `credit_ledger`
- `ai_model_pricing`
- `usage_records`
- `event_outbox` (gets `published_at` instead of `deleted_at`)
- `event_log`
- `audit_log` *(CR-DB-3)*
- `cost_reconciliations`
- `webhook_deliveries`
- `workflow_checkpoints`

## 6. Optimistic Locking

Mutable aggregate roots carry a monotonic integer `version` column (separate from CR-6 project versions, which are first-class history rows). Increment via trigger on every UPDATE. Application code uses it for compare-and-swap writes.

Tables with `version` column:
- `projects`
- `timelines`
- `storyboards`
- `scenes`
- `workflow_runs`
- `library_assets`
- `subscriptions`
- `feature_flags`

## 7. Tenancy Column

Every multi-tenant business table carries `tenant_id uuid NOT NULL` immediately after `id`. RLS policies (added in a later phase) will pin queries to the current tenant.

Tenant-scoped tables: `users`, `projects`, `folders`, `tags`, `media_assets`, `library_assets`, `library_folders`, `workflow_runs`, `notifications`, `subscriptions`, `invoices`, `credit_ledger`, `usage_records`, `analytics_events`, `settings`, `templates`.

Global tables (no tenant_id): `ai_models`, `ai_model_pricing`, `provider_plugin_registrations`, `plans`, `feature_flags` (with per-tenant override table), `event_log` (carries optional `tenant_id` for filtering).

## 8. Money & Decimal Conventions

- Money stored as **integer minor units**: column `amount_cents bigint`, currency in `currency varchar(3)` (ISO-4217).
- Sub-cent costs (per-token pricing): `numeric(18,8)` columns for pricing tables only; aggregated to cents at billing.
- Credits: `numeric(18,6)` — supports fractional credit consumption.
- Never use `float` / `double precision` for any value involved in billing.

## 9. JSON Conventions

- Always `jsonb`, never `json`.
- Schema for every `jsonb` column is owned by a Pydantic model in the matching `app/domain/<context>/value_objects.py`; this Pydantic schema is the source of truth.
- `jsonb` columns that must support search: GIN-indexed.
- Top-level keys inside JSONB are `snake_case` to match column conventions.

## 10. Foreign Key `ON DELETE` Policy

Default is **`ON DELETE RESTRICT`** — accidental drops should fail loudly. The exceptions, in order of frequency:

| Pattern | Policy | Rationale |
|---|---|---|
| Soft-deletable parent owns child rows that should not survive | `ON DELETE CASCADE` | e.g. `tracks → clips`, `timelines → tracks` (a render artefact has no meaning without its timeline) |
| Reference column is informational only | `ON DELETE SET NULL` | e.g. `library_assets.library_folder_id` |
| Reference is to an immutable record (must never break audit trail) | `ON DELETE RESTRICT` | e.g. `usage_records.model_id → ai_models.id` |
| Reference to a tenant / user that owns everything | `ON DELETE RESTRICT` plus tombstone in app code | hard-delete of a user is a multi-step process |

Every FK column declares its `ON DELETE` policy explicitly in the migration; the schema document records each decision.

## 11. NULL Policy

- `NOT NULL` is the default. Every nullable column is justified in the schema document.
- Foreign key columns referencing **required** parents are `NOT NULL`. References to optional parents are nullable and the FK is `ON DELETE SET NULL`.

## 12. Migration Naming

`alembic/versions/<revision>_<verb>_<short_description>.py`

- `<revision>`: four-digit sequence starting at `0001` (we override Alembic's hash with a sequence for chronological clarity).
- `<verb>`: `create`, `add`, `drop`, `rename`, `alter`, `seed`, `partition`, `index`.
- Examples: `0001_baseline.py`, `0002_seed_system_data.py`, `0003_add_library_asset_embedding.py`, `0004_partition_usage_records_y2026m07.py`.

Every migration includes a working `downgrade()`. If a downgrade is destructive (drops a column), the docstring documents the data loss explicitly.

## 13. Reserved Column Names

Forbidden as column names (reserved for future use or framework conflicts): `metadata` (use `extra` or domain-specific), `type` (use `kind`), `class` (use `category`), `user` (use `user_id`), `order` (use `position`), `value` (qualify it: `flag_value`), `data` (qualify it).
