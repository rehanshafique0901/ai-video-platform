# Execution Runtime Contract — α8.6 (Increment 4)

> **Type:** Engineering design document (implementation contract). **Not an ADR.**
> The governing decisions live in **ADR-0044** (α8.5x AI runtime architecture),
> **ADR-0045** (AI runtime core freeze — the three planes) and, once this increment
> lands, **ADR-0046** (Execution Runtime boundaries). This document is the reference
> engineers consult while building the persistent Execution Runtime and its tables.
>
> **Status:** Implementation contract for Increment 4 (Execution Runtime & Provenance).
> Builds directly on the resolver contract (`RESOLVER_RUNTIME_CONTRACT.md`) and the
> provider runtime data model (`PROVIDER_RUNTIME_DATA_MODEL.md`).
>
> **One-line purpose:** make the **Execution plane persistent** — every generation
> records its full lifecycle, artefacts, and provenance — while it stays a pure
> *consumer* of immutable plans (Planner) and ordered candidates (Resolver), never a
> planner or a selector.

---

## 0. Why this document exists

Increments 1–3 built the Execution plane as an in-memory pipeline: `GenerateVideo`
plans, resolves, generates, verifies, repairs, assembles, and returns a result — but
persists only the frame/video bytes to storage. Nothing is replayable; there is no
lifecycle, no artefact registry, no provenance trail.

Increment 4 makes that plane **persistent** without letting it absorb responsibilities
that belong to other planes. The well-known failure at this boundary is an "execution
runtime" that quietly starts choosing providers, re-planning shots, or writing directly
into the platform's content library. This contract fixes the boundaries **before** the
schema is frozen by ADR-0046.

```
Planner ── immutable GenerationPlan ──┐
                                       ▼
Resolver ── ordered candidates ──▶ Execution Runtime ──┬── Generation Ledger  (generations + generation_shots)
                                                        ├── Asset Store        (generation_assets)
                                                        ├── Resolution Ledger  (generation_resolution_ledger, α8.5e)
                                                        ├── Model Cache        (model_cache, via IModelManager)
                                                        ├── Execution State     (generations.status machine)
                                                        └── Provenance + Events (component versions, outbox)
```

---

## 1. The Execution Runtime is a persistent consumer

It **consumes** two immutable inputs and **produces** persisted state:

- Input A — an immutable `GenerationPlan` (from the Planner). Execution never edits it.
- Input B — an ordered, explainable candidate list (from the Resolver). Execution
  picks the top eligible candidate and, on failure, may walk the list — but it never
  re-scores or invents candidates.

Output — durable rows: one `generations` aggregate, one `generation_shots` row per shot,
`generation_assets` for every artefact, one `generation_resolution_ledger` entry, updated
`model_cache`/`generations.status`, and outbox lifecycle events.

---

## 2. Invariants (W8.6.1 – W8.6.8) — frozen by ADR-0046

- **W8.6.1 — Execution never chooses providers.** Selection is the Resolver's. Execution
  consumes the ordered candidate list; it contains no `if provider == "..."` logic and
  no scoring.
- **W8.6.2 — Execution never plans shots.** Shot decomposition is the Planner's.
  Execution consumes an immutable `GenerationPlan`.
- **W8.6.3 — Execution consumes immutable plans + ordered candidates only.** It treats
  both as read-only value objects.
- **W8.6.4 — All generated artefacts flow through the Asset Store.** Every frame,
  reference, mask, audio, video, thumbnail, or metadata blob is registered as a
  `generation_assets` row; nothing else in the Execution plane owns media.
- **W8.6.5 — Every execution produces provenance + ledger entries.** A `generations` row
  and a `generation_resolution_ledger` row are written for every run, carrying the full
  component-version set and the ranked candidate list — reproducible after the catalogue
  changes (values, not FKs).
- **W8.6.6 — All model acquisition goes through the Model Manager.** Local weights are
  never fetched ad-hoc; `IModelManager` + `model_cache` own download/verify/register.
- **W8.6.7 — All progress changes go through the Execution State machine.** Status only
  advances through the defined transitions (§4); no ad-hoc status writes.
- **W8.6.8 — `generation_assets` is execution-owned.** Promotion into the platform's
  `media_assets` library is an **explicit** future use case (`PublishGenerationAssets`),
  never a direct write from the Execution Runtime. The two bounded contexts stay
  decoupled (generation domain vs. content/platform domain).

---

## 3. Table ownership (raw-SQL, ORM-less — ADR-0045 F4/F5)

All Increment 4 tables follow the α8.5d/α8.5e pattern: raw-SQL DDL in migration `0012`,
ORM-less, allowlisted in `validate_schema.py`, documented in the ERD (Cluster 12), and
read/written through explicit raw-SQL repositories.

| Table | Role | Cardinality |
|---|---|---|
| `generations` | Execution aggregate + state machine + provenance head | 1 / run |
| `generation_shots` | Per-shot prompt, attempts, verification, repair, accepted asset | 1 / shot |
| `generation_assets` | **Canonical execution artefact registry** (frame/reference/mask/audio/video/thumbnail/metadata) with a `parent_asset_id` lineage graph | N / run |
| `model_cache` | Persistent local-model registry (version/sha/size/backend/tier/capabilities) | 1 / model |
| `generation_resolution_ledger` | Resolution provenance (α8.5e — **reused**, not re-created) | 1 / resolution |

### Ingress-owned columns on `generations` (α9.7, migration `0016`; `identity_id` reclassified α10.0)

`generations` carries five columns the Execution Runtime **does not own and never writes**:

| Column | Owner | Purpose |
|---|---|---|
| `tenant_id`, `owner_user_id` | Generation **ingress** | Who asked for this generation (ADR-0052 D1). Nullable: legacy rows predate ingress and their owner must never be inferred |
| `idempotency_key` | Generation **ingress** | Client-supplied create idempotency, backed by `uq_generations_owner_idempotency_key` (ADR-0052 D4) |
| `request` | Generation **ingress** | The creator's asserted `GenerateVideoRequest`, persisted verbatim so a worker can reconstruct it exactly — the `publish_jobs.content_package` pattern |
| `identity_id` | Generation **ingress** | Which authored world the request's snapshot was taken from (ADR-0055 D4 — a *request fact*, neither a decision nor an execution fact). A provenance **value**, never an FK, so it survives the profile's deletion. Present since `0012` but written by nobody until α10.0, when it was removed from the runtime's upsert enumeration: the runtime supplied `NULL` on every call, so the first status write of a claimed run erased what ingress had recorded |

**Ingress owns identity; the Execution Runtime owns execution state.** Neither writes the
other's columns. The runtime remains completely ownership-blind: `GenerateVideoRequest` carries
no tenant/owner field, `IExecutionRuntimeStore`'s signature is unchanged, and no execution-plane
module knows what a user is.

**Reused, not owned:** the `StorageResolver` (local/s3/r2) is the storage abstraction —
Execution never writes bytes to disk directly. `generation_resolution_ledger.generation_id`
remains a **logical** reference (no DB FK): every pre-Increment-4 ledger row predates
`generations`, so a hard FK would be destructive; the Execution Runtime instead guarantees
in code that a `generations` row exists before it records the ledger.

### `generation_assets` lineage

`parent_asset_id` (self-reference) turns repair into a **graph**, not an overwrite:

```
frame(shot 3)  ──parent──▶  repaired frame  ──parent──▶  upscaled frame
```

This directly supports targeted verification/repair history without losing originals.

---

## 4. Execution State machine (W8.6.7)

```
queued → planning → resolving → generating → verifying → repairing → rendering → exporting → completed
                                     ▲            │
                                     └──repair────┘
any state → failed        (with failure_reason)
any state → cancelled     (external request)
```

`repairing` loops back to `verifying` for the affected shot; `verifying`↔`repairing` is
the only backward edge. `failed`/`cancelled`/`completed` are terminal. The machine is
persisted (`generations.status`) so future work (resume, cancellation, UI progress,
retries, distributed execution) has a durable anchor.

### Where the lifecycle begins (α9.7 — ADR-0052 D2-B)

The runtime's lifecycle now **begins with an already-created `queued` generation supplied by
ingress.** `POST /api/v1/generations` inserts the owned row; a worker claims it (`queued →
planning` CAS under a `generation:<id>` lease) and only then runs the pipeline. **The runtime is
no longer responsible for establishing the existence of a generation — only for executing one.**

Accordingly, `IExecutionRuntimeStore.begin()` is an **idempotent state-initialisation
operation, not a generic upsert** (pre-flight GEN-1). It:

1. **may create** the row when it is absent — the direct-invocation path (`scripts/generate_demo.py`,
   integration tests) remains fully supported, so ingress is not mandatory;
2. **may initialise runtime-owned execution fields** when the row already exists — the ingress path;
3. **must never overwrite** ownership, identity, the persisted `request`, `idempotency_key`,
   `created_at`, or any other ingress-owned metadata;
4. **must never permit** a queued generation to be rebound to another owner or another request;
5. **must remain safe** when called repeatedly for the same `generation_id`, converging on the same
   runtime state.

The `ON CONFLICT (id) DO UPDATE` clause enumerates only runtime-owned columns, so (3) and (4) hold
by construction rather than by convention.

There is **no job-level retry**: `max_attempts = 1` (ADR-0052 D2) is enforced structurally — the
claim is a `queued → planning` CAS and nothing ever writes a row back to `queued`. A generation
abandoned by a crashed worker is *terminalised* to `failed` by the worker's reap phase, never
re-run, because one execution equals one external spend opportunity. Shot-level repair
(`DEFAULT_MAX_ATTEMPTS = 3`) is intra-run and unaffected.

---

## 5. Provenance schema

Every `generations` row records the exact component versions that produced it, so a bug
found months later is attributable to a specific component revision:

`planner_version`, `storyboard_version`, `prompt_builder_version`, `resolver_version`,
`verifier_version`, `repair_version`, `renderer_version`, `score_schema_version`,
`catalogue_version`, `manifest_digest`, plus `execution_mode`, `execution_tier`,
`chosen_provider`, `chosen_adapter`, `seed`. The full structured breakdown (including
per-shot checks) is also mirrored into a `provenance` JSONB column for replay/analytics.

---

## 6. Events (internal, via the existing outbox)

Lifecycle events are emitted through the platform's transactional outbox (no new bus):
`GenerationStarted`, `ShotGenerated`, `VerificationFailed`, `RepairSucceeded`,
`VideoRendered`, `ExportCompleted`. They have **no external consumers** in Increment 4;
they exist so UI/telemetry/analytics/notifications can subscribe later without touching
execution code.

---

## 7. Non-goals (deferred)

- Distributed workers, Redis/Celery/RabbitMQ queues, Kubernetes orchestration.
- Publishing, and any direct write into `media_assets` (see W8.6.8).
- Actual model downloading (no local adapter until Increment 6); Increment 4 ships the
  `model_cache` registry + DB-backed `IModelManager`, not a downloader.
- Resume/cancellation *behaviour* (the state machine is persisted to make it possible).

---

## 8. Change log

| Date | Change |
|---|---|
| 2026-07-25 | Initial contract for Increment 4 (Execution Runtime & Provenance). Q1/Q2 rulings: new execution-owned `generation_assets` (raw-SQL, ORM-less), promotion to `media_assets` deferred to an explicit use case. |
