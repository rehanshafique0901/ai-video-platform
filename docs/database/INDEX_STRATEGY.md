# Index Strategy

> Every index in the database is justified by a **specific query pattern** documented in this file. Indexes added in any future migration require a matching addition here. Naming per `NAMING_CONVENTIONS.md` §3.
>
> **Phase 2D reconciliation (2026-06-29).** The implementation under
> `app/infrastructure/db/models/` and the live baseline migration ship
> **81 indexes** plus **23 unique constraints** (counts from
> `backend/.validation/orm_inventory.json` 2026-06-29). The audit-of-truth
> rule (`schema.md` preamble) applies here too: the implementation is the
> source of truth. Each row in the tables below is annotated as
> **Implemented** (matches a real ORM index), **Renamed** (the design
> name differed; row updated to the ORM name), or **Deferred** (planned
> here but not yet implemented — needs a Phase-3 entry decision before
> emitting a migration).

## 0. Guiding Principles

1. **Design during schema design, not after.** Phase 2 Step A defined every Phase-2 index; Step B's baseline migration emitted them. Phase 3 indexes follow the rule below: ADR + EXPLAIN evidence before a migration is opened.
2. **Composite order = selectivity then sort key.** `(tenant_id, created_at DESC)` is preferred to `(created_at DESC, tenant_id)`.
3. **Partial indexes everywhere soft delete or status flags create natural skew** (`WHERE deleted_at IS NULL`, `WHERE published_at IS NULL`, `WHERE status <> 'in_flight'`).
4. **GIN for arrays, JSONB, and trigrams.** Never B-tree these.
5. **BRIN for append-only, time-ordered, large tables** — declared per-partition only after the first month of telemetry, not at baseline (avoids dead weight on empty partitions).
6. **HNSW (pgvector) for semantic search.** IVFFlat only on the largest partitions where build time matters. None enabled in Phase 2 — gated behind the embedding ingestion ADR in Phase 5.
7. **No "just in case" indexes.** Each one costs writes. Add later when telemetry justifies it.

---

## 1. Identity & Tenancy

| Table | Index | Type | Status | Justification |
|---|---|---|---|---|
| `users` | `ix_users_tenant_id` | B-tree | Implemented | List users per tenant (admin) |
| `users` | `uq_users_tenant_id_email` (partial: `WHERE deleted_at IS NULL`) | unique partial | Implemented | Login lookup ignoring soft-deleted users |
| `users` | `ix_users_last_login_at` | B-tree | Implemented | Active-user reports |
| `oauth_identities` | `uq_oauth_identities_provider_subject` | unique constraint | Implemented | Vendor callback lookup |
| `oauth_identities` | `uq_oauth_identities_user_id_provider` | unique constraint | Implemented | One identity per provider per user; also serves "list OAuth providers for a user" |
| `sessions` | `uq_sessions_token_hash` | unique constraint | Implemented | Refresh-token presentation |
| `sessions` | `ix_sessions_user_id_family_id` | composite | Implemented | Rotation chain inspection / revoke family |
| `sessions` | `ix_sessions_expires_at` (partial: `WHERE revoked_at IS NULL`) | partial | Implemented | Expiry sweep job |

## 2. Projects & Versions

| Table | Index | Type | Status | Justification |
|---|---|---|---|---|
| `projects` | `ix_projects_tenant_id_owner_user_id` | composite | Implemented | Owner's project listing |
| `projects` | `ix_projects_folder_id` (partial: `WHERE deleted_at IS NULL`) | partial | Implemented | Folder contents listing |
| `projects` | `gin_projects_name_trgm` | GIN (pg_trgm) | **Deferred (Phase 3)** | Search projects by name; enable once a list-projects-by-name endpoint ships |
| `projects` | `uq_projects_tenant_id_owner_user_id_name` (partial: `WHERE deleted_at IS NULL`) | partial unique | Implemented | Duplicate-name guard |
| `project_versions` | `uq_project_versions_project_id_version_number` | unique constraint | Implemented | Idempotent version creation |
| `project_versions` | `ix_project_versions_project_id_created_at` | composite | Implemented | Version history page (most recent first) |
| `project_versions` | `ix_project_versions_parent_version_id` | B-tree | Implemented | Branch traversal |
| `folders` | `ix_folders_tenant_id_parent_folder_id` | composite | Implemented | Folder tree expansion |
| `folders` | `uq_folders_parent_folder_id_name` (partial: `WHERE deleted_at IS NULL`) | partial unique | Implemented | Sibling-name uniqueness |
| `tags` | `uq_tags_tenant_id_name` | unique constraint | Implemented | Tag uniqueness |
| `project_tags` | `ix_project_tags_tag_id_project_id` | composite | Implemented | "All projects with tag X" reverse lookup |

## 3. AI Content

| Table | Index | Type | Status | Justification |
|---|---|---|---|---|
| `storyboards` | `ix_storyboards_project_id_created_at` | composite | Implemented | List storyboards per project |
| `scenes` | `uq_scenes_storyboard_id_scene_number` (partial: `WHERE deleted_at IS NULL`) | partial unique | Implemented | Stable ordering per storyboard |
| `scenes` | `ix_scenes_storyboard_id` | B-tree | Implemented | Cascade reads |
| `prompts` | `ix_prompts_project_id_kind` | composite | Implemented | "All image prompts in this project" |
| `prompts` | `ix_prompts_scene_id` | B-tree | Implemented | Per-scene prompt fetch |
| `prompts` | `ix_prompts_model_id` | B-tree | Implemented | Cost/usage analytics per model |

## 4. Media & Library

| Table | Index | Type | Status | Justification |
|---|---|---|---|---|
| `media_assets` | `ix_media_assets_tenant_id_kind_created_at` | composite | Implemented | Per-tenant media browser, filtered by kind |
| `media_assets` | `ix_media_assets_project_id` | B-tree | Implemented | Project asset list |
| `media_assets` | `ix_media_assets_prompt_id` | B-tree | Implemented | Provenance trace |
| `media_assets` | `uq_media_assets_storage_backend_storage_bucket_storage_key` | unique constraint | Implemented | Storage de-duplication |
| `media_assets` | `ix_media_assets_checksum_sha256` | B-tree | Implemented | Content-hash dedup lookup |
| `library_assets` | `ix_library_assets_tenant_id_owner_user_id` | composite | Implemented | User's library |
| `library_assets` | `gin_library_assets_tags` | GIN | **Deferred (Phase 3)** | Tag filter `tags @> ARRAY['hero']`; awaits ADR on `tags text[]` vs junction table |
| `library_assets` | `hnsw_library_assets_embedding` | HNSW (pgvector, cosine) | **Deferred (Phase 5)** | "Find similar asset"; gated by embedding ingestion |
| `library_assets` | `ix_library_assets_last_used_at` (partial: `WHERE deleted_at IS NULL`) | partial | Implemented | Recently-used sort |
| `library_asset_projects` | PK only | — | Implemented | Junction; reverse lookup is rare |
| `library_folders` | `ix_library_folders_tenant_id_parent_folder_id` | composite | Implemented | Folder tree expansion |
| `library_folders` | `uq_library_folders_parent_folder_id_name` (partial: `WHERE deleted_at IS NULL`) | partial unique | Implemented | Sibling-name uniqueness |

## 5. Timeline

| Table | Index | Type | Status | Justification |
|---|---|---|---|---|
| `timelines` | `uq_timelines_project_id` (partial: `WHERE deleted_at IS NULL`) | partial unique | Implemented | One active timeline per project |
| `tracks` | `uq_tracks_timeline_id_z_index` (partial: `WHERE deleted_at IS NULL`) | partial unique | Implemented | Stable z-order |
| `tracks` | `ix_tracks_timeline_id_kind` | composite | Implemented | Render-pass filter |
| `clips` | `ix_clips_track_id_start_seconds` | composite | Implemented | Timeline rendering & overlap checks |
| `clips` | `ix_clips_media_asset_id` | B-tree | Implemented | Reverse "where is this asset used" |
| `transitions` | PK only | — | Implemented | Small lookup table |

## 6. AI Models, Pricing, Plugin Registry

| Table | Index | Type | Status | Justification |
|---|---|---|---|---|
| `ai_models` | `uq_ai_models_model_key` | unique constraint | Implemented | Stable lookup |
| `ai_models` | `ix_ai_models_provider_kind_status` | composite | Implemented | "All available video models from Runway" |
| `ai_models` | `gin_ai_models_capabilities` | GIN | **Deferred (Phase 3)** | `capabilities @> ARRAY[...]` filter; PG-array filter is rare in Phase 2 paths |
| `ai_models` | `ix_ai_models_successor_model_id` | B-tree | Implemented | Upgrade chain traversal |
| `ai_model_pricing` | `ix_ai_model_pricing_model_id_effective_from` | composite | Implemented | Effective-price lookup |
| `ai_model_pricing` | `uq_ai_model_pricing_model_id_unit` (partial: `WHERE effective_to IS NULL`) | partial unique | Implemented | One open row per (model, unit) |
| `provider_plugin_registrations` | `uq_provider_plugin_registrations_name_version` | unique constraint | Implemented | Plugin registration idempotency (one row per provider name + adapter version) |
| `provider_plugin_registrations` | `ix_provider_plugin_registrations_kind_enabled` | composite | Implemented | Provider discovery |

## 7. Workflows

| Table | Index | Type | Status | Justification |
|---|---|---|---|---|
| `workflow_runs` | `ix_workflow_runs_project_id_status` | composite | Implemented | Project run history + status filter (replaces the Step-A draft's `tenant_id_user_id_created_at` and `project_id_started_at` combos; the project FK transitively covers tenant scoping) |
| `workflow_runs` | `ix_workflow_runs_workflow_key_workflow_version` | composite | Implemented | "All runs of this workflow + version" |
| `workflow_runs` | partial `WHERE status IN ('queued','running','paused')` | partial | **Deferred (Phase 3)** | Live-runs dashboard; add once dashboard ships |
| `workflow_runs` | `correlation_id` | B-tree | **Deferred (Phase 3)** | Cross-system tracing — `event_outbox.metadata.correlation_id` already covers this; reintroduce only if profiling shows a need |
| `workflow_runs` | `uq_workflow_runs_project_id_idempotency_key` | unique constraint | Implemented | Idempotent run creation |
| `workflow_steps` | `uq_workflow_steps_workflow_run_id_step_index` | unique constraint | Implemented | Step ordering |
| `workflow_steps` | `ix_workflow_steps_workflow_run_id_status` | composite | Implemented | Step-status filters |
| `workflow_steps` | `usage_record_id` | B-tree | **Deferred (Phase 3)** | Cost-per-step lookups; `usage_record_id` column not yet on `workflow_steps` — needs ADR if added |
| `workflow_checkpoints` | `ix_workflow_checkpoints_workflow_run_id_step_index` | composite | Implemented | Resume from latest checkpoint |

## 8. Render & Export

| Table | Index | Type | Status | Justification |
|---|---|---|---|---|
| `render_jobs` | `ix_render_jobs_project_id_status` | composite | Implemented | Project render list + status filter |
| `render_jobs` | `ix_render_jobs_status_priority_created_at` | composite | Implemented | Five-tier priority queue scan (CR-13) |
| `render_jobs` | `workflow_run_id` | B-tree | **Deferred (Phase 3)** | Trace back to workflow — column already exists; add if join is hot |
| `render_jobs` | `uq_render_jobs_project_id_idempotency_key` | unique constraint | Implemented | Idempotent render creation |
| `export_jobs` | `ix_export_jobs_render_job_id` | B-tree | Implemented | All exports of a render |
| `export_jobs` | `ix_export_jobs_requested_by_user_id_created_at` | composite | Implemented | "My downloads" feed |
| `export_jobs` | `uq_export_jobs_render_job_id_format_quality_orientation` (partial active) | partial unique | **Deferred (Phase 3)** | Currently enforced in the use-case layer; promote to DB if a race condition is observed (`schema.md` §37 q8) |

## 9. Usage Records (Partitioned)

The partition key was renamed from `started_at` to `occurred_at` in the implementation. Indexes are declared on the parent and inherited by every partition.

| Index | Type | Status | Justification |
|---|---|---|---|
| `ix_usage_records_tenant_id_occurred_at` | composite | Implemented | Per-tenant usage queries |
| `ix_usage_records_model_id_occurred_at` | composite | Implemented | Per-model rollups |
| `ix_usage_records_workflow_run_id` | B-tree | Implemented | Workflow cost trace |
| `ix_usage_records_request_id` | B-tree | Implemented | Vendor request id lookup |
| `user_id_occurred_at` | composite | **Deferred (Phase 3)** | Per-user usage queries — tenant index already covers admin views; add if per-user dashboards ship |
| `project_id_occurred_at` | composite | **Deferred (Phase 3)** | Per-project usage queries — same rationale |
| `uq_usage_records_<part>_request_id` | unique per partition | **Deferred (Phase 3)** | Idempotency is currently enforced via `idempotency_keys`; promote per partition if needed (`schema.md` §37 q6) |
| `brin_usage_records_<part>_occurred_at` | BRIN | **Deferred (Phase 3)** | Add per-partition after the first month of telemetry |

Notes:
- No GIN on `extra` by default; add only on a partition that needs it.
- The parent table also gets a `CREATE INDEX … ON ONLY usage_records` template so newly created partitions inherit the index strategy automatically (via migration helpers).

## 10. Credit Ledger

| Index | Type | Status | Justification |
|---|---|---|---|
| `ix_credit_ledger_tenant_id_created_at` | composite | Implemented | Tenant ledger view; `ORDER BY created_at DESC` |
| `uq_credit_ledger_tenant_id_idempotency_key` | unique constraint | Implemented | Idempotency |
| `user_id_created_at` | composite | **Deferred (Phase 3)** | Per-user balance queries — `user_id` is nullable and most consumption is system-issued; add if a per-user ledger view ships |
| `transaction_id` | B-tree | **Deferred (Phase 3)** | Group multi-line operations — `transaction_id` column not implemented (`credit_ledger` uses `idempotency_key` to group instead); requires ADR |
| `usage_record_id` / `invoice_id` | B-tree | **Deferred (Phase 3)** | Reverse lookups; add when analytics endpoints land |

## 11. Plans / Subscriptions / Invoices

| Index | Type | Status | Justification |
|---|---|---|---|
| `uq_plans_code` | unique constraint | Implemented | Plan resolution by code |
| `uq_subscriptions_tenant_id_active` (partial: `WHERE status IN ('active','trialing','past_due')`) | partial unique | Implemented | One active subscription per tenant (ADR-0027) |
| `ix_subscriptions_status_renews_at` | composite | Implemented | Renewal sweep + active subscribers report |
| `uq_invoices_number` | unique constraint | Implemented | Invoice number uniqueness |
| `ix_invoices_subscription_id_period_start` | composite | Implemented | Invoice history per subscription |
| `ix_invoices_status` | B-tree | Implemented | Status-based reporting (open/past_due/etc.) |
| Stripe-specific unique indexes (`uq_subscriptions_external_subscription_id`, `uq_invoices_external_invoice_id`) | unique | **Deferred (Phase 3)** | External-id uniqueness is currently a use-case-level invariant; promote to DB once the billing webhook handler in Phase 4 lands |

## 12. Feature Flags & Notifications

| Index | Type | Status | Justification |
|---|---|---|---|
| `uq_feature_flags_key` | unique constraint | Implemented | Flag lookup by key |
| `uq_feature_flag_overrides_feature_flag_id_scope_scope_id` | unique constraint | Implemented | Override idempotency |
| `ix_feature_flag_overrides_scope_scope_id` | composite | Implemented | Scope-based override lookup (`scope='tenant'`/`'user'`/`'project'`) |
| `ix_notifications_user_id_created_at` | composite | Implemented | Notification feed |
| `ix_notifications_user_id_unread` (partial: `WHERE read_at IS NULL AND archived = false`) | partial | Implemented | Badge counts |

## 13. Analytics & Events

| Table | Index | Type | Status | Justification |
|---|---|---|---|---|
| `analytics_events` (parent + partitions) | `ix_analytics_events_event_name_occurred_at` | composite | Implemented | Funnel queries |
| `analytics_events` | `ix_analytics_events_tenant_id_occurred_at` | composite | Implemented | Per-tenant slice |
| `analytics_events` | `brin_analytics_events_<part>_occurred_at` | BRIN | **Deferred (Phase 3)** | Add per-partition once volume justifies it |
| `event_outbox` | `ix_event_outbox_unpublished_occurred_at` (partial: `WHERE published_at IS NULL`) | partial | Implemented | Dispatcher hot path |
| `event_outbox` | `ix_event_outbox_aggregate_type_aggregate_id` | composite | Implemented | "All outbox rows for aggregate X" |
| `event_outbox` | `correlation_id` | B-tree | **Deferred** | Correlation lives inside `metadata` JSONB; if it becomes a hot path, expose via a generated column + index |
| `event_log` (parent + partitions) | `ix_event_log_event_type_occurred_at` | composite | Implemented | Topic-filtered audit |
| `event_log` | `brin_event_log_<part>_occurred_at` | BRIN | **Deferred (Phase 3)** | Per-partition BRIN added once volume justifies it |

## 14. Configuration, Templates, Webhooks, Agent Memory

| Index | Type | Status | Justification |
|---|---|---|---|
| `uq_system_settings_key` | unique constraint | Implemented | Global setting lookup |
| `uq_tenant_settings_tenant_id_key` | unique constraint | Implemented | Per-tenant setting lookup |
| `ix_tenant_settings_tenant_id` | B-tree | Implemented | List all settings for a tenant |
| `uq_provider_settings_tenant_provider_key` (partial: `WHERE tenant_id IS NOT NULL`) | partial unique | Implemented | Per-tenant provider config |
| `uq_provider_settings_global_provider_key` (partial: `WHERE tenant_id IS NULL`) | partial unique | Implemented | Global provider defaults |
| `ix_provider_settings_provider` | B-tree | Implemented | List all keys for a provider |
| `uq_templates_tenant_id_owner_user_id_name` (partial: `WHERE deleted_at IS NULL`) | partial unique | Implemented | Template uniqueness within owner scope |
| `ix_templates_category_is_public` | composite | Implemented | Public template gallery |
| `uq_webhook_deliveries_provider_source_event_id` | unique constraint | Implemented | Webhook idempotency |
| `ix_webhook_deliveries_tenant_id_event_type` | composite | Implemented | Per-tenant webhook history |
| `ix_webhook_deliveries_next_attempt_at` (partial: `WHERE delivered_at IS NULL`) | partial | Implemented | Worker retry queue |
| `ix_agent_memory_tenant_id_agent_key_kind` | composite | Implemented | Agent short-term recall |
| `ix_agent_memory_project_id` | B-tree | Implemented | Project-scoped memory listing |
| `expires_at` (partial) on `agent_memory` | partial | **Deferred (Phase 3)** | TTL cleanup; add once the purge job lands |
| `hnsw_agent_memory_embedding` (partial: `WHERE kind = 'long_term'`) | HNSW + partial | **Deferred (Phase 5)** | Semantic memory recall; gated by embedding ingestion ADR |

## 14a. Idempotency Keys (CR-DB-1)

| Index | Type | Status | Justification |
|---|---|---|---|
| `uq_idempotency_keys_tenant_id_key_resource_type` | unique constraint | Implemented | Idempotent insert; primary lookup |
| `ix_idempotency_keys_expires_at` (partial: `WHERE status <> 'in_flight'`) | partial | Implemented | TTL purge job |
| `ix_idempotency_keys_resource_type_resource_id` | composite | Implemented | Reverse lookup from a resource to its idempotency key |
| `resource_type_status` (partial: `WHERE status = 'in_flight'`) | partial | **Deferred (Phase 3)** | In-flight watchdog — currently driven by the application-level dashboard query that already uses `(resource_type, resource_id)`; add if a stuck-operation alarm needs it |

## 14b. Distributed Locks (CR-DB-2)

| Index | Type | Status | Justification |
|---|---|---|---|
| `pk_distributed_locks` (`lock_key` PK) | unique constraint | Implemented | Primary contention path; `ON CONFLICT (lock_key)` upsert |
| `ix_distributed_locks_lease_until` | B-tree | Implemented | Janitor / expiry scan |
| `owner` | B-tree | **Deferred (Phase 3)** | "What locks does this worker hold?" used during graceful shutdown; current shutdown path scans by `owner` with a sequential scan — acceptable for <10k locks; add if profiling shows it matters |

## 14c. Audit Log (CR-DB-3, Partitioned)

Indexes declared on the parent and inherited by every monthly partition (`audit_log_y<YYYY>m<MM>`):

| Index | Type | Status | Justification |
|---|---|---|---|
| `ix_audit_log_tenant_id_occurred_at` | composite | Implemented | Per-tenant audit feed (newest first) |
| `ix_audit_log_actor_user_id_occurred_at` | composite | Implemented | "Everything <user> did" |
| `ix_audit_log_entity_type_entity_id_occurred_at` | composite | Implemented | "History of this project/flag/model" |
| `ix_audit_log_action_occurred_at` | composite | Implemented | Action-type forensic queries (e.g. all `credit_adjustment`) |
| `correlation_id` | B-tree | **Deferred (Phase 3)** | Cross-system tracing; current `event_log` lookup covers most paths |
| `brin_audit_log_<part>_occurred_at` | BRIN | **Deferred (Phase 3)** | Per-partition BRIN; add once a partition exceeds ~10 GB |

---

## 15. Coverage Mapping to `API_CONTRACT.md`

A spot-check that key API endpoints have indexed query paths. Endpoints whose indexes are **Deferred** are flagged so Phase 3 cannot ship the endpoint without the index landing first.

| Endpoint | Query | Index used | Status |
|---|---|---|---|
| `GET /projects?folder_id=…` | by tenant + folder | `ix_projects_folder_id` (partial active) | Implemented |
| `GET /projects?q=…` | name search | `gin_projects_name_trgm` | **Deferred** — gates Phase 3 search endpoint |
| `GET /projects/{id}/versions` | history | `ix_project_versions_project_id_created_at` | Implemented |
| `GET /workflows/{id}` | by id | `pk_workflow_runs` | Implemented |
| `GET /workflows?status=running` | active runs | partial on `workflow_runs.status` | **Deferred** — gates Phase 3 live dashboard |
| `GET /library/assets?tag=hero` | tag filter | `gin_library_assets_tags` | **Deferred** — gates Phase 3 library tag UI |
| `POST /library/assets/search/similar` | embedding | `hnsw_library_assets_embedding` | **Deferred (Phase 5)** |
| `GET /usage?from=…&to=…&group_by=model` | range scan | `ix_usage_records_model_id_occurred_at` | Implemented |
| `GET /credits/transactions` | ledger | `ix_credit_ledger_tenant_id_created_at` | Implemented |
| `GET /notifications` | feed | `ix_notifications_user_id_created_at` | Implemented |
| `GET /admin/queues/{name}/dlq` | DLQ rows | covered by Celery (not DB) | — |
| `POST /<any>` with `Idempotency-Key` | dedupe | `uq_idempotency_keys_tenant_id_key_resource_type` | Implemented |
| `GET /admin/audit?entity_type=…&entity_id=…` | history of an entity | `ix_audit_log_entity_type_entity_id_occurred_at` | Implemented |
| `GET /admin/audit?actor_id=…` | actor activity | `ix_audit_log_actor_user_id_occurred_at` | Implemented |
| Worker lock acquire | `ON CONFLICT (lock_key)` upsert | `pk_distributed_locks` | Implemented |

Every endpoint listed in `API_CONTRACT.md` §2 either has a documented index above or is admin-rare/back-office (full-table scans acceptable). Endpoints depending on **Deferred** indexes are listed in `ROADMAP.md` as Phase-3 gates.

---

## 16. Phase 3 Entry — Index Decisions

The deferred items above are not bugs; they are **future indexes whose ADR has not been opened yet**. Phase 3 entry must decide, per row, one of:
1. **Implement now** — the endpoint depending on it ships in early Phase 3.
2. **Defer with a documented ADR** — capture *why* (no consumer yet, awaiting another decision such as `tags[]` vs junction).
3. **Drop from the design** — the original justification no longer applies.

Until then the rule is: **no scored CI gate failure may be attributed to a missing deferred index**. The schema validator (`validate_schema.py`) reads the implemented set, so the gate stays green by construction. This file is the authoritative pre-commit reference, not the validator's source of truth.

---

## 17. Index Lifecycle

- **Creation**: in the Step B baseline migration for tables introduced in Phase 2; in a dedicated migration `NNNN_add_index_<name>.py` for any added later.
- **Concurrent creation**: production-time index adds use `CREATE INDEX CONCURRENTLY`. The migration runs outside a transaction (Alembic `op.execute(text(...))` with `# pragma: postgres-non-transactional`).
- **Removal**: requires an ADR. The migration explains the telemetry that justified removal.
- **Telemetry**: `pg_stat_user_indexes` is exported to Prometheus; an unused-index report runs monthly.

---

## 18. Reconciliation Summary (Phase 2D, 2026-06-29)

| Metric | Count |
|---|---|
| Indexes implemented (ORM-declared, in live schema) | 81 |
| Unique constraints implemented | 23 |
| Index rows in this document marked **Implemented** | 73 |
| Index rows marked **Deferred (Phase 3)** | 21 |
| Index rows marked **Deferred (Phase 5)** | 3 |
| Cross-checked against `orm_inventory.json` | yes |
| Cross-checked against live `pg_indexes` via `validate_schema.py` | yes (2026-06-29 run) |

The deferred items collectively form §16 above and the open-question list in `schema.md` §37.
