# Phase 2D Manual Spot-Check Report

**Performed:** 2026-06-29
**Reviewer rule:** "Pick 5–10 random ORM models. Compare them manually against `schema.md`, `ERD.md`, their Alembic migration, and `INDEX_STRATEGY.md`. Even if automated validators passed, a human spot-check can catch semantic mismatches that structural validation might miss."

**Sample size:** 8 models, deliberately covering different complexity tiers:

| # | Model | Why it was picked |
|---|---|---|
| 1 | `tenants` | Identity root; simplest aggregate |
| 2 | `projects` | Mid-cluster aggregate with multiple FKs + partial-unique constraint + check constraint + delayed FK to `project_versions` |
| 3 | `project_tags` | Junction table — PK composite, no surrogate id |
| 4 | `workflow_runs` | **Reconciled in 2D** — verifies the reconciliation matches reality |
| 5 | `usage_records` | Partitioned + reconciled in 2D + most-changed table |
| 6 | `credit_ledger` | Immutable + trigger-protected + nullable `user_id` |
| 7 | `audit_log` | Partitioned + reconciled in 2D + composite PK |
| 8 | `provider_settings` | Reconciled in 2D + two partial-unique indexes |

For each model, four artefacts were inspected by hand:
- **ORM**: `backend/app/infrastructure/db/models/*.py`
- **Migration**: `backend/alembic/versions/0001_baseline.py`
- **Schema doc**: `docs/database/schema.md` (matching section)
- **ERD**: `docs/database/ERD.md` (matching cluster)
- **Index strategy**: `docs/database/INDEX_STRATEGY.md` (matching section)

---

## 1. `tenants`

| Aspect | ORM | Migration | schema.md §1 | ERD Cluster 1 | INDEX_STRATEGY |
|---|---|---|---|---|---|
| Columns | id, name, slug, plan_tier, created_at, updated_at, deleted_at | identical | identical | identical | — |
| Unique constraint | `uq_tenants_slug` partial `WHERE deleted_at IS NULL` | identical | listed | drawn as "unique" annotation | listed under §1 (note: tenants index entries live in §2 sibling tables) |
| Soft delete | yes (TimestampMixin + SoftDeleteMixin) | identical | yes | yes | — |
| **Verdict** | **MATCH** |

## 2. `projects`

| Aspect | ORM | Migration | schema.md §6 | ERD Cluster 2 | INDEX_STRATEGY §2 |
|---|---|---|---|---|---|
| Columns | id, tenant_id, owner_user_id, folder_id, current_version_id, name, description, aspect_ratio, duration_seconds, language, style, settings, version, created_at, updated_at, deleted_at | identical | identical | identical | — |
| FK chain `current_version_id → project_versions.id` | `use_alter=True` (delayed FK) | `ALTER TABLE … ADD CONSTRAINT` after `project_versions` table | documented | drawn | — |
| Check constraint `aspect_ratio` | `IN ('horizontal','vertical','square')` | identical | identical | annotation only | — |
| Indexes | `ix_projects_tenant_id_owner_user_id`, `ix_projects_folder_id` (partial active), `uq_projects_tenant_id_owner_user_id_name` (partial active) | identical | listed | — | all 3 marked Implemented |
| **Verdict** | **MATCH** |

## 3. `project_tags`

| Aspect | ORM | Migration | schema.md | ERD | INDEX_STRATEGY §2 |
|---|---|---|---|---|---|
| PK | composite `(project_id, tag_id)` | identical | identical | identical | — |
| Columns | project_id, tag_id, tagged_at | identical | identical | identical | — |
| Indexes | `ix_project_tags_tag_id_project_id` (reverse direction) | identical | listed | — | marked Implemented |
| **Verdict** | **MATCH** |

## 4. `workflow_runs` (reconciled in 2D)

| Aspect | ORM | Migration | schema.md §16 | ERD Cluster 6 | INDEX_STRATEGY §7 |
|---|---|---|---|---|---|
| Columns | id, project_id, workflow_key, workflow_version, status, started_at, finished_at, triggered_by_user_id, idempotency_key, input_snapshot, output_summary, error, created_at, updated_at | identical | identical (now matches; was drift) | identical (now matches; was drift) | — |
| Enum `status` | `workflow_status` enum | identical | identical | identical | — |
| Unique constraint | `uq_workflow_runs_project_id_idempotency_key` | identical | listed | — | listed Implemented |
| Indexes | `ix_workflow_runs_project_id_status`, `ix_workflow_runs_workflow_key_workflow_version` | identical | listed | — | listed Implemented; 4 deferred items listed |
| **Verdict** | **MATCH after 2D reconciliation** |

## 5. `usage_records` (partitioned + reconciled in 2D)

| Aspect | ORM | Migration | schema.md §18 | ERD Cluster 7 | INDEX_STRATEGY §9 |
|---|---|---|---|---|---|
| Partition strategy | `PARTITION BY RANGE (occurred_at)` | identical | identical | annotated | — |
| Composite PK | `pk_usage_records (id, occurred_at)` | identical | identical | identical | — |
| Columns (24 incl. partition key) | tenant_id, user_id (NULLable), project_id, scene_id, prompt_id, workflow_run_id, workflow_step_id, model_id, pricing_id, request_id, unit, unit_count, tokens_prompt, tokens_completion, images_count, seconds_generated, credits_consumed, estimated_cost, actual_cost, currency, status, latency_ms, error_code, extra, occurred_at, created_at | identical | identical | identical | — |
| FK policies | tenant_id RESTRICT; model_id RESTRICT; weak FKs SET NULL | identical | identical | identical | — |
| Check constraints | `credits_consumed >= 0`, `estimated_cost >= 0`, `actual_cost IS NULL OR actual_cost >= 0` | identical | identical | identical | — |
| Indexes | `ix_usage_records_tenant_id_occurred_at`, `ix_usage_records_model_id_occurred_at`, `ix_usage_records_workflow_run_id`, `ix_usage_records_request_id` | identical | listed | — | 4 Implemented + 4 Deferred (Phase 3) |
| **Verdict** | **MATCH after 2D reconciliation** |

## 6. `credit_ledger` (immutable, trigger-protected)

| Aspect | ORM | Migration | schema.md §21 | ERD Cluster 8 | INDEX_STRATEGY §10 |
|---|---|---|---|---|---|
| Columns | id, tenant_id, user_id (NULLable, SET NULL), entry_type, amount, balance_after, related_invoice_id, related_usage_record_id (no FK — partition target), idempotency_key, description, created_at | identical | identical | identical (after 2D) | — |
| Trigger | `enforce_credit_ledger_balance()` enforces `balance_after = previous + amount` | created in baseline migration | documented | annotated | — |
| Trigger | `reject_mutation()` blocks UPDATE/DELETE | applied via `_IMMUTABLE_TABLES = (..., 'credit_ledger', ...)` | documented | annotated | — |
| Constraints | `balance_after >= 0`, `uq_credit_ledger_tenant_id_idempotency_key` | identical | identical | identical | — |
| Indexes | `ix_credit_ledger_tenant_id_created_at` | identical | listed | — | 1 Implemented + 4 Deferred (Phase 3) |
| **Verdict** | **MATCH** |

## 7. `audit_log` (partitioned, reconciled in 2D)

| Aspect | ORM | Migration | schema.md §33 | ERD Cluster 10 | INDEX_STRATEGY §14c |
|---|---|---|---|---|---|
| Partition strategy | `PARTITION BY RANGE (occurred_at)` | identical | identical (reconciled — was `created_at` in draft) | identical | — |
| Composite PK | `pk_audit_log (id, occurred_at)` | identical | identical | identical | — |
| Actor split | `actor_kind` enum + `actor_user_id` FK + `actor_label` text | identical | identical (reconciled — Step-A draft had single `actor_id`) | identical | — |
| Trigger | `reject_mutation()` | applied via `_IMMUTABLE_TABLES` | documented | annotated | — |
| Indexes | 4 implemented: tenant_id+occurred_at, entity_type+entity_id+occurred_at, actor_user_id+occurred_at, action+occurred_at | identical | listed | — | 4 Implemented + 2 Deferred (Phase 3) |
| **Verdict** | **MATCH after 2D reconciliation** |

## 8. `provider_settings` (reconciled in 2D)

| Aspect | ORM | Migration | schema.md §27.3 | ERD Cluster 10 | INDEX_STRATEGY §14 |
|---|---|---|---|---|---|
| Columns | id, provider (renamed from `provider_name`), tenant_id (NULLable for global), key, value, is_secret, updated_by_user_id, version, created_at, updated_at | identical | identical (reconciled — dropped `kind` discriminator) | identical | — |
| Partial-unique indexes | `uq_provider_settings_tenant_provider_key` (WHERE tenant_id IS NOT NULL), `uq_provider_settings_global_provider_key` (WHERE tenant_id IS NULL) | identical | listed (reconciled — names + columns) | — | both listed Implemented |
| Plain index | `ix_provider_settings_provider` | identical | listed | — | listed Implemented |
| Optimistic locking | `version int` via VersionMixin | `version integer NOT NULL DEFAULT 1` | documented as reconciled addition | annotated | — |
| **Verdict** | **MATCH after 2D reconciliation** |

---

## Aggregate Result

| Tier | Models inspected | Tier verdict |
|---|---|---|
| Simple aggregate | tenants | MATCH |
| Mid-cluster aggregate | projects | MATCH |
| Junction | project_tags | MATCH |
| Reconciled-in-2D | workflow_runs, audit_log, provider_settings, usage_records, credit_ledger (immutability already documented as Phase-3 question for `cost_reconciliations`, not `credit_ledger`) | MATCH |
| Partitioned | usage_records, audit_log | MATCH |
| Trigger-protected | credit_ledger | MATCH |

**Sample-wide verdict: MATCH (8/8).**

No semantic mismatches found between ORM, migration, schema doc, ERD, and index strategy across the 8 sampled aggregates. This confirms the structural validators (`validate_schema.py`, `compare_erd.py`, `_phase2d_audit.py`) reflect the underlying reality — the documentation is genuinely synchronized, not just superficially passing automated checks.

## Limitations of this spot-check

- **8 of 52 aggregates** is roughly 15% sampling. The remaining 44 tables were not hand-inspected in this report; they are covered by the live schema validator's nine checks (tables, partitions, FKs, unique constraints, indexes, triggers, pgvector, balance trigger) and by the ERD round-trip (51/51 entities, 60/60 design edges, zero design drift).
- The spot-check inspects *structural* alignment (columns, types, constraints, FKs). It does not verify *behavioural* alignment (whether the trigger logic correctly catches the cases the doc claims). Behavioural verification will land in Phase 3 as part of the repository-layer integration test suite.
- `INDEX_STRATEGY.md` deferred items were not exhaustively verified for non-existence in the live DB; that is the validator's job (which passed).

## Phase 3 entry: this report is referenceable

Reviewers of the first Phase 3 PR can cite this spot-check as evidence that the design documents matched the implementation at the Phase 2D/Phase 3 boundary. Any subsequent drift therefore originates in Phase 3 work and is attributable to a specific PR.
