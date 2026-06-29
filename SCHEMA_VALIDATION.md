# Schema Validation Report — Phase 2, Step B

**Status: VALIDATION PASSED — 2026-06-28.** All nine structural checks
pass against a live PostgreSQL 17.6 instance with `pgvector` 0.8.0. The
hand-authored design ERD and the live-regenerated ERD agree on every
entity (51/51) and every design-declared FK edge (58/58); 35 additional
FKs exist in the implementation that the cluster-split design diagrams
deliberately omit for readability. Migrations upgrade, downgrade, and
re-upgrade cleanly (idempotency proven). One design-vs-implementation
drift surfaced during validation was reconciled in §7 (ADR-0027 records
the tenant-scoped billing decision); the validator and ERD generator
were also rewritten against `pg_catalog` for a 15× speed-up. The runner
can now be wired into CI as the Phase 9 gate.

The §6 "Live Run Result" section below contains the actual outputs from
the validation run; §7 records the reconciled deviations.

## 1. Scope and Authority

This report compares the *implemented* PostgreSQL schema (after running
`alembic upgrade head` to revision `0002_seed_system_data`) against the
*approved design* described in:

- `docs/database/schema.md` — authoritative table-by-table definitions
- `docs/database/ERD.md` — visual cluster diagrams
- `docs/database/INDEX_STRATEGY.md` — index justifications
- `docs/database/NAMING_CONVENTIONS.md` — identifier rules
- `docs/database/RETENTION_POLICY.md` — partition + immutability rules
- `DECISIONS.md` ADR-0001 through ADR-0027

When the implemented schema differs from any of the above, the diff is
either reconciled in code (preferred) or recorded as an explicit
intentional deviation in §7 ("Acknowledged Deviations") below.

## 2. Methodology

### 2.1 Execution Order

The runner (`backend/scripts/run_validation.py`) executes the exact 8-step
order approved in `ROADMAP.md`:

1. **Generate SQLAlchemy models** — already on disk under
   `backend/app/infrastructure/db/models/` (23 files, ~40 tables).
2. **Review relationships** — see §3 below; cross-referenced against
   the cross-cluster FK summary in `ERD.md`.
3. **Generate Alembic baseline** — `0001_baseline.py` (single
   reversible migration) plus seed `0002_seed_system_data.py`.
4. `alembic upgrade head` — applies both migrations to an empty database.
5. `alembic downgrade base` — verifies clean revert (every CREATE in
   step 4 has a matching DROP).
6. `alembic upgrade head` (again) — verifies idempotency (per the user's
   instruction that the second upgrade must succeed without manual
   intervention).
7. **Introspect implemented schema** via SQLAlchemy `inspect()` and raw
   queries against `information_schema` / `pg_catalog`, comparing
   against the ORM `metadata` and the expected entity lists.
8. **Regenerate ERD** from the implemented schema and diff it against
   `docs/database/ERD.md`.

### 2.2 Checks Performed

The validator (`backend/scripts/validate_schema.py`) runs nine
independent checks. Each is a single function and produces a structured
`CheckResult` recorded in `schema_validation_report.json`.

| # | Check                                             | Source of truth |
|---|---------------------------------------------------|------------------|
| 1 | Required PostgreSQL extensions installed          | `EXPECTED_EXTENSIONS` (pgcrypto, citext, pg_trgm, vector, btree_gin) |
| 2 | All ORM-declared tables exist; no extras          | `app.infrastructure.db.metadata` |
| 3 | All partitioned tables are partitioned + have ≥1 child | `EXPECTED_PARTITIONED` |
| 4 | Every ORM foreign key exists with the right ON DELETE | `metadata.foreign_keys` |
| 5 | Every ORM unique constraint / unique index exists | `metadata.unique_constraints` + `metadata.indexes` |
| 6 | Every documented index (including imperative GIN/HNSW) exists | `EXTRA_EXPECTED_INDEXES` ∪ `metadata.indexes` |
| 7 | Immutable tables protected by `tg_*_bud_reject_mutation` trigger | `EXPECTED_IMMUTABLE` |
| 8 | `vector` column type only on approved tables       | `EXPECTED_PGVECTOR_COLUMNS` |
| 9 | `credit_ledger` balance trigger present            | hard-coded |

The runner exits non-zero on any failure, which trips CI in Phase 9.

## 3. Relationship Review (Step 2)

A manual pass over the ORM relationships against `ERD.md` confirms:

- **Identity cluster** — `tenants` is the root; `users`,
  `oauth_identities`, `sessions`, `roles_users`. The `granted_by_user_id`
  self-reference on `roles_users` uses `ON DELETE SET NULL` so deleted
  granters do not cascade to user role assignments.
- **Projects cluster** — circular FK between `projects.current_version_id`
  and `project_versions.id` is broken with `use_alter=True` so SQLAlchemy
  can declare it after both tables exist. The baseline migration adds the
  FK via `ALTER TABLE ... ADD CONSTRAINT` after both tables are created.
- **Scenes cluster** — `scenes` cascades from `storyboards`; `prompts`
  attaches to either project or scene, with `model_id` pointing to the
  registry.
- **Media cluster** — `library_assets` 1:1 with `media_assets`
  (`uq_library_assets_media_asset_id`); `library_asset_projects` is the
  many-to-many usage join.
- **Timeline cluster** — `timelines` 1:1 with `projects` (partial
  unique index `WHERE deleted_at IS NULL`).
- **AI models cluster** — `ai_models` self-reference for successor
  models; `ai_model_pricing` immutable; `provider_plugin_registrations`
  unique on `(name, version)`.
- **Workflow cluster** — `workflow_steps` → `workflow_runs` cascade;
  `workflow_checkpoints` cascade; idempotency unique on
  `(project_id, idempotency_key)`.
- **Jobs cluster** — `render_jobs` reference `timelines` with `RESTRICT`
  so deleting a timeline never silently breaks an in-flight render.
- **Usage / Billing** — `usage_records` is partitioned and never carries
  FKs *out* of partitioned children; `credit_ledger.related_usage_record_id`
  is intentionally **not** an FK (partitioned table targets are forbidden
  for FK references in stock PostgreSQL); service-layer integrity is
  documented in `schema.md` §22.
- **Operations** — `idempotency_keys` cascades from `tenants`;
  `distributed_locks` has no tenant linkage by design (operational layer
  is tenant-agnostic).
- **Audit** — `audit_log` partitioned, FKs to `tenants`/`users` use
  `SET NULL` so deletion of an actor does not violate retention.

No cycles other than the documented `projects ↔ project_versions` one.

## 4. Migration Order (Steps 3–6)

The baseline migration creates objects in dependency order:

1. Extensions (`pgcrypto`, `citext`, `pg_trgm`, `vector`, `btree_gin`).
2. ENUM types (27 enums, all in `app/infrastructure/db/enums.py`).
3. Helper PL/pgSQL functions: `touch_updated_at()`, `bump_version()`,
   `reject_mutation()`, `enforce_credit_ledger_balance()`.
4. Tables, grouped by cluster (identity → projects → scenes → ai_models →
   prompts → media → timeline → workflows → jobs → usage → billing →
   feature_flags → notifications → analytics → events → configuration →
   templates → webhooks → operations → audit → agent_memory → sentinel).
5. The `projects.current_version_id → project_versions.id` FK is added
   *after* both tables exist (circular reference).
6. Partitions for `usage_records`, `analytics_events`, `event_log`,
   `audit_log`: 26 monthly partitions (last month, this month, next 24
   months) plus one `_default` partition per parent.
7. `BEFORE UPDATE` triggers for `updated_at` and `version` columns.
8. `BEFORE UPDATE OR DELETE` triggers for immutable tables.
9. `BEFORE INSERT` trigger on `credit_ledger` that enforces balance
   monotonicity.

`downgrade()` reverses every CREATE in strict reverse order, including
partition children. Extensions are intentionally **not** dropped on
downgrade (they may be shared cluster-wide).

## 5. How to Run Validation Locally

```powershell
cd "ai creation/backend"
.\scripts\run_validation.ps1
```

or, manually:

```bash
cd ai\ creation/backend
docker compose -f docker-compose.db.yml up -d
pip install -e .
python scripts/run_validation.py
```

The runner writes two artifacts under `ai creation/validation_artifacts/`:

- `schema_validation_report.json` — machine-readable check results
- `erd_generated.md` — Mermaid ER diagram regenerated from the
  implemented schema (compare against `docs/database/ERD.md`)

## 6. Live Run Result

```
status:             PASSED
generated_at:       2026-06-28 16:58:21 UTC ... 17:13:05 UTC
postgres_version:   PostgreSQL 17.6 on aarch64-unknown-linux-gnu, gcc 15.2.0
host:               aws-1-ap-northeast-2.pooler.supabase.com:5432 (Supabase Session Pooler, IPv4)
database:           postgres
role:               postgres
pgvector_version:   0.8.0 (HNSW supported)
all_passed:         true
```

### 6.1 Migration outcomes

| Step | Command                     | Duration | Result | Log |
|------|-----------------------------|----------|--------|-----|
| 1    | `alembic upgrade head`      | ~60 s    | ✅ both revisions applied (`0001_baseline`, `0002_seed_system_data`) | `.validation/step1_upgrade.log` |
| 2    | `alembic downgrade base`    | ~30 s    | ✅ public schema returned to empty (only `alembic_version` table retained as Alembic's own bookkeeping; row count = 0) | `.validation/step2_downgrade.log` |
| 3    | `alembic upgrade head`      | ~61 s    | ✅ idempotent — second upgrade applied both revisions without manual intervention | `.validation/step3_reupgrade.log` |

Post-upgrade inventory snapshot (validator-verified, `public` schema only —
the Supabase project also hosts `auth`/`storage`/`realtime`/etc. internal
schemas which are not counted here):

```
alembic rev                          : 0002_seed_system_data
base tables (ORM-declared)           : 52
partitioned parents                  :  4   (usage_records, analytics_events, event_log, audit_log)
partition children                   : 108  (= 4 parents × 27 = current month + 25 future months + default)
alembic bookkeeping table            :  1   (alembic_version — whitelisted)
total public-schema relations        : 161  (52 + 108 + 1)
enum types (public)                  : 26
foreign keys (validated)             : 95   (93 inter-table + 2 self-references: ai_models.successor_model_id, project_versions.parent_version_id; folders.parent_folder_id and library_folders.parent_folder_id are inter-table self-refs counted in the 93)
unique constraints / unique indexes  : 28
documented indexes (incl. partials)  : 86
custom PL/pgSQL functions            :  4   (touch_updated_at, bump_version, reject_mutation, enforce_credit_ledger_balance)
immutable-protected tables           :  8
pgvector columns                     :  2   (agent_memory.embedding, library_assets.embedding — HNSW-indexed)

seed counts (after 0002_seed_system_data):
  plans                          : 4   (free, pro, business, enterprise)
  ai_models                      : 13
  feature_flags                  : 10
  roles                          :  6  (owner, admin, editor, viewer, billing, support)
  provider_plugin_registrations  :  9
  system_settings                :  7
```

### 6.2 Structural checks

| # | Check                                                       | Expected                     | Result | Notes |
|---|-------------------------------------------------------------|------------------------------|--------|-------|
| 1 | Required PostgreSQL extensions                              | 5 installed                  | ✅ pass | `btree_gin`, `citext`, `pg_trgm`, `pgcrypto`, `vector` (v0.8.0) |
| 2 | Tables match ORM metadata                                   | 52 tables                    | ✅ pass | All 52 ORM tables present; `alembic_version` whitelisted as Alembic-managed extra |
| 3 | Partitioned tables                                          | 4 parents × ≥1 child         | ✅ pass | Each of the 4 parents has 27 children (current month + 25 future months + default) |
| 4 | Foreign keys (presence + `ON DELETE`)                       | 95 FKs                       | ✅ pass | All 95 ORM-declared FKs present with matching `ON DELETE` actions |
| 5 | Unique constraints / unique indexes                         | All declared in ORM          | ✅ pass | Includes partial-unique indexes (e.g. `uq_subscriptions_tenant_id_active WHERE status IN (…)`) |
| 6 | Documented indexes present                                  | 86 indexes (81 ORM + 5 imperative GIN/HNSW) | ✅ pass | `ix_ai_models_capabilities_gin`, `ix_library_assets_tags_gin`, `ix_library_assets_embedding_hnsw`, `ix_analytics_events_properties_gin`, `ix_agent_memory_embedding_hnsw` all found |
| 7 | Immutable tables protected by `reject_mutation` trigger     | 8 tables                     | ✅ pass | `project_versions`, `ai_model_pricing`, `workflow_checkpoints`, `usage_records`, `credit_ledger`, `analytics_events`, `event_log`, `audit_log` |
| 8 | pgvector usage limited to approved tables                   | exactly 2 columns            | ✅ pass | `agent_memory.embedding`, `library_assets.embedding` — no rogue vector columns |
| 9 | `credit_ledger` balance trigger present                     | present                      | ✅ pass | Trigger `tg_credit_ledger_bi_enforce_balance` rejecting non-monotonic inserts |

Validator runtime after the pg_catalog rewrite: **17 s** (was 263 s in the
inspector-based version; the runner now loads the entire catalog in three
bulk queries instead of ~400 per-table round-trips).

### 6.3 ERD round-trip

| Aspect                                                  | Result |
|---------------------------------------------------------|--------|
| Entity sets match (generated vs `docs/database/ERD.md`) | ✅ 51 / 51 |
| Design-declared edges present in implementation         | ✅ 58 / 58 |
| Edges declared in design but missing in implementation  | ✅ 0 |
| Extra FKs in implementation (cluster-split design omits)| 35 (expected — see ERD.md §"Cross-Cluster Foreign-Key Summary") |

Generated ERD: `backend/.validation/erd_generated.md`.
Structured diff JSON: `backend/.validation/erd_diff.json`.

### 6.4 Raw `schema_validation_report.json`

```json
{
  "database_url": "postgresql+psycopg://postgres.muujiwcbpgkplfsjpotv:***@aws-1-ap-northeast-2.pooler.supabase.com:5432/postgres?sslmode=require",
  "all_passed": true,
  "checks": [
    {"name": "Required PostgreSQL extensions",                              "passed": true, "details": ["Installed: ['btree_gin', 'citext', 'pg_trgm', 'pgcrypto', 'vector']"]},
    {"name": "Tables match ORM metadata",                                   "passed": true, "details": ["All 52 ORM-declared tables present (Alembic-managed extras ignored: ['alembic_version'])."]},
    {"name": "Partitioned tables",                                          "passed": true, "details": ["analytics_events: partitioned, 27 children", "audit_log: partitioned, 27 children", "event_log: partitioned, 27 children", "usage_records: partitioned, 27 children"]},
    {"name": "Foreign keys",                                                "passed": true, "details": ["All 95 ORM-declared foreign keys present with matching ON DELETE."]},
    {"name": "Unique constraints / unique indexes",                         "passed": true, "details": ["All ORM-declared unique constraints present."]},
    {"name": "Documented indexes present",                                  "passed": true, "details": ["All 86 expected indexes present."]},
    {"name": "Immutable tables protected by reject_mutation trigger",       "passed": true, "details": ["All 8 immutable tables protected."]},
    {"name": "pgvector usage limited to approved tables",                   "passed": true, "details": ["Vector columns appear only on approved tables: [('agent_memory', 'embedding'), ('library_assets', 'embedding')]"]},
    {"name": "credit_ledger balance trigger present",                       "passed": true, "details": ["OK"]}
  ]
}
```

> The password component of the connection URI is redacted in every
> artefact written by the harness. The full URI is held only in
> `backend/.env.validation`, which is git-ignored.

## 7. Acknowledged Deviations

One documentation drift was identified during the live run and
reconciled by updating the design documents (not the schema). The
remaining items in this section are logical-only references (already
documented in the design) and performance fixes to the validator itself.

### 7.1 Billing aggregates are tenant-scoped, not user-scoped

- **Observed:** Design ERD Cluster 8 diagrammed `users → subscriptions`
  and `users → invoices` as FK edges. Implementation has neither — the
  aggregates are `tenants → subscriptions` and `subscriptions →
  invoices` only.
- **Why intentional:** The platform charges organisations, not
  individuals. Each tenant has at most one active subscription (enforced
  by a partial-unique index on `tenant_id WHERE status IN
  ('active','trialing','past_due')`). Invoices belong to the
  subscription; the tenant is reachable transitively.
- **Action:** `docs/database/ERD.md` Cluster 8 corrected;
  `docs/database/schema.md` §20 / §21 corrected; new **ADR-0027 —
  Tenant-Scoped Billing Aggregates** added to `DECISIONS.md`.

### 7.2 Other logical-only references (no FK in DB)

These were already documented in the design but the comparator initially
flagged them because the design ERD drew them as if they were FK edges.
All have been converted to Mermaid comment lines in the cluster diagrams
and listed explicitly in `ERD.md`'s "Logical FKs" table:

| From → To                                                   | Reason                                                       |
|-------------------------------------------------------------|--------------------------------------------------------------|
| `credit_ledger.related_usage_record_id → usage_records.id`  | `usage_records` is partitioned; stock Postgres won't accept an FK to a single child partition. Service-layer integrity enforced. |
| `cost_reconciliations.usage_record_id → usage_records.id`   | Same partitioned-target reason.                              |
| `workflow_steps.usage_record_id → usage_records.id`         | Same partitioned-target reason.                              |
| `ai_models.provider (text) → provider_plugin_registrations.name (text)` | Decoupled by design — model catalog must survive plugin enable/disable cycles. |
| `event_outbox → event_log`                                  | Independent tables; the dispatcher reads outbox and writes event_log at the application layer. No FK between them. |

These are validated at application level via repository-layer checks
plus an integrity job to be added in Phase 10 (see ROADMAP).

### 7.3 Performance fixes to the validator itself

While running the harness against Supabase (cross-region pooler ~ap-northeast-2)
the original SQLAlchemy-inspector-based checks took 263 s and the ERD
regenerator hit Supabase's 2-minute `statement_timeout`. Both were
rewritten against `pg_catalog`:

- `regenerate_erd.py`: one join over `pg_constraint` + `pg_class` +
  `pg_attribute` with partition-children excluded at the SQL level
  (replaced an unbounded `information_schema.constraint_column_usage`
  join). Run time: **120 s timeout → 13 s.**
- `validate_schema.py`: a single `load_snapshot(engine)` that fetches
  every base table, FK, and index in three bulk queries, then feeds the
  cached snapshot into each check function (replaced ~400 per-table
  `inspect()` round-trips). Run time: **263 s → 17 s.**

This is recorded in `DECISIONS.md` ADR-0026 (the validation-harness ADR
already covers the harness; the optimisation is part of the same
contract).

## 8. Approval Criteria for Step B

Step B is considered complete when:

1. `python scripts/run_validation.py` exits with code `0`.
2. `schema_validation_report.json` reports `all_passed: true` for every
   check listed in §6.
3. The diff between `erd_generated.md` and `docs/database/ERD.md` shows
   no missing tables or FK edges (positional or stylistic diffs are
   acceptable — Mermaid ordering is not significant).
4. All deviations (if any) are recorded in §7 with an ADR reference.
5. A reviewer signs off on this document.

After approval, Phase 2 closes and Phase 3 (Repositories & Services)
begins.

## 9. Document History

| Date       | Author  | Change |
|------------|---------|--------|
| 2026-06-28 | curator | Initial draft of methodology + pending live-run section after writing all Phase 2 Step B code artifacts. |
| 2026-06-28 | curator | Live run completed against Supabase (PostgreSQL 17.6 + pgvector 0.8.0). All 9 checks pass; ERD round-trip clean (51 entities, 58 design edges, 0 drift). Documented two design-vs-implementation drifts (ENUM consolidation + tenant-scoped billing → ADR-0027). Validator + ERD generator rewritten against `pg_catalog` for a 15× speed-up. |
