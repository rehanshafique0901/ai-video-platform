# Database Schema (Phase 2 Step A, reconciled in Phase 2D)

> **Authoritative table-by-table schema.** Implementation (SQLAlchemy + Alembic) in Step B has been validated end-to-end (`SCHEMA_VALIDATION.md` §6). All column types use Postgres native types. Audit columns and conventions per `NAMING_CONVENTIONS.md`. Index details per `INDEX_STRATEGY.md`.
>
> **Phase 2D reconciliation (2026-06-29).** This document is the *design intent*; the live schema (and ORM under `app/infrastructure/db/models/`) is the *source of truth*. Per the audit-of-truth rule adopted at the close of Phase 2C — *"Implementation is the source of truth if it has passed migrations, validation, and CI, unless there is explicit evidence that the implementation violates an accepted ADR or functional requirement"* — sections below were updated to match the validated implementation. Each section that was reconciled carries an inline **"Reconciled in 2D"** note describing what changed and why. Open architectural questions that *do* require a Phase 3-entry decision (and were therefore **not** changed in 2D) are catalogued in §37.

---

## 0. Required Extensions

```
pgcrypto    -- gen_random_uuid()
citext      -- case-insensitive email
pg_trgm     -- trigram indexes for search
vector      -- pgvector for embeddings
btree_gin   -- composite GIN indexes
```

## 0.1 Enum Types

Defined once, used widely:

| Type | Values |
|---|---|
| `auth_role` | `user, pro, business, enterprise, admin` |
| `version_reason` | `manual_save, autosave, restore, branch, generated` |
| `media_kind` | `image, video, narration, subtitle, music, sound_effect, thumbnail` |
| `media_source` | `generated, uploaded, stock` |
| `storage_backend` | `local, s3, r2, azure_blob, gcs` |
| `track_kind` | `video, audio, subtitle, effect` |
| `prompt_kind` | `image, video, animation, negative, camera, motion, lighting, style` |
| `workflow_status` | `queued, running, paused, succeeded, failed, canceled` |
| `step_status` | `pending, running, succeeded, failed, skipped, retrying` |
| `render_status` | `queued, running, succeeded, failed, canceled` |
| `export_status` | `queued, running, succeeded, failed, canceled` |
| `export_format` | `mp4, mov, gif, webm` |
| `export_quality` | `sd, hd_1080p, qhd_2k, uhd_4k` |
| `export_orientation` | `horizontal, vertical, square` |
| `plugin_kind` | `llm, image, video, voice` |
| `model_status` | `available, preview, deprecated, retired` |
| `pricing_unit` | `prompt_token, completion_token, image, megapixel, video_second, audio_second, embedding` |
| `usage_status` | `success, failed, partial, timeout` |
| `subscription_status` | `active, past_due, canceled, trialing, expired` |
| `invoice_status` | `draft, open, paid, void, uncollectible` |
| `billing_cycle` | `monthly, yearly, custom` |
| `ledger_entry_type` | `purchase, grant, consumption, refund, expiry, adjustment` |
| `flag_type` | `boolean, percent_rollout, multivariate` |
| `flag_scope` | `tenant, user, role` |
| `idempotency_status` | `in_flight, succeeded, failed` |
| `audit_actor_kind` | `user, system, admin, api_key, webhook` |

> The generic `setting_scope` ENUM has been **removed**. Configuration is now split into dedicated tables (`system_settings`, `tenant_settings`, `provider_settings`) per CR-DB-4. The generic `settings` table from the earlier revision is replaced; see §27.

---

## 1. Tenants

| Column | Type | Null | Default | Notes |
|---|---|---|---|---|
| `id` | uuid | NO | `gen_random_uuid()` | PK |
| `name` | text | NO | — | |
| `slug` | text | NO | — | unique |
| `plan_tier` | text | NO | `'free'` | informational; truth in `subscriptions` |
| `created_at` | timestamptz | NO | `now()` | |
| `updated_at` | timestamptz | NO | `now()` | trigger-maintained |
| `deleted_at` | timestamptz | YES | NULL | soft delete |

**Constraints**
- `pk_tenants (id)`
- `uq_tenants_slug (slug) WHERE deleted_at IS NULL`

---

## 2. Users

| Column | Type | Null | Default | Notes |
|---|---|---|---|---|
| `id` | uuid | NO | `gen_random_uuid()` | PK |
| `tenant_id` | uuid | NO | — | FK → `tenants.id` ON DELETE RESTRICT |
| `email` | citext | NO | — | |
| `password_hash` | text | YES | NULL | Argon2id; null for OAuth-only |
| `display_name` | text | NO | — | |
| `email_verified_at` | timestamptz | YES | NULL | |
| `last_login_at` | timestamptz | YES | NULL | |
| `extra` | jsonb | NO | `'{}'::jsonb` | small preferences (theme, locale, notification toggles) — see ADR-0025 |
| `version` | int | NO | 1 | optimistic lock |
| `created_at` / `updated_at` / `deleted_at` | timestamptz | std | std | |

**Constraints**
- `pk_users (id)`
- `uq_users_tenant_id_email (tenant_id, email) WHERE deleted_at IS NULL`
- `ck_users_password_or_oauth CHECK (password_hash IS NOT NULL OR EXISTS via oauth_identities)` *(application-enforced)*

---

## 3. OAuth Identities

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | uuid | NO | PK |
| `user_id` | uuid | NO | FK → `users.id` ON DELETE CASCADE |
| `provider` | text | NO | `google` for v1 |
| `subject` | text | NO | vendor user id |
| `linked_at` | timestamptz | NO | default `now()` |
| `created_at` / `updated_at` | std | | |

**Constraints**
- `uq_oauth_identities_provider_subject (provider, subject)`
- `uq_oauth_identities_user_id_provider (user_id, provider)` (one identity per provider per user)

---

## 4. Sessions

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | uuid | NO | PK |
| `user_id` | uuid | NO | FK → `users.id` ON DELETE CASCADE |
| `family_id` | uuid | NO | rotation chain |
| `token_hash` | text | NO | SHA-256 of refresh token |
| `ip` | inet | YES | |
| `user_agent` | text | YES | |
| `issued_at` | timestamptz | NO | |
| `last_used_at` | timestamptz | NO | |
| `revoked_at` | timestamptz | YES | nullable; rotation reuse sets this on the whole family |
| `expires_at` | timestamptz | NO | |

**Constraints**
- `uq_sessions_token_hash (token_hash)`

---

## 5. Roles & Roles_Users

```
roles (
  id uuid PK,
  code text NOT NULL UNIQUE,        -- one of: 'user','pro','business','enterprise','admin'
  description text
)

roles_users (
  role_id uuid FK → roles.id ON DELETE CASCADE,
  user_id uuid FK → users.id ON DELETE CASCADE,
  granted_at timestamptz NOT NULL DEFAULT now(),
  granted_by_user_id uuid FK → users.id ON DELETE SET NULL,
  PRIMARY KEY (role_id, user_id)
)
```

Roles are a lookup table (not an ENUM) so that we can attach permissions to roles in a future phase.

---

## 6. Folders

```
folders (
  id uuid PK,
  tenant_id uuid NOT NULL FK → tenants.id ON DELETE RESTRICT,
  owner_user_id uuid NOT NULL FK → users.id ON DELETE RESTRICT,
  parent_folder_id uuid FK → folders.id ON DELETE CASCADE,
  name text NOT NULL,
  created_at/updated_at/deleted_at
)
```
- `uq_folders_parent_name (parent_folder_id, name) WHERE deleted_at IS NULL`
- `ck_folders_no_self_parent CHECK (id <> parent_folder_id)`

---

## 7. Tags & Project_Tags

```
tags (
  id uuid PK,
  tenant_id uuid NOT NULL FK → tenants.id ON DELETE RESTRICT,
  name text NOT NULL,
  created_at/updated_at,
  UNIQUE (tenant_id, name)
)

project_tags (
  project_id uuid FK → projects.id ON DELETE CASCADE,
  tag_id uuid FK → tags.id ON DELETE CASCADE,
  tagged_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (project_id, tag_id)
)
```

---

## 8. Projects

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | uuid | NO | PK |
| `tenant_id` | uuid | NO | FK → `tenants.id` ON DELETE RESTRICT |
| `owner_user_id` | uuid | NO | FK → `users.id` ON DELETE RESTRICT |
| `folder_id` | uuid | YES | FK → `folders.id` ON DELETE SET NULL |
| `current_version_id` | uuid | YES | FK → `project_versions.id` ON DELETE SET NULL (head pointer) |
| `name` | text | NO | |
| `description` | text | YES | |
| `aspect_ratio` | text | NO | `horizontal\|vertical\|square` (check constraint) |
| `duration_seconds` | numeric(10,3) | YES | target duration |
| `language` | varchar(8) | NO | BCP-47, default `'en'` |
| `style` | text | YES | |
| `settings` | jsonb | NO | default `'{}'::jsonb` — provider/model overrides |
| `version` | int | NO | 1 (optimistic lock) |
| `created_at` / `updated_at` / `deleted_at` | std | | |

**Constraints**
- `uq_projects_tenant_owner_name (tenant_id, owner_user_id, name) WHERE deleted_at IS NULL`
- `ck_projects_aspect_ratio CHECK (aspect_ratio IN ('horizontal','vertical','square'))`

---

## 9. Project Versions (CR-6) — Immutable

```
project_versions (
  id uuid PK,
  project_id uuid NOT NULL FK → projects.id ON DELETE CASCADE,
  version_number int NOT NULL,
  parent_version_id uuid FK → project_versions.id ON DELETE RESTRICT,
  created_by_user_id uuid NOT NULL FK → users.id ON DELETE RESTRICT,
  reason version_reason NOT NULL,
  snapshot jsonb NOT NULL,
  diff_summary jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
  -- NO updated_at, NO deleted_at
)
```
- `uq_project_versions_project_version (project_id, version_number)`
- `ck_project_versions_no_self_parent CHECK (id <> parent_version_id)`
- INSERT-ONLY: trigger `tg_project_versions_no_update_delete` raises on UPDATE/DELETE.

---

## 10. Storyboards & Scenes

```
storyboards (
  id uuid PK,
  project_id uuid NOT NULL FK → projects.id ON DELETE CASCADE,
  project_version_id uuid FK → project_versions.id ON DELETE SET NULL,
  generated_by text NOT NULL CHECK (generated_by IN ('system','user')),
  generated_at timestamptz NOT NULL DEFAULT now(),
  version int NOT NULL DEFAULT 1,
  created_at/updated_at/deleted_at
)

scenes (
  id uuid PK,
  storyboard_id uuid NOT NULL FK → storyboards.id ON DELETE CASCADE,
  scene_number int NOT NULL,
  title text NOT NULL,
  duration_seconds numeric(8,3) NOT NULL CHECK (duration_seconds > 0),
  narration text,
  subtitle text,
  emotion text,
  camera_angle text,
  camera_motion text,
  lens text,
  lighting text,
  weather text,
  location text,
  animation text,
  transition_in text,
  music_mood text,
  extra jsonb NOT NULL DEFAULT '{}'::jsonb,
  version int NOT NULL DEFAULT 1,
  created_at/updated_at/deleted_at,
  UNIQUE (storyboard_id, scene_number) WHERE deleted_at IS NULL
)
```

---

## 11. Prompts

```
prompts (
  id uuid PK,
  project_id uuid NOT NULL FK → projects.id ON DELETE CASCADE,
  scene_id uuid FK → scenes.id ON DELETE SET NULL,
  kind prompt_kind NOT NULL,
  text_content text NOT NULL,
  model_id uuid FK → ai_models.id ON DELETE SET NULL,
  generated_by_agent text,
  extra jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at/updated_at/deleted_at
)
```

---

## 12. Media Assets

```
media_assets (
  id uuid PK,
  tenant_id uuid NOT NULL FK → tenants.id ON DELETE RESTRICT,
  owner_user_id uuid NOT NULL FK → users.id ON DELETE RESTRICT,
  kind media_kind NOT NULL,
  project_id uuid FK → projects.id ON DELETE SET NULL,
  scene_id uuid FK → scenes.id ON DELETE SET NULL,
  prompt_id uuid FK → prompts.id ON DELETE SET NULL,
  model_id uuid FK → ai_models.id ON DELETE RESTRICT,         -- audit integrity
  provider text,
  storage_backend storage_backend NOT NULL,
  storage_bucket text NOT NULL,
  storage_key text NOT NULL,
  mime_type text NOT NULL,
  size_bytes bigint NOT NULL CHECK (size_bytes >= 0),
  width int,                                                    -- nullable for audio
  height int,
  duration_seconds numeric(10,3),                               -- nullable for image
  checksum_sha256 bytea NOT NULL,
  source media_source NOT NULL,
  source_metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at/updated_at/deleted_at,
  UNIQUE (storage_backend, storage_bucket, storage_key)
)
```
- `ck_media_assets_dim_for_image_video CHECK ( kind IN ('image','video','thumbnail') = (width IS NOT NULL AND height IS NOT NULL) OR kind NOT IN ('image','video','thumbnail') )`
- `ck_media_assets_duration_for_av CHECK ( kind IN ('video','narration','music','sound_effect','subtitle') IMPLIES duration_seconds IS NOT NULL )` *(written as boolean expression)*

---

## 13. Library Assets, Folders, Junctions (CR-8)

```
library_folders (
  id uuid PK,
  tenant_id uuid NOT NULL FK → tenants.id ON DELETE RESTRICT,
  owner_user_id uuid NOT NULL FK → users.id ON DELETE RESTRICT,
  parent_folder_id uuid FK → library_folders.id ON DELETE CASCADE,
  name text NOT NULL,
  created_at/updated_at/deleted_at,
  UNIQUE (parent_folder_id, name) WHERE deleted_at IS NULL,
  CHECK (id <> parent_folder_id)
)

library_assets (
  id uuid PK,
  tenant_id uuid NOT NULL FK → tenants.id ON DELETE RESTRICT,
  owner_user_id uuid NOT NULL FK → users.id ON DELETE RESTRICT,
  media_asset_id uuid NOT NULL FK → media_assets.id ON DELETE RESTRICT,
  library_folder_id uuid FK → library_folders.id ON DELETE SET NULL,
  name text NOT NULL,
  description text,
  tags text[] NOT NULL DEFAULT '{}',
  embedding vector(1536),
  usage_count int NOT NULL DEFAULT 0,
  last_used_at timestamptz,
  version int NOT NULL DEFAULT 1,
  created_at/updated_at/deleted_at,
  UNIQUE (media_asset_id)
)

library_asset_projects (
  library_asset_id uuid FK → library_assets.id ON DELETE CASCADE,
  project_id uuid FK → projects.id ON DELETE CASCADE,
  first_used_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (library_asset_id, project_id)
)
```

---

## 14. Timeline, Tracks, Clips, Transitions

```
timelines (
  id uuid PK,
  project_id uuid NOT NULL FK → projects.id ON DELETE CASCADE,
  project_version_id uuid FK → project_versions.id ON DELETE SET NULL,
  duration_seconds numeric(10,3) NOT NULL DEFAULT 0,
  aspect_ratio text NOT NULL,
  frame_rate int NOT NULL DEFAULT 30 CHECK (frame_rate BETWEEN 1 AND 240),
  background_color text NOT NULL DEFAULT '#000000',
  version int NOT NULL DEFAULT 1,
  created_at/updated_at/deleted_at,
  UNIQUE (project_id) WHERE deleted_at IS NULL    -- one active timeline per project
)

tracks (
  id uuid PK,
  timeline_id uuid NOT NULL FK → timelines.id ON DELETE CASCADE,
  kind track_kind NOT NULL,
  z_index int NOT NULL,
  locked boolean NOT NULL DEFAULT false,
  muted boolean NOT NULL DEFAULT false,
  name text NOT NULL,
  created_at/updated_at/deleted_at,
  UNIQUE (timeline_id, z_index) WHERE deleted_at IS NULL
)

transitions (
  id uuid PK,
  name text NOT NULL,
  kind text NOT NULL,        -- 'fade','wipe','dissolve','cut','custom' (lookup-style; no ENUM)
  duration_seconds numeric(6,3) NOT NULL DEFAULT 0.5,
  params jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at/updated_at
)

clips (
  id uuid PK,
  track_id uuid NOT NULL FK → tracks.id ON DELETE CASCADE,
  media_asset_id uuid FK → media_assets.id ON DELETE SET NULL,
  start_seconds numeric(10,3) NOT NULL CHECK (start_seconds >= 0),
  end_seconds numeric(10,3) NOT NULL CHECK (end_seconds > start_seconds),
  source_start_seconds numeric(10,3) NOT NULL DEFAULT 0,
  source_end_seconds numeric(10,3) NOT NULL DEFAULT 0,
  transition_in_id uuid FK → transitions.id ON DELETE SET NULL,
  transition_out_id uuid FK → transitions.id ON DELETE SET NULL,
  effects jsonb NOT NULL DEFAULT '[]'::jsonb,
  volume numeric(4,2) NOT NULL DEFAULT 1.00 CHECK (volume BETWEEN 0 AND 4),
  locked boolean NOT NULL DEFAULT false,
  created_at/updated_at/deleted_at
)
```
- Optional advanced: an EXCLUSION constraint `ex_clips_track_time_no_overlap USING gist (track_id WITH =, numrange(start_seconds, end_seconds) WITH &&) WHERE (deleted_at IS NULL)` to prevent overlapping clips on the same track. **Deferred** to Step B if performance permits.

---

## 15. AI Models, Pricing, Plugin Registry (CR-11)

```
ai_models (
  id uuid PK,
  model_key text NOT NULL UNIQUE,
  provider text NOT NULL,
  vendor_model_id text NOT NULL,
  kind plugin_kind NOT NULL,
  capabilities text[] NOT NULL DEFAULT '{}',
  modalities text[] NOT NULL DEFAULT '{}',
  context_window int,
  max_output_tokens int,
  max_output_pixels bigint,
  max_output_seconds int,
  status model_status NOT NULL DEFAULT 'available',
  released_at date,
  deprecated_at date,
  retires_at date,
  successor_model_id uuid FK → ai_models.id ON DELETE SET NULL,
  tags text[] NOT NULL DEFAULT '{}',
  extra jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at/updated_at,
  CHECK (deprecated_at IS NULL OR released_at IS NULL OR deprecated_at >= released_at),
  CHECK (retires_at IS NULL OR deprecated_at IS NULL OR retires_at >= deprecated_at),
  CHECK (id <> successor_model_id)
)

ai_model_pricing (
  id uuid PK,
  model_id uuid NOT NULL FK → ai_models.id ON DELETE RESTRICT,
  effective_from timestamptz NOT NULL,
  effective_to   timestamptz,
  unit pricing_unit NOT NULL,
  price_per_unit numeric(18,8) NOT NULL CHECK (price_per_unit >= 0),
  currency varchar(3) NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),  -- immutable; no updates
  CHECK (effective_to IS NULL OR effective_to > effective_from)
)
-- One open-ended row per (model_id, unit):
-- UNIQUE (model_id, unit) WHERE effective_to IS NULL

provider_plugin_registrations (
  id uuid PK,
  name text NOT NULL,
  version text NOT NULL,
  kind plugin_kind NOT NULL,
  capabilities text[] NOT NULL DEFAULT '{}',
  enabled boolean NOT NULL DEFAULT true,
  last_health_status text,
  last_health_at timestamptz,
  extra jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at/updated_at,
  UNIQUE (name, version)
)
```

---

## 16. Workflows, Steps, Checkpoints (CR-7)

```
workflow_runs (
  id uuid PK,
  project_id uuid NOT NULL FK → projects.id ON DELETE CASCADE,
  workflow_key text NOT NULL,                          -- e.g. 'storyboard_to_render'
  workflow_version text NOT NULL,                      -- adapter semver, e.g. '1.0.0'
  status workflow_status NOT NULL,                     -- queued|running|paused|succeeded|failed|canceled
  started_at timestamptz,
  finished_at timestamptz,
  triggered_by_user_id uuid FK → users.id ON DELETE SET NULL,
  idempotency_key text,
  input_snapshot jsonb NOT NULL,                       -- canonicalised inputs
  output_summary jsonb,                                -- nullable summary of outputs
  error jsonb,                                         -- nullable; { code, message, trace_id }
  created_at/updated_at,
  UNIQUE (project_id, idempotency_key)
)

workflow_steps (
  id uuid PK,
  workflow_run_id uuid NOT NULL FK → workflow_runs.id ON DELETE CASCADE,
  step_index int NOT NULL,
  step_name text NOT NULL,
  status step_status NOT NULL,                         -- pending|running|succeeded|failed|skipped|retrying
  started_at timestamptz,
  finished_at timestamptz,
  retries int NOT NULL DEFAULT 0,
  input jsonb,
  output jsonb,
  error jsonb,
  created_at/updated_at,
  UNIQUE (workflow_run_id, step_index)
)

workflow_checkpoints (
  id uuid PK,
  workflow_run_id uuid NOT NULL FK → workflow_runs.id ON DELETE CASCADE,
  step_index int NOT NULL,
  state jsonb NOT NULL,                                -- opaque resume state (LangGraph or custom engine)
  created_at timestamptz NOT NULL DEFAULT now()        -- immutable; reject_mutation trigger applied
  -- UNIQUE (workflow_run_id, step_index) is intentionally OMITTED:
  -- multiple checkpoints per step are allowed for sub-state advances.
)
```

> **Reconciled in 2D (2026-06-29).** The pre-Step-B draft of this section
> mirrored an earlier engine design (`tenant_id`/`user_id`/`pipeline_id`/
> `correlation_id`/`queue_name`/`paused_at`/`error_code`+`error_message`/
> `version` columns; `name`/`agent`/`queue_hint`/`attempts`+`max_attempts`
> on steps; `thread_id`+`checkpoint_data` on checkpoints). The
> implementation chose a leaner, idempotency-first shape: tenancy is
> reachable via `project_id → projects.tenant_id`; correlation is
> handled by the application-level event-bus context (`event_outbox`
> covers it); retries are a single counter; errors are structured JSON;
> queueing belongs to the worker layer (see `render_jobs.queue`/`priority`),
> not the workflow record. Whether the omitted fields (`correlation_id`,
> `paused_at`, `version`, etc.) should be reintroduced is recorded as a
> Phase-3 entry decision in §37. No migration was performed.

---

## 17. Render & Export Jobs

```
render_jobs (
  id uuid PK,
  project_id uuid NOT NULL FK → projects.id ON DELETE CASCADE,
  timeline_id uuid NOT NULL FK → timelines.id ON DELETE RESTRICT,
  workflow_run_id uuid FK → workflow_runs.id ON DELETE SET NULL,
  pipeline text NOT NULL,                              -- pipeline id (e.g. 'ffmpeg','moviepy','opencv')
  pipeline_version text NOT NULL,                      -- adapter semver
  queue text NOT NULL CHECK (queue IN ('critical','high','normal','low','background')),
  priority int NOT NULL DEFAULT 0,                     -- secondary ordering inside a queue
  status render_status NOT NULL,
  started_at timestamptz,
  finished_at timestamptz,
  progress text NOT NULL DEFAULT '0.00',               -- decimal stored as text for portability; 0.00..100.00
  error jsonb,                                         -- { code, message, trace_id, retries }
  output_media_asset_id uuid FK → media_assets.id ON DELETE SET NULL,
  idempotency_key text,
  version int NOT NULL DEFAULT 1,
  created_at/updated_at,
  UNIQUE (project_id, idempotency_key)
)

export_jobs (
  id uuid PK,
  render_job_id uuid NOT NULL FK → render_jobs.id ON DELETE CASCADE,
  requested_by_user_id uuid NOT NULL FK → users.id ON DELETE RESTRICT,
  format export_format NOT NULL,
  quality export_quality NOT NULL,
  orientation export_orientation NOT NULL,
  status export_status NOT NULL,
  output_media_asset_id uuid FK → media_assets.id ON DELETE SET NULL,
  download_count int NOT NULL DEFAULT 0,
  last_downloaded_at timestamptz,
  file_size_bytes bigint,
  finished_at timestamptz,
  version int NOT NULL DEFAULT 1,
  created_at/updated_at
)
```

> **Reconciled in 2D (2026-06-29).** Differences from the pre-Step-B
> draft, with rationale:
> - `render_jobs.project_version_id` (NOT NULL FK) **not implemented.**
>   Render jobs reach the version via `timeline_id → timelines.project_version_id`;
>   adding a redundant FK is left for Phase 3 if profiling shows we want
>   it on the hot path.
> - `pipeline_id` text → split into `pipeline` + `pipeline_version` so
>   the queue layer can dispatch on adapter version without parsing.
> - `progress_percent int` → `progress text` (decimal-as-text). Decision
>   recorded in §37 — typing is a Phase-3 follow-up if profiling shows
>   it matters.
> - `error_code` + `error_message` → single `error jsonb`. Structured
>   error envelope aligns with `workflow_runs.error` and lets workers
>   record trace ids without schema migrations.
> - `queue_name` → `queue` + `priority`. CR-13 (five-tier priority
>   queues) requires explicit priority — the original draft missed it.
> - `deleted_at` **not implemented** on render/export jobs (they are
>   operationally terminal records, not soft-deletable user objects;
>   purge is by retention policy, not user action).
> - `export_jobs.project_id` **not implemented.** Reachable via
>   `render_job_id → render_jobs.project_id`; same reasoning as version
>   above.
> - `export_jobs` adds `requested_by_user_id`, `download_count`,
>   `last_downloaded_at`, `file_size_bytes` because the original draft
>   did not anticipate the user-facing "my downloads" feed and the
>   storage-quota reconciliation that came out of CR-12.
> - `export_jobs` `error_code`/`error_message`/`started_at` are not
>   implemented; structured error envelopes live in a JSONB column on
>   `render_jobs.error` and are not duplicated per export.
> - The partial-unique `(render_job_id, format, quality, orientation)`
>   constraint was promoted from the use-case layer to the database in
>   **Phase 3 W1.1** as `uq_export_jobs_render_job_id_format_quality_orientation`
>   with `WHERE status IN ('queued','running','succeeded')`
>   (ADR-0030; migration `0003_export_jobs_partial_unique`; see
>   `INDEX_STRATEGY.md` §8). `failed`/`canceled` rows are deliberately
>   excluded so retries after failure are permitted; `succeeded` is
>   included because `export_jobs` is the canonical artefact row for
>   that export configuration (`download_count`, `last_downloaded_at`,
>   `output_media_asset_id` live on it). §37 Q8 is therefore Resolved.

---

## 18. Usage Records (CR-12) — Partitioned

```
CREATE TABLE usage_records (
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL FK → tenants.id ON DELETE RESTRICT,
  user_id uuid FK → users.id ON DELETE SET NULL,            -- nullable: system-initiated calls
  project_id uuid FK → projects.id ON DELETE SET NULL,
  scene_id uuid FK → scenes.id ON DELETE SET NULL,
  prompt_id uuid FK → prompts.id ON DELETE SET NULL,
  workflow_run_id uuid FK → workflow_runs.id ON DELETE SET NULL,
  workflow_step_id uuid FK → workflow_steps.id ON DELETE SET NULL,
  model_id uuid NOT NULL FK → ai_models.id ON DELETE RESTRICT,
  pricing_id uuid FK → ai_model_pricing.id ON DELETE SET NULL,
  request_id text,                                         -- vendor-supplied id (when present)
  unit pricing_unit NOT NULL,                              -- 'tokens'|'images'|'video_seconds'|...
  unit_count numeric(18,4) NOT NULL,
  tokens_prompt int,
  tokens_completion int,
  images_count int,
  seconds_generated numeric(10,3),
  credits_consumed numeric(18,4) NOT NULL DEFAULT 0 CHECK (credits_consumed >= 0),
  estimated_cost numeric(18,8) NOT NULL DEFAULT 0 CHECK (estimated_cost >= 0),
  actual_cost numeric(18,8) CHECK (actual_cost IS NULL OR actual_cost >= 0),
  currency varchar(3) NOT NULL,
  status usage_status NOT NULL,                            -- pending|ok|failed|reconciled
  latency_ms int,
  error_code text,
  extra jsonb NOT NULL DEFAULT '{}'::jsonb,
  occurred_at timestamptz NOT NULL DEFAULT now(),          -- partition key
  created_at timestamptz NOT NULL DEFAULT now(),

  PRIMARY KEY (id, occurred_at)
) PARTITION BY RANGE (occurred_at);
```

**Partitions:** one per month, named `usage_records_y<YYYY>m<MM>`. Two seed partitions (`y2025m12`, `y2026m01`) are created by the baseline migration; the rolling-window job in Phase 3 creates new partitions 90 days ahead. See `RETENTION_POLICY.md`.

**Indexes (per parent + propagated to children):** `ix_usage_records_tenant_id_occurred_at`, `ix_usage_records_model_id_occurred_at`, `ix_usage_records_workflow_run_id`, `ix_usage_records_request_id`.

**FKs are real** (not logical). The earlier "logical FK" note from the Step-A draft applied to a previous design where this table was append-only across tenants without DB-level integrity; ADR-0019 and CR-12 settled on enforced FKs with `RESTRICT` on the strong references (`tenant_id`, `model_id`) and `SET NULL` on the weak ones (`user_id`, `project_id`, `scene_id`, `prompt_id`, `workflow_run_id`, `workflow_step_id`, `pricing_id`).

> **Reconciled in 2D (2026-06-29).** The Step-A draft used `started_at`/
> `finished_at` + `duration_ms` + `prompt_tokens`/`completion_tokens` +
> `image_megapixels`/`video_seconds`/`audio_seconds`/`embedding_count` +
> integer cents (`estimated_cost_cents`/`actual_cost_cents`) and a
> `billable` flag. The implementation chose:
> - a single partition key (`occurred_at`) instead of separate
>   `started_at` (semantically the same — when the metered event happened);
> - a generic `(unit, unit_count)` pair plus typed counters
>   (`tokens_prompt`/`tokens_completion`/`images_count`/`seconds_generated`)
>   so a new pricing unit (e.g. `seconds_processed`) can be added without
>   a migration;
> - decimal costs (`numeric(18,8)`) — cents math is brittle when vendors
>   bill in micro-dollars and when reconciliation produces fractional
>   variance;
> - no `billable` column — billable-vs-non-billable is derived from
>   `status` + the pricing row;
> - `latency_ms` instead of `duration_ms` (consistent with platform-wide
>   metric naming);
> - explicit FKs on every relational column (the prior "logical FK only"
>   notion was abandoned at CR-12).
> The per-partition `(provider, request_id)` uniqueness from the Step-A
> draft is **not implemented** — application-level idempotency uses
> `idempotency_keys` (§31). Whether to add a partial-unique `request_id`
> index per partition is a Phase-3 decision (§37).

---

## 19. Cost Reconciliations

```
cost_reconciliations (
  id uuid PK,
  tenant_id uuid NOT NULL FK → tenants.id ON DELETE RESTRICT,
  model_id uuid NOT NULL FK → ai_models.id ON DELETE RESTRICT,
  period_start timestamptz NOT NULL,
  period_end timestamptz NOT NULL,
  invoiced_amount numeric(18,4) NOT NULL,
  estimated_amount numeric(18,4) NOT NULL,
  variance numeric(18,4) NOT NULL,
  currency varchar(3) NOT NULL,
  notes text,
  created_at timestamptz NOT NULL DEFAULT now(),   -- audit only; see immutability note below
  CONSTRAINT period_valid CHECK (period_end > period_start)
)
```

**Indexes:** `ix_cost_reconciliations_tenant_id_period_start`.

> **Reconciled in 2D (2026-06-29).** The Step-A draft modeled cost
> reconciliation per-`usage_record` (one row per metered call) with a
> vendor invoice reference and a `(usage_record_id, vendor_invoice_ref)`
> unique key. The implementation instead aggregates reconciliation per
> `(tenant_id, model_id, period_start..period_end)` window — this is
> closer to how vendors actually invoice (a monthly statement, not a
> per-call breakdown) and avoids the partition-key carry that the
> Step-A draft required.
>
> **Immutability is a Phase-3 decision, not a confirmed bug.** The
> Step-A draft annotated `created_at -- immutable` on this table. The
> shipped baseline migration does **not** install a `reject_mutation`
> trigger on `cost_reconciliations`; only `payment_intents`,
> `credit_ledger`, `audit_log`, `event_log`, and `workflow_checkpoints`
> are trigger-protected. The audit determined that whether reconciliation
> rows should be append-only (forbidding corrections after the period
> closes) or correctable (allowing a finance operator to fix a wrongly
> entered invoice) is a product/finance decision, not a documentation
> question. Recorded as an open question in §37. The current behaviour
> is "mutable, audit-logged" — finance corrections rewrite the row and
> `audit_log` captures the diff.

---

## 20. Plans, Subscriptions, Invoices

```
plans (
  id uuid PK,
  code text NOT NULL UNIQUE,                       -- 'free','pro_monthly','business_yearly',…
  name text NOT NULL,
  description text,
  cycle billing_cycle NOT NULL,                    -- enum, see §14
  monthly_credits numeric(18,4) NOT NULL DEFAULT 0 CHECK (monthly_credits >= 0),
  monthly_price numeric(18,4) NOT NULL DEFAULT 0 CHECK (monthly_price >= 0),
  currency varchar(3) NOT NULL DEFAULT 'USD',
  features jsonb NOT NULL DEFAULT '{}'::jsonb,
  active boolean NOT NULL DEFAULT true,
  created_at/updated_at
)

subscriptions (
  id uuid PK,
  tenant_id uuid NOT NULL FK → tenants.id ON DELETE RESTRICT,
  plan_id uuid NOT NULL FK → plans.id ON DELETE RESTRICT,
  -- Subscriptions are tenant-scoped (one active subscription per tenant). The
  -- user who initiated the subscription is recoverable via `audit_log` if
  -- needed; not denormalised onto this table. See ADR-0027.
  status subscription_status NOT NULL,
  started_at timestamptz NOT NULL,
  renews_at timestamptz,
  canceled_at timestamptz,
  trial_ends_at timestamptz,
  payment_provider text NOT NULL,        -- 'stripe' | 'paddle' | …
  external_customer_id text,             -- e.g. Stripe customer id
  external_subscription_id text,         -- e.g. Stripe subscription id
  version int NOT NULL DEFAULT 1,
  created_at/updated_at,
  -- Partial UNIQUE index: only one active/trialing/past_due row per tenant.
  UNIQUE (tenant_id) WHERE status IN ('active','trialing','past_due')
)

invoices (
  id uuid PK,
  subscription_id uuid NOT NULL FK → subscriptions.id ON DELETE RESTRICT,
  -- tenant is reachable via subscription_id → subscriptions.tenant_id; not
  -- duplicated onto this table. See ADR-0027.
  number text NOT NULL UNIQUE,
  status invoice_status NOT NULL,
  amount_due numeric(18,4) NOT NULL,
  amount_paid numeric(18,4) NOT NULL DEFAULT 0,
  currency varchar(3) NOT NULL,
  period_start timestamptz NOT NULL,
  period_end timestamptz NOT NULL,
  issued_at timestamptz NOT NULL,
  paid_at timestamptz,
  external_invoice_id text,              -- e.g. Stripe invoice id
  created_at/updated_at,
  CHECK (period_end > period_start)
)
```

---

## 21. Credit Ledger (Immutable, Append-Only)

```
credit_ledger (
  id uuid PK,
  tenant_id uuid NOT NULL FK → tenants.id ON DELETE RESTRICT,
  user_id uuid FK → users.id ON DELETE SET NULL,    -- nullable: system / admin adjustments
  entry_type ledger_entry_type NOT NULL,
  amount numeric(18,4) NOT NULL,
  balance_after numeric(18,4) NOT NULL CHECK (balance_after >= 0),  -- enforced by trigger
  related_usage_record_id uuid,                     -- logical FK to usage_records (partitioned)
  related_invoice_id uuid FK → invoices.id ON DELETE SET NULL,
  description text,
  idempotency_key text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, idempotency_key)
  -- NO updated_at, NO deleted_at; trigger forbids UPDATE/DELETE
)
```

- **Source of truth:** `balance = sum(amount)` per (tenant_id, user_id). `balance_after` is denormalised for read performance and verified by an integrity job.
- Compensating entries (with negative `amount` for reversal) replace UPDATE/DELETE semantics.

---

## 22. Feature Flags (CR-9)

```
feature_flags (
  id uuid PK,
  key text NOT NULL UNIQUE,
  description text,
  flag_type flag_type NOT NULL,                                     -- 'boolean'|'percent'|'variant'
  default_value jsonb NOT NULL DEFAULT 'false'::jsonb,
  rollout_percent int CHECK (rollout_percent IS NULL OR rollout_percent BETWEEN 0 AND 100),
  variants jsonb,                                                   -- for variant flags
  archived boolean NOT NULL DEFAULT false,
  version int NOT NULL DEFAULT 1,
  created_at/updated_at
)

feature_flag_overrides (
  id uuid PK,
  feature_flag_id uuid NOT NULL FK → feature_flags.id ON DELETE CASCADE,
  scope flag_scope NOT NULL,                                        -- 'tenant'|'user'|'project'
  scope_id uuid NOT NULL,
  value jsonb NOT NULL,
  expires_at timestamptz,
  created_at/updated_at,
  UNIQUE (feature_flag_id, scope, scope_id)
)
```

> **Reconciled in 2D (2026-06-29).** The Step-A draft used `type` (a
> reserved-feeling word) and a single `rules jsonb` array for advanced
> targeting. The implementation chose `flag_type` (avoids shadowing the
> Python builtin in ORM code) plus a flatter `rollout_percent` + `variants`
> shape that covers the three flag kinds we ship in Phase 3 (boolean,
> percent, variant) without an over-engineered rules engine. An
> `archived` boolean replaces the prior `enabled` (a flag can be `enabled`
> conceptually while `archived` is the operational column for cleanup
> jobs). Overrides renamed `flag_id`→`feature_flag_id` (clearer when the
> FK name is read in isolation) and `override_value`→`value` (consistent
> with `feature_flags.default_value`); `expires_at` is new and supports
> time-bound per-tenant rollouts.

---

## 23. Notifications

```
notifications (
  id uuid PK,
  tenant_id uuid NOT NULL FK → tenants.id ON DELETE RESTRICT,
  user_id uuid NOT NULL FK → users.id ON DELETE CASCADE,
  kind text NOT NULL,            -- 'render_complete', 'export_ready', 'billing_failed', …
  title text NOT NULL,
  body text NOT NULL,
  payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  read_at timestamptz,
  created_at/updated_at
)
```

---

## 24. Analytics Events (Partitioned)

```
CREATE TABLE analytics_events (
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  tenant_id uuid,
  user_id uuid,
  session_id text,
  event_name text NOT NULL,
  properties jsonb NOT NULL DEFAULT '{}'::jsonb,
  occurred_at timestamptz NOT NULL,
  ingested_at timestamptz NOT NULL DEFAULT now(),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (id, occurred_at)
) PARTITION BY RANGE (occurred_at);
```
Partitions: monthly. No FK constraints (high-volume; integrity at app level).

---

## 25. Event Outbox (CR-4)

```
event_outbox (
  id uuid PK,
  aggregate_type text NOT NULL,
  aggregate_id uuid NOT NULL,
  event_type text NOT NULL,             -- 'project.created', etc. — from topics registry
  event_version text NOT NULL DEFAULT '1.0',
  payload jsonb NOT NULL,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,   -- carries correlation_id, causation_id, trace_id
  occurred_at timestamptz NOT NULL,
  published_at timestamptz,             -- partial index: WHERE published_at IS NULL
  attempts int NOT NULL DEFAULT 0,
  last_error text,
  created_at timestamptz NOT NULL DEFAULT now()
)
```

**Indexes:** `ix_event_outbox_unpublished_occurred_at` (partial: `WHERE published_at IS NULL`), `ix_event_outbox_aggregate_type_aggregate_id`.

The dispatcher (`workers/event_worker.py`) does `SELECT … FOR UPDATE SKIP LOCKED WHERE published_at IS NULL ORDER BY occurred_at LIMIT N`.

> **Reconciled in 2D (2026-06-29).** Step-A used `topic` (legacy
> terminology), top-level `correlation_id`/`causation_id`, and
> `schema_version int`. Implementation chose `event_type` (matches the
> CloudEvents-style topic registry), folded correlation/causation into
> the `metadata` jsonb envelope (so adding `trace_id`/`baggage`/etc.
> needs no migration), and uses a string `event_version` (`'1.0'`,
> `'1.0.1'`) — events evolve faster than a strict integer count.

---

## 26. Event Log (Partitioned, Immutable)

```
CREATE TABLE event_log (
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  aggregate_type text NOT NULL,
  aggregate_id uuid NOT NULL,
  aggregate_version bigint NOT NULL,                -- monotonically increasing per aggregate
  event_type text NOT NULL,
  event_version text NOT NULL DEFAULT '1.0',
  payload jsonb NOT NULL,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,      -- correlation_id, causation_id, trace_id, tenant_id
  occurred_at timestamptz NOT NULL,                 -- partition key
  PRIMARY KEY (id, occurred_at),
  UNIQUE (aggregate_type, aggregate_id, aggregate_version, occurred_at)
) PARTITION BY RANGE (occurred_at);
```

**Indexes:** `ix_event_log_event_type_occurred_at`.

> **Reconciled in 2D (2026-06-29).** Step-A used `tenant_id`/`topic`/
> `correlation_id`+`causation_id`/`received_at` as top-level columns
> with `created_at` denormalised. The implementation chose an
> event-sourcing-friendly canonical shape: every event is keyed by
> `(aggregate_type, aggregate_id, aggregate_version)`, supports the
> CloudEvents-style `event_type` + `event_version`, and folds tenant
> scoping plus correlation/causation into the `metadata` envelope so
> trace headers can evolve without migrations. The partition key was
> renamed from `received_at` to `occurred_at` to match the actor's
> wall-clock semantics (when the event happened, not when our service
> wrote it).

---

## 27. Configuration Tables (CR-DB-4)

The generic `settings` table from the prior revision has been replaced by three explicitly-scoped tables. Project-level configuration continues to live in the `projects.settings jsonb` column; user-level preferences are intentionally deferred (see ADR-0025 in `DECISIONS.md`).

### 27.1 `system_settings` — platform-wide

```
system_settings (
  id uuid PK,
  key text NOT NULL UNIQUE,
  value jsonb NOT NULL,
  description text,
  is_secret boolean NOT NULL DEFAULT false,     -- secrets are app-encrypted before insert
  updated_by_user_id uuid FK → users.id ON DELETE SET NULL,
  version int NOT NULL DEFAULT 1,
  created_at/updated_at
)
```

Examples: `pipeline.default_id`, `retention.class_a_grace_days`, `partition.create_lead_days`.

### 27.2 `tenant_settings` — per-tenant overrides

```
tenant_settings (
  id uuid PK,
  tenant_id uuid NOT NULL FK → tenants.id ON DELETE CASCADE,
  key text NOT NULL,
  value jsonb NOT NULL,
  description text,
  is_secret boolean NOT NULL DEFAULT false,
  updated_by_user_id uuid FK → users.id ON DELETE SET NULL,
  version int NOT NULL DEFAULT 1,
  created_at/updated_at,
  UNIQUE (tenant_id, key)
)
```

Examples: `branding.logo_url`, `storage.preferred_backend`, `models.video.default`, `queue.priority_override`.

### 27.3 `provider_settings` — per-AI-provider config (optionally per-tenant)

```
provider_settings (
  id uuid PK,
  provider text NOT NULL,                               -- 'openai','google','runway',…
  tenant_id uuid FK → tenants.id ON DELETE CASCADE,     -- NULL = global default
  key text NOT NULL,                                    -- 'api_key','region','base_url','rate_limit_rps','enabled'
  value jsonb NOT NULL,
  is_secret boolean NOT NULL DEFAULT false,
  updated_by_user_id uuid FK → users.id ON DELETE SET NULL,
  version int NOT NULL DEFAULT 1,
  created_at/updated_at
)
-- Postgres treats NULL as distinct, so uniqueness is split into two partial-unique indexes:
--   uq_provider_settings_tenant_provider_key  (tenant_id, provider, key)   WHERE tenant_id IS NOT NULL
--   uq_provider_settings_global_provider_key  (provider, key)              WHERE tenant_id IS NULL
-- plus a non-unique ix_provider_settings_provider for lookup-by-provider.
```

Lookup precedence at runtime (highest first): tenant-scoped row → global default row → environment variable → built-in default. Sensitive values (`is_secret = true`) are encrypted with a KMS-managed key before insert; the column stores the ciphertext.

> **Reconciled in 2D (2026-06-29).** The Step-A draft included a
> `value_schema_ref` pointer on `system_settings` / `tenant_settings`
> and a `kind plugin_kind` discriminator on `provider_settings`. The
> implementation drops both: validation now lives on the application
> side (`app.application.settings.schemas`, Phase 3) and is keyed by
> the setting `key` itself, so the schema pointer was redundant; the
> `kind` column was removed because plugins are uniquely identified by
> their `provider` name within the registry, and a single setting like
> `openai.api_key` covers LLM+image+voice without forcing operators to
> duplicate the same secret three times. Whether `kind` should be
> reintroduced is recorded as a Phase-3 question in §37. All three
> tables also gained a `version` column (optimistic locking via
> `VersionMixin`) so a settings UI can detect concurrent edits.

> **Note:** `feature_flag_overrides` (CR-DB-4 fourth table) already exists at §22 and is unchanged.

---

## 28. Templates

```
templates (
  id uuid PK,
  tenant_id uuid FK → tenants.id ON DELETE RESTRICT,   -- NULL = built-in/global
  name text NOT NULL,
  kind text NOT NULL,                                  -- 'video_template','script_template','storyboard_template'
  content jsonb NOT NULL,
  preview_media_asset_id uuid FK → media_assets.id ON DELETE SET NULL,
  created_by_user_id uuid FK → users.id ON DELETE SET NULL,
  active boolean NOT NULL DEFAULT true,
  created_at/updated_at/deleted_at,
  UNIQUE (tenant_id, name) WHERE deleted_at IS NULL
)
```

---

## 29. Webhook Deliveries

```
webhook_deliveries (
  id uuid PK,
  source text NOT NULL,                                -- 'stripe' or 'provider:<name>'
  source_event_id text NOT NULL,                       -- vendor's id; idempotency
  signature text,
  payload jsonb NOT NULL,
  received_at timestamptz NOT NULL DEFAULT now(),
  processed_at timestamptz,
  processing_status text NOT NULL DEFAULT 'pending',   -- 'pending','done','failed'
  last_error text,
  UNIQUE (source, source_event_id)
)
```

---

## 30. Agent Memory (CR-3 — `ai/memory/`)

```
agent_memory (
  id uuid PK,
  user_id uuid NOT NULL FK → users.id ON DELETE CASCADE,
  project_id uuid FK → projects.id ON DELETE CASCADE,
  agent_name text NOT NULL,
  memory_kind text NOT NULL CHECK (memory_kind IN ('short_term','long_term','summary')),
  content jsonb NOT NULL,
  embedding vector(1536),
  created_at/updated_at,
  expires_at timestamptz                              -- short-term TTL
)
```

---

## 31. Idempotency Keys (CR-DB-1)

A first-class table that any unsafe operation can register against to guarantee exactly-once semantics.

```
idempotency_keys (
  id uuid PK,
  tenant_id uuid NOT NULL FK → tenants.id ON DELETE CASCADE,
  key text NOT NULL,                              -- client-supplied (e.g. Idempotency-Key header) or system-generated
  resource_type text NOT NULL,                    -- 'payment','ai_generation','export_job','workflow_retry','webhook'
  resource_id uuid,                               -- set when the operation produces a known id
  request_hash text NOT NULL,                     -- hex-encoded SHA-256 of canonicalised request body
  response_hash text,                             -- hex-encoded SHA-256 of canonicalised response
  response_payload jsonb,                         -- cached small response body
  status idempotency_status NOT NULL,             -- 'in_flight' | 'succeeded' | 'failed'
  http_status text,                               -- HTTP status (kept as text for portability)
  expires_at timestamptz NOT NULL,                -- TTL: 24h synchronous / 30d billing-class
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, key, resource_type)
)
```

**Indexes:** `ix_idempotency_keys_expires_at` (partial: `WHERE status <> 'in_flight'`) for the purge job; `ix_idempotency_keys_resource_type_resource_id` for cache lookups by downstream resource.

> **Reconciled in 2D (2026-06-29).** Step-A drafted `request_hash`/
> `response_hash` as `bytea`, `response_status_code` as `int`, included
> a denormalised `updated_at`, and a CHECK linking `status='in_flight'`
> to `response_hash IS NULL`. Implementation uses hex-encoded text for
> hashes (avoids client-side base64/hex driver inconsistencies),
> renamed the column to `http_status` and typed it as text (matches
> our convention of "wire-format strings stay strings until the
> service layer parses them"), and dropped the redundant `updated_at`
> (any state transition produces an audit event). The CHECK constraint
> is enforced in the use-case layer rather than the DB; whether to
> reintroduce it is a Phase-3 decision (§37). `ON DELETE` on the
> tenant FK is `CASCADE` (a deleted tenant cannot have stuck
> in-flight idempotency keys); Step-A's `RESTRICT` would have blocked
> tenant deletion forever, which is the wrong behaviour for GDPR
> erasure.

**Use cases (registered as `resource_type`):**

| `resource_type` | Triggered by | TTL |
|---|---|---|
| `payment` | Stripe checkout / refund | 30 days |
| `ai_generation` | any provider plugin call (CR-12 also keys on `(provider, request_id)`) | 24 hours |
| `webhook` | inbound webhook delivery (also enforced via `webhook_deliveries`) | 7 days |
| `export_job` | export request | 24 hours |
| `workflow_retry` | manual or auto retry of a workflow run | 24 hours |

**Behaviour:**
1. Caller submits with `Idempotency-Key` header.
2. App computes `request_hash`; performs an `INSERT … ON CONFLICT (tenant_id, key, resource_type) DO NOTHING`.
3. Insert wins → process the request; on completion update to `succeeded`/`failed` with `response_hash` + `response_payload`.
4. Insert loses → look up the row. If `status = 'in_flight'` → return `409 Conflict (in progress)`. If `'succeeded'` → return cached response. If `'failed'` → return cached failure (callers retry with a new key).
5. Mismatched `request_hash` on the same key returns `422 Unprocessable Entity (idempotency key reused with different body)`.

Purge: daily Celery beat `purge_expired_idempotency_keys` deletes `WHERE expires_at < now()`.

---

## 32. Distributed Locks (CR-DB-2)

Lightweight database-backed lock primitive for cases where Redis advisory locks would couple too tightly to broker availability. Used **in addition to** Postgres `pg_advisory_lock` and Redis where appropriate.

```
distributed_locks (
  lock_key text PRIMARY KEY,                     -- 'render_job:<uuid>','project_publish:<uuid>','timeline_edit:<uuid>'
  owner text NOT NULL,                            -- worker id: '<host>:<pid>:<uuid>'
  lease_until timestamptz NOT NULL,               -- auto-expiry if heartbeat lapses
  heartbeat_at timestamptz NOT NULL,
  acquired_at timestamptz NOT NULL DEFAULT now(),
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb    -- holder-defined (run id, attempt #, etc.)
)
```

**Indexes:** `ix_distributed_locks_lease_until` for the janitor's `lease_until < now()` scan.

> **Reconciled in 2D (2026-06-29).** Step-A used a surrogate `id uuid PK`
> with a separate `UNIQUE (lock_key)`. Implementation makes
> `lock_key` the primary key directly (one row per lock by definition;
> the surrogate id added a join column with no consumer). The
> `created_at` column was renamed to `acquired_at` so its meaning is
> obvious to the worker reading it. The `lease_until > created_at`
> CHECK was dropped — every acquisition path computes
> `now() + $lease`, so a runtime CHECK at the DB just makes failure
> harder to diagnose without preventing any real bug; whether to add
> it back is a Phase-3 decision (§37).

**Use cases (canonical `lock_key` prefixes):**
- `render_job:<id>` — exactly one worker renders a job at a time.
- `workflow_run:<id>` — only one engine instance executes a workflow tick.
- `project_publish:<id>` — serialise publish operations on the same project.
- `timeline_edit:<id>` — protect concurrent server-side timeline mutations (CRDT layer in a later phase will replace this).

**Acquire / steal-after-expiry pattern (atomic, single round-trip):**

```sql
INSERT INTO distributed_locks (lock_key, owner, lease_until)
VALUES ($lock_key, $owner, now() + $lease)
ON CONFLICT (lock_key) DO UPDATE
   SET owner = EXCLUDED.owner,
       lease_until = EXCLUDED.lease_until,
       heartbeat_at = now(),
       metadata = EXCLUDED.metadata
 WHERE distributed_locks.lease_until < now()
RETURNING (xmax = 0) AS is_new_holder;
```

**Heartbeat:** the holder updates `heartbeat_at` and extends `lease_until` every `lease/3` seconds. If two consecutive heartbeats fail, the holder voluntarily releases the lock.

**Release:** `DELETE FROM distributed_locks WHERE lock_key = $lock_key AND owner = $owner`.

A janitor job `purge_expired_locks` runs every minute and deletes rows whose `lease_until` was exceeded by more than 5 minutes (safety net against orphan rows).

---

## 33. Audit Log (CR-DB-3) — Partitioned, Immutable

Separate from `event_log` (which records every domain event for replay/debugging). The audit log captures **actor-attributed state changes** for compliance and forensic review. Retained 7 years per `RETENTION_POLICY.md` Class C.

```
CREATE TABLE audit_log (
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  tenant_id uuid FK → tenants.id ON DELETE SET NULL,    -- NULL for platform-admin actions
  actor_kind audit_actor_kind NOT NULL,                  -- 'user'|'system'|'admin'|'api_key'|'webhook'
  actor_user_id uuid FK → users.id ON DELETE SET NULL,   -- NULL for system / api_key / webhook actions
  actor_label text,                                       -- label for non-user actors (api key id, webhook source, …)
  entity_type text NOT NULL,                              -- 'project','feature_flag','credit_ledger','ai_model','subscription','plugin',…
  entity_id uuid,                                         -- target id; NULL for bulk/aggregate actions
  action text NOT NULL,                                   -- 'create','update','delete','enable','disable','adjust','restore','grant','revoke',…
  before_json jsonb,                                      -- NULL on create
  after_json jsonb,                                       -- NULL on delete
  correlation_id uuid,                                    -- ties to the event bus correlation chain
  request_id text,                                        -- ties to API request log (text, not uuid — supports tracing libraries)
  ip inet,
  user_agent text,
  occurred_at timestamptz NOT NULL DEFAULT now(),         -- partition key
  PRIMARY KEY (id, occurred_at)
) PARTITION BY RANGE (occurred_at);
```

**Indexes:** `ix_audit_log_tenant_id_occurred_at`, `ix_audit_log_entity_type_entity_id_occurred_at`, `ix_audit_log_actor_user_id_occurred_at`, `ix_audit_log_action_occurred_at`.

Partitions: monthly, named `audit_log_y<YYYY>m<MM>`. Insert-only; the migration installs a `reject_mutation` trigger that raises on any UPDATE/DELETE. Detached partitions are archived to cold storage at 24 months and dropped at 7 years (the legal retention boundary).

> **Reconciled in 2D (2026-06-29).** Step-A had a top-level `actor_id`
> with no actor-type discrimination, a `reason` column, `request_id`
> typed as uuid, and `created_at` as the partition key. Implementation
> splits the actor into `actor_user_id` (a real FK, used when the
> actor *is* a user) plus a free-form `actor_label` (used for api keys,
> webhooks, scheduled jobs). `request_id` is text so it can hold W3C
> trace ids / OpenTelemetry span ids / custom request ids without a
> migration. The partition key was renamed `occurred_at` for parity
> with `event_log` and `usage_records`. The `reason` column was
> dropped — admin-supplied notes are stored inside `after_json.reason`
> when present, so the column was a denormalisation with no consumer.
> Whether to reintroduce `reason` as a first-class column is a
> Phase-3 question (§37).

**What MUST be audited (initial scope):**

| Domain | Actions captured |
|---|---|
| Projects | create, update, delete, restore-from-version, transfer-owner |
| Billing | subscribe, cancel, plan-change, manual credit adjustment, refund |
| Credit Ledger | every admin-initiated `grant` / `adjustment` / `expiry` |
| Feature Flags | flag value change, rule change, override add/remove |
| Plugins | enable, disable, version pin |
| AI Models | status change (available → deprecated → retired), default change |
| Tenant / User | role grant/revoke, password reset (admin-initiated), erasure request |
| Configuration | any `system_settings` / `tenant_settings` / `provider_settings` write |
| Webhooks | reject, replay |

Every captured action is written via the same recorder (`application/audit/record_audit.py` — created in Phase 3) so consistency is structural, not convention-dependent. The recorder is invoked from use cases inside the transaction that performs the underlying change → atomicity guaranteed.

---

## 34. Foreign Key Summary (Cross-Reference)

Full FK list with `ON DELETE` policy is enumerated in `ERD.md` §"Cross-Cluster Foreign-Key Summary". A second pass during Step B will produce an authoritative table directly from the SQLAlchemy model metadata to confirm 1:1 alignment with this document.

---

## 35. Tables NOT in Phase 2 (Deferred)

Acknowledged but intentionally postponed until later phases:

- `user_preferences` — small per-user preferences table (theme, locale, notification settings). **Deferred per ADR-0025**; prefs of this nature live in `users.extra` JSONB column until a clear product need justifies extraction.
- `rate_limit_state` (Phase 4 — Redis is the source of truth; DB persistence is optional for analytics).
- `row_level_security_policies` (Phase 9 — declared via Alembic but not enforced until then).
- `daily_usage_rollups` materialised view (Phase 6 — derived from `usage_records`).
- `api_keys` table for service accounts (Phase 4).
- `cross_region_replication_log` (post-M2; not required until multi-region GA).

---

## 36. Open Questions Carried to Step B Review

1. Confirm `numeric(18,4)` precision for `credit_ledger.amount` and `credits_consumed` — closed: shipped as `numeric(18,4)` (ADR-0022).
2. Confirm pgvector dimension `1536` for OpenAI / OSS compatibility; should embeddings be normalised before insert? — partly open (normalisation is application-layer responsibility, deferred to Phase 5 when ingestion lands).
3. Confirm partitioning cadence: monthly (current default) vs weekly for very-high-volume tenants. — closed: monthly for Phase 2; revisit at M2 GA based on tenant size data.
4. Confirm whether `library_assets.tags text[]` should be a junction table `library_asset_tags` instead, for better tag analytics. — open, deferred to Phase 6 (analytics).

---

## 37. Phase 3 Entry — Deferred Decisions (added in Phase 2D reconciliation)

The Phase 2D documentation reconciliation deliberately did **not** make any of the following changes, because they require architectural / product / finance decisions rather than documentation alignment. Each item is recorded here so the Phase 3 kickoff can sequence them explicitly.

| # | Question | Affected tables / files | Default if undecided |
|---|---|---|---|
| 1 | Should the ORM models declare `relationship()` helpers, or keep the explicit-joins pattern used in Phase 2? | every model under `app/infrastructure/db/models/` | keep explicit joins — repositories do their own joins |
| 2 | Are any of the indexes listed in `INDEX_STRATEGY.md` but absent from the live schema actually required for the Phase 3 workload, or were they aspirational? | `INDEX_STRATEGY.md` | review per-index against EXPLAIN of Phase 3 hot paths; default = remove from doc |
| 3 | Should `cost_reconciliations` be append-only (forbid UPDATE/DELETE via the `reject_mutation` trigger)? | §19, baseline migration `_IMMUTABLE_TABLES` | keep mutable; finance corrections are audit-logged |
| 4 | Should the `auth_role` enum (declared but unused — Phase 4 will own auth/RBAC) be removed now or kept dormant? | `app/infrastructure/db/enums.py`, baseline migration | keep dormant until Phase 4 |
| 5 | Should the ERD draw cross-cluster FK edges that currently sit at "logical FK" (e.g. `notifications → audit_log`, `audit_log → workflow_runs`)? | `docs/database/ERD.md` | leave as logical (in-text comments) |
| 6 | Should `usage_records` add a per-partition partial-unique `(request_id)` index, or rely on `idempotency_keys`? | §18, `INDEX_STRATEGY.md` | rely on `idempotency_keys` |
| 7 | Should `render_jobs.progress` be retyped from `text` to `numeric(5,2)`? | §17 | leave as text until profiling shows a need |
| 8 | Should the `(render_job_id, format, quality, orientation)` partial-unique constraint on `export_jobs` be promoted to a DB constraint? | §17 | **Resolved (Phase 3 W1.1, 2026-06-29)** — promoted to partial-unique index `uq_export_jobs_render_job_id_format_quality_orientation` with `WHERE status IN ('queued','running','succeeded')` (ADR-0030, migration `0003_export_jobs_partial_unique`). |
| 9 | Should `idempotency_keys` reintroduce the `(status='in_flight') = (response_hash IS NULL)` CHECK and an `updated_at` column? | §31 | application invariant only |
| 10 | Should `distributed_locks` reintroduce a `lease_until > acquired_at` CHECK? | §32 | rely on application logic |
| 11 | Should `audit_log` reintroduce a top-level `reason` column? | §33 | keep inside `after_json.reason` |
| 12 | Should `provider_settings` reintroduce a `kind plugin_kind` discriminator? | §27.3 | keep flat namespace |
| 13 | Should `workflow_runs` reintroduce `correlation_id`, `paused_at`, `version`, queue-related columns? | §16 | derive correlation from `event_outbox`; pause state via `status='paused'` |

These are tracked at the same level as ADRs (small, dated, reviewable). Each item resolved in Phase 3 graduates to an ADR amendment or a new ADR.

### Phase 3 — recommended sequencing of the deferred decisions

Per reviewer guidance at the Phase 2D close, the 13 items above should not all be tackled simultaneously. The order below groups them by their *kind of risk* so Phase 3 PRs stay reviewable:

**Wave 1 — Schema integrity (correctness).** Promote use-case-layer invariants into the DB before they accumulate workarounds.
- §17 q8: `export_jobs (render_job_id, format, quality, orientation)` DB constraint promotion. **✅ Done — Phase 3 W1.1 (ADR-0030, migration `0003_export_jobs_partial_unique`, 2026-06-29).**
- §31 q9: `idempotency_keys` CHECK + `updated_at`.
- §32 q10: `distributed_locks` lease CHECK.
- §18 q6: `usage_records` per-partition `(request_id)` uniqueness.

**Wave 2 — Data model evolution (extension).** Add columns and shape changes the application needs as Phase 3 services land. Each one is a clean migration with no behavioural side-effects on existing rows.
- §16 q13: `workflow_runs` correlation_id / paused_at / version / queue columns.
- §33 q11: `audit_log.reason` first-class column.
- §27.3 q12: `provider_settings.kind` discriminator.
- §17 q7: `render_jobs.progress` type (text → numeric(5,2)).

**Wave 3 — Performance (read-side).** Decide after Wave 1+2 because index choices depend on the final column set.
- Index strategy §16 — all deferred indexes promoted or removed per Phase-3 hot-path EXPLAINs.
- ORM `relationship()` adoption — decide once repositories exist and join patterns are visible.
- ERD cross-cluster edge policy — decide once the cluster-split diagrams' 33 omitted edges have been triaged.

**Wave 4 — Business / product (policy).** Block until product or finance signs off; do not pre-empt with engineering defaults.
- `auth_role` enum retention (Phase 4 owns RBAC).
- `cost_reconciliations` immutability (finance correction workflow).

Each wave produces its own ADR(s); a wave does not start until the previous wave has merged. The CI gate must stay green at the close of every wave's final PR.
