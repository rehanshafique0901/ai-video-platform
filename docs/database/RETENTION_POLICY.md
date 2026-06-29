# Data Retention Policy

> Defines what we keep, for how long, and how it is purged. This is a contractual commitment: changes require an ADR plus a customer-facing privacy-policy update.

---

## 1. Classification

Every persisted record falls into one of four classes. The class determines retention.

| Class | Examples | Default retention |
|---|---|---|
| **A — Customer Content** | projects, scenes, prompts, media assets, library assets, timelines, project versions, templates | Indefinite while subscription active; **30 days** post account closure (recoverable), then permanent delete |
| **B — Operational State** | workflow_runs, workflow_steps, workflow_checkpoints, render_jobs, export_jobs | **180 days** rolling, then archived to cold storage; deletable on request |
| **C — Financial & Audit** | invoices, credit_ledger, ai_model_pricing, cost_reconciliations, webhook_deliveries (financial subset), **audit_log** (CR-DB-3) | **7 years** (jurisdiction-driven; configurable) — append-only, never deleted |
| **D — Telemetry & Events** | analytics_events, event_log, event_outbox (after publish), agent_memory short-term, sessions | **90 days** hot in OLTP, then export to cold storage / data warehouse |

---

## 2. Per-Table Retention Schedule

### 2.1 Class A — Customer Content

| Table | Retention | Mechanism |
|---|---|---|
| `projects`, `folders`, `tags` | Indefinite while live; 30 days after `deleted_at` is set, then hard delete | Daily Celery beat: `purge_soft_deleted_after_30d` |
| `project_versions` | Indefinite while parent project is live; deleted with project | Cascade via `ON DELETE CASCADE` from `projects` |
| `storyboards`, `scenes`, `prompts` | Same as `projects` | Cascade |
| `media_assets` | Same as owning project (orphans purged after 7 days) | Daily `purge_orphan_media` job |
| `library_assets`, `library_folders` | Indefinite while user account active; soft delete 30-day window | `purge_soft_deleted_after_30d` |
| `timelines`, `tracks`, `clips` | Same as `projects` | Cascade |
| `templates` | Indefinite while `active = true`; soft-deleted templates purged after 90 days | Beat job |

**Account closure flow:** when a user soft-deletes their account, all Class A is moved to a `pending_purge` state for 30 days (export available). At day 30 the purge job runs.

### 2.2 Class B — Operational State

| Table | Retention | Mechanism |
|---|---|---|
| `workflow_runs` (terminal status) | 180 days in OLTP | Monthly job exports + deletes runs older than 180 days where `status IN ('succeeded','failed','canceled')` |
| `workflow_runs` (active) | Indefinite while active | n/a |
| `workflow_steps` | Cascade with `workflow_runs` | |
| `workflow_checkpoints` | Deleted when the run terminates successfully; kept 180 days for failed runs (post-mortem) | `purge_checkpoints_for_succeeded_runs` |
| `render_jobs`, `export_jobs` | 180 days; output `media_assets` follow Class A | Monthly purge job |
| `notifications` | 90 days; or 30 days after `read_at` | Daily |
| `webhook_deliveries` (non-financial) | 30 days | Daily |

### 2.3 Class C — Financial & Audit

| Table | Retention | Mechanism |
|---|---|---|
| `invoices` | **7 years** (configurable via `settings.retention.invoice_years`) | Never auto-deleted; archival job moves rows to cold storage after 2 years |
| `credit_ledger` | **Append-only forever** — no purge | Trigger forbids DELETE; configurable cold-storage offload after 2 years |
| `ai_model_pricing` | Forever | Immutable price history is required for cost reconciliation across time |
| `cost_reconciliations` | 7 years | Same as `invoices` |
| `subscriptions` | 7 years post-cancellation | Soft delete only |
| `webhook_deliveries` (Stripe, financial providers) | 7 years | Marked at receive time; subset of the table |
| `audit_log` | **7 years** | Partitioned monthly; immutable; partitions ≥ 24 months exported to cold Parquet, then dropped at 7 years per legal limit |

### 2.4 Class D — Telemetry & Events

| Table | Hot retention | Cold retention | Mechanism |
|---|---|---|---|
| `analytics_events` | 90 days (3 monthly partitions) | 24 months in S3/R2 Parquet | `detach_old_partitions_to_cold` |
| `event_log` | 90 days | 24 months in cold storage | Same |
| `event_outbox` (`published_at NOT NULL`) | 7 days | n/a | Daily purge |
| `event_outbox` (`published_at NULL`) | Forever until published; alarm at 1h unpublished | Dispatcher job |
| `agent_memory` (`memory_kind = 'short_term'`) | Honour `expires_at` (default 24h) | n/a | Hourly TTL job |
| `agent_memory` (`memory_kind = 'long_term'`) | Indefinite while parent user active | n/a | Cascade with user |
| `sessions` | 30 days after `revoked_at` or `expires_at` | n/a | Daily |
| `logs` (if persisted to DB; default is sink to Loki) | 14 days | 90 days in cold | Daily |
| `idempotency_keys` (CR-DB-1) | Honour `expires_at` (default 24h / 30d for billing) | n/a | Daily `purge_expired_idempotency_keys` |
| `distributed_locks` (CR-DB-2) | Honour `lease_until` + 5 min safety net | n/a | Per-minute `purge_expired_locks` |
| `system_settings` / `tenant_settings` / `provider_settings` (CR-DB-4) | Indefinite while parent tenant active; deleted with tenant via CASCADE | n/a | n/a (changes captured by `audit_log`) |

---

## 3. Mechanisms

### 3.1 Soft Delete

- All Class A and most Class B tables carry `deleted_at timestamptz`.
- Application reads add `WHERE deleted_at IS NULL`; partial indexes ensure performance.
- A daily beat job `purge_soft_deleted_after_30d` hard-deletes rows where `deleted_at < now() - INTERVAL '30 days'`.

### 3.2 Partition Detach & Archive

Partitioned tables (`usage_records`, `analytics_events`, `event_log`) use a three-step monthly job:

1. `pre_create_next_partition` — runs day-22 of the month; creates next month's partition with all indexes.
2. `detach_expired_partition` — runs day-1; detaches partitions older than the hot-retention window via `ALTER TABLE … DETACH PARTITION CONCURRENTLY`.
3. `archive_detached_partition` — exports the detached partition to Parquet on cold storage, then `DROP TABLE`.

Each job emits a domain event (`partition.detached`, `partition.archived`) for observability.

### 3.3 Right-to-Erasure (GDPR / CCPA)

- A user-initiated erasure request triggers `application/identity/erase_user.py`.
- Behaviour:
  - Class A is hard-deleted immediately (skipping the 30-day window).
  - Class B is hard-deleted immediately.
  - Class C is **not** deleted (legal retention) but PII fields are tokenised: `users.email`, `users.display_name` replaced with deterministic hashes; `invoices.billing_name` redacted. `audit_log.actor_id` remains valid (points at the hashed user row); `audit_log.ip`, `audit_log.user_agent`, and PII inside `before_json`/`after_json` are scrubbed in place via a controlled `UPDATE` exempted from the immutability trigger — this exemption is itself audited.
  - Class D is partition-wise scrubbed (events bearing the `user_id` are nullified; partitions older than hot window are queued for re-export with the scrubbed user).
- A signed receipt is stored in `event_log` (Class C-retained).

### 3.4 Tenant Offboarding

When a tenant churns:
- Subscription closure event triggers a 30-day grace window.
- On day 30, Class A/B is deleted as above.
- Class C is preserved as required.

---

## 4. Cold Storage

| Source | Cold target | Format | Lifecycle |
|---|---|---|---|
| Detached partitions of `usage_records` | R2 bucket `cold-usage` | Parquet (snappy) | 24 months, then Glacier (or equivalent) |
| Detached partitions of `analytics_events` | R2 bucket `cold-analytics` | Parquet (snappy) | 24 months |
| Detached partitions of `event_log` | R2 bucket `cold-events` | Parquet (snappy) | 24 months |
| Pre-warm invoice PDFs | R2 bucket `invoices` | PDF | 7 years |
| Backups (database) | per `BACKUP_RESTORE.md` | pg_basebackup + WAL | per backup policy |

Cold storage access is gated by an admin endpoint that streams Parquet through a signed URL.

---

## 5. Anonymisation & Aggregation

For long-term analytics that outlive Class D hot retention, daily materialised views aggregate to coarse grain (per-day, per-tenant, per-model). These do not contain PII and are kept indefinitely:

- `mv_daily_usage_summary`
- `mv_daily_credit_consumption`
- `mv_daily_active_users`
- `mv_daily_render_throughput`

Materialised views refresh nightly during off-peak (`background` queue per CR-13).

---

## 6. Verification Jobs

| Job | Cadence | What it verifies |
|---|---|---|
| `verify_credit_ledger_integrity` | Daily | `SUM(amount)` per user matches `balance_after` of the latest row |
| `verify_immutable_tables_no_changes` | Hourly | No UPDATE/DELETE happened on `credit_ledger`, `project_versions`, `usage_records`, `ai_model_pricing`, `event_log`, `audit_log`, `cost_reconciliations` (using trigger audit) |
| `verify_partition_retention` | Daily | No partition exists outside the documented retention window |
| `verify_soft_delete_purge_ran` | Daily | `MAX(deleted_at)` < `now() - 30d` for all Class A tables |

Failures emit `retention.violation` to the Event Bus and page on-call.

---

## 7. Configuration Surface

Per-tenant or per-environment overrides are stored in `settings` rows with `scope='tenant'`:

| Key | Default | Notes |
|---|---|---|
| `retention.class_a_grace_days` | 30 | Customer content recovery window |
| `retention.class_b_days` | 180 | Operational state |
| `retention.class_c_years` | 7 | Financial / audit |
| `retention.telemetry_hot_days` | 90 | Class D in OLTP |
| `retention.gdpr_immediate_erasure` | true | Whether GDPR requests skip the grace window |

All overrides are audited.

---

## 8. Open Questions Carried to Step B Review

1. Should `library_assets` (heavy storage) honour user-set per-asset retention, similar to retention on a vault? *(Tentative: yes, optional per-asset TTL in v2.)*
2. Should `agent_memory long_term` have an opt-out at the user level for memory-free interaction? *(Yes — surfaces in Phase 5 settings.)*
3. Confirm 24-month cold retention is sufficient for product analytics vs business intelligence requirements.
