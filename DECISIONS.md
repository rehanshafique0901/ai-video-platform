# DECISIONS — Architecture Decision Records

> Each entry follows a lightweight ADR template: Context → Decision → Consequences. New decisions are appended (never rewritten in-place); superseded entries are marked rather than deleted. ADRs are mirrored from `ARCHITECTURE.md` §12.

---

## ADR-0001 — Record Architecture Decisions

**Status:** Accepted (Phase 1)
**Context:** The project will outlive any single contributor; non-obvious decisions must be recoverable.
**Decision:** Every significant architectural decision is recorded in this file. Each is numbered, dated by phase, and labelled with one of: Proposed / Accepted / Superseded by ADR-XXXX / Deprecated.
**Consequences:** Slightly more upfront writing; durable architectural knowledge.

---

## ADR-0002 — Monolith-First, Microservice-Ready

**Status:** Accepted (Phase 1)
**Context:** Premature microservices add operational cost without product validation. But long-running AI workloads and the Event Bus point toward eventual splitting.
**Decision:** Ship a single FastAPI application + Celery workers in one repo, deployed as separate containers (api, workers per queue). Module boundaries follow Clean Architecture so any bounded context can be lifted into a dedicated service later without code rewrite.
**Consequences:** Lower day-one ops cost. Strict discipline required on cross-context imports — enforced by `import-linter` in CI.

---

## ADR-0003 — LangGraph as the Workflow / Pipeline Orchestrator

**Status:** Accepted (Phase 1)
**Context:** AI pipelines have many steps, fan-out, retries, and must be resumable.
**Decision:** All Rendering Pipelines (CR-2) and the Workflow Engine (CR-7) are implemented as LangGraph state machines with a Postgres-backed checkpointer.
**Consequences:** Workflow code is graph-shaped and reviewable; pause/resume is free. Adds LangGraph as a hard dependency.

---

## ADR-0004 — AI Provider Plugin System (CR-1)

**Status:** Accepted (Phase 1)
**Context:** A new AI vendor appears almost monthly. Hardcoding any vendor would force shotgun edits across the codebase.
**Decision:** Every external AI vendor implements `BasePlugin` plus a capability ABC (`LLMProvider`, `ImageProvider`, `VideoProvider`, `VoiceProvider`). Registration is via the `@register_plugin` decorator. Discovery also reads Python `entry_points`, enabling out-of-tree third-party plugins.
**Consequences:** Adding a vendor = one new file + one decorator. Layered selection (per-request / project / user / tenant / global) keeps business logic unaware of vendor names.

---

## ADR-0005 — PostgreSQL + Alembic

**Status:** Accepted (Phase 1)
**Context:** Need transactions, JSONB for snapshots, pgvector for embeddings, and a mature migration story.
**Decision:** Postgres 15+ as the primary store. Alembic for migrations. JSONB columns for snapshots and metadata. pgvector for asset-library and agent-memory semantic search.
**Consequences:** Single database technology to operate. JSONB schema must still be Pydantic-validated to avoid drift.

---

## ADR-0006 — Next.js 15 App Router (Server-First)

**Status:** Accepted (Phase 1)
**Context:** SaaS marketing + authenticated app + heavy editor UI on the same domain.
**Decision:** Next.js 15 App Router. Server Components by default; client components for interactive surfaces (timeline, preview). React Query for client cache, Zustand for cross-feature client state, ShadCN UI for primitives.
**Consequences:** First-class SSR + streaming. Editor heavy lifting still happens client-side.

---

## ADR-0007 — Celery + Redis for Async Jobs

**Status:** Accepted (Phase 1)
**Context:** Generation and rendering are long-running and must survive deployments.
**Decision:** Celery with Redis as broker, Postgres as result backend. Beat for scheduled jobs (cost reconciliation, catalogue refresh).
**Consequences:** Operationally familiar. Five priority queues introduced in ADR-0020.

---

## ADR-0008 — Argon2id + JWT Rotation

**Status:** Accepted (Phase 1)
**Context:** Password security and stateless API auth.
**Decision:** Argon2id (`argon2-cffi`) for passwords. Access JWT 15 min + refresh JWT 30 days, with rotation on every use and reuse detection (a re-used refresh token invalidates the family).
**Consequences:** Stateless auth + strong revocation story.

---

## ADR-0009 — Multiple Rendering Pipelines as Registered Strategies (CR-2)

**Status:** Accepted (Phase 1)
**Context:** Different use cases need different generation strategies (stock footage vs AI images + motion vs AI video clips).
**Decision:** Each pipeline is a separate LangGraph graph implementing `RenderingPipeline` and registered in `PipelineRegistry`. Frontend lists available pipelines dynamically.
**Consequences:** New pipeline = one new file. No conditional logic in callers.

---

## ADR-0010 — Split AI Orchestration into 7 Subpackages (CR-3)

**Status:** Accepted (Phase 1)
**Context:** Putting everything into one "ai" package makes failures undiagnosable.
**Decision:** `app/ai/` is split into `agents`, `providers`, `prompts`, `memory`, `tools`, `chains`, `workflows`. Strict downward-only dependency graph enforced by `import-linter`.
**Consequences:** Every layer is testable alone. Slightly more files; vastly easier debugging.

---

## ADR-0011 — Event Bus with Transactional Outbox (CR-4)

**Status:** Accepted (Phase 1)
**Context:** Cross-context coupling must be loose; events must never be lost or duplicated.
**Decision:** Domain events published through `IEventBus`. Default implementation: Redis Streams with consumer groups. Outbox pattern guarantees atomicity with DB writes. Pluggable to NATS / Kafka via additional implementations.
**Consequences:** At-least-once delivery; subscribers must be idempotent. Outbox table needs periodic cleanup.

---

## ADR-0012 — Multi-Storage Provider Plugin (CR-5)

**Status:** Accepted (Phase 1)
**Context:** Different tenants prefer different clouds; multi-region routing needed.
**Decision:** Single `StorageProvider` ABC with implementations: Local, S3, R2, Azure Blob, GCS. `StorageRegistry` resolves the active backend per asset (tenant pin / region / asset-kind override).
**Consequences:** Migrations between clouds are `copy(src, dst)` plus pointer update — no schema change.

---

## ADR-0013 — Project Versioning (CR-6)

**Status:** Accepted (Phase 1)
**Context:** Users expect Canva-style history with restore.
**Decision:** Each edit produces an immutable `ProjectVersion` with monotonic version_number, parent_version_id (branching), snapshot JSON, and cached diff. Project head pointer moves forward; restores create new versions instead of mutating.
**Consequences:** Storage grows linearly with edits; mitigated by autosave coalescing and snapshot delta compression in a later phase.

---

## ADR-0014 — Resumable Workflow Engine (CR-7)

**Status:** Accepted (Phase 1)
**Context:** Workflows can take minutes to hours; redeploys and crashes must not lose progress.
**Decision:** Workflow runs persist checkpoints in Postgres after each LangGraph node. API supports pause / resume / cancel / replay. Crashed runs auto-resume from last checkpoint.
**Consequences:** Strong UX (no "lost" jobs). Checkpoints add write volume; controlled via per-pipeline checkpoint policy (e.g., only at boundaries).

---

## ADR-0015 — Asset Library (CR-8)

**Status:** Accepted (Phase 1)
**Context:** Generated artefacts have value beyond the project that produced them.
**Decision:** Every generated artefact (image / video / music / subtitle / voice / prompt / thumbnail) is auto-persisted to `LibraryAsset` via event subscribers. Searchable by tag + semantic similarity (pgvector). Soft-deleted on user removal.
**Consequences:** Storage growth; mitigated by user-level retention policies in the Billing context.

---

## ADR-0016 — Feature Flag System (CR-9)

**Status:** Accepted (Phase 1)
**Context:** Need to enable / disable providers, pipelines, and UI features without redeploy.
**Decision:** `FeatureFlagProvider` ABC; default `db_flag_provider` (Postgres-backed); optional `unleash_provider`. The DI container consults flags on every plugin lookup so disabled providers are invisible system-wide.
**Consequences:** Adds one DB lookup per provider call; mitigated by per-request cache.

---

## ADR-0017 — Explicit Domain Layer (CR-10)

**Status:** Accepted (Phase 1)
**Context:** AI projects routinely conflate ORM models with domain entities, leading to leaky abstractions and untestable business rules.
**Decision:** `app/domain/` is pure Python: no FastAPI, no SQLAlchemy, no SDKs. Entities, value objects, aggregate roots, domain events. Repositories translate between ORM models and domain entities.
**Consequences:** Slight verbosity at the boundary; complete testability of business rules without infrastructure.

---

## ADR-0018 — AI Model Registry (CR-11)

**Status:** Accepted (Phase 1)
**Context:** Each vendor exposes many models with their own lifecycles (Veo 2 → Veo 3, GPT-4.1 → GPT-5, …). Hardcoding model names would create another shotgun-surgery problem.
**Decision:** A separate `AIModel` aggregate, populated by static seed YAML and runtime discovery via `provider.list_models()`. Lifecycle (available → deprecated → retired) with `successor_id` chains for graceful upgrades. Cost per model is stored on the model, not the provider.
**Consequences:** Provider plugins are leaner. Default model selection has a clear priority chain. Deprecation announcements become a non-event.

---

## ADR-0019 — Immutable AI Cost Tracking (CR-12)

**Status:** Accepted (Phase 1)
**Context:** Billing accuracy, credit fairness, and analytics all require a tamper-proof record of every AI call.
**Decision:** A single `usage_recorder` middleware wraps every provider plugin call. Each call produces exactly one immutable `UsageRecord` capturing provider, model, tokens / images / seconds, estimated_cost, actual_cost, credits_consumed, duration. Direct calls to provider plugins are forbidden (enforced by `import-linter`).
**Consequences:** The recorder is the bottleneck for cost truth; tested heavily. Reconciliation against vendor invoices closes the loop nightly.

---

## ADR-0020 — Five-Tier Priority Queues (CR-13)

**Status:** Accepted (Phase 1)
**Context:** Mixing a paid customer's render with a free user's bulk regeneration leads to SLA violations.
**Decision:** Five Celery queues — `critical`, `high`, `normal`, `low`, `background` — with subscription-tier-driven routing, per-tenant and per-provider token buckets, per-queue DLQs, and admin visibility endpoints. Per-step `queue_hint` lets the Workflow Engine route steps independently.
**Consequences:** Five worker pools to operate. Clear SLAs per tier. Background work runs off-peak without blocking.

---

---

## ADR-0021 — First-Class Idempotency Framework (CR-DB-1)

**Status:** Accepted (Phase 2 Step A)
**Context:** Per-table idempotency columns (`webhook_deliveries.source_event_id`, `usage_records.request_id`, `credit_ledger.idempotency_key`) cover specific surfaces but leave gaps: payment retries, AI generation requests with client-supplied keys, manual workflow retries, and synchronous API mutations. Each new surface that needs idempotency would otherwise grow its own ad-hoc column.
**Decision:** Introduce a dedicated `idempotency_keys` table with `(tenant_id, key, resource_type)` uniqueness, a `request_hash` for body-equality checks, a cached response, an `in_flight | succeeded | failed` status, and an `expires_at` TTL. All unsafe API endpoints honour the `Idempotency-Key` header through a single middleware that touches this table. Per-table idempotency columns (CR-12, webhook deliveries) remain in place as defense in depth.
**Consequences:** One round-trip extra per write request (mitigated by an `ON CONFLICT` upsert). Uniform replay semantics across payments, AI generation, workflows, webhooks, and exports. Predictable cleanup via TTL.

---

## ADR-0022 — Database-Backed Distributed Locks (CR-DB-2)

**Status:** Accepted (Phase 2 Step A)
**Context:** Multiple workers can contend on the same render job, workflow tick, project publish, or server-side timeline mutation. Postgres advisory locks are powerful but session-bound; Redis locks couple us to the broker's availability for safety-critical operations.
**Decision:** Introduce a `distributed_locks` table keyed by a canonical `lock_key` with `owner`, `lease_until`, `heartbeat_at`, and `metadata`. Acquire/steal-after-expiry is a single atomic `INSERT … ON CONFLICT DO UPDATE … WHERE lease_until < now() RETURNING (xmax = 0)` round trip. The holder heartbeats every `lease/3` seconds; a janitor purges orphan rows beyond a 5-minute safety window. Postgres advisory locks and Redis locks remain available for short, hot critical sections; this table is the durable, observable option.
**Consequences:** Locks survive Redis outages. A dedicated DB write/heartbeat per held lock (modest). Releases are auditable; lock state is visible to admins.

---

## ADR-0023 — Audit Log Separate from Event Log (CR-DB-3)

**Status:** Accepted (Phase 2 Step A)
**Context:** The Event Bus's `event_log` records every domain event for replay/debugging, including system-internal events. Compliance teams need a focused, actor-attributed, immutable record of state changes — not the firehose. Conflating the two makes audit forensics expensive and risky.
**Decision:** Introduce a partitioned, immutable `audit_log` table with `actor_id`, `actor_kind`, `entity_type`, `entity_id`, `action`, `before_json`, `after_json`, `ip`, `user_agent`, `request_id`, `correlation_id`. Class C retention (7 years). A single application-layer recorder (`application/audit/record_audit.py`) is invoked inside the same transaction as the underlying mutation — atomicity guaranteed. Detached partitions are exported to cold Parquet at 24 months and dropped at 7 years.
**Consequences:** Two related but separate logs (acceptable separation of concerns). The recorder is now mandatory at every audit-relevant use case; enforced via `import-linter` contract added in Step B.

---

## ADR-0024 — Explicit Configuration Tables (CR-DB-4)

**Status:** Accepted (Phase 2 Step A); **supersedes** the generic `settings` table from the previous revision.
**Context:** Earlier revision used one polymorphic `settings` table with a `setting_scope` ENUM (`user|tenant|project|global`). This obscures schema validation, complicates ON DELETE behaviour (e.g. cascade only when tenant-scoped), and conflates secrets with non-secrets. It also makes querying provider-specific configuration awkward.
**Decision:** Replace the generic `settings` table with three dedicated tables: `system_settings` (platform-wide), `tenant_settings` (per-tenant), and `provider_settings` (per-AI-provider, optionally per-tenant). Each row carries `value_schema_ref` (pointer to the Pydantic schema) and `is_secret` (encrypted before insert). Provider settings have two partial unique indexes covering "tenant-scoped" and "global default" rows. Project-scoped configuration continues to live in `projects.settings jsonb`. Feature-flag overrides remain in `feature_flag_overrides` (unchanged).
**Consequences:** More tables but clearer schemas, sharper indexes, easier validation, cleaner cascade behaviour. Lookup precedence (tenant → global → env → built-in default) is explicit and centralised in the configuration repository.

---

## ADR-0025 — Defer Dedicated `user_preferences` Table

**Status:** Accepted (Phase 2 Step A) — deferred deliberately.
**Context:** With explicit configuration tables (ADR-0024), one might expect a `user_preferences` table for theme/locale/notification toggles. The product surface for user preferences is currently tiny and unlikely to require independent indexing, retention, or auditing.
**Decision:** Defer. Store small per-user preferences in the existing `users.extra jsonb` column. Promote to a dedicated table only when (a) a query pattern requires indexing on a preference field, or (b) preferences need their own retention/audit policy distinct from the user record. The eventual promotion is straightforward: copy from JSONB into a new table, then drop the JSONB key — a single migration.
**Consequences:** Schema stays lean today; we accept a future migration if the product evolves. Acceptable trade-off given current scope.

---

## ADR-0026 — Automated Schema Validation Harness for Phase 2 Step B

**Status:** Accepted (Phase 2 Step B).
**Context:** With 52 base tables (+ 108 monthly partition children), 26 ENUMs, 95 foreign keys, 86 indexes, 8 immutable tables, 4 partitioned parents, and several PL/pgSQL triggers, drift between the hand-authored design documents (`schema.md`, `ERD.md`, `INDEX_STRATEGY.md`) and the implemented schema becomes nearly impossible to detect by code review alone. Catching drift at PR time prevents a class of production incidents (silent FK loss, dropped partitions, missing immutability protection) that would otherwise only surface in a future incident.
**Decision:** Introduce a one-command schema-validation harness (`backend/scripts/run_validation.py`) that, against a pgvector-enabled PostgreSQL instance: (a) applies `alembic upgrade head`, (b) `alembic downgrade base`, (c) re-applies `alembic upgrade head` to prove idempotency, (d) introspects the implemented schema and runs nine structural checks (extensions, table parity, partition coverage, FK presence + `ON DELETE` match, unique constraints, documented indexes including imperative GIN/HNSW, immutability triggers, pgvector column scope, credit_ledger balance trigger), and (e) regenerates a deterministic Mermaid ERD from the implemented schema for diff against `docs/database/ERD.md`. The harness writes a machine-readable `schema_validation_report.json` so it can be wired into CI in Phase 9. Results are recorded in `SCHEMA_VALIDATION.md` §6 with acknowledged deviations in §7.
**Consequences:** Slightly more code to maintain alongside the migrations (~600 lines of validator + runner). One-time setup cost paid for itself the first time a missing imperative GIN index was caught before review. Validator becomes a required CI gate in Phase 9; pre-commit hook added in Phase 3. Live runs require any reachable Postgres 15+ with pgvector (Docker Desktop locally, or a cloud Postgres such as Supabase / Neon / RDS); the harness is platform-agnostic and ships a PowerShell wrapper for Windows operators.

---

## ADR-0027 — Tenant-Scoped Billing Aggregates (Subscriptions & Invoices)

**Status:** Accepted (Phase 2 Step B).
**Context:** During Phase 2 Step A, `schema.md §20` denormalised `user_id` onto both `subscriptions` and `invoices` "for convenience." Implementation in Step B revealed this conflicts with the rest of the architecture: tenants are the billing entity (one paid plan per organisation, members share usage), and the platform never queries "the invoice that user X owns" because users do not own invoices in B2B SaaS. The denormalisation would also have created ambiguity ("which user gets billed when a tenant has multiple owners?") and forced ON DELETE RESTRICT on every user deletion across billing tables.
**Decision:** Subscriptions and invoices are tenant-scoped, not user-scoped:
  - `subscriptions(tenant_id, plan_id)` — one active subscription per tenant (enforced by a partial-unique index on `tenant_id WHERE status IN ('active','trialing','past_due')`).
  - `invoices(subscription_id)` — tenant is reachable via `subscription_id → subscriptions.tenant_id`; not duplicated.
  - `credit_ledger.user_id` is **nullable** with `ON DELETE SET NULL` — system / admin adjustment entries have no specific user; user deletion does not block the ledger (which is immutable anyway).
The `usage_records.user_id` FK is kept as-is (per-user metering remains important for fair-use enforcement and per-seat analytics).
**Consequences:** Cluster 8 in `ERD.md` and §20/§21 of `schema.md` were corrected to match this decision during Step B (commit alongside ADR-0027). One less FK on `users` deletion path, simpler cascade semantics, conventional SaaS pattern. The "which user issued this invoice" question is answered via `audit_log` (an `INVOICE_CREATED` event records the actor), which is the right place because the actor is usually a system process, not a customer. No data migration required (Step B never deployed; this is a pre-launch correction).

---

## ADR-0028 — CI Quality Gate as Hard Prerequisite for Phase 3

**Status:** Accepted (Phase 2 closing → Phase 2C in progress).
**Context:** Phase 2B established a live-validated schema with an automated, fast (~17 s) validator and an ERD round-trip. Before Phase 3 (repositories + services) lands its first line of business logic, the reviewer required that *every* future pull request — including the first repository commit — be automatically gated on the same level of rigor: lint, format, type-check, unit tests, alembic upgrade → downgrade → upgrade idempotency, the schema validator, the ERD comparator, and a coverage threshold. The alternative (introduce gates piecemeal during Phase 3) historically allows broken patterns to land first and harden into "the way we do it here," after which retrofitting is significantly more expensive.

**Decision:** A single 10-stage CI quality gate is shipped *before* the first repository commit. The stages are executed strictly in order and fail-fast:

1. **Lint** — `ruff check` over `app`, `tests`, `scripts` (catches dead imports, name shadowing, comprehension misuse, RUF/B/UP/N/SIM rules).
2. **Format** — `black --check` over the same tree (a non-conforming file fails the gate; no auto-rewrite in CI).
3. **Static analysis** — `mypy` over `app/` in strict mode + `lint-imports` for the existing architecture rules (`app.domain` cannot import infrastructure; `app.infrastructure.db` cannot import api or application).
4. **Unit tests + coverage instrumentation** — `pytest -m unit --cov=app` produces `.coverage` and `coverage.xml`. The Phase 2C smoke suite is deliberately lightweight (~25 tests over metadata, mixins, enums, model imports); the floor will be raised in Phase 3.
5. **`alembic upgrade head`** — forward migration against a pgvector-enabled service container in CI (and against `DATABASE_URL` locally, with `.env.validation` as a fallback).
6. **`alembic downgrade base`** — verifies reversibility; only `alembic_version` may remain afterwards.
7. **`alembic upgrade head`** (second run) — proves idempotency.
8. **Schema validator** — `validate_schema.py` runs the nine pg_catalog-based structural checks introduced in Phase 2B (extensions, table parity, partitions, FKs, unique constraints, indexes, immutability triggers, pgvector scope, credit_ledger balance trigger). Runtime ≈ 17 s.
9. **ERD comparison** — `regenerate_erd.py` + `compare_erd.py` produce a fresh Mermaid ERD and diff it against `docs/database/ERD.md`. Entities must match exactly; every design-declared edge must be present in the implementation.
10. **Coverage gate** — `coverage report` with `fail_under = 60` (deliberately low for Phase 2C; raised to 80 in Phase 3 once repositories arrive).

The pipeline is driven by a single Python entrypoint, `backend/scripts/ci_gate.py`, that the GitHub Actions workflow and a developer's laptop invoke identically. Stages 5–9 are skipped (not failed) when `DATABASE_URL` is unset, so contributors without a local Postgres can still iterate on stages 1–4. `.pre-commit-config.yaml` covers the *fast* subset (hygiene + ruff + black) for git-hook-level enforcement, so a contributor cannot commit code that would fail stages 1–2.

**Consequences:**
- One new file under `.github/workflows/` (`ci.yml`) and one new script under `backend/scripts/` (`ci_gate.py`) become the single source of truth for "what 'green' means." Any change to the gate has to be a PR against these files, which is exactly the level of visibility we want.
- The Phase 2C smoke test suite (`backend/tests/`, ~25 tests) ships now, before any business logic. The tests assert structural properties (no naive datetimes, every FK declares `ON DELETE`, native ENUMs only, etc.) and act as a fitness function against future drift.
- Coverage floor is deliberately 60 % at Phase 2C (the smoke suite touches mixins, enums, model imports, and metadata structure — not enough for 80 %). It is bumped to 80 % in Phase 3 once repositories are testable, and to 85 % at Phase 9 entry.
- Local invocation contract: `python scripts/ci_gate.py` runs every stage; `python scripts/ci_gate.py --stages 1-4` iterates fast; `python scripts/ci_gate.py --stages 5-9` re-runs only the live-DB stages. The runner is cross-platform (PowerShell, bash, zsh).
- The reviewer's instruction "create the CI gate BEFORE writing repositories and services" is honored: ADR-0028 is closed before the first commit under `app/application/` or `app/api/` exists. Phase 3 begins only after this gate has at least one green CI run on `main`.

---

## ADR-0029 — CI Quality Gate Operational Contract (Phase 2C Ratification)

**Status:** Accepted (Phase 2C closeout, 2026-06-29). Supplements ADR-0028.
**Context:** ADR-0028 established *that* a CI quality gate is a hard prerequisite for Phase 3 and described the gate's intent and consequences. This ADR ratifies the *operational contract* of the gate as actually shipped: the exact stage list, ordering, runner, fail-fast semantics, service-container model in CI, local-skip behaviour, architectural-fitness contracts, and the coverage-threshold roadmap across phases. Recording these as a separate ADR (rather than amending ADR-0028) keeps the original ADR's design rationale intact while giving future contributors a single reference card for "what 'green' means today." A duplicate ADR-0028 entry in `DECISIONS.md` (introduced during Phase 2C) has been renumbered to this ADR.
**Decision:** Every pull request to `main` and every push to `main` runs a single CI workflow (`.github/workflows/ci.yml` → `backend/scripts/ci_gate.py`) executing **ten stages, fail-fast, in this exact order**:

| Stage | Check                                | Tool                | DB? |
|-------|--------------------------------------|---------------------|-----|
| 1     | Lint                                 | `ruff check`        | no  |
| 2     | Format                               | `black --check`     | no  |
| 3     | Static analysis                      | `mypy --strict` + `lint-imports` | no  |
| 4     | Unit tests + coverage collection     | `pytest -m unit --cov=app`       | no  |
| 5     | Forward migration                    | `alembic upgrade head`           | yes |
| 6     | Reverse migration                    | `alembic downgrade base`         | yes |
| 7     | Idempotency check                    | `alembic upgrade head` (re-apply)| yes |
| 8     | Live schema validator (9 checks)     | `scripts/validate_schema.py`     | yes |
| 9     | ERD regenerate + design diff         | `scripts/regenerate_erd.py` + `scripts/compare_erd.py` | yes |
| 10    | Coverage threshold enforcement       | `coverage report` (`fail_under`) | no  |

The runner is implemented in Python (`ci_gate.py`) so a developer's local invocation is byte-identical to CI. GitHub Actions provides a pgvector-enabled Postgres 16 sidecar (`pgvector/pgvector:pg16`) for stages 5–9. Locally, `backend/.env.validation` (git-ignored) supplies the connection URI; without it, stages 5–9 are reported as SKIPPED rather than failing — so contributors can iterate on stages 1–4 without a DB. Architectural fitness is enforced by `import-linter` contracts in `pyproject.toml`: `app.domain` cannot import `app.infrastructure` / `app.application` / `app.api`; `app.infrastructure.db` cannot import `app.application` / `app.api`; `app.api` cannot import `app.infrastructure` directly; `app.application` cannot import `app.api`. These contracts go live immediately because the package skeletons (`app/domain/__init__.py`, `app/application/__init__.py`, `app/api/__init__.py`) are created at the close of Phase 2 (empty, intentionally) so the very first Phase 3 commit is validated against them.
**Consequences:** (a) Every database or code change is automatically verified end-to-end, including a live up/down/up migration cycle. (b) Phase 3 code that violates the layered architecture cannot land (lint-imports contracts are part of stage 3). (c) Coverage threshold starts at 60 % (Phase 2 ships ORM + harness only; no business logic to test) and will be raised to 80 % at the close of Phase 3 once repositories carry meaningful unit tests. (d) PR review time drops because reviewers can trust the green check; they focus on logic and design, not on running the validator manually. (e) The gate is a hard prerequisite: no repository, service, router, or other Phase 3 code is written until this ADR is approved.

---

## ADR-0030 — Promote `export_jobs (render_job_id, format, quality, orientation)` Uniqueness to a Partial-Unique DB Constraint

**Status:** Accepted (Phase 3 W1.1, 2026-06-29).
**File:** [`docs/decisions/ADR-0030-export-jobs-partial-unique.md`](docs/decisions/ADR-0030-export-jobs-partial-unique.md)
**Summary:** Promotes the `(render_job_id, format, quality, orientation)` uniqueness invariant from the use-case layer (which had no consumer yet) directly to the database as `uq_export_jobs_render_job_id_format_quality_orientation` with `WHERE status IN ('queued','running','succeeded')`. First ADR stored as a standalone file under `docs/decisions/`; ADRs 0001–0029 remain inline in this document and are not being retro-migrated. See the ADR file for full Context, Decision, 7 Alternatives Considered, Migration Plan, Rollback, Consequences, and 15-item Acceptance Criteria.

---

## ADR-0031 — Promote `idempotency_keys` Mutability Tracking and Status↔Response Invariant to the Database

**Status:** Proposed (Phase 3 W1.2). To be flipped to Accepted in the pre-merge `docs(adr): mark ADR-0031 Accepted` commit after live validation passes.
**File:** [`docs/decisions/ADR-0031-idempotency-keys-invariants.md`](docs/decisions/ADR-0031-idempotency-keys-invariants.md)
**Summary:** Promotes two long-standing application-layer assumptions about `idempotency_keys` to the database. First, corrects the Phase 2 Step-A mixin misclassification that placed the table under `CreatedAtOnlyMixin` (documented for immutable tables) even though rows transition `in_flight → succeeded`/`failed` — adds `updated_at timestamptz NOT NULL DEFAULT now()` and binds the shared `touch_updated_at()` trigger as `tg_idempotency_keys_biu_touch_updated_at`. Second, enforces the status↔response FSM invariant as `chk_idempotency_keys_response_hash_matches_status CHECK ((status = 'in_flight') = (response_hash IS NULL))`. Both changes ship in a single hand-written migration `0004_idempotency_keys_invariants`. See the ADR file for full Context (two distinct issues), Decision (three coordinated changes), 8 Alternatives Considered, Migration Plan, 3-tier Rollback, Consequences (including the application-layer contract change), and 17-item Acceptance Criteria.

---

## Decisions Log Format (for future entries)

```
## ADR-NNNN — Title

**Status:** Proposed | Accepted (Phase N) | Superseded by ADR-MMMM | Deprecated
**Context:** Why this came up.
**Decision:** What was chosen, in one paragraph.
**Consequences:** Concrete trade-offs.
```
