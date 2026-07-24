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
| **α8.2** ✅ | Video provider (async, submit-only) | first **real async** provider — submit-only **`FalVideoProvider`** behind the α7.4 dispatcher: **one** HTTP submit per dispatch (W7.6.2) returns `IN_PROGRESS` + `provider_job_id` (= Fal `request_id`), driving the α7.6 pause seam with a real system; completion URLs ride a **versioned opaque `output` envelope** (`schema_version: 1`, W7.6.1) for α8.3. HTTP status → typed `ProviderError` (D10, same map as α8.1); **no usage on submit** (runner discards it on pause; α8.3 records terminal usage). Registry composes VIDEO by config **independently of IMAGE** (`FAL_API_KEY` → real, else mock — one provider per capability). **Nothing above the leaf changes**; adapter is configuration-blind (W8.1.1), observationally equivalent to the mock on the `IN_PROGRESS` path (W8.2.1), stops at the pause boundary (W8.2.2), and never mutates orchestration state (W8.2.3). **No polling/webhooks/completion-service/broker/storage/media/`video_ref`/selection.** Zero migration — ADR-0041 D1/D4/D5/D10 — shipped `v0.4.22` | none |
| **α8.3** ✅ | Completion engine (poll-first) | the single idempotent **`CompletionEngine.complete()`** (poll ingress `poll_once()`) closes the async loop: resolve the paused job via the new `VideoProvider.resolve` lifecycle, record **terminal** usage under the checkpointed `request_id`, then **`ResumeWorkflowRun`** atomically flips `paused → running` + marks the step + delegates continuation to the **unchanged** runner (public `continue_paused_run_in_uow`). Never re-dispatches (W8.3.3); exactly-once via the `workflow_run:<id>` lease + `paused → running` CAS (W8.3.2). New `WorkflowRunResumed` event; two no-migration repo methods (`resume_run`/`list_paused`) + additive `_paused` handoff fields (Fork 1A). Library-only/synchronous (no broker). **Webhook ingress deferred to α8.3b.** No media/`video_ref` (α8.4) — ADR-0041 D5/D6/D8/D11 — shipped `v0.4.23` | none |
| **Freeze** ✅ | Orchestration platform freeze (governance) | **ADR-0042** freezes the core orchestration surface (runner, `ResumeWorkflowRun`, `CompletionEngine`, dispatcher, provider ports+registry+DTOs, usage recorder, relay, lock manager, workflow registry/aggregate/status) as a stable platform API: bug/security/perf/observability/docs allowed; contract changes (signature/DTO/checkpoint/lifecycle/retry/provider-protocol/usage-semantics) require a new ADR. Enforced by `check_frozen_platform.py` + a `freeze-guard` CI job + `CODEOWNERS`. Guarantees G1–G10. **No code/behaviour/schema change, no version bump.** α8.3b→α8.5 must stay *additive* | none |
| **α8.3b** ✅ | Webhook completion ingress | inbound **`POST /webhooks/providers/{provider}`** as a **thin second ingress** to the same frozen `complete()` — Fal ED25519/JWKS signature verification (`IWebhookVerifier` port + `FalWebhookVerifier` leaf) + additive checkpoint lookup (`find_paused_by_provider_job_id`) + `ReceiveProviderWebhook` (verify → find paused run → `complete()`, **no writes**). Webhook is a **signal, not a source of truth** (**W8.3b.1**): payload only locates the run; `complete()` re-resolves authoritatively. Exactly-once already owned by the lease+CAS → **receipt persistence deferred** (`idempotency_keys` unused until ≥2 ingresses). **W8.1.1 clarified** (public JWKS keys are trust anchors, not credentials). **No ADR-0042-frozen module changed — freeze guard green, zero overrides.** Zero migration — ADR-0041 D7 — shipped `v0.4.24` | none |
| **α8.4a** ✅ | Generated media ingestion | download / store / register the generated artifact of a **succeeded** run. First real outbox consumer: **`GeneratedMediaIngestionSubscriber`** on **`WorkflowRunSucceeded`** → **`IngestGeneratedMedia`** reads the run's steps, extracts `image_ref`/`video_ref` from the opaque provider envelopes, downloads via a neutral **`IMediaDownloader`** (HTTP leaf), stores via backend-neutral **`IObjectStorage`** (local-FS first), registers a **`MediaAsset(source="generated")`**. Idempotent via a deterministic storage key (`ConflictError` = no-op); **W8.4.1** (strictly downstream of the frozen pipeline) + **W8.4.2** (observational — never mutates orchestration). Additive non-frozen `IProjectRepository.get_ownership` resolves the owning `(tenant,user)`. Minimal metadata only (checksum/mime/size/coords/provider). **Freeze guard green, zero overrides. Zero migration** — ADR-0041 D12 — shipped `v0.4.25` | none |
| **α8.4b** ✅ | FFmpeg render engine | a poll worker **`RenderWorker.run_once()`** (mirrors the α8.3 completion engine — CPU-bound render runs behind a poller, not the relay; Fork A) drains `queued` `RenderJob`s; **`ProcessRenderJob`** claims each (`queued → running` CAS under a `render_job:<id>` lease), resolves the Timeline into ordered **video** `MediaAsset`s, materializes their bytes from `IObjectStorage`, composes via a neutral **`IRenderer`** (**`FfmpegRenderer`** leaf; Fork B), stores the output under a deterministic key, registers a **`MediaAsset(kind="video", source="generated")`**, and settles `succeeded` (`output_media_asset_id`) / `failed` + `RenderJobSucceeded`/`RenderJobFailed`. **W8.4b.1** (pure Timeline → Media transform — never touches orchestration/checkpoints/provider/completion) + **W8.4b.2** (renderer consumes only `MediaAsset` ids + Timeline data, never provider artifacts). Idempotent via the deterministic output key (`ConflictError` → recover via `get_by_storage_coords`). Additive non-frozen `IRenderJobRepository` transitions (`list_claimable`/`mark_running`/`mark_succeeded`/`mark_failed`). Video-concat baseline; thumbnails/waveform/audio-mix/richer-metadata/previews → **α8.4c** (Fork E). **Freeze guard green, zero overrides. Zero migration** — ADR-0041 D12 — shipped `v0.4.26` | none |
| **α8.4c** ✅ | Media enrichment (thumbnail + metadata) | a poll worker **`MediaEnrichmentWorker.run_once()`** (symmetric with the α8.3 completion engine + α8.4b render worker — FFmpeg belongs to workers, never the relay; Fork B → **B2**) scans the **media table** (not render-job history — W8.4c.3) for un-enriched generated video `MediaAsset`s via the additive **`IMediaRepository.list_unenriched_generated_videos`** (`NOT (source_metadata ? 'enrichment')`); **`EnrichGeneratedMedia`** claims each under a `media_enrichment:<id>` lease, materializes the bytes, extracts one frame + probes bitrate via a neutral **`IThumbnailer`** (**`FfmpegThumbnailer`** leaf; Fork D — kept separate from `IRenderer`), stores the thumbnail under a deterministic key, registers a derived **`MediaAsset(kind="image", source="generated")`** cross-linked to the parent (Fork C), and augments the parent's `source_metadata` with an `enrichment` marker (`{thumbnail_media_asset_id, bitrate, enriched_at}`). **W8.4c.1** (observational + downstream — never mutates orchestration/checkpoints/provider/workflow-render-lifecycle/Timeline/renderer-inputs) + **W8.4c.2** (consumes only `MediaAsset` bytes + ids) + **W8.4c.3** (pure function of the parent — thumbnails reproducible from the parent alone). Idempotent via the deterministic key (`ConflictError` → recover via `get_by_storage_coords`; Fork E). Previews/GIF/waveform/audio-mix/transitions/quality-tuning → **α8.4d** (Fork A). **Freeze guard green, zero overrides. Zero migration** — ADR-0041 D12 — shipped `v0.4.27` | none |
| **α8.4d** ✅ | Derived previews (preview clip + GIF + waveform) | the α8.4c `MediaEnrichmentWorker` now runs a **pipeline of independent enrichers** (thumbnail + preview + gif + waveform), each wrapping a discrete neutral port (**`IPreviewClipper`** / **`IGifPreviewer`** / **`IWaveformRenderer`**; Fork C) with FFmpeg leaves. `EnrichGeneratedMedia` materializes once, runs each applicable enricher, registers each derived `MediaAsset` (idempotent, deterministic keys), and writes a **versioned** `enrichment` marker (Fork D — `list_enrichable_generated_videos(target_version)` backfills α8.4c assets by claiming `enrichment.version < current`). **W8.4d.1** (derived media is terminal — the scan excludes assets with `parent_media_asset_id`, Fork E) + W8.4c.1–3 carry over. Per-artifact failure isolation (version bumped only when all applicable enrichers succeed). Waveform yields `None` for silent sources. **Freeze guard green, zero overrides. Zero migration** — ADR-0041 D12 — shipped `v0.4.28` | none |
| **α8.4e** ✅ | Render composition — audio mixing | first **ADR-0043** slice (RC1–RC6, outside the ADR-0042 freeze): extends `IRenderer`/`FfmpegRenderer` from video-only to **video + deterministic audio** using already-authorable Timeline state (audio tracks, `clip.volume`, `track.muted`). Video-clip audio travels with its segment; dedicated audio-track clips are `adelay`-mixed via a **pure** `amix=…:normalize=0` (Fork B1, no DSP). Fork C1 (extend the render contract: `RenderInput.volume`/`muted` + `AudioInput` + `RenderSpec.audio_inputs`), Fork D1 (keep sequential concat). **W8.4e.1** (audio is a pure function of Timeline audio state). Silent timelines stay `a=0` (Fork F). **Zero migration** — ADR-0041 D12 / ADR-0043 — shipped `v0.4.29` | none |
| **α8.4f** | Render composition — transitions / effects / color grading / subtitles | changes *what the render is* **and** requires α6.4 Timeline **authoring** write paths (`transition_in_id`/`transition_out_id`/`effects`/subtitles) — deferred until authoring exists; ADR-0043 RC1–RC6 | none |
| **α8.5a** ✅ | Export engine — render output → delivery encoding | delivery transform downstream of render (ADR-0043 RC5/RC6): `ExportWorker`/`ProcessExportJob` + discrete `IExporter`/`FfmpegExporter` transcode the render master into `(format, quality)` **delivery** `MediaAsset`s within the master's orientation (Fork F, tightened). `CreateExportJob` ingress; **W8.5.1–W8.5.3**. **Zero migration** (`export_jobs`/`export_*` enums predate) — ADR-0041 D12 / ADR-0030 — shipped `v0.4.30` | none |
| **α8.5b.1** ✅ | Download serving — deliver an export artifact | owner-scoped `GET …/exports/{id}/download` → `DownloadExport` streams a completed export's bytes via the neutral `IDownloadDelivery` seam (`LocalStreamDelivery` now; signed-URL redirect ready for α8.5b.2). Best-effort `download_count` accounting; **W8.5b.1–W8.5b.3**. Below the ADR-0043 render boundary (pure transfer, RC5). **Zero migration** (`download_count`/`last_downloaded_at` predate) — shipped `v0.4.31` | none |
| **α8.5b.2** ✅ | Storage backends & signed-URL delivery — where artifacts live & how they're delivered | `StorageResolver` (write-active/read-persisted, E2) + `DeliveryResolver` registries behind the unchanged `IObjectStorage`/`IDownloadDelivery` seams; `S3ObjectStorage` (S3+R2) + `S3RedirectDelivery` (fixed-TTL offline presigned `302`); cloud SDK import-linter-isolated (Ruling D). `200` stream (local) / `302` redirect (cloud), endpoint unchanged. **W8.5b.4–W8.5b.5**. **Zero migration** (`storage_backend`/`storage_bucket`/`storage_key` predate) — CR-5 — shipped `v0.4.32` | none |
| **α8.5b.3** | Notifications | `INotifier` + relay subscriber on `ExportJobSucceeded` (the `notifications` table already exists) | none |
| **α8.6** | Publishing | `PublishJob` + `SocialAccount` + destination OAuth — a **new bounded context** (destinations are not AI providers); needs a migration | none |

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
| 2026-07-21 | **α8.2 First real async provider — Fal.ai Video (submit-only) shipped** (`v0.4.22`). The pause-proving slice: the remaining async-shaped mock — the video provider — is replaced by a submit-only **`FalVideoProvider`** (`app/infrastructure/ai/providers/fal/video.py`) implementing the existing `VideoProvider` protocol over the Fal.ai queue endpoint. It exercises the α7.6 pause seam with a real system: **exactly one** HTTP request (W7.6.2) *submits* the job and returns `IN_PROGRESS` + `provider_job_id` (= the Fal `request_id`, the runner's resume coordinate) — the adapter never polls/waits/resolves; completion (poll/webhook/resume/terminal usage) stays α8.3. The completion URLs (`status_url`, `response_url`) ride a **versioned opaque `output` envelope** (`schema_version: 1`) the runner checkpoints verbatim (W7.6.1), giving α8.3 a stable payload contract. HTTP status → the existing typed `ProviderError` buckets (same map as α8.1). **No usage on submit** — the runner already discards usage on the `IN_PROGRESS` pause (α7.6); α8.3 records the priced terminal row under the same `request_id`. The DI container composes VIDEO by config **independently of IMAGE** — `FAL_API_KEY` set → real `FalVideoProvider`, absent → `MockVideoProvider`; **one provider per capability, no selection/fallback**; LLM/VOICE stay mock. **Nothing above the leaf changed** — runner, dispatcher, recorder, relay, lock manager, `ProviderRegistry` class, neutral DTOs, `ports.py`, and the `generate-video` pipeline untouched (whole diff = new `fal/` leaf + one container branch). Invariants: **W8.1.1** configuration-blind (pre-authenticated `httpx.AsyncClient`, `Authorization: Key …`), **W8.2.1** observational equivalence with the mock on the `IN_PROGRESS` path (identical pause), **W8.2.2** stops at the pause boundary, **W8.2.3** never mutates orchestration state (pure request→response leaf). No polling/webhooks/completion-service/Celery/Redis/storage/media/`video_ref`/export/selection. Zero migrations. Next slice: α8.3 (completion service — polling + webhook receiver → resume paused run → terminal usage → succeeded/failed). |
| 2026-07-22 | **α8.3 Completion engine (poll-first) shipped** (`v0.4.23`). The resume slice — first orchestration-state move since α7.6. The single idempotent **`CompletionEngine`** (`app/application/use_cases/workflow/completion_engine.py`) closes the async loop α8.2 opened: `complete(project_id, run_id)` acquires the per-run `workflow_run:<id>` lease (D8), reads the `_paused` handoff, resolves the job via the new `VideoProvider.resolve` lifecycle (through `ProviderDispatcherPort.resolve_job`), and — if still `IN_PROGRESS` — leaves the run paused; on a terminal result it delegates to **`ResumeWorkflowRun`**. `poll_once()` is the D6 polling ingress (scans `paused` runs oldest-first, completes each under its own lease). **`ResumeWorkflowRun`** (`resume_workflow_run.py`) owns the **atomic** transaction: idempotent no-op if not `paused` → `resume_run` CAS (`paused → running`, exactly-once gate) → `WorkflowRunResumed` → terminal usage under the **checkpointed** `request_id` (Fork 1A coordinates — never a handler re-run) → `mark_step_succeeded` → delegate continuation to the **unchanged** runner via its new **public** `continue_paused_run_in_uow` (drives remaining steps + settles on the same open UoW, so resume+continue+settle commit atomically); on `FAILED` it settles the run failed itself (a failed step must not be driven), emitting a `WorkflowRunFailed` error shape-identical to the α7.6 inline path. The async capability is now a **lifecycle** (Q3): `VideoProvider.submit()` (renamed from `generate_video`) + new `resolve()`; Fal `resolve` GETs the α8.2 envelope's `status_url`/`response_url` (status → typed `ProviderError`), mock resolves deterministically; the closed `StepCommand.kind` table is unchanged (only the VIDEO call-site method name moved). Invariants **W8.3.1** (single idempotent entrypoint; replay = no-op), **W8.3.2** (exactly-once resume — lease + CAS), **W8.3.3** (completion delegates, never re-dispatches), **W8.3.4** (orchestration stays provider-agnostic — the engine reads only `status`/`usage`/`output`). Config: `completion_lock_owner`, `completion_lease_seconds`. **Ingress = polling only** — the webhook receiver is a thin second ingress to the same `complete()`, deferred to **α8.3b**. Library-only/synchronous (D11 — no Celery/Redis/daemon). **Zero migration** — two repo methods on existing tables (`resume_run`/`list_paused`) + additive `_paused` handoff fields. Unchanged: pure handlers, dispatch `kind` contract, neutral DTOs, `generate-video` pipeline, `ProviderRegistry` class, relay, lock-manager impl, recorder public API, runner step-execution semantics. No media/`video_ref` (α8.4). Next slice: α8.3b (webhook ingress) or α8.4 (FFmpeg render + generated-media registration). |
| 2026-07-22 | **Orchestration platform freeze established** (governance, **ADR-0042**, no version bump). With `v0.4.23` the async loop is closed and the core is feature-complete; α7.x built the substrate and α8.1–α8.3 proved it against real synchronous and asynchronous providers without duplicating orchestration or exposing runner internals. ADR-0042 declares the core orchestration surface a **frozen stable platform API** (§D1: runner, `ResumeWorkflowRun`, `CompletionEngine`, workflow events, dispatcher, provider ports/registry/neutral DTOs, usage recorder+pricing+port, relay, distributed lock manager+port, workflow registry/aggregate/status enums), fixes the **change policy** (§D2 — allowed: bug/security/perf/observability/docs; ADR-required: signature/DTO/checkpoint/lifecycle/retry/provider-protocol/usage-semantics), and enumerates **platform guarantees G1–G10** (§D3 — single dispatch, deterministic request IDs, exactly-once completion under locks, provider-agnostic orchestration, exactly-once usage, versioned checkpoints, resume never re-dispatches, configuration-blind providers, runner/provider boundary, two public resume seams). Enforcement (§D4) is deliberately lightweight and byte-identical local↔CI: `backend/scripts/check_frozen_platform.py` (diff vs base ref; fails on a frozen-path change without a `Freeze-Override: ADR-XXXX …` commit trailer or `ALLOW_FROZEN_CHANGES=1`), a fast DB-free `freeze-guard` CI job (separate from the ADR-0028 gate), and `.github/CODEOWNERS`. **Not frozen:** concrete provider adapters + all new-capability surfaces (ingress/downstream use cases, repositories, routers, DI wiring, tests) — the growth surfaces α8.3b→α8.5 plug into. Next slice: α8.3b (webhook ingress) — must stay additive to the frozen surface. |
| 2026-07-22 | **α8.3b Webhook completion ingress shipped** (`v0.4.24`) — the first integration slice built **entirely on top of the ADR-0042 freeze**. Fal delivers an inbound webhook when a queued video job finishes; α8.3b verifies it and routes it into the **unchanged** α8.3 `CompletionEngine.complete()` — a second ingress alongside polling (D6), converging on the one idempotent entrypoint. New provider-agnostic **`IWebhookVerifier`** port + neutral DTOs (`app/application/interfaces/webhook_verifier.py`) and a strict-leaf **`FalWebhookVerifier`** (`app/infrastructure/ai/providers/fal/webhook.py`): ED25519 over `"\n".join([request_id, user_id, timestamp, sha256(body)])`, verified against Fal's **public** JWKS keys (fetched + TTL-cached via an injected `httpx` client + `cryptography`), with a timestamp-tolerance replay guard. New additive, non-frozen **`IWorkflowRunRepository.find_paused_by_provider_job_id`** matches `_paused.provider_job_id` in the checkpoint JSONB (documented as an implementation detail — **zero migration**). New **`ReceiveProviderWebhook`** use case does exactly *verify → find paused run → `complete()`* and performs **no writes** (**W8.3b.1** — webhook payloads never directly mutate workflow state; all changes stay in the frozen pipeline, and `complete()` re-resolves authoritatively so a forged payload is inert). New unauthenticated **`POST /webhooks/providers/{provider}`** router (the signature is the auth): reads the **raw** body, maps `401` bad/stale/missing signature · `400` malformed · `404` unknown provider · `200` accepted (incl. duplicate/unknown job id — Fal retries are acked). **W8.1.1 clarified:** the configuration-blind invariant governs *credentials*; public JWKS verification keys are configuration-independent trust anchors, so fetching them injects no secret. **Deferred (signed off):** inbound receipt persistence — the `idempotency_keys` table has no consumer, and exactly-once + 200-on-duplicate are already guaranteed by `complete()`'s `workflow_run:<id>` lease + `paused → running` CAS; a first-class `IdempotencyRepository` waits for ≥2 inbound endpoints (Fal + Stripe + publishing/OAuth). Config: `FAL_WEBHOOK_JWKS_URL`, `FAL_WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS`, `FAL_WEBHOOK_JWKS_CACHE_SECONDS`; verifier lazily wired so the common test path opens no HTTP client. **The freeze guard stayed green with zero override markers for the whole branch — no frozen orchestration path changed** (whole diff = new port + fal leaf + repo method + use case + router + DI). Zero migration. Next slice: α8.4 (FFmpeg render + generated-media registration). |
| 2026-07-22 | **α8.4a Generated media ingestion shipped** (`v0.4.25`). The first *producing* slice and the platform's **first real outbox consumer** — proof the orchestration layer is now a **platform**. When a run settles `running → succeeded`, the new **`GeneratedMediaIngestionSubscriber`** (registered on the in-process `PublisherPort`; the α7.3 relay is untouched) fires on the existing **`WorkflowRunSucceeded`** event and triggers **`IngestGeneratedMedia`** (`app/application/use_cases/media/ingest_generated_media.py`). It reads the succeeded run's steps, extracts each `image_ref`/`video_ref` from the **opaque** provider-output envelopes the runner already checkpointed (W7.6.1 still holds — the runner never interprets them; the *downstream* consumer does), downloads the bytes via a neutral **`IMediaDownloader`** (`HttpMediaDownloader` leaf — single GET, injected client, byte cap, all errors → `MediaDownloadError`), stores them via a backend-neutral **`IObjectStorage`** (`LocalObjectStorage` filesystem adapter in α8.4a — S3/R2/GCS later with **no use-case change**), and registers a **`MediaAsset(source="generated")`**. Two new invariants: **W8.4.1** — ingestion is strictly **downstream** of the frozen completion pipeline (runner/completion/dispatcher never download/store/register); **W8.4.2** — ingestion is **observational** (creates storage objects + `MediaAsset` rows + logs, never mutates `WorkflowRun`/`WorkflowCheckpoint`/steps/`UsageRecord`/orchestration). Idempotent via a **deterministic** storage key in `(run, step, request_id)`: a redelivered event re-writes identical bytes and the `media_assets` storage-key uniqueness raises `ConflictError`, caught as an already-ingested no-op; downloads run **outside** any DB transaction. Ownership resolved by a new additive, non-frozen **`IProjectRepository.get_ownership(project_id)`** — a **system-only** lookup (never an HTTP route; mirrors α8.3b's `find_paused_by_provider_job_id`). Minimal metadata only (checksum/mime/size/coordinates/provider) — duration/dimensions/codec/thumbnails deferred to **α8.4b** (FFmpeg/render). Config: `MEDIA_STORAGE_ROOT`, `MEDIA_STORAGE_BUCKET`, `MEDIA_DOWNLOAD_TIMEOUT_SECONDS`, `MEDIA_DOWNLOAD_MAX_BYTES`; storage + downloader (with a dedicated `httpx` client) wired **lazily** so the common test path opens no client (disposed in `shutdown()`). **Freeze guard green, zero override markers — no frozen orchestration path changed** (whole diff = two ports + two adapters + one use case + one subscriber + one additive repo method + DI). **Zero migration** (`MediaAsset`/`IMediaRepository` predate this slice). Next slice: α8.4b (FFmpeg render engine). |
| 2026-07-23 | **α8.4b Render engine shipped** (`v0.4.26`). The first media-*transforming* slice — completes ADR-0041 D12 (α8.4a persisted *provider* output; α8.4b produces *composed* output). A new poll worker **`RenderWorker.run_once()`** (`app/application/use_cases/render/render_worker.py`) mirrors the α8.3 completion engine — rendering is CPU-bound, so it runs behind a **poller**, not the α7.3 relay fan-out (the relay stays fast for lightweight subscribers; Fork A). It scans the oldest `queued` `RenderJob`s (FIFO, capped by `RENDER_BATCH_SIZE`) and hands each to **`ProcessRenderJob`** (`process_render_job.py`), which claims the job (`queued → running` CAS under a `render_job:<id>` lease — exactly-once, same shape as completion), resolves the project Timeline into ordered **video** `MediaAsset`s, **materializes** their bytes from `IObjectStorage`, composes them via a new neutral **`IRenderer`** port (**`FfmpegRenderer`** leaf — `filter_complex` trim+concat then `ffprobe`; **configuration-blind** per W8.1.1, binary paths+timeout injected; Fork B), stores the output under a **deterministic** key, registers a **`MediaAsset(kind="video", source="generated")`**, and settles the job `succeeded` (`output_media_asset_id`) or `failed` — emitting `RenderJobSucceeded`/`RenderJobFailed`. Two new invariants: **W8.4b.1** — the worker is a **pure Timeline → Media transform** (neither reads nor mutates orchestration state, checkpoints, provider state, workflow status, or the completion lifecycle); **W8.4b.2** — the renderer consumes **only** `MediaAsset` identifiers + Timeline data, never provider outputs/URLs/checkpoints/request-IDs/provider-job-IDs/webhooks — completing the dependency graph `Provider → Completion → Ingestion → MediaAsset → Timeline → Renderer → output MediaAsset`. Idempotent via the deterministic output key: a re-render hits the `media_assets` storage-key uniqueness → `ConflictError` → the existing asset is recovered via the additive non-frozen **`IMediaRepository.get_by_storage_coords`**, never duplicated; render + all file I/O run **outside** any DB transaction. Additive non-frozen `IRenderJobRepository` worker transitions (`list_claimable`/`mark_running`/`mark_succeeded`/`mark_failed`, each a status-predicated CAS with hand-set `version+1`). Config: `RENDER_FFMPEG_PATH`, `RENDER_FFPROBE_PATH`, `RENDER_TIMEOUT_SECONDS`, `RENDER_WORKSPACE_DIR`, `RENDER_BATCH_SIZE`; renderer lazily wired. Video-concat baseline — thumbnails/waveform/audio-mix/richer-metadata/previews → **α8.4c** (Fork E scope split). **Freeze guard green, zero override markers — no frozen orchestration path changed** (whole diff = one port + one adapter + one use case + one worker + additive repo methods + events + DI). **Zero migration** (additive methods on existing `render_jobs`/`media_assets`). Next slice: α8.4c (render enhancements) or α8.5 (export). |
| 2026-07-24 | **α8.4c Media enrichment shipped** (`v0.4.27`). The first *derived-media* slice — takes a generated video `MediaAsset` and produces a thumbnail + probed metadata. A new poll worker **`MediaEnrichmentWorker.run_once()`** (`app/application/use_cases/media/media_enrichment_worker.py`) is symmetric with the α8.3 completion engine + α8.4b render worker — FFmpeg is CPU-bound, so it runs behind a **poller**, not the α7.3 relay fan-out. Per the sign-off this was **Fork B → B2** (changed from the recommended `RenderJobSucceeded` subscriber): *`PublisherPort` subscribers orchestrate work; they do not perform media processing*, so thumbnail extraction never lands on the relay path. It scans the **media table** (not render-job history — W8.4c.3) for un-enriched generated video `MediaAsset`s via the additive, non-frozen **`IMediaRepository.list_unenriched_generated_videos`** (`kind='video' AND source='generated' AND NOT (source_metadata ? 'enrichment')`, oldest first, capped by `ENRICHMENT_BATCH_SIZE` — a bounded, shrinking claim set). **`EnrichGeneratedMedia`** (`enrich_generated_media.py`) claims each under a `media_enrichment:<id>` lease (exactly-once, same shape as completion/render), re-reads the parent (must be a live, un-enriched generated video — the **sole source of truth**, W8.4c.3), **materializes** the bytes from `IObjectStorage`, extracts one frame + probes bitrate via a new neutral **`IThumbnailer`** port (**`FfmpegThumbnailer`** leaf — `-ss … -frames:v 1` + `ffprobe`; **configuration-blind** per W8.1.1; kept separate from `IRenderer` because `Video → Image` ≠ `Timeline → Video`; Fork D), stores the thumbnail under a **deterministic** key in `(tenant, parent)`, registers a derived **`MediaAsset(kind="image", source="generated")`** cross-linked to the parent (`source_metadata.origin="thumbnail"`; Fork C), and augments the parent's `source_metadata` with an `enrichment` marker (`{thumbnail_media_asset_id, bitrate, enriched_at}`) — which also drops it from the scan. Three new invariants: **W8.4c.1** — enrichment is **observational + downstream** (may derive artifacts + augment `source_metadata`, but never mutates orchestration/checkpoints/provider/workflow-render-lifecycle/Timeline/renderer-inputs — never "smart rendering"); **W8.4c.2** — consumes **only** `MediaAsset` bytes + identifiers (mirror of W8.4b.2); **W8.4c.3** — a **pure function of the parent** `MediaAsset` (never provider payloads/checkpoints/Timeline/render-job history), so thumbnails are reproducible from the parent alone (regenerable years later after an FFmpeg upgrade). Idempotent via the deterministic key: a re-run hits the `media_assets` storage-key uniqueness → `ConflictError` → the existing thumbnail is recovered via `get_by_storage_coords`, never duplicated; transient FFmpeg/storage failures leave the parent un-enriched so a later scan retries; FFmpeg + all file I/O run **outside** any DB transaction. Config: `ENRICHMENT_THUMBNAIL_AT_SECONDS`, `ENRICHMENT_BATCH_SIZE`; thumbnailer lazily wired, α8.4b FFmpeg config reused. Previews/GIF/waveform/audio-mix/transitions/quality-tuning → **α8.4d** (Fork A scope split). **Freeze guard green, zero override markers — no frozen orchestration path changed** (whole diff = one port + one adapter + one use case + one worker + one additive repo method + DI). **Zero migration** (thumbnails are `media_assets` rows; enrichment scalars + marker are JSONB `source_metadata`). Next slice: α8.4d (media/render enhancements) or α8.5 (export). |
| 2026-07-24 | **α8.4d Derived-preview enrichment shipped** (`v0.4.28`). Extends the α8.4c seam with preview clip + GIF + waveform. The gating question (*"pure downstream transform of an existing `MediaAsset`?"*) split the α8.4c-deferred list: preview/GIF/waveform → α8.4d; audio-mix/transitions/quality-tuning change **composition** (*what the render is*) → a new **α8.4e** render slice. The **same** `MediaEnrichmentWorker` now runs a **pipeline of independent enrichers** (`app/application/use_cases/media/enrichers/` — `ThumbnailEnricher` + `PreviewEnricher` + `GifEnricher` + `WaveformEnricher`), each wrapping a discrete neutral port (**`IPreviewClipper`** / **`IGifPreviewer`** / **`IWaveformRenderer`**; Fork C — no "God" `IMediaEnricher`) with configuration-blind FFmpeg leaves (W8.1.1; shared `_ffmpeg_exec` helper). `EnrichGeneratedMedia` (`enrich_generated_media.py`) materializes the source **once**, runs each applicable enricher **outside any DB txn**, registers each derived `MediaAsset(source="generated")` under a deterministic per-artifact key (`thumbnails/` `previews/` `gifs/` `waveforms/`; `ConflictError` → `get_by_storage_coords` recovery; Fork F), and augments the parent's `source_metadata.enrichment` with the ids + scalars + a **version**. Fork D (versioning): the scan (renamed `list_enrichable_generated_videos(target_version)`) claims `COALESCE((source_metadata #>> '{enrichment,version}')::int, 0) < CURRENT_ENRICHMENT_VERSION` (1 → 2), so bumping the version **backfills** α8.4c-era assets with previews; the version is set only when **every applicable enricher succeeds** (per-artifact failure isolation — a transient failure leaves the asset re-claimable and recovers already-registered artifacts on retry). Fork E / **W8.4d.1 (new)**: derived media is **terminal** — the scan excludes assets carrying `parent_media_asset_id`, so a derived preview video is never itself enriched (the derivation graph is a shallow tree, never a cycle). `IWaveformRenderer` returns `None` for a silent source (not applicable, not a failure). The enricher pipeline is an **implementation detail** — no new worker, port, or ADR. Config: `ENRICHMENT_PREVIEW_*`, `ENRICHMENT_GIF_*`, `ENRICHMENT_WAVEFORM_*`; adapters lazily wired, α8.4b FFmpeg config reused. **Freeze guard green, zero override markers — no frozen orchestration path changed.** **Zero migration** (derived artifacts are `media_assets` rows on the existing `media_kind` enum; marker is JSONB). Next slice: α8.4e (composition) or α8.5 (export). |
| 2026-07-24 | **α8.4e Render composition — audio mixing shipped** (`v0.4.29`; **first ADR-0043 slice**). The first feature governed by the render composition boundary (RC1–RC6) rather than the ADR-0042 freeze: two gates in the pre-flight — Gate 1 (does it touch the frozen orchestration surface? **No**) and Gate 2 (does every change satisfy RC1–RC6? **Yes**, per an explicit per-feature matrix). It extends `IRenderer`/`FfmpegRenderer` from **video-only** composition (the α8.4b `concat …:a=0` discarded all audio) to **video + deterministic audio**, using **already-authorable** Timeline state — audio-kind tracks, `clip.volume`, `track.muted` — so **zero migration** and no dependency on the (still-deferred) α6.4 authoring paths. **Fork B1** (mixing model): each video clip's own audio travels with its segment (at `clip.volume`, silenced if its track is `muted`); dedicated **audio-track** clips (music/voiceover) are `atrim`/`volume`/`adelay`-ed to their `start_seconds` and combined with the video bed via a **pure** `amix=inputs=…:duration=first:normalize=0` — the `normalize=0` is the concrete enforcement of **no implicit gain staging** (no ducking/side-chain/compression/fades/DSP). Audio-bearing streams are format-normalized (stereo/44.1 kHz/fltp) so `concat`/`amix` see matching streams; clips lacking audio are silence-filled to keep the bed synced to the concat; `ffprobe` decides per-source audio presence, and a timeline with **no** authored audio stays `a=0` exactly as α8.4b (**Fork F**). **Fork C1** (extend the render contract, never reach back into the Timeline): `RenderInput` gains `volume`+`muted`, a new `AudioInput` DTO carries `(path, source window, start_seconds, volume)`, and `RenderSpec` gains `audio_inputs` — the renderer receives everything as immutable composition inputs (RC1/RC2/RC3). **Fork D1**: keep α8.4b **sequential-concat** video semantics (absolute-time placement is a separate future slice). `ProcessRenderJob._resolve_clips` → `_resolve_composition` now reads `list_tracks` for each track's `kind`/`muted` and resolves audio-kind, non-muted clips into ordered `AudioInput`s (shared `_materialize` helper — W8.4b.2, only `MediaAsset` coordinates). **W8.4e.1 (new)** — audio composition is a **pure function of Timeline audio state** (tracks, clips, mute flags, volumes) + `RenderSpec`; the renderer introduces no implicit gain staging, normalization, dynamic processing, fades, or hidden sources (reinforces RC3+RC6). Transitions/crossfades/color-grading/effects/subtitle burn-in → **α8.4f** (they require the α6.4 Timeline **authoring** write paths — you cannot render what cannot be authored). Validated against real FFmpeg (opt-in integration: an audio-mix roundtrip yields an audio stream; a silent timeline yields none). **Freeze guard green, zero override markers — no frozen orchestration path changed.** **Zero migration.** Next slice: α8.4f (composition, after α6.4 authoring) or α8.5 (export). |
| 2026-07-24 | **α8.5a Export engine shipped** (`v0.4.30`; **first delivery-stage slice**). Opens the delivery stage downstream of render+enrichment. Two gates in the pre-flight — Gate 1 (frozen orchestration surface? **No**) and Gate 2 (ADR-0043: export is a delivery transform on a *finished, immutable* render — RC5 — and deterministic — RC6/RP1–RP9). A new poll worker **`ExportWorker.run_once()`** (`app/application/use_cases/export/export_worker.py`) mirrors the α8.4b render worker — transcoding is CPU-bound, so it runs behind a **poller**, not the α7.3 relay fan-out (**Fork B1**). It scans the oldest `queued` `ExportJob`s (FIFO, capped by `EXPORT_BATCH_SIZE`, each resolving its owning `project_id` via a `render_jobs` join) and hands each to **`ProcessExportJob`** (`process_export_job.py`), which claims it (`queued → running` CAS under an `export_job:<id>` lease), resolves the referenced render's **master** `MediaAsset` (`render_job.output_media_asset_id` — the **only** legal source, **Fork D**; never Timeline/provider/intermediate artifacts), **materializes** its bytes from `IObjectStorage`, transcodes via a new discrete neutral **`IExporter`** port (**`FfmpegExporter`** leaf — `quality`→resolution box, `orientation`→box orientation, `format`→container/codec [`mp4`/`mov`=h264/aac, `webm`=vp9/opus, `gif`=palettegen/paletteuse]; aspect preserved with **no** pad/crop; configuration-blind per W8.1.1, kept separate from `IRenderer` because delivery-encoding ≠ Timeline composition — **Fork C1**), stores under a **deterministic** key, registers a delivery **`MediaAsset(source="generated"`, `source_metadata.origin="export"` + master lineage)**, and settles `succeeded` (`output_media_asset_id`+`file_size_bytes`) or `failed` — emitting `ExportJobCreated`/`ExportJobSucceeded`/`ExportJobFailed`. Owner ingress `POST …/render-jobs/{id}/exports` (**`CreateExportJob`**, 201/200-idempotent) gates project ownership + master-readiness + the **same-orientation** guard (a cross-orientation request is a `422`), and `GET …/exports/{id}` reports status. **Fork F (tightened at sign-off)**: **delivery-only, same-orientation** — format/codec/bitrate/**resolution** within the master's own orientation, never a reframe (cross-orientation + letterbox/pillarbox/smart-crop → a future policy slice); publishing/notifications/download-service/storage-backends/CDN → **α8.5b** (**Fork A**). **W8.5.1** (downstream-only, upholds RC5), **W8.5.2** (consumes only a `MediaAsset`+params), **W8.5.3** (master is canonical; exports are replaceable, regenerable — RC6). Idempotent per `(render_job, format, quality, orientation)` via the **existing** partial-unique index (**Fork E**, ADR-0030 W1.1) + deterministic-key `ConflictError`→`get_by_storage_coords` recovery; transcode+I/O run **outside** any DB txn. Additive non-frozen `IExportJobRepository`; `export_jobs` wired into the UoW. Validated against real FFmpeg (opt-in: mp4 orientation-preserving + gif roundtrips). **Freeze guard green, zero override markers — no frozen orchestration path changed.** **Zero migration** (`export_jobs`+`export_*` enums predate this slice). Next slice: α8.5b (publishing / storage backends / download) or α8.4f (composition, after α6.4 authoring). |
| 2026-07-24 | **α8.5b.1 Download serving shipped** (`v0.4.31`; **first distribution-stage slice**). α8.5a made the delivery artifact exist; α8.5b.1 makes it obtainable. Grounding split the α8.5b umbrella into four downstream capabilities of very different risk — download / storage-backends / notifications / publishing — and this slice ships only the smallest, **zero-migration**, highest-value one. A new owner-scoped endpoint **`GET /projects/{pid}/render-jobs/{rid}/exports/{eid}/download`** routes to **`DownloadExport`** (`app/application/use_cases/export/download_export.py`), which resolves + authorizes through the existing project → render-job ownership gate (foreign/missing → `404`, anti-enumeration), requires the export `succeeded` with a live `output_media_asset_id` (`409` otherwise — **Fork C**), resolves the delivery `MediaAsset`, and asks the new neutral **`IDownloadDelivery`** seam (**Fork A**) how to deliver it. The seam returns a **`DeliveryDecision`** — `StreamDelivery` (bytes through the API) or `RedirectDelivery` (signed URL) — so the endpoint contract survives the arrival of cloud delivery unchanged; α8.5b.1 implements **`LocalStreamDelivery` only** (`app/infrastructure/delivery/local_stream_delivery.py` — reads `IObjectStorage`, chunk-streams, refuses non-local backends), with **no** `signed_url()` / S3 / R2 / CDN code (α8.5b.2). Download telemetry is best-effort (**Fork B**): the new additive, non-frozen `IExportJobRepository.record_download` bumps `download_count` + `last_downloaded_at` guarded on `status='succeeded'`, **no `version` bump** (telemetry, not OCC), in its own short txn, **swallowed on failure** — a counter outage never fails a download, and no retry (**W8.5b.3**). Three new invariants: **W8.5b.1** (download is observational/read-only — its only write is accounting), **W8.5b.2** (pure transfer — no encode/transcode/resize; reinforces RC5 + W8.5.3), **W8.5b.3** (accounting isolation). **Fork D**: owner-only (team / share-link / public / expiring-token access deferred). **Fork F**: storage boundary unchanged — `MediaAsset` owns the location, `ExportJob` references the canonical output only. Cloud storage backends + signed URLs → **α8.5b.2**; notifications → **α8.5b.3**; publishing (a new bounded context — destinations are **not** AI providers) → **α8.6**. **Freeze guard green, zero override markers — no frozen orchestration path changed.** **Zero migration** (`download_count` / `last_downloaded_at` predate this slice, ADR-0030). Next slice: α8.5b.2 (storage backends) / α8.5b.3 (notifications) / α8.6 (publishing), or α8.4f (composition, after α6.4 authoring). |
| 2026-07-24 | **α8.5b.2 Storage backends & signed-URL delivery shipped** (`v0.4.32`; **second distribution-stage slice**). Completes the α8.5b.1 delivery seam by making storage **multi-backend** — **AWS S3 / Cloudflare R2** object storage + **fixed-TTL presigned-URL redirect** delivery — with **no change** to `DownloadExport` or the `GET …/exports/{id}/download` endpoint (only the observable status differs: `200` stream for local vs `302` redirect for cloud). Two gates in the pre-flight — Gate 1 (frozen orchestration surface? **No**) and Gate 2 (ADR-0043: storage/delivery are transport/persistence below the render boundary, never re-encoding — RC5/W8.5.3). Backend selection is centralised in two registries (**Ruling A**) so no use case is backend-aware: **`StorageResolver`** (`app/infrastructure/storage/storage_resolver.py` — `active()` = the single configured write backend; `resolve(backend)` = an existing artifact's persisted backend) and **`DeliveryResolver`** (`app/infrastructure/delivery/delivery_resolver.py` — an `IDownloadDelivery` facade dispatching on `MediaAsset.storage_backend`). Signing lives in the **delivery** adapter, never `IObjectStorage` (**Ruling B**): **`S3RedirectDelivery`** returns a `RedirectDelivery` to an **offline**-presigned URL (`botocore.generate_presigned_url` — no request-path network call) with a **fixed** central TTL (`download_signed_url_ttl_seconds`, default 900 s; **no** per-request TTL, **no** CDN — **Fork F**) and populates `expires_at`. One S3-compatible **`S3ObjectStorage`** adapter serves both S3 and R2 (R2 = injected endpoint + credentials — **Ruling C**); sync boto3 runs in a worker thread; SDK errors map to neutral `ObjectStorageError`. Write-side is **E2**: `storage_active_backend ∈ {local,s3,r2}` (default `local`) selects where *new* `MediaAsset`s are persisted (`active()`), while `ProcessExportJob` / `ProcessRenderJob` / `EnrichGeneratedMedia` / `IngestGeneratedMedia` now take an **`IStorageResolver`** and **read** each source by its *persisted* backend (`resolve(...)`) — **exactly one** active write backend (no preferred/fallback/mirror/replication), **no backfill**. Two new invariants: **W8.5b.4** — delivery selection is a pure function of the persisted `storage_backend`; **W8.5b.5** — changing `storage_active_backend` affects **only future writes**, never the location/interpretation of existing assets (operational, not migratory). The cloud SDK is confined to `app.infrastructure.{storage,delivery}` by a new **import-linter** `forbidden` contract keeping `boto3`/`botocore` out of domain/application/api/core (**Ruling D**); `boto3` added as a runtime dependency. Notifications → **α8.5b.3**; publishing → **α8.6**; GCS/Azure plug into the same resolver with no use-case change (CR-5). **Freeze guard green, zero override markers — no frozen orchestration path changed.** **Zero migration** (`storage_backend` / `storage_bucket` / `storage_key` predate this slice, ADR-0030). Next slice: α8.5b.3 (notifications) / α8.6 (publishing), or α8.4f (composition, after α6.4 authoring). |
