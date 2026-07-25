# Provider Runtime Data Model — ERD, Ownership & Runtime Flow

> **Type:** Engineering design document (implementation blueprint). **Not an ADR.**
> The governing decisions live in **ADR-0041** (provider runtime contract),
> **ADR-0044** (α8.5x AI runtime architecture) and the **α8.5d pre-flight**
> (`PHASE3_ALPHA8_5d_PREFLIGHT.md`). This document is the *reference* engineers
> consult while writing migration `0010`, the seeder, and the α8.5e resolver.
>
> **Status:** Blueprint for α8.5d (pre-implementation). Version target
> `0.4.35-phase3-alpha8.5d-dev`. No runtime change lands from this document.
>
> **One-line purpose:** answer, forever, the question *"where should this field
> live, and who is allowed to write it?"* — so the catalogue never drifts into an
> operational-state dumping ground (invariant **W8.5d.10**).

---

## 0. Why this document exists

α8.5d is no longer "provider metadata." It is becoming the **runtime catalogue for
everything the AI system knows about generation** — what can run, on what hardware,
at what cost, producing what outputs, depending on what other capabilities. Once a
migration lands, columns are expensive to move. Before writing `0010` we lock:

1. **Ownership** — exactly one writer per table (§A).
2. **Flow** — the immutable, idempotent path from YAML to execution (§B).
3. **The catalogue/operational firewall** — invariant **W8.5d.10** (§C).
4. **Stable bounded contexts** — so future work *fills in* components rather than
   inventing new ones (§D).

Everything here upholds the α8.5c/α8.5d contract: **the runtime never reads YAML**
(W8.5c.2 / W8.5d.1); the **DB is the sole runtime source of truth**; the **seeder is
the only writer of catalogue metadata**.

---

## A. Ownership matrix

Two disjoint families of tables. **Catalogue** = declared, design-time, seeder-owned,
immutable at runtime. **Operational** = live, runtime-owned, never in Git.

### A.1 Catalogue tables (seeded by `scripts/seed_providers.py`, migration `0010`)

| Table | Owned by | Written by | Read by | Mutability at runtime |
| --- | --- | --- | --- | --- |
| `capabilities` | Seeder | **Seeder only** | Planner, Resolver | Immutable |
| `capability_dependencies` | Seeder | **Seeder only** | Planner | Immutable |
| `providers` | Seeder | **Seeder only** | Resolver, Cost Estimator | Immutable |
| `provider_adapters` | Seeder | **Seeder only** | Resolver, Planner, Cost Estimator, Execution Runtime | Immutable |
| `adapter_fallbacks` | Seeder | **Seeder only** | Resolver | Immutable |
| `routing_policies` | Seeder | **Seeder only** | Resolver | Immutable |
| `device_profiles` | Seeder | **Seeder only** | Execution Resolver | Immutable |
| `provider_registry_meta` (digest/revision) | Seeder | **Seeder only** | Seeder (idempotency), ops/telemetry | Immutable (rewritten only by a seed run) |

`ai_models` / `ai_model_pricing` are the **existing** model catalogue + authoritative
price. α8.5d flattens variants into `ai_models` (W8.5d.6) and treats
`ai_model_pricing` as the *authoritative* cost source; adapter `cost_*` columns are
**estimation-only hints** (W8.5d.8). Writer remains the seeder / existing migration
path, not the runtime.

### A.2 Operational tables (NOT part of migration `0010`; runtime-owned)

These are listed to fix ownership **now** so no one is tempted to bolt operational
columns onto catalogue tables. They land with α8.5e+ (or already exist), never here.

| Table | Owned by | Written by | Read by | Notes |
| --- | --- | --- | --- | --- |
| `provider_plugin_registrations` (existing) | Runtime | Plugin loader | Resolver | Loaded-code plugins + health surface |
| `provider_health` *(future)* | Runtime | **Health Worker** | Resolver | health score, last_success, 429 frequency |
| `adapter_runtime_metrics` *(future)* | Runtime | Runtime | Resolver | avg latency, success rate, current queue |
| `provider_quota_state` *(future)* | Runtime | Runtime | Resolver | quota *remaining*, window reset |
| `local_runtime_state` *(future)* | Runtime | Execution / device agent | Resolver | GPU availability, loaded models, free VRAM |
| `generation_resolution_ledger` *(future)* | Runtime | Execution (resolver caller) | Analytics, Memory Runtime | per-request provenance: catalogue_version, digest, resolver_version, ordered candidates, chosen adapter |
| `usage_records` (existing) | Runtime | Runtime | Cost Estimator, Billing | authoritative spend |

> These operational tables are **grounded in detail** (columns, cadence, writers) in
> `RESOLVER_RUNTIME_CONTRACT.md` §7 and land with α8.5e (migration `0011+`), never here.

### A.3 Ownership rules (normative)

1. **The seeder is the only writer of catalogue metadata.** No service, worker, job,
   admin action, or UI writes to §A.1 tables.
2. **The runtime never mutates catalogue rows.** Reads only.
3. **The runtime writes only operational state** — and only to §A.2 tables.
4. **The planner never writes.** It reads capabilities + dependencies + adapters and
   emits a plan (its own artifact), never a catalogue mutation.
5. **The resolver never writes.** It *joins* catalogue × operational × pricing at
   query time and returns a selection.
6. **The health worker never edits catalogue metadata.** It writes `provider_health`
   / metrics only.
7. **The UI never edits catalogue metadata.** Catalogue changes happen through a
   manifest edit → validator → re-seed, reviewed in Git.

> Enforcement: catalogue tables SHOULD be written only by a role/connection the
> seeder uses; application DB roles get `SELECT` on §A.1 and `SELECT/INSERT/UPDATE`
> on §A.2. (Grants are an α8.5e hardening item, tracked, not required by `0010`.)

---

## B. Runtime flow

```
   capabilities.yaml   providers.yaml   routing.yaml   devices.yaml
                         │  (design-time, Git)
                         ▼
                    ┌──────────┐
                    │ Validator│   offline, no DB, no network (W8.5c.5)
                    └────┬─────┘
                         ▼
                  ┌───────────────┐
                  │ Manifest Digest│  sha256 over the normalized manifests
                  └──────┬────────┘
                         ▼
                    ┌─────────┐
                    │ Seeder  │   the ONLY catalogue writer
                    └────┬────┘
                         ▼
                 ┌────────────────┐
                 │  Database (§A.1)│  runtime source of truth (W8.5d.1)
                 └───┬────────┬────┘
        (catalogue)  │        │  (operational, §A.2 — separate writers)
                     ▼        ▼
                ┌──────────┐  join at query time
                │ Resolver │◄──────────────┐
                └────┬─────┘               │
                     ▼                     │
                ┌─────────┐          ┌───────────┐
                │ Planner │          │Health/Ops │ writes §A.2 only
                └────┬────┘          └───────────┘
                     ▼
              ┌──────────────┐
              │  Execution   │  local-first → free cloud → paid (AR7)
              └──────────────┘
```

Per-arrow contract:

| Arrow | Writer | Reader | Immutable? | Idempotent? |
| --- | --- | --- | --- | --- |
| YAML → Validator | authors (Git) | Validator | source is versioned | yes (pure) |
| Validator → Digest | Validator | Seeder | — | yes (same input ⇒ same digest) |
| Digest → Seeder | Seeder | Seeder | — | **yes** — matching digest ⇒ no-op (W8.5d.2) |
| Seeder → DB (§A.1) | **Seeder only** | — | rows immutable post-seed | **yes** — upsert converges DB→manifest (W8.5d.5) |
| DB → Resolver | — | Resolver | catalogue read-only | reads are pure |
| DB(§A.2) → Resolver | Health/Runtime | Resolver | operational, mutable | n/a (live) |
| Resolver → Planner | — (returns value) | Planner | — | selection is deterministic given state |
| Planner → Execution | — (plan artifact) | Execution Runtime | — | resumable/checkpointed (AR14/AR17) |

**Key firewall:** the only path *into* §A.1 is `YAML → Validator → Seeder`. The only
path *into* §A.2 is the runtime. They meet **only** at the resolver's read-time join.

---

## C. Runtime invariants (the catalogue/operational firewall)

Alongside **W8.5d.1–9** (see the α8.5d pre-flight), this document introduces:

> ### W8.5d.10
> **Runtime operational state shall never be stored in catalogue metadata (and
> catalogue metadata shall never be written by the runtime).**

| ✅ Catalogue (§A.1 — seeded, immutable) | ❌ Operational (§A.2 — runtime, never in catalogue) |
| --- | --- |
| provider / adapter identity, name, license | health score |
| capabilities + capability dependencies | average latency (`average_latency_ms`) |
| supported outputs / formats, features | success rate / failure rate |
| hardware requirements, resource estimates | quota **remaining**, window reset |
| declared quota **ceilings**, pricing model | current queue depth |
| cost **hints**, device profiles, routing policy | `last_success` / `last_request` |

If a proposed field describes *what a provider is* → catalogue. If it describes *how a
provider is behaving right now* → operational. When in doubt, it is operational.

Corollaries already in force: W8.5d.1 (DB is source of truth), W8.5d.3
(non-destructive seeding), W8.5c.3 (static-only in Git).

---

## D. Reserved bounded contexts

Named now to give the architecture **stable boundaries**. Most contain no code yet;
future capabilities are added *inside* these contexts, not as new inventions.

| Context | Responsibility | Writes | Status |
| --- | --- | --- | --- |
| **Provider Catalogue** | seed + serve the static catalogue (§A.1) | §A.1 (seeder) | α8.5d (this slice) |
| **Provider Resolver** | scoring, routing, health-aware selection, fallback execution | nothing (reads only) | α8.5e |
| **Planner** | prompt → structured project; capability-graph construction; provider *requests* | plan artifacts | α8.5x-mrc |
| **Execution Runtime** | run a chosen adapter; local-first; device matching | §A.2 metrics | α8.5x-exec |
| **Verification Runtime** | CLIP/face/prompt similarity; repair loop | verification records | α8.5x (MRC-6) |
| **Identity Runtime** | character sheets, seeds, reference images, voice (AR2/MRC-4) | identity records | α8.5x |
| **Memory Runtime** | project memory: assets, provider ledger, costs, failures (AR16/AR18) | project memory | α8.5x |
| **Publishing** | YouTube/TikTok/Instagram adapters (separate registry) | publish records | α8.6 |
| **Analytics** | cost analysis, provider comparison, telemetry | analytics store | later |

These names are **not** a request to create empty modules today; they are the agreed
vocabulary so ownership discussions (and this matrix) stay stable as the platform
grows.

---

## E. Implementation order (α8.5d)

Strictly phased; each phase is independently reviewable and the earlier phases are
fully verifiable **offline** (important given the local Docker/live-DB limits — see §F).

**Phase 1 — Migration `0010` (schema only, no data)**
1. Postgres enums (`provider_pricing_enum`, `adapter_status_enum`,
   `routing_strategy_enum`, `fallback_mode_enum`, `selection_enum`,
   `gpu_backend_enum`, `generation_mode_enum`; `kind` reuses `plugin_kind_enum`).
2. Catalogue tables (§A.1) — columns per pre-flight §5 (D-B tiers: stable⇒typed,
   semi-structured⇒JSONB, highly-variable⇒own tables).
3. Indexes (FKs, `capability_id`, `provider_id`, adapter `status`+`enabled`).
4. Constraints (PK/unique/NOT NULL) and **FKs** (`adapter_fallbacks`,
   `capability_dependencies`, `provider_adapters`).
   → *Down-migration drops it all; `0010` touches no existing table (additive).*

**Phase 2 — Seeder (`scripts/seed_providers.py`)**
1. Load manifests via `provider_manifest.py`; **re-run the validator** first (a seed
   never trusts an unvalidated manifest).
2. Compute the **manifest digest**; compare to `provider_registry_meta.digest`.
3. **Revision tracking** — bump `revision`, stamp `seeded_at`.
4. **Idempotent upsert** in dependency order (capabilities → providers → adapters →
   fallbacks → routing → devices); variants flattened into `ai_models` (W8.5d.6);
   removed entries disabled/deprecated, never deleted (W8.5d.3).
5. Matching digest ⇒ **no-op** (W8.5d.2).

**Phase 3 — Verification (CI, rides the stage 5–9 hardening seam)**
```
seed → seed again → 0 rows changed (idempotency, W8.5d.2)
     → DB == manifest for digest (round-trip, W8.5d.5)
     → digest verification
```

**Phase 4 — Runtime reads**
Only **after** the catalogue exists does any runtime component (α8.5e resolver) begin
reading from it. No runtime read is added in α8.5d itself.

Only then start **α8.5e** (resolver).

---

## F. Local environment note (does not block α8.5d)

Docker is unavailable and the shared live DB has been intermittently unreachable in
the current environment. That blocks only the DB round-trip (gate stages 5–9), not
the bulk of α8.5d. Therefore:

- **Implement + verify now (offline):** migration `0010`, the seeder, validator
  integration, unit tests, and offline verification (schema builds, seeder logic
  against a fixture/in-memory or SQLite-shaped harness where feasible).
- **Defer to a proper environment:** run stages 5–9 (seed round-trip against
  Docker `pgvector/pgvector:pg16` via `--ephemeral-db`, or a stable Postgres/Supabase)
  **before** treating α8.5d as release-ready — same release bar every α8.x slice has
  met (all required gates green before dropping `-dev`).

---

## Change log

| Date | Change |
| --- | --- |
| 2026-07-25 | α8.5d complete (schema + seeder + live-PG round-trip). Grounded two more §A.2 operational tables (`local_runtime_state`, `generation_resolution_ledger`) ahead of α8.5e; full column-level grounding + the resolver selection contract now live in `RESOLVER_RUNTIME_CONTRACT.md`. No runtime change. |
| 2026-07-25 | Initial blueprint — ownership matrix (§A), runtime flow (§B), invariant **W8.5d.10** (§C), reserved bounded contexts (§D), implementation order (§E), local-env note (§F). Pairs with the SIGNED-OFF α8.5d pre-flight; no runtime change. |
