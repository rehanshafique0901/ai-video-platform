# Entity Relationship Diagram (ERD)

> **Phase 2 Step A artifact, reconciled in Phase 2D (2026-06-29).** Covers every aggregate root listed in `ARCHITECTURE.md` §6 plus required infrastructure tables (outbox, event log, partitions, plugin registry). Diagrams are split into clusters for readability; cross-cluster references are listed at the end.
>
> **Phase 2D reconciliation note.** Column shapes in each mermaid block were updated to match the validated ORM (`app/infrastructure/db/models/`) and the live schema as of `validate_schema.py` 2026-06-29. Where this document and the live schema disagreed, the implementation was treated as the source of truth (audit rule, see `schema.md` preamble). Cluster-split FK edges that exist as logical references but are not declared in the implementation are noted as Mermaid comments, not drawn as `||--o{` edges, to keep `regenerate_erd.py + compare_erd.py` ≡ 0 design-edge drift. Whether to promote any of those logical FKs to real DB constraints is a Phase-3 entry decision (see `schema.md` §37).

Legend used in every diagram:

```
{ }   primary key
PK    primary key (alt notation)
FK    foreign key
"||"  one-and-only-one
"o|"  zero-or-one
"|{"  one-to-many (required parent)
"o{"  zero-or-many (optional parent)
```

---

## Cluster 1 — Identity & Tenancy

```mermaid
erDiagram
    tenants ||--|{ users : "owns"
    users   ||--o{ oauth_identities : "federates"
    users   ||--o{ sessions : "has refresh tokens"
    users   ||--o{ roles_users : "assigned"
    roles   ||--o{ roles_users : "applies to"

    tenants {
        uuid id PK
        text name
        text slug "unique"
        text plan_tier
        timestamptz created_at
        timestamptz updated_at
        timestamptz deleted_at "nullable"
    }
    users {
        uuid id PK
        uuid tenant_id FK
        citext email "unique per tenant"
        text password_hash "nullable for OAuth-only"
        text display_name
        timestamptz email_verified_at
        timestamptz last_login_at
        int version "optimistic lock"
        timestamptz created_at
        timestamptz updated_at
        timestamptz deleted_at
    }
    oauth_identities {
        uuid id PK
        uuid user_id FK
        text provider "google|github|…"
        text subject "vendor user id"
        timestamptz linked_at
        timestamptz created_at
        timestamptz updated_at
    }
    sessions {
        uuid id PK
        uuid user_id FK
        uuid family_id "rotation chain"
        text token_hash "unique"
        inet ip
        text user_agent
        timestamptz issued_at
        timestamptz last_used_at
        timestamptz revoked_at
        timestamptz expires_at
    }
    roles {
        uuid id PK
        text code "unique"
        text description
    }
    roles_users {
        uuid role_id FK
        uuid user_id FK
        timestamptz granted_at
        uuid granted_by_user_id FK
    }
```

---

## Cluster 2 — Projects & Versions (CR-6)

```mermaid
erDiagram
    users       ||--o{ projects        : "owns"
    folders     ||--o{ projects        : "contains"
    folders     ||--o{ folders         : "parent_of"
    projects    ||--o{ project_versions : "history"
    project_versions ||--o{ project_versions : "parent_of (branching)"
    projects    }o--o{ tags             : "tagged"
    tags        }o--o{ projects         : "applied to"
    projects    ||--|| project_versions : "current_version"

    projects {
        uuid id PK
        uuid tenant_id FK
        uuid owner_user_id FK
        uuid folder_id FK "nullable"
        uuid current_version_id FK "nullable; points to project_versions.id"
        text name
        text description
        text aspect_ratio "horizontal|vertical|square"
        numeric duration_seconds
        varchar language
        text style
        jsonb settings "providers/models overrides"
        int version "optimistic lock"
        timestamptz created_at
        timestamptz updated_at
        timestamptz deleted_at
    }
    folders {
        uuid id PK
        uuid tenant_id FK
        uuid owner_user_id FK
        uuid parent_folder_id FK "nullable"
        text name
        timestamptz created_at
        timestamptz updated_at
        timestamptz deleted_at
    }
    tags {
        uuid id PK
        uuid tenant_id FK
        text name "unique per tenant"
        timestamptz created_at
        timestamptz updated_at
    }
    project_tags {
        uuid project_id FK
        uuid tag_id FK
        timestamptz tagged_at
    }
    project_versions {
        uuid id PK
        uuid project_id FK
        int version_number "monotonic per project"
        uuid parent_version_id FK "nullable; supports branching"
        uuid created_by_user_id FK
        version_reason reason "manual_save|autosave|restore|branch|generated"
        jsonb snapshot "full state"
        jsonb diff_summary "cached vs parent"
        timestamptz created_at "immutable; no updated_at/deleted_at"
    }
```

---

## Cluster 3 — AI Content (Storyboards, Scenes, Prompts)

```mermaid
erDiagram
    projects         ||--o{ storyboards     : "has"
    project_versions ||--o{ storyboards     : "snapshot of"
    storyboards      ||--|{ scenes          : "contains"
    scenes           ||--o{ prompts         : "uses"
    projects         ||--o{ prompts         : "owns"
    ai_models        ||--o{ prompts         : "intended for"

    storyboards {
        uuid id PK
        uuid project_id FK
        uuid project_version_id FK "nullable; tied to a snapshot"
        text generated_by "system|user"
        timestamptz generated_at
        int version
        timestamptz created_at
        timestamptz updated_at
        timestamptz deleted_at
    }
    scenes {
        uuid id PK
        uuid storyboard_id FK
        int scene_number "unique per storyboard"
        text title
        numeric duration_seconds
        text narration
        text subtitle
        text emotion
        text camera_angle
        text camera_motion
        text lens
        text lighting
        text weather
        text location
        text animation
        text transition_in
        text music_mood
        jsonb extra
        int version
        timestamptz created_at
        timestamptz updated_at
        timestamptz deleted_at
    }
    prompts {
        uuid id PK
        uuid project_id FK
        uuid scene_id FK "nullable"
        prompt_kind kind "image|video|animation|negative|camera|motion|lighting|style"
        text text_content
        uuid model_id FK "nullable; intended model"
        text generated_by_agent
        jsonb extra
        timestamptz created_at
        timestamptz updated_at
        timestamptz deleted_at
    }
```

---

## Cluster 4 — Media Assets & Asset Library (CR-8)

```mermaid
erDiagram
    media_assets       }o--|| ai_models         : "produced by"
    media_assets       }o--o| prompts           : "from prompt"
    media_assets       }o--o| scenes            : "of scene"
    media_assets       }o--o| projects          : "of project"
    media_assets       ||--o| library_assets     : "wrapped by (library entry)"
    library_folders    ||--o{ library_assets    : "contains"
    library_folders    ||--o{ library_folders   : "parent_of"
    library_assets     ||--o{ library_asset_projects : "used in"
    projects           ||--o{ library_asset_projects : "uses"

    media_assets {
        uuid id PK
        uuid tenant_id FK
        uuid owner_user_id FK
        media_kind kind "image|video|narration|subtitle|music|sound_effect|thumbnail"
        uuid project_id FK "nullable"
        uuid scene_id FK "nullable"
        uuid prompt_id FK "nullable"
        uuid model_id FK "nullable"
        text provider "vendor name"
        storage_backend storage_backend "local|s3|r2|azure_blob|gcs"
        text storage_bucket
        text storage_key "unique per backend+bucket"
        text mime_type
        bigint size_bytes
        int width
        int height
        numeric duration_seconds
        bytea checksum_sha256
        media_source source "generated|uploaded|stock"
        jsonb source_metadata
        timestamptz created_at
        timestamptz updated_at
        timestamptz deleted_at
    }
    library_assets {
        uuid id PK
        uuid tenant_id FK
        uuid owner_user_id FK
        uuid media_asset_id FK "unique"
        uuid library_folder_id FK "nullable"
        text name
        text description
        text[] tags
        vector embedding "pgvector(1536), nullable"
        int usage_count
        timestamptz last_used_at
        int version
        timestamptz created_at
        timestamptz updated_at
        timestamptz deleted_at
    }
    library_folders {
        uuid id PK
        uuid tenant_id FK
        uuid owner_user_id FK
        uuid parent_folder_id FK "nullable"
        text name
        timestamptz created_at
        timestamptz updated_at
        timestamptz deleted_at
    }
    library_asset_projects {
        uuid library_asset_id FK
        uuid project_id FK
        timestamptz first_used_at
    }
```

---

## Cluster 5 — Timeline

```mermaid
erDiagram
    projects         ||--|| timelines        : "1:1 current timeline"
    project_versions ||--o{ timelines        : "snapshot"
    timelines        ||--|{ tracks           : "ordered by z_index"
    tracks           ||--o{ clips            : "contains"
    media_assets     ||--o{ clips            : "source"
    transitions      ||--o{ clips            : "applied at boundary"

    timelines {
        uuid id PK
        uuid project_id FK
        uuid project_version_id FK "nullable"
        numeric duration_seconds
        text aspect_ratio
        int frame_rate
        text background_color
        int version
        timestamptz created_at
        timestamptz updated_at
        timestamptz deleted_at
    }
    tracks {
        uuid id PK
        uuid timeline_id FK
        track_kind kind "video|audio|subtitle|effect"
        int z_index "unique per timeline"
        boolean locked
        boolean muted
        text name
        timestamptz created_at
        timestamptz updated_at
        timestamptz deleted_at
    }
    clips {
        uuid id PK
        uuid track_id FK
        uuid media_asset_id FK "nullable for empty/placeholder"
        numeric start_seconds
        numeric end_seconds
        numeric source_start_seconds
        numeric source_end_seconds
        uuid transition_in_id FK "nullable"
        uuid transition_out_id FK "nullable"
        jsonb effects
        numeric volume
        boolean locked
        timestamptz created_at
        timestamptz updated_at
        timestamptz deleted_at
    }
    transitions {
        uuid id PK
        text name
        text kind "fade|wipe|dissolve|cut|custom"
        numeric duration_seconds
        jsonb params
        timestamptz created_at
        timestamptz updated_at
    }
```

---

## Cluster 6 — Workflows, Render & Export (CR-7)

```mermaid
erDiagram
    users         ||--o{ workflow_runs       : "triggered by"
    projects      ||--|{ workflow_runs       : "for"
    workflow_runs ||--|{ workflow_steps      : "ordered"
    workflow_runs ||--o{ workflow_checkpoints : "resume from"
    workflow_runs ||--o{ render_jobs         : "produces"
    render_jobs   ||--o{ export_jobs         : "exported as"
    timelines     ||--|{ render_jobs         : "renders"
    users         ||--|{ export_jobs         : "requested by"
    media_assets  ||--o| render_jobs         : "output of"
    media_assets  ||--o| export_jobs         : "output of"

    workflow_runs {
        uuid id PK
        uuid project_id FK
        text workflow_key "e.g. storyboard_to_render"
        text workflow_version "adapter semver"
        workflow_status status "queued|running|paused|succeeded|failed|canceled"
        timestamptz started_at "nullable"
        timestamptz finished_at "nullable"
        uuid triggered_by_user_id FK "nullable; SET NULL"
        text idempotency_key "nullable; unique per project"
        jsonb input_snapshot "canonicalised inputs"
        jsonb output_summary "nullable"
        jsonb error "nullable; { code, message, trace_id }"
        timestamptz created_at
        timestamptz updated_at
    }
    workflow_steps {
        uuid id PK
        uuid workflow_run_id FK
        int step_index "unique per run"
        text step_name
        step_status status "pending|running|succeeded|failed|skipped|retrying"
        timestamptz started_at "nullable"
        timestamptz finished_at "nullable"
        int retries
        jsonb input "nullable"
        jsonb output "nullable"
        jsonb error "nullable"
        timestamptz created_at
        timestamptz updated_at
    }
    workflow_checkpoints {
        uuid id PK
        uuid workflow_run_id FK
        int step_index
        jsonb state "opaque resume state"
        timestamptz created_at "immutable (reject_mutation trigger)"
    }
    render_jobs {
        uuid id PK
        uuid project_id FK
        uuid timeline_id FK
        uuid workflow_run_id FK "nullable"
        text pipeline "ffmpeg|moviepy|opencv"
        text pipeline_version "adapter semver"
        text queue "critical|high|normal|low|background"
        int priority "secondary ordering"
        render_status status
        timestamptz started_at "nullable"
        timestamptz finished_at "nullable"
        text progress "decimal-as-text 0.00..100.00"
        jsonb error "nullable"
        uuid output_media_asset_id FK "nullable"
        text idempotency_key "nullable; unique per project"
        int version
        timestamptz created_at
        timestamptz updated_at
    }
    export_jobs {
        uuid id PK
        uuid render_job_id FK
        uuid requested_by_user_id FK
        export_format format "mp4|mov|gif|webm"
        export_quality quality "sd|hd_1080p|qhd_2k|uhd_4k"
        export_orientation orientation "horizontal|vertical|square"
        export_status status
        uuid output_media_asset_id FK "nullable"
        int download_count
        timestamptz last_downloaded_at "nullable"
        bigint file_size_bytes "nullable"
        timestamptz finished_at "nullable"
        int version
        timestamptz created_at
        timestamptz updated_at
    }
```

> **Reconciled in 2D:** column shapes match `workflows.py` / `jobs.py`
> ORM models and the live schema as of 2026-06-29. Project_version_id,
> deleted_at, paused_at, correlation_id, queue_name, attempts, error_code+error_message
> are not implemented and were removed from the diagram (rationale in `schema.md` §16–§17).

---

## Cluster 7 — AI Models, Plugins & Cost Tracking (CR-11, CR-12)

```mermaid
erDiagram
    ai_models                       ||--o{ ai_model_pricing : "priced as"
    ai_models                       ||--o{ ai_models       : "successor_of"
    ai_models                       ||--o{ usage_records   : "consumed by"
    workflow_steps                  ||--o{ usage_records   : "billed against"
    users                           ||--o{ usage_records   : "incurred by"
    projects                        ||--o{ usage_records   : "for"
    %% usage_records -> cost_reconciliations: logical only (no FK; usage_records is partitioned)

    ai_models {
        uuid id PK
        text model_key "unique; stable internal id"
        text provider "google|openai|runway|…"
        text vendor_model_id "what we send on the wire"
        plugin_kind kind "llm|image|video|voice"
        text[] capabilities
        text[] modalities
        int context_window
        int max_output_tokens
        bigint max_output_pixels
        int max_output_seconds
        model_status status "available|preview|deprecated|retired"
        date released_at
        date deprecated_at
        date retires_at
        uuid successor_model_id FK "nullable"
        text[] tags
        jsonb extra
        timestamptz created_at
        timestamptz updated_at
    }
    ai_model_pricing {
        uuid id PK
        uuid model_id FK
        timestamptz effective_from
        timestamptz effective_to "nullable; null = open-ended"
        pricing_unit unit "prompt_token|completion_token|image|megapixel|video_second|audio_second|embedding"
        numeric price_per_unit "decimal(18,8)"
        varchar currency "ISO-4217"
        timestamptz created_at "immutable"
    }
    provider_plugin_registrations {
        uuid id PK
        text name "provider name"
        text version "adapter semver"
        plugin_kind kind
        text[] capabilities
        boolean enabled
        text last_health_status
        timestamptz last_health_at
        jsonb extra
        timestamptz created_at
        timestamptz updated_at
    }
    usage_records {
        uuid id "PK with occurred_at"
        uuid tenant_id FK
        uuid user_id FK "nullable; SET NULL"
        uuid project_id FK "nullable; SET NULL"
        uuid scene_id FK "nullable; SET NULL"
        uuid prompt_id FK "nullable; SET NULL"
        uuid workflow_run_id FK "nullable; SET NULL"
        uuid workflow_step_id FK "nullable; SET NULL"
        uuid model_id FK "RESTRICT"
        uuid pricing_id FK "nullable; SET NULL"
        text request_id "nullable; vendor id when present"
        pricing_unit unit "tokens|images|video_seconds|audio_seconds|embeddings|..."
        numeric unit_count "numeric(18,4)"
        int tokens_prompt "nullable"
        int tokens_completion "nullable"
        int images_count "nullable"
        numeric seconds_generated "numeric(10,3), nullable"
        numeric credits_consumed "numeric(18,4), CHECK >= 0"
        numeric estimated_cost "numeric(18,8), CHECK >= 0"
        numeric actual_cost "numeric(18,8), nullable, CHECK >= 0"
        varchar currency "ISO-4217"
        usage_status status "pending|ok|failed|reconciled"
        int latency_ms "nullable"
        text error_code "nullable"
        jsonb extra
        timestamptz occurred_at "partition key"
        timestamptz created_at "PARTITION BY RANGE (occurred_at) monthly"
    }
    cost_reconciliations {
        uuid id PK
        uuid tenant_id FK "RESTRICT"
        uuid model_id FK "RESTRICT"
        timestamptz period_start
        timestamptz period_end "CHECK > period_start"
        numeric invoiced_amount "numeric(18,4)"
        numeric estimated_amount "numeric(18,4)"
        numeric variance "numeric(18,4)"
        varchar currency
        text notes "nullable"
        timestamptz created_at
    }
```

> **Reconciled in 2D:** `usage_records` partition key was renamed from
> `started_at` to `occurred_at`; provider/vendor_model_id/capability
> and the legacy "logical FK" annotation were removed — every
> relational column is a real FK now. Costs are `numeric(18,8)` (not
> integer cents). `cost_reconciliations` aggregates per
> `(tenant_id, model_id, period)` rather than per usage record (see
> `schema.md` §19 for rationale).

---

## Cluster 8 — Billing, Plans & Credit Ledger

```mermaid
erDiagram
    plans         ||--o{ subscriptions : "subscribed under"
    tenants       ||--o{ subscriptions : "owns"
    subscriptions ||--o{ invoices      : "billed via"
    invoices      ||--o{ credit_ledger : "credit purchase entries"
    tenants       ||--|{ credit_ledger : "ledger of"
    users         ||--o{ credit_ledger : "incurred by (nullable, SET NULL)"
    %% Logical-only references (no FK constraint; service-layer enforced):
    %%   usage_records -> credit_ledger.related_usage_record_id   (partitioned target)
    %%   usage_records -> cost_reconciliations.usage_record_id    (partitioned target)

    plans {
        uuid id PK
        text code "unique"
        text name
        text description "nullable"
        billing_cycle cycle "monthly|yearly|custom"
        numeric monthly_credits "numeric(18,4), default 0"
        numeric monthly_price "numeric(18,4), default 0"
        varchar currency "ISO-4217, default USD"
        jsonb features
        boolean active
        timestamptz created_at
        timestamptz updated_at
    }
    subscriptions {
        uuid id PK
        uuid tenant_id FK "RESTRICT"
        uuid plan_id FK "RESTRICT"
        subscription_status status "active|trialing|past_due|canceled|expired"
        timestamptz started_at
        timestamptz renews_at "nullable"
        timestamptz canceled_at "nullable"
        timestamptz trial_ends_at "nullable"
        text payment_provider "stripe|paddle|..."
        text external_customer_id "nullable"
        text external_subscription_id "nullable"
        int version "optimistic lock"
        timestamptz created_at
        timestamptz updated_at
    }
    invoices {
        uuid id PK
        uuid subscription_id FK "RESTRICT"
        text number "unique"
        invoice_status status "draft|open|paid|void|uncollectible"
        numeric amount_due "numeric(18,4)"
        numeric amount_paid "numeric(18,4), default 0"
        varchar currency
        timestamptz period_start
        timestamptz period_end "CHECK > period_start"
        timestamptz issued_at
        timestamptz paid_at "nullable"
        text external_invoice_id "nullable"
        timestamptz created_at
        timestamptz updated_at
    }
    credit_ledger {
        uuid id PK
        uuid tenant_id FK "RESTRICT"
        uuid user_id FK "nullable; SET NULL"
        ledger_entry_type entry_type "purchase|grant|consumption|refund|expiry|adjustment"
        numeric amount "signed; non-zero"
        numeric balance_after "CHECK >= 0; trigger-enforced"
        uuid related_invoice_id FK "nullable; SET NULL"
        uuid related_usage_record_id "nullable; logical FK (usage_records partitioned)"
        text idempotency_key "unique per tenant"
        text description "nullable"
        timestamptz created_at "immutable (reject_mutation trigger)"
    }
```

> **Reconciled in 2D:** subscriptions/invoices are now tenant-scoped
> (no `user_id`; per ADR-0027). The Step-A `current_period_start/end` /
> `ended_at` columns are replaced by `started_at` + `renews_at` +
> `canceled_at` + `trial_ends_at`; `stripe_*` were generalised to
> `payment_provider` + `external_customer_id` + `external_subscription_id`
> so Paddle/Lemon Squeezy/etc. work without a migration. Amounts
> are `numeric(18,4)` decimals (not integer cents) — matches the
> usage-record cost decision. `plans.billing_cycle` → `cycle`,
> `price_cents` → `monthly_price`, `credits_per_cycle` →
> `monthly_credits`.

---

## Cluster 9 — Cross-Cutting (Flags, Notifications, Events, Templates, Webhooks)

```mermaid
erDiagram
    feature_flags        ||--o{ feature_flag_overrides : "scoped by"
    users                ||--o{ notifications          : "addressed to"
    users                ||--o{ analytics_events       : "performed"
    %% event_outbox and event_log are independent tables (no FK):
    %%   outbox = transactional staging; event_log = durable audit. The dispatcher
    %%   reads outbox rows and writes event_log rows in the application layer.

    feature_flags {
        uuid id PK
        text key "unique"
        text description "nullable"
        flag_type flag_type "boolean|percent|variant"
        jsonb default_value
        int rollout_percent "nullable; CHECK 0..100"
        jsonb variants "nullable; for variant flags"
        boolean archived "default false"
        int version
        timestamptz created_at
        timestamptz updated_at
    }
    feature_flag_overrides {
        uuid id PK
        uuid feature_flag_id FK
        flag_scope scope "tenant|user|project"
        uuid scope_id "interpreted per scope"
        jsonb value
        timestamptz expires_at "nullable"
        timestamptz created_at
        timestamptz updated_at
    }
    notifications {
        uuid id PK
        uuid tenant_id FK
        uuid user_id FK
        text kind
        text title
        text body
        jsonb payload
        timestamptz read_at "nullable"
        timestamptz created_at
        timestamptz updated_at
    }
    analytics_events {
        uuid id "PK with occurred_at"
        uuid tenant_id "nullable"
        uuid user_id "nullable"
        text session_id
        text event_name
        jsonb properties
        timestamptz occurred_at "partition key"
        timestamptz ingested_at
        timestamptz created_at "PARTITION BY RANGE (occurred_at)"
    }
    event_outbox {
        uuid id PK
        text aggregate_type
        uuid aggregate_id
        text event_type "from infrastructure/events/topics.py"
        text event_version "semver; default '1.0'"
        jsonb payload
        jsonb metadata "carries correlation_id, causation_id, trace_id"
        timestamptz occurred_at "NOT NULL"
        timestamptz published_at "nullable; partial index for dispatcher"
        int attempts
        text last_error "nullable"
        timestamptz created_at
    }
    event_log {
        uuid id "PK with occurred_at"
        text aggregate_type
        uuid aggregate_id
        bigint aggregate_version "monotonic per aggregate"
        text event_type
        text event_version
        jsonb payload
        jsonb metadata "correlation, causation, trace, tenant_id"
        timestamptz occurred_at "partition key; PARTITION BY RANGE (occurred_at)"
    }
    templates {
        uuid id PK
        uuid tenant_id "nullable; null = global"
        text name
        text kind "video_template|script_template|storyboard_template"
        jsonb content
        uuid preview_media_asset_id "nullable"
        uuid created_by_user_id "nullable"
        boolean active
        timestamptz created_at
        timestamptz updated_at
        timestamptz deleted_at
    }
    webhook_deliveries {
        uuid id PK
        text source "stripe|provider:<name>"
        text source_event_id "unique with source"
        text signature
        jsonb payload
        timestamptz received_at "NOT NULL"
        timestamptz processed_at
        text processing_status "pending|done|failed"
        text last_error
    }
    agent_memory {
        uuid id PK
        uuid user_id FK
        uuid project_id FK "nullable"
        text agent_name
        text memory_kind "short_term|long_term|summary"
        jsonb content
        vector embedding "pgvector(1536), nullable"
        timestamptz created_at
        timestamptz updated_at
        timestamptz expires_at "nullable; short-term TTL"
    }
```

---

---

## Cluster 10 — Configuration & Operations (CR-DB-1 … CR-DB-4)

```mermaid
erDiagram
    tenants ||--o{ idempotency_keys   : "scoped to"
    tenants ||--o{ tenant_settings    : "configured by"
    tenants ||--o{ provider_settings  : "configured by"
    users   ||--o{ system_settings    : "last updated by"
    users   ||--o{ tenant_settings    : "last updated by"
    users   ||--o{ provider_settings  : "last updated by"
    users   ||--o{ audit_log          : "actor"
    tenants ||--o{ audit_log          : "scoped to"

    idempotency_keys {
        uuid id PK
        uuid tenant_id FK "CASCADE"
        text key
        text resource_type "payment|ai_generation|export_job|workflow_retry|webhook"
        uuid resource_id "nullable"
        text request_hash "hex-encoded SHA-256"
        text response_hash "nullable"
        jsonb response_payload "nullable"
        idempotency_status status "in_flight|succeeded|failed"
        text http_status "nullable; kept as text"
        timestamptz expires_at
        timestamptz created_at
    }
    distributed_locks {
        text lock_key PK
        text owner "worker id"
        timestamptz lease_until
        timestamptz heartbeat_at
        timestamptz acquired_at
        jsonb metadata
    }
    audit_log {
        uuid id "PK with occurred_at"
        uuid tenant_id FK "nullable for platform actions; SET NULL"
        audit_actor_kind actor_kind "user|system|admin|api_key|webhook"
        uuid actor_user_id FK "nullable; SET NULL"
        text actor_label "nullable; label for non-user actors"
        text entity_type "project|feature_flag|credit_ledger|ai_model|…"
        uuid entity_id "nullable for bulk"
        text action "create|update|delete|enable|disable|adjust|restore|…"
        jsonb before_json "null on create"
        jsonb after_json "null on delete"
        uuid correlation_id "nullable"
        text request_id "nullable; supports trace ids"
        inet ip "nullable"
        text user_agent "nullable"
        timestamptz occurred_at "PARTITION BY RANGE (occurred_at) monthly"
    }
    system_settings {
        uuid id PK
        text key "unique"
        jsonb value
        text description "nullable"
        boolean is_secret
        uuid updated_by_user_id FK "nullable"
        int version "optimistic lock"
        timestamptz created_at
        timestamptz updated_at
    }
    tenant_settings {
        uuid id PK
        uuid tenant_id FK "CASCADE"
        text key
        jsonb value
        text description "nullable"
        boolean is_secret
        uuid updated_by_user_id FK "nullable"
        int version
        timestamptz created_at
        timestamptz updated_at
    }
    provider_settings {
        uuid id PK
        text provider "openai|google|runway|…"
        uuid tenant_id FK "nullable; null = global default; CASCADE"
        text key "api_key|region|base_url|rate_limit_rps|enabled"
        jsonb value
        boolean is_secret
        uuid updated_by_user_id FK "nullable"
        int version
        timestamptz created_at
        timestamptz updated_at
    }
```

> **Reconciled in 2D:** `idempotency_keys` uses text hex hashes (not
> `bytea`); `http_status` is text; `updated_at` dropped (every transition
> emits an audit event). `distributed_locks` makes `lock_key` the
> primary key directly. `audit_log` splits actor into `actor_user_id`
> (real FK) + `actor_label` (free-form for api keys / webhooks /
> schedulers); `reason` is folded into `after_json`. `provider_settings`
> drops the `kind` discriminator (single namespace by `provider`+`key`).
> All three settings tables gained a `version` column (`VersionMixin`).

> `feature_flag_overrides` (the fourth CR-DB-4 table) is shown in **Cluster 9** and is unchanged from the previous revision.

---

## Cross-Cluster Foreign-Key Summary

These FKs span clusters and are easy to miss; documented here for review:

| From → To | Cardinality | ON DELETE |
|---|---|---|
| `projects.owner_user_id` → `users.id` | many:1 | RESTRICT (users have a soft-delete tombstone process) |
| `projects.current_version_id` → `project_versions.id` | 1:1 (head pointer) | SET NULL (FK is informational; truth is in `project_versions`) |
| `media_assets.model_id` → `ai_models.id` | many:1 | RESTRICT (audit integrity) |
| `media_assets.prompt_id` → `prompts.id` | many:1 | SET NULL |
| `library_assets.media_asset_id` → `media_assets.id` | 1:1 | RESTRICT (library wraps a real asset; deletion is via library) |
| `clips.media_asset_id` → `media_assets.id` | many:1 | SET NULL (clip becomes a placeholder) |
| `render_jobs.output_media_asset_id` → `media_assets.id` | 1:1 | SET NULL |
| `export_jobs.output_media_asset_id` → `media_assets.id` | 1:1 | SET NULL |
| `workflow_runs.project_id` → `projects.id` | many:1 | SET NULL (run history survives project deletion) |
| `usage_records.model_id` → `ai_models.id` | many:1 | RESTRICT (immutable) |
| `usage_records.tenant_id/user_id/project_id` | many:1 | RESTRICT |
| `credit_ledger.user_id/tenant_id` | many:1 | RESTRICT |
| `credit_ledger.invoice_id` → `invoices.id` | many:1 | RESTRICT |
| `ai_models.successor_model_id` → `ai_models.id` | self | SET NULL |
| `feature_flag_overrides.flag_id` → `feature_flags.id` | many:1 | CASCADE |
| `subscriptions.plan_id` → `plans.id` | many:1 | RESTRICT |

Logical FKs (no DB-level constraint, because the target table is partitioned and a composite FK would be needed):

| From → To | Reason |
|---|---|
| `workflow_steps.usage_record_id` → `usage_records.id` | usage_records is partitioned; FK requires (id, started_at) |
| `cost_reconciliations.usage_record_id` → `usage_records.id` | same |
| `credit_ledger.usage_record_id` → `usage_records.id` | same |

These are validated at application level via repository-layer checks plus an integrity job in Phase 10.

---

## Aggregate Root Coverage Check

Verifying that every aggregate root from `ARCHITECTURE.md` §6 has a table:

| Aggregate Root | Primary Table |
|---|---|
| `User` | `users` |
| `Project` (with `ProjectVersion`s) | `projects`, `project_versions` |
| `Storyboard` (with `Scene`s) | `storyboards`, `scenes` |
| `Prompt` | `prompts` |
| `Image / Video / Narration / Subtitle / Music / SoundEffect / Thumbnail` | `media_assets` (discriminated by `kind`) |
| `Timeline` (with `Track`s & `Clip`s) | `timelines`, `tracks`, `clips`, `transitions` |
| `RenderJob` | `render_jobs` |
| `ExportJob` | `export_jobs` |
| `WorkflowRun` (with `WorkflowStep`s & `Checkpoint`s) | `workflow_runs`, `workflow_steps`, `workflow_checkpoints` |
| `LibraryAsset` | `library_assets`, `library_folders`, `library_asset_projects` |
| `Subscription` | `subscriptions`, `plans`, `invoices` |
| `CreditLedger` (aggregate root) / `CreditTransaction` (entries) | `credit_ledger` |
| `AnalyticsEvent` | `analytics_events` |
| `Notification` | `notifications` |
| `FeatureFlag` | `feature_flags`, `feature_flag_overrides` |
| `AIModel` | `ai_models`, `ai_model_pricing`, `provider_plugin_registrations` |
| `UsageRecord` | `usage_records`, `cost_reconciliations` |

**Coverage = 100%.** No aggregate root is missing.

Additional infrastructure tables not tied to an aggregate root: `tenants`, `sessions`, `oauth_identities`, `roles`, `roles_users`, `folders`, `tags`, `project_tags`, `system_settings`, `tenant_settings`, `provider_settings`, `templates`, `event_outbox`, `event_log`, `webhook_deliveries`, `agent_memory`, `idempotency_keys` (CR-DB-1), `distributed_locks` (CR-DB-2), `audit_log` (CR-DB-3).

---

## Notes for Step B (Implementation)

- All `timestamptz` columns are stored UTC; the Python layer never writes naïve datetimes.
- Partitioned tables (`usage_records`, `analytics_events`, `event_log`, optionally `logs`) are created with a partition-management Celery beat job that pre-creates next-month partitions and detaches expired ones (see `RETENTION_POLICY.md`).
- `pgvector` columns are nullable to allow lazy embedding generation.
- The `version` column starts at `1` on insert and is incremented by `tg_<table>_biu_version_bump` on every update.
- The `tg_<table>_biu_touch_updated_at` trigger sets `updated_at = now()` on every update (skipping the no-op case where only `updated_at` changed).
