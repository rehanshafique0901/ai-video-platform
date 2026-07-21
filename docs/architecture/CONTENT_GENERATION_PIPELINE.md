# Content Generation & Rendering Pipeline — Architectural Blueprint

> **Status:** **SIGNED OFF (2026-07-16).** Runtime decisions D1–D9 accepted (§14).
> This blueprint is the **architectural contract for the remainder of Phase 3**
> (orchestration era, α7+) — the pipeline's equivalent of what
> [`ADR-0035`](../decisions/ADR-0035-project-version-snapshots.md) is to
> versioning. The design phase is complete; the first slice is **α7.1 —
> `RenderJob` Aggregate**.
> **Scope:** the stable design that sits **above** all orchestration slices
> (α7+). This document is to the pipeline what
> [`ADR-0035`](../decisions/ADR-0035-project-version-snapshots.md) was to the
> versioning subsystem: a blueprint that individual slices implement without
> re-litigating the shape.
>
> **This document does not introduce new architecture.** The target was already
> set in [`ARCHITECTURE.md`](../../ARCHITECTURE.md) §7 (data flow), §8 (plugin
> system), §8a (rendering pipelines), §8b (AI orchestration split), and the
> physical substrate was provisioned in Phase 2
> ([`docs/database/schema.md`](../database/schema.md) §16–§18, §25, §27.3, §31,
> §32). This blueprint **bridges** the two: it maps what is implemented today to
> that target, pins the runtime lifecycle and state machines against the tables
> that already exist, and sequences the work into reviewable slices — all under
> the same **zero-migration, aggregate-boundary, OCC-discipline** rules that
> governed α5–α6.

---

## 1. Where we are vs. where this leads

The project just completed the **domain-modelling** era (α5–α6): Projects,
Scenes, Versioning (capture/restore/diff/branch), Prompts, Media, and the
Timeline aggregate (Timeline → Tracks → Clips). Every one of those is a
**state-management** aggregate with a synchronous, in-process use-case layer and
a request-scoped Unit of Work.

The pipeline era is different in kind. It is **workflow orchestration**:
long-running, resumable, multi-step processes that call external AI providers,
survive worker crashes, retry, and emit progress — none of which the current
synchronous request/response layer does yet.

| Dimension | α5–α6 (done) | α7+ (this blueprint) |
|---|---|---|
| Execution | Synchronous, one HTTP request | Asynchronous, background workers, minutes-to-hours |
| Failure model | Transaction rolls back | Checkpoint + resume; per-step retry; fallback chains |
| Concurrency | OCC on one aggregate row | Distributed locks + idempotency keys + outbox |
| External I/O | None (DB only) | AI provider plugins, storage backends, webhooks |
| State | Aggregate rows | Aggregate rows **+** job/run rows **+** event stream |
| New tables | Reused existing schema | **Still reuses existing schema — zero migrations** |

**Load-bearing fact:** the schema for all of this already exists and is
migrated. `workflow_runs`, `workflow_steps`, `workflow_checkpoints`,
`render_jobs`, `export_jobs`, `event_outbox`, `idempotency_keys`,
`distributed_locks`, `usage_records`, and `provider_settings` are live tables
(Phase 2, ADR-0027 baseline). α7+ builds the **application + infrastructure**
layers over them; it does not design new persistence unless a §14 decision says
otherwise.

---

## 2. Lifecycle of a video (end-to-end narrative)

This is the canonical story every slice serves. It is the concrete form of
`ARCHITECTURE.md` §7.

```
Author intent
   │  (Project + Scenes + Prompts already exist as aggregates)
   ▼
1. START      POST /projects/{id}/workflows { pipeline_id, inputs }
              → WorkflowRun created (status=queued), checkpoint step:0,
                WorkflowStarted emitted, background task enqueued.
   ▼
2. GENERATE   Workflow engine walks the pipeline graph (A/B/C), node by node:
              Script → Analyze → Storyboard → SceneSplit → Prompts
              → [Image | Video | Voice | Subtitle | Music] provider calls
              Each produced asset is persisted as a media_asset (source=generated)
              and (optionally) surfaced into the Asset Library.
              Every node writes a checkpoint and emits a domain event.
   ▼
3. ASSEMBLE   Generated media_assets are placed onto the Timeline aggregate
              (Tracks + Clips referencing media_asset_id) — the α6.3 model.
   ▼
4. RENDER     RenderJob created against a Timeline snapshot; a render worker
              consumes the timeline, resolves clips → media, applies
              transitions/effects, muxes with FFmpeg, writes an output media_asset.
   ▼
5. EXPORT     ExportJob(s) per {format, quality, orientation} produce the
              downloadable artefact via a Storage Provider; download feed updated.
   ▼
6. PUBLISH    (optional, α7 binding) freeze the render against a ProjectVersion
              for reproducibility; expose a shareable/publishable artefact.
```

Every transition in steps 1–6 publishes a domain event (§6 event catalogue in
`ARCHITECTURE.md`) through the **outbox** so state and notification commit
atomically. A WebSocket bridge streams those events to the editor.

---

## 3. The orchestration substrate (existing tables → responsibility)

The single reference map. Column shapes are authoritative in
[`schema.md`](../database/schema.md); this table pins **who owns what** at
runtime.

| Table (schema §) | Aggregate / role | Owns | OCC / integrity |
|---|---|---|---|
| `workflow_runs` (§16) | `WorkflowRun` root | one generation run's lifecycle, `input_snapshot`, `output_summary`, `error` | `UNIQUE(project_id, idempotency_key)`; status ENUM |
| `workflow_steps` (§16) | child of run | per-node status, `retries`, `input`/`output`/`error` | `UNIQUE(run_id, step_index)` |
| `workflow_checkpoints` (§16) | child of run | opaque resume state (LangGraph or custom); **immutable** | append-only (reject-mutation trigger) |
| `render_jobs` (§17) | `RenderJob` root | render of a timeline → output `media_asset`; `queue`, `priority`, `progress`, `error` | `version` (OCC); `UNIQUE(project_id, idempotency_key)` |
| `export_jobs` (§17) | `ExportJob` root | per-format artefact, `download_count`, `file_size_bytes` | `version` (OCC); partial-unique `(render_job_id, format, quality, orientation)` (ADR-0030) |
| `event_outbox` (§25) | transactional outbox | domain events pending publish; `metadata` carries correlation/causation/trace | relay: `FOR UPDATE SKIP LOCKED WHERE published_at IS NULL` |
| `idempotency_keys` (§31) | dedupe ledger | `(resource_type, key)` → in-flight/succeeded/failed; response hash | `idempotency_status` + response-hash CHECK (ADR-0031) |
| `distributed_locks` (§32) | lease lock | single-writer guarantees per `lock_key` | lease CHECK `lease_until > acquired_at` (ADR-0032) |
| `usage_records` (§18, partitioned) | `UsageRecord` (immutable) | one row per provider call (tokens/seconds/cost) | append-only; per-child unique `(request_id)` (ADR-0033) |
| `provider_settings` (§27.3) | settings | per-provider (optionally per-tenant) config/secrets | `version` (OCC); split partial-unique indexes |
| `media_assets` (§12) | `Media` roots | generation **outputs** and render/export **outputs** | `UNIQUE(storage coords)`; α6.2 aggregate |
| `ai_models` / `ai_model_pricing` (§15) | `AIModel` | model registry + pricing for usage costing (CR-11) | `model_status`; immutable pricing ledger |

**Rule of thumb:** aggregates already modelled in α5–α6 keep their existing
concurrency contracts. The **new** roots (`WorkflowRun`, `RenderJob`,
`ExportJob`) get their own OCC via their `version`/uniqueness columns and are
**never** folded into `projects.version` or `timelines.version` — consistent
with the aggregate-isolation principle behind ADR-0035 and ADR-0038.

---

## 4. State machines (authoritative transitions)

ENUM values are fixed by the schema (§ reference table). This section pins the
**legal transitions** so every slice enforces the same guard rails.

### 4.1 `workflow_runs.status` (`workflow_status`)
```
queued ──▶ running ──▶ succeeded
   │          │  ▲
   │          │  └── paused ──▶ running        (resume from last checkpoint)
   │          ├──▶ failed                       (terminal unless retried → new run or reset)
   └──────────┴──▶ canceled                     (terminal)
```
- `paused` is the resumability hinge: a worker crash or explicit pause leaves
  the last `workflow_checkpoint` authoritative; `resume` replays from there with
  **no repeated work**.
- Terminal states: `succeeded`, `failed`, `canceled`.

### 4.2 `workflow_steps.status` (`step_status`)
```
pending ─▶ running ─▶ succeeded
              │  ▲
              │  └── retrying ─▶ running        (retries++ up to policy cap)
              ├─▶ failed                         (propagates to run.failed unless step is optional)
              └─▶ skipped                        (conditional pipeline branch not taken)
```

### 4.3 `render_jobs.status` (`render_status`)
```
queued ─▶ running ─▶ succeeded   (output_media_asset_id set)
   │         ├─▶ failed          (error envelope: {code,message,trace_id,retries})
   └─────────┴─▶ canceled
```

### 4.4 `export_jobs.status` (`export_status`) — identical shape to render
```
queued ─▶ running ─▶ succeeded   (output_media_asset_id set, file_size_bytes recorded)
   │         ├─▶ failed
   └─────────┴─▶ canceled
```

**Invariant:** a status may only advance along an arrow above. Every advance is
a fenced write (OCC `version` bump on render/export; `UNIQUE(run, step_index)`
on steps) and emits the corresponding domain event.

---

## 5. Provider abstraction (reference, not redefinition)

The plugin system is **already specified** in `ARCHITECTURE.md` §8. This
blueprint only pins the runtime resolution/costing path so the generation slice
implements it consistently.

- **Contract:** `BasePlugin` + one capability ABC
  (`LLMProvider` / `ImageProvider` / `VideoProvider` / `VoiceProvider`),
  `plugin_kind ∈ {llm, image, video, voice}`. Registration is a single
  `@register_plugin(...)` decorator (`ARCHITECTURE.md` §8.1–§8.2).
- **Selection precedence** (§8.3): per-request override → project default →
  user/tier default → tenant/global default → configurable **fallback chain**
  (`NoHealthyProvider` if exhausted). Feature flags (CR-9) gate overrides.
- **Config precedence** (schema §27.3): `provider_settings` tenant row →
  global row → env var → built-in default. `is_secret` values are KMS-encrypted
  ciphertext at rest.
- **Costing:** every **terminal** provider call is wrapped by the Usage Recorder
  (`application/use_cases/usage/usage_recorder_service.py`, α7.5 / CR-12 / ADR-0041
  D13) which writes one `usage_records` row priced against `ai_model_pricing`
  (`estimated_cost = Σ(unit_price × quantity)`), **idempotent on `request_id`**
  (ADR-0033 per-partition unique index; a replay returns the existing row). As of
  α7.6 the runner calls it **in-transaction** via `record_usage_in_uow(...)` right
  after a terminal dispatch (`SUCCEEDED`/`FAILED`), so the usage row commits or rolls
  back atomically with the step. Scoped for α7.5: the
  `credit_ledger` debit is deferred (`credits_consumed = 0`), missing pricing never
  blocks (cost 0, `pricing_id` NULL, WARN), only terminal outcomes are recorded
  (`IN_PROGRESS` is recorded later by the α8.3 completion service under the same
  `request_id`), and no `UsageRecorded` event is emitted (no consumer). The recorder
  is **purely observational** — its only write is `usage_records` (W7.5.1).

The provider layer is a **leaf** (`ARCHITECTURE.md` §8b): `providers` never
imports `agents`/`workflows`; enforced by `import-linter` in the CI gate.

---

## 6. Queue & worker architecture

Per `ARCHITECTURE.md` (Workflow Engine = LangGraph + Celery + Redis) and the
`render_jobs.queue` five-tier design (CR-13):

- **Queues:** `critical, high, normal, low, background` with an integer
  `priority` for secondary ordering inside a queue.
- **Worker roles** (all stateless, horizontally scalable):
  1. **Workflow worker** — executes one LangGraph tick per `workflow_run`,
     holding `distributed_locks: workflow_run:<id>`.
  2. **Render worker** — consumes a `render_job`, holding `render_job:<id>`.
  3. **Outbox relay** — publishes `event_outbox` rows
     (`FOR UPDATE SKIP LOCKED`), marks `published_at`.
  4. **Lock janitor** — reclaims expired leases (`lease_until < now()`).
  5. **Webhook/ingress worker** — provider callbacks (async video jobs),
     idempotent on `idempotency_keys(resource_type='webhook')`.
- **Progress:** workers advance `render_jobs.progress` (0.00–100.00) and emit
  progress events; the frontend consumes via the WebSocket bridge.

---

## 7. Concurrency & correctness (the three guarantees)

The pipeline's correctness rests on three mechanisms that already have DB
support and ADRs:

1. **Single-writer via leases** (`distributed_locks`, ADR-0032). Canonical
   keys: `render_job:<id>`, `workflow_run:<id>`, `project_publish:<id>`,
   `timeline_edit:<id>`. Atomic acquire/steal-after-expiry in one round trip.
2. **Exactly-once effects via idempotency** (`idempotency_keys`, ADR-0031).
   `resource_type ∈ {payment, ai_generation, export_job, workflow_retry,
   webhook}`. A retried request replays the stored response instead of
   re-executing the side effect.
3. **Atomic state+event via the outbox** (`event_outbox`, CR-4). Domain state
   and the event that announces it commit in the **same transaction**; the relay
   publishes after commit. No lost or phantom events.

Aggregate OCC (`version` columns) remains the inner guard for individual row
updates, exactly as in α5–α6.

### 7.1 Aggregate ownership & the no-cross-mutation rule (D9)

Every orchestration aggregate **owns exactly one state machine** and is the
**sole writer** of its own status. This generalises the aggregate-isolation
discipline of ADR-0035 (Projects) and ADR-0038 (Timeline) to the pipeline roots:

| Aggregate | Owns the lifecycle of |
|---|---|
| `Project` | project lifecycle (draft/active/archived/deleted — α5) |
| `Timeline` | timeline/track/clip composition (self-contained OCC — α6.3) |
| `WorkflowRun` | workflow lifecycle (§4.1) + its steps/checkpoints |
| `RenderJob` | render lifecycle (§4.3) |
| `ExportJob` | export lifecycle (§4.4) |

**Invariant: no aggregate may directly mutate another aggregate's state.**
Cross-aggregate coordination flows **only** through domain events on the outbox:

```
RenderJob  ──(RenderFinished)──▶  event_outbox  ──▶  Workflow Runner  ──▶  ExportJob
   (owns its own status)                (relay)         (reacts; starts)     (owns its own status)
```

A step in a workflow does not reach in and flip a `render_jobs.status`; it
*starts* a `RenderJob` (which drives its own machine) and *reacts* to the
`RenderFinished`/`RenderFailed` event. This keeps the orchestration graph loosely
coupled, makes every transition observable, and means a new consumer (analytics,
notifications, a future publish step) subscribes to events without touching the
producing aggregate. It is the same one-way-dependency rule the CI `import-linter`
already enforces at the package level, applied to runtime state.

---

## 8. Generation flow — prompt → provider → `media_assets`

```
Prompt (α6.1 aggregate)  ──▶  Agent (ai/agents/*)  ──▶  Provider plugin (ai/providers/*)
                                                             │  (Usage Recorder wraps the call → usage_records)
                                                             ▼
                                                   raw bytes + metadata
                                                             ▼
                                Storage Provider (CR-5) writes to bucket/key
                                                             ▼
                          media_assets row (source=generated, kind, model_id,
                          provider, storage coords, checksum, dims/duration)
                                                             ▼
                          (optional) library_assets entry (CR-8, reusable)
                                                             ▼
                                  MediaFinished event → outbox
```

- Output registration reuses the **α6.2 Media aggregate** and its register-by-
  metadata contract (ADR-0037). The pipeline is a *producer* into the same
  table the user's uploads land in; `source=generated` distinguishes them.
- `model_id` is `RESTRICT` on `media_assets` for audit integrity — the pipeline
  must resolve a registered `ai_models` row before persisting output.

---

## 9. Render flow — Timeline → resolved media → output asset

```
render_jobs row (timeline_id, pipeline='ffmpeg', queue, priority, status=queued)
        │  lock: render_job:<id>
        ▼
Render worker loads the Timeline aggregate (Tracks + Clips, α6.3)
        │  resolves each clip.media_asset_id → media_assets (storage coords)
        │  orders clips by start_seconds; overlaps allowed (α6.3 policy)
        ▼
Compose: trims (source_start/end), transitions*, effects*, audio mix, subtitle burn
        │  (*transitions/effects write paths are α6.4 — see §12/§14)
        ▼
FFmpeg mux → output file → Storage Provider → media_assets (kind=video, source=generated)
        ▼
render_jobs.output_media_asset_id set; status=succeeded; RenderFinished → outbox
```

- The render consumes the **timeline as-is**. Whether it renders against a
  *frozen* `project_version` (reproducibility) or the live timeline is a §14
  decision (α7 timeline-version binding).
- `render_jobs.timeline_id` is `ON DELETE RESTRICT` — a timeline with a render
  history cannot be hard-deleted out from under its jobs.

---

## 10. Export & publish flow

- **Export:** one `export_jobs` row per `{format, quality, orientation}`
  (`export_format ∈ {mp4, mov, gif, webm}`, `export_quality ∈ {sd, hd_1080p,
  qhd_2k, uhd_4k}`, `export_orientation ∈ {horizontal, vertical, square}`).
  Partial-unique constraint (ADR-0030) permits retries after `failed`/`canceled`
  but dedupes live/succeeded configs. Produces a downloadable `media_asset`;
  tracks `download_count`, `last_downloaded_at`, `file_size_bytes`.
- **Storage Providers (CR-5):** `local, s3, r2, azure_blob, gcs` behind the
  same plugin discipline as AI providers.
- **Publish (optional, α7):** serialised by `project_publish:<id>` lock; freezes
  the artefact against a `ProjectVersion` for a stable, shareable output.

---

## 11. Retries & failure handling

| Layer | Mechanism | Terminal behaviour |
|---|---|---|
| Provider call | fallback chain (§5) + Usage Recorder records `failed`/`timeout` | `NoHealthyProvider` → step fails |
| Workflow step | `workflow_steps.retries` up to policy cap; `retrying → running` | step `failed` → run `failed` unless step optional (`skipped`) |
| Workflow run | resume from last `workflow_checkpoint`; `workflow_retry` idempotency | manual retry spawns fenced re-run |
| Render/export | structured `error` envelope `{code,message,trace_id,retries}` | `failed`; user may re-queue (new job row) |
| Poison / stuck | lock lease expiry → janitor reclaim; DLQ for repeatedly-failing tasks | surfaced as run/job `failed` with trace id |

Failure is always **observable** (structured error + trace id + event) and
**recoverable** (checkpoint/resume or a fresh fenced job) — never a silent hang.

---

## 12. Ownership & tenancy

- Tenancy is reachable via `project_id → projects.tenant_id` (the workflow/render
  rows deliberately do not duplicate `tenant_id`; schema §16/§17 reconciliation).
- `export_jobs.requested_by_user_id` is `RESTRICT` — the requesting user is an
  audit anchor for the downloads feed and quota reconciliation.
- RBAC (`auth_role`) gates who may start workflows, cancel jobs, and manage
  `provider_settings`; enforced with the α-series `CurrentUserDep` pattern
  (ADR-0034).

---

## 13. Proposed slice sequencing (α7+)

Design-first, one reviewable slice at a time, each with a pre-flight doc and its
own ADR where it makes a load-bearing choice — the α5/α6 rhythm. Sequencing is
**accepted** (D8); α7.1 is the entry point.

| Slice | Theme | Core deliverable | New tables? |
|---|---|---|---|
| **α7.1** ✅ | `RenderJob` aggregate + API | create/list/get/cancel render jobs; OCC via `version`; lock key wiring; **no worker yet** (synchronous stub renderer or `queued`-only) — shipped `v0.4.15` | none |
| **α7.2** ✅ | `WorkflowRun` aggregate + API | start/get/cancel/**synchronous deterministic runner**; `workflow_steps`/`checkpoints`; state machine §4.1–4.2 — shipped `v0.4.16` | none |
| **α8.0** ✅ | **Provider Runtime Blueprint** (docs-only) | locks the runtime *contract* every α7.3→α8.x slice implements against — **ADR-0041** (ProviderPort, registry, dispatcher, completion service, lock manager, relay, retries, workers, media/usage seams). No code/branch/migration/version bump | none |
| **α7.3** ✅ | Outbox relay + **Lock manager** | **library-only** `RelayService.relay_once() -> RelayResult` publishes the events α7.1/α7.2 already produce through a `PublisherPort` (`FOR UPDATE SKIP LOCKED` → `published_at`; poison rows parked in-place after `max_attempts`); first `distributed_locks` consumer (owner-fenced acquire/renew/release + explicit `reclaim_expired()`, steal-after-expiry, ADR-0032). **No worker/daemon/broker** — the worker loop is α8.1 — ADR-0041 D8/D9 — shipped `v0.4.17` | none |
| **α7.4** ✅ | Provider plugin skeleton | four async capability protocols + framework-free registry (explicit registration + capability discovery) + `StepCommandDispatcher` (closed table, four kinds; render/export/storage excluded) + typed provider-error hierarchy + immutable `ProviderMetadata`; **one** deterministic mock per kind (video models the async `IN_PROGRESS` + `provider_job_id` path); minimal read-only `provider_settings` seam (tenant-shadows-global); `import-linter` strict leaf on `infrastructure.ai.providers`. **No HTTP/keys/external calls/Redis/Celery/retries/fallback/usage/events/polling/webhooks** — runner untouched — ADR-0041 D1–D4 — shipped `v0.4.18` | none |
| **α7.5** ✅ | Usage Recorder | `UsageRecorderService` (application seam) turns one **terminal** provider call into one immutable, priced `usage_records` row — priced `Σ(unit_price × quantity)` against `ai_model_pricing`, idempotent on `request_id` (ADR-0033 insert-in-SAVEPOINT + recover), purely observational (W7.5.1). **Deferred:** `credit_ledger` debit (`credits_consumed = 0`), `UsageRecorded` event, and non-terminal recording (α8.3). Missing pricing never blocks (cost 0, WARN). Not yet wired into the runner (α7.6). — ADR-0041 D13 — shipped `v0.4.19` | none |
| **α7.6** ✅ | First pipeline (mock provider) | runner (`AdvanceWorkflowRun`) now **interprets** a step's `StepCommand`s: mints a deterministic `request_id` (`run_id:step_index:command_index`, D5), dispatches each **exactly once** (W7.6.2) via the injected `ProviderDispatcherPort`, records **terminal** usage in the **same** transaction (`record_usage_in_uow`, Q5), and either **pauses** on `IN_PROGRESS` (`running → paused` + `WorkflowRunPaused` + checkpointed `provider_job_id`, Q2/Q8) or checkpoints the **opaque** provider envelope (W7.6.1 — never inspects the payload). Two pipelines: `generate-image@1.0.0` (prompt → mock image → priced usage → checkpoint → succeeded) and `generate-video@1.0.0` (mock → `IN_PROGRESS` → pause; nothing beyond pause). Fail-fast on missing `model_id` (Q4). **No broker/HTTP/real providers/polling/webhooks/media rows** (Q7) — mocks behind the dispatcher — ADR-0041 D4/D11/D13 — shipped `v0.4.20` | none |
| **α8.1** ✅ | Image provider | first **real** `ImageProvider` — synchronous **`OpenAIImageProvider`** (`dall-e-3`, URL response) behind the α7.4 dispatcher; one HTTP call per dispatch (W7.6.2), HTTP status → typed `ProviderError` (D10), registry composed by config (`OPENAI_API_KEY` → real, else mock — one provider per capability). **Nothing above the leaf changes** (W8.1.2); adapter is configuration-blind (W8.1.1) and observationally equivalent to the mock (W8.1.3). **Broker deferred:** the blueprint's Celery/Redis were re-slotted out of α8.1 — the runner still drives synchronously; a worker/broker arrives with async completion (α8.3). **No storage/media/polling/webhooks/selection.** Zero migration — ADR-0041 D1/D4/D10 — shipped `v0.4.21` | none |
| **α8.2** | Video provider | first **async** provider → exercises the completion service — ADR-0041 D5 | none |
| **α8.3** | Webhook / polling completion | polling worker + inbound `/webhooks/providers/{name}`, both idempotent into one `complete()` — ADR-0041 D5–D7 | none |
| **α8.4** | FFmpeg render engine | RenderJob worker transitions (`running/succeeded/failed`, progress, `output_media_asset_id`) + compose; transitions/effects (α6.4 write paths first) — ADR-0041 D12 | none |
| **α8.5** | Export engine | `export_jobs` + storage providers (CR-5) — ADR-0041 D12 | none |

> **Runtime contract locked (α8.0, 2026-07-17).** The provider-runtime interfaces
> — `ProviderPort`, provider registry, adapter lifecycle, `StepCommand`
> dispatcher, the single poll-first completion service, polling/webhook ingresses,
> the `IDistributedLockManager` lease contract, the outbox relay, layered retry
> semantics, and the five worker roles — are fixed in **ADR-0041** so each slice
> above implements a stable seam. Decided: honour §13 as-is (no renumbering),
> runner-before-worker (Celery/Redis wait for α8.1), docs-only blueprint first.

Every slice above is **zero-migration**: it consumes tables provisioned in
Phase 2. A migration is introduced only if a §14 decision explicitly reverses a
Phase-2 "reconciled/deferred" call (schema §37).

---

## 14. Runtime decisions — SIGNED OFF (2026-07-16)

The load-bearing calls, resolved. These are now binding for α7+ slices.

**D1 — Timeline-version binding (reproducibility). ✅ Accepted — snapshot-bind
with two explicit modes.** A `RenderJob` renders against a **frozen
`ProjectVersion`** by default. Two modes:
- **Release Render (default):** binds to a specific `ProjectVersion` — fully
  reproducible; future-proof for publishing and audit. Aligns with ADR-0035.
- **Draft Render:** renders the **live timeline** for editor/developer preview —
  not reproducible, not publishable.
The bound version is reachable via `timeline_id → timelines.project_version_id`
(no migration, per D7); the render request carries the mode + resolved version id.

**D2 — Queue/broker technology. ✅ Accepted — Celery + Redis.** The documented
target; Redis is already assumed for agent memory/event bus. No alternative broker
introduced.

**D3 — Workflow engine substrate. ✅ Accepted — minimal in-house runner first.**
α7.2 ships a lean runner that persists to the existing `workflow_runs` /
`workflow_steps` / `workflow_checkpoints` tables. LangGraph is introduced **later
via an adapter** over the *same persistence model* — the checkpoint format stays
LangGraph-compatible so the migration path is straightforward and the table shape
never changes.

**D4 — Provider execution. ✅ Accepted — worker polling first.** Provider calls
poll to completion inside the worker. A webhook ingress path (anticipated by
`idempotency_keys('webhook')`) is added later; **both webhook and polling converge
on the same application service** — completion logic is written once, never
duplicated.

**D5 — `render_jobs.progress` type. ✅ Accepted — keep `text`.** Progress
semantics live in the application layer (e.g. `queued`, `10%`, `extracting
frames`, `waiting on provider`, `encoding`, `uploading`, `complete`). No
migration.

**D6 — `provider_settings.kind` discriminator. ✅ Accepted — keep as-is.**
Provider adapters interpret the existing `provider` + `key` values; the registry
discriminates by capability. No schema churn.

**D7 — Migration budget for α7. ✅ Strongly accepted — zero migrations.** α7
stays `existing schema → application layer`, never `application idea → schema
rewrite`. Version binding (D1) reaches the `ProjectVersion` via the timeline FK,
so even snapshot-binding needs no new column.

**D8 — First α7 slice. ✅ Accepted — `RenderJob` aggregate.** Right complexity:
new aggregate, existing table, CRUD + OCC + ownership + locks + status machine,
**no external providers** — validates the orchestration architecture before AI
providers enter. See α7.1 pre-flight.

**D9 — Every orchestration aggregate owns one state machine. ✅ Accepted (added
2026-07-16).** Documented as a first-class principle in **§7.1**. Each aggregate
is the sole writer of its own status; **no aggregate may directly mutate
another's state** — cross-aggregate coordination flows only through domain events
on the `event_outbox`. This keeps orchestration loosely coupled and aligns with
the existing outbox design.

---

## 15. Relationship to existing documents

- **`ARCHITECTURE.md`** §7/§8/§8a/§8b — the target architecture (authoritative
  for plugin contracts, pipelines, agent split). This doc does not override it.
- **`docs/database/schema.md`** §16–§18, §25, §27.3, §31, §32 — authoritative for
  table shapes; §37 for deferred decisions referenced in §14.
- **ADR-0030/0031/0032/0033** — the correctness primitives (export uniqueness,
  idempotency invariants, lock lease, usage uniqueness) this pipeline relies on.
- **ADR-0035 / ADR-0038** — the aggregate-isolation + OCC discipline extended
  here to the new `WorkflowRun` / `RenderJob` / `ExportJob` roots.
- **ROADMAP.md** Phases 6/7/8 — the product-level phases this blueprint's α7+
  slices execute.

---

## Change log

| Date | Change |
|---|---|
| 2026-07-16 | Initial blueprint drafted (bridges ARCHITECTURE.md target to implemented reality; pins lifecycle, state machines, substrate map, slice sequencing; §14 open decisions raised for sign-off). |
| 2026-07-16 | **Signed off.** D1–D8 accepted (D1: Release/Draft render modes; D2: Celery+Redis; D3: in-house runner → LangGraph adapter; D4: poll-first, single completion service; D5: progress stays `text`; D6: no `provider_settings.kind`; D7: zero migrations; D8: `RenderJob` first). **D9 added** — aggregate-owns-one-state-machine + no-cross-mutation, coordinated via `event_outbox` (§7.1). Status → contract for Phase 3 orchestration. Next slice: α7.1 `RenderJob`. |
| 2026-07-16 | α7.1 (`RenderJob`, `v0.4.15`, ADR-0039) and α7.2 (`WorkflowRun` + synchronous deterministic runner, `v0.4.16`, ADR-0040) shipped. |
| 2026-07-17 | **α8.0 Provider Runtime Blueprint** — the runtime contract locked in **ADR-0041** (docs-only). §13 refined: shipped slices marked, α8.x expanded into α8.1 image / α8.2 video / α8.3 webhook+polling / α8.4 FFmpeg render / α8.5 export **without renumbering** α7.3–α7.6. Decisions: honour §13 as-is; runner-before-worker (Celery/Redis deferred to α8.1); docs-only blueprint before any provider code. Next slice: α7.3 (outbox relay + lock manager — the two missing prerequisites). |
| 2026-07-17 | **α7.3 Outbox relay + distributed lock manager shipped** (`v0.4.17`). Library-only `RelayService.relay_once() -> RelayResult` drains the `event_outbox` through a `PublisherPort` (default synchronous `InProcessPublisher`, no `event_log` projection); poison events parked in-place after `max_attempts` with an ERROR structured log (no DLQ/scheduler). Owner-fenced `SqlAlchemyDistributedLockManager` (steal-after-expiry + explicit `reclaim_expired()`, ADR-0032). Zero migrations. **No worker/daemon/CLI/HTTP/broker/Celery/Redis** — the worker loop is α8.1. Next slice: α7.4 (provider plugin skeleton). |
| 2026-07-17 | **α7.4 Provider Skeleton shipped** (`v0.4.18`). Four async capability ports (LLM/Image/Video/Voice), a framework-free registry (explicit `register` + capability discovery: `supports`/`has_provider`/`list_capabilities`/`list_providers`), a `StepCommandDispatcher` (closed `kind`→capability table for the four provider kinds; `start_render`/render/export/storage excluded), a typed provider-error hierarchy (`ProviderError` + transient/terminal subclasses + `NoProviderAvailable`), immutable `ProviderMetadata`, and one deterministic mock per capability (video models the async `IN_PROGRESS` + `provider_job_id` path). Minimal read-only `provider_settings` seam (tenant-shadows-global). **Port-placement refinement of ADR-0041 D1/D4:** the neutral DTOs live in `app.application.interfaces.providers` and the runner-facing `ProviderDispatcherPort` in a sibling `provider_dispatcher` module, so the `app.infrastructure.ai.providers` leaf (capability ports/registry/mocks) is a strict `import-linter` leaf (forbids `use_cases`/`api`/workflow domain); the `StepCommandDispatcher` sits just above the leaf. **No HTTP/keys/external calls/Redis/Celery/retries/fallback/usage/events/polling/webhooks** — pure architecture, runner untouched, zero migration. Next slice: α7.5 (usage recorder). |
| 2026-07-18 | **α7.5 Usage Recorder shipped** (`v0.4.19`). `UsageRecorderService` (`application/use_cases/usage/`) turns one **terminal** provider call into exactly one immutable, priced `usage_records` row (ADR-0019, partitioned) — the producer ADR-0033 assumed but Phase 2 never built (ADR-0041 D13's usage half). Pure `account`/`price` policy (per-capability primary billing axis + `Σ(unit_price × quantity)` against `ai_model_pricing`); idempotent on `request_id` (insert-inside-SAVEPOINT + recover on the ADR-0033 per-partition unique index → return existing row, `idempotent_replay=True`); `IUsageRecordRepository` + read-only `IModelPricingRepository` on the UoW; `RecordUsageCommand` is the application contract (carries `render_job_id` with no column, stashed in `extra`). **Deferred/absent:** `credit_ledger` debit (`credits_consumed = 0`, Q1), `UsageRecorded` event (Q8), non-terminal recording (`IN_PROGRESS` rejected → α8.3, Q6); missing pricing never blocks (cost 0, `pricing_id` NULL, WARN — Q5). **Purely observational** (W7.5.1 — only writes `usage_records`). Shipped as a **seam**, not wired into the runner (Q2). Zero migrations. Next slice: α7.6 (first mock pipeline — wires runner → dispatcher → mock → recorder → outbox). |
| 2026-07-19 | **α7.6 First pipeline (mock) shipped** (`v0.4.20`). Composition slice — **no new infrastructure**: `AdvanceWorkflowRun` is extended (not forked, Q6) to interpret a succeeded step's `StepCommand`s. The runner mints a deterministic `request_id` (`run_id:step_index:command_index`, D5/Q3), dispatches each command **exactly once** through the injected `ProviderDispatcherPort` (W7.6.2 — retries are the runner's, never the dispatcher's), records **terminal** usage (`SUCCEEDED`/`FAILED`) in its **own** single transaction via the new `record_usage_in_uow(...)` helper (Q5 — the α7.5 `record()` API is unchanged), and threads the **opaque** provider envelope into the checkpoint/output verbatim (**W7.6.1** — the runner never reads `image_ref`/payload keys; capability + `model_id` come from the command it minted). Error mapping keeps three buckets (Q9): transient `ProviderError` → runner retry up to the step bound; terminal `ProviderError` (or malformed command) → fail; provider `FAILED` → record failed usage **then** fail. On `IN_PROGRESS` the runner takes `running → paused` (new `mark_run_paused` CAS — `paused` is **not** terminal, `finished_at` stays unset), checkpoints the resume coordinates (`provider_job_id`, `pending_step_index`), and emits the single new **`WorkflowRunPaused`** event (Q8) — α8.3 owns resumption. Fail-fast `MODEL_ID_MISSING` before dispatch (Q4). Two registry pipelines: `generate-image@1.0.0` (prepare-prompt → mock image `SUCCEEDED` → priced usage → checkpoint → `succeeded`) and `generate-video@1.0.0` (mock `IN_PROGRESS` → pause; nothing beyond pause — Q1). **No media rows** (Q7 — checkpoint only; α8.4 owns generated-media registration), no broker/HTTP/real providers/polling/webhooks. Dispatcher wired into the runner factory in the container. Zero migrations. Next slice: α8.1 (first real provider behind the same seam). |
| 2026-07-21 | **α8.1 First real provider — OpenAI Images (synchronous) shipped** (`v0.4.21`). The adapter slice: the one mocked box below the dispatcher — the image provider — is replaced by a real synchronous **`OpenAIImageProvider`** (`app/infrastructure/ai/providers/openai/image.py`) implementing the existing `ImageProvider` protocol over `POST /images/generations` (`dall-e-3`, `response_format="url"` → compact `image_ref`, **no storage** until α8.4). It makes **exactly one** HTTP request per call (W7.6.2 — retries stay the runner's) and maps HTTP status → the existing typed `ProviderError` buckets (401/403→auth·terminal; other 4xx/policy→validation·terminal; 429→rate-limited·transient; 5xx/connection→unavailable·transient; timeout→timeout·transient) so nothing HTTP leaks upward. The DI container composes the registry by config — `OPENAI_API_KEY` set → IMAGE resolves to the real provider, absent → `MockImageProvider`; **one provider per capability, no selection/fallback**; LLM/VIDEO/VOICE stay mock. **Nothing above the leaf changed** — runner, dispatcher, recorder, relay, lock manager, `ProviderRegistry` class, neutral DTOs, and `ports.py` untouched (whole diff = leaf + container wiring + `httpx` promoted to a core dep). Invariants: **W8.1.1** adapters are configuration-blind (provider is handed a pre-authenticated `httpx.AsyncClient`, never reads env/DB/secrets), **W8.1.2** exactly one real capability (IMAGE), **W8.1.3** observational equivalence with the mock (same DTO shape/status/fields; only values differ). **Broker still deferred** (no Celery/Redis) — the runner drives synchronously; async completion + worker arrive in α8.3. No webhooks/polling/storage/media/rate-limiter/circuit-breaker. Zero migrations. Next slice: α8.2 (first async provider → exercises the completion service). |
