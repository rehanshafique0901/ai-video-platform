# Platform Status — Architectural Baseline

> **Purpose.** A single, concise snapshot of what is considered **frozen platform
> infrastructure** versus **unfrozen feature surface**, plus the capability
> lifecycles completed so far. This exists so a future contributor (or your future
> self) can reconstruct the architectural state *without* reading dozens of ADRs
> and changelog entries.
>
> **This document is descriptive, not normative.** The **authoritative** sources
> remain [`ADR-0042`](../decisions/ADR-0042-orchestration-platform-freeze.md) (the
> freeze), [`ADR-0041`](../decisions/ADR-0041-provider-runtime-contract.md) (the
> provider runtime contract), [`CONTENT_GENERATION_PIPELINE.md`](CONTENT_GENERATION_PIPELINE.md)
> (the pipeline blueprint), and [`backend/scripts/check_frozen_platform.py`](../../backend/scripts/check_frozen_platform.py)
> (the machine-readable frozen-path list). If this file and those disagree, **they
> win** — update this file.
>
> **Keep it current:** refresh the *Current baseline* line and the *Completed
> capability lifecycles* table at the end of each runtime slice.

---

## Current baseline

| | |
|---|---|
| **Application version** | `0.4.34-phase3-alpha8.5b3r` |
| **Latest runtime tag** | `v0.4.34-phase3-alpha8.5b3r` |
| **Phase** | Phase 3 — orchestration era (α7+) |
| **Orchestration core** | **Frozen** since `v0.4.23` (ADR-0042, 2026-07-22) |
| **Freeze overrides used to date** | **0** (α8.3b, α8.4a–e, α8.5a, α8.5b.1–3, α8.5b.3r all shipped additively) |

The project has crossed from *building the orchestration engine* to *building
capabilities on top of a stable platform*. Every slice since the freeze has been
strictly additive — no frozen path changed, no `Freeze-Override:` trailer used.

---

## Frozen orchestration platform (ADR-0042 §D1)

These modules are the **stable platform API**. Changing any of them trips the
freeze guard and requires a `Freeze-Override: ADR-XXXX …` commit trailer backed by
a new ADR (see *Change policy* below). The list below mirrors `FROZEN_PATHS` in
`check_frozen_platform.py` — the mechanical source of truth.

**Runner + resume + completion (the async orchestration loop)**
- `application/use_cases/workflow/advance_workflow_run.py` — the deterministic runner
- `application/use_cases/workflow/resume_workflow_run.py` — atomic resume seam
- `application/use_cases/workflow/completion_engine.py` — the single completion entrypoint
- `application/use_cases/workflow/_events.py` — workflow event shapes

**Dispatch + provider contracts (ports & neutral DTOs)**
- `infrastructure/ai/dispatcher.py` — the `StepCommand` → capability dispatcher
- `infrastructure/ai/providers/ports.py` — capability protocols
- `infrastructure/ai/providers/registry.py` — the provider registry class
- `application/interfaces/providers.py` — neutral provider DTOs
- `application/interfaces/provider_dispatcher.py` — the runner-facing dispatcher port

**Usage recording (service + pricing + port)**
- `application/use_cases/usage/usage_recorder_service.py`
- `application/use_cases/usage/accounting.py`
- `application/interfaces/usage_recorder.py`

**Relay + distributed locks**
- `application/use_cases/relay/relay_service.py`
- `infrastructure/repositories/distributed_lock_manager.py`
- `application/interfaces/locks.py`

**Workflow registry + aggregate + status enums (lifecycle + checkpoint owner)**
- `domain/workflow/registry.py`
- `domain/workflow/workflow_run.py`
- `domain/workflow/workflow_run_status.py`
- `domain/workflow/workflow_step_status.py`

### Platform guarantees (ADR-0042 §D3, G1–G10)

Single dispatch per command · deterministic request IDs · exactly-once completion
under distributed locks · provider-agnostic orchestration · exactly-once usage ·
versioned checkpoint envelopes · resume never re-dispatches provider work ·
configuration-blind providers (credentials injected, never fetched) · runner owns
orchestration / providers own external communication · two public resume seams.

### Change policy (ADR-0042 §D2)

- **Allowed without an ADR:** bug fixes, security fixes, performance improvements,
  observability, documentation.
- **Requires a new ADR + freeze override:** public method signature changes, DTO
  changes, checkpoint-schema changes, workflow-lifecycle changes, retry-semantics
  changes, provider-protocol changes, usage-recording-semantics changes.

### Enforcement (ADR-0042 §D4)

- `backend/scripts/check_frozen_platform.py` — diffs against a base ref; fails on a
  frozen-path change lacking a `Freeze-Override:` trailer (or `ALLOW_FROZEN_CHANGES=1`
  for local iteration). Byte-identical local ↔ CI.
- A fast, DB-free `freeze-guard` CI job (separate from the ADR-0028 gate).
- `.github/CODEOWNERS` review requirement on frozen paths.

---

## Completed capability lifecycles

The runtime pipeline is complete through rendering. Each boundary is clean —
providers know nothing about rendering, rendering knows nothing about providers,
orchestration doesn't know FFmpeg exists, completion doesn't know storage exists,
storage doesn't know timelines exist:

```
Provider → Completion → Generated Media Ingestion → MediaAsset → Timeline → Render Engine → Output MediaAsset
```

| Lifecycle | Status | Slice(s) | Notes |
|---|---|---|---|
| Workflow foundations + deterministic runner | ✅ | α7.1 / α7.2 | `RenderJob` aggregate; checkpointed runner |
| Outbox relay + distributed locks | ✅ | α7.3 | `RelayService` + lock manager |
| Provider architecture (ports/registry/dispatcher) | ✅ | α7.4 | four capability ports; typed errors |
| Usage recorder | ✅ | α7.5 | exactly-once priced `usage_records` |
| First end-to-end pipeline (mock) | ✅ | α7.6 | dispatch + terminal usage + checkpoint |
| Real synchronous provider (OpenAI Images) | ✅ | α8.1 | configuration-blind adapter (W8.1.1) |
| Real async provider (Fal.ai Video, submit-only) | ✅ | α8.2 | pause + `provider_job_id` |
| Completion engine (poll-first) | ✅ | α8.3 | `CompletionEngine.complete()`; exactly-once resume |
| Webhook completion ingress | ✅ | α8.3b | thin second ingress → same `complete()` |
| Generated media ingestion | ✅ | α8.4a | download / store / register `MediaAsset` |
| Render engine | ✅ | α8.4b | Timeline → FFmpeg → output `MediaAsset` |
| Media enrichment (thumbnail + metadata) | ✅ | α8.4c | derived-media poll worker; pure function of the parent `MediaAsset` |
| Derived previews (preview clip / GIF / waveform) | ✅ | α8.4d | enricher pipeline; versioned marker + backfill; derived media terminal |
| Render composition — audio mixing | ✅ | α8.4e | first ADR-0043 slice; video + deterministic audio (`amix`, no DSP) |
| Export engine (delivery encodings) | ✅ | α8.5a | delivery transform (RC5/RC6): master → `(format, quality)` delivery `MediaAsset`, same-orientation; poll worker + discrete `IExporter` |
| Download serving (deliver artifact bytes) | ✅ | α8.5b.1 | owner-scoped `GET …/exports/{id}/download`; neutral `IDownloadDelivery` seam (`LocalStreamDelivery` now, redirect-ready); best-effort accounting |
| Storage backends & signed-URL delivery (S3/R2) | ✅ | α8.5b.2 | `StorageResolver` + `DeliveryResolver` registries; write-active/read-persisted (E2); `S3ObjectStorage` (S3+R2) + fixed-TTL offline-presigned `302` redirects; cloud SDK import-linter-isolated; endpoint unchanged |
| Notifications (in-app projection) | ✅ | α8.5b.3 | `NotificationProjection` (2nd relay consumer) on `ExportJobSucceeded`/`ExportJobFailed`; exactly-once per recipient per source event, DB-enforced via partial `UNIQUE (user_id, source_event_id)`; write path only (read API → α8.5b.3r, email → α8.5b.4) |
| Notification read API (list / count / mark-read) | ✅ | α8.5b.3r | owner-scoped read-model completion on the α8.5b.3 projection: `GET /notifications` (keyset feed, reuses α5a pagination), `GET /notifications/unread-count`, `POST /notifications/{id}/read` + `/read-all` (action verbs); read-state metadata-only + order-stable (W8.5b.8/9/10); pure additive repo methods; **zero migration**; archive/inbox features out |

### Invariant catalog

Behavioural invariants adopted alongside the frozen guarantees. Each is enforced
by review + tests, not the mechanical guard:

- **W7.5.1** — the usage recorder is purely observational (writes only `usage_records`).
- **W7.6.1** — the runner never interprets provider payloads (opaque envelopes).
- **W7.6.2** — no dispatcher-side retry; retries stay the runner's.
- **W8.1.1** — adapters are configuration-blind (credentials injected, never fetched;
  public JWKS verification keys are permitted trust anchors, α8.3b clarification).
- **W8.1.2** — exactly one real capability per real adapter slice; others stay mock.
- **W8.1.3** — observational equivalence between real and mock provider responses.
- **W8.3.1–W8.3.4** — single idempotent completion entrypoint; exactly-once resume
  (lease + CAS); completion delegates and never re-dispatches; orchestration stays
  provider-agnostic.
- **W8.3b.1** — webhook payloads never directly mutate workflow state (signal, not source).
- **W8.4.1** — generated-media ingestion is strictly downstream of the frozen pipeline.
- **W8.4.2** — ingestion is observational (never mutates orchestration state).
- **W8.4b.1** — the render worker is a pure Timeline → Media transform; it neither
  reads nor mutates orchestration state, checkpoints, provider state, workflow
  status, or the completion lifecycle.
- **W8.4b.2** — the renderer consumes only `MediaAsset` identifiers + Timeline data;
  never provider outputs, URLs, checkpoints, request IDs, provider job IDs, or webhooks.
- **W8.4c.1** — media enrichment is observational and downstream; it may derive
  artifacts + augment the owning `MediaAsset`'s `source_metadata`, but never mutates
  orchestration state, checkpoints, provider state, workflow/render lifecycle,
  Timeline definitions, or renderer inputs.
- **W8.4c.2** — the enricher consumes only `MediaAsset` bytes + identifiers; never
  provider outputs, URLs, checkpoints, request IDs, provider job IDs, webhooks, or
  Timeline internals.
- **W8.4c.3** — derived media is reproducible from its parent `MediaAsset` alone;
  enrichment never depends on provider payloads, checkpoints, Timeline state, or
  render-job history — `MediaAsset → Thumbnail` is a pure function of the parent.
- **W8.4d.1** — derived media is terminal. A derived `MediaAsset` SHALL NOT participate
  as the source of further enrichment; enrichment operates exclusively on primary
  generated/rendered assets. Derived artifacts are observational outputs only (the
  derivation graph is a shallow tree, never a cycle).
- **W8.4e.1** — audio composition is a pure function of Timeline audio state (tracks,
  clips, `muted` flags, `volume` values) + `RenderSpec`. The renderer introduces no
  implicit gain staging, normalization, dynamic processing (ducking/side-chain/
  compression), fades, or hidden audio sources — a deterministic weighted sum of the
  authored inputs (reinforces ADR-0043 RC3 + RC6; enforced by `amix …:normalize=0`).
- **W8.5.1** — export is downstream-only: it never recomposes, mutates, or re-renders the
  master; it only reads a finished `MediaAsset` and produces a new delivery `MediaAsset`
  (upholds ADR-0043 RC5).
- **W8.5.2** — export consumes only a `MediaAsset` + request params (`format`/`quality`/
  `orientation`). Never a Timeline, provider output/URL, checkpoint, request/job id, or
  webhook (mirror of W8.4b.2 / W8.4c.2).
- **W8.5.3** — the rendered `MediaAsset` is the canonical master; exports are replaceable
  delivery artifacts. Same master + same `(format, quality, orientation)` ⇒ a functionally
  equivalent delivery (RC6), regenerable at any time; deleting/regenerating a delivery never
  affects the master (one master → N replaceable encodings).
- **W8.5b.1** — download serving is observational and read-only: it reads a finished delivery
  `MediaAsset` and transfers its bytes; its only write is the `export_jobs` download accounting
  (`download_count` / `last_downloaded_at`). It never mutates the artifact, the master, or any
  upstream orchestration/render/export state.
- **W8.5b.2** — delivery is a pure transfer: no encoding, transcoding, re-composition,
  re-timing, or resize on the download path (the export engine already owns those; reinforces
  RC5 + W8.5.3 — deliveries are replaceable byte artifacts of the canonical master).
- **W8.5b.3** — accounting never blocks or corrupts delivery: download-count updates are
  best-effort, non-transactional with the byte transfer, and non-retrying; a failure is
  telemetry loss, not a user-visible error, and delivery never depends on the counter write.
- **W8.5b.4** — delivery selection is derived solely from the artifact's persisted storage
  backend: the delivery mechanism is a pure function of `MediaAsset.storage_backend` via the
  `DeliveryResolver` (local → stream, s3/r2 → presigned redirect) — never request headers,
  endpoint/query params, feature flags, client preference, or the active *write* backend. The
  same artifact always delivers the same way.
- **W8.5b.5** — the active write backend affects only future writes: changing
  `storage_active_backend` changes where *new* `MediaAsset`s are persisted and **never** changes
  the location or interpretation of existing ones — each stays readable/deliverable from its own
  `(storage_backend, storage_bucket, storage_key)`. Backend changes are operational, not migratory
  (no backfill).
- **W8.5b.6** — notification creation is a pure projection of immutable events: the projection +
  use case only **read** a terminal, already-committed event and **write** notification state. They
  never mutate export/render/orchestration state, re-drive the export, dispatch provider/render
  work, or call back into the frozen pipeline (kin to W8.4.2 / W8.5b.1).
- **W8.5b.7** — a notification is projected exactly once per recipient per source event, and this
  invariant is enforced by the persistence layer, not by subscriber control flow. The relay may
  deliver more than once and the projection may execute more than once; the partial `UNIQUE
  (user_id, source_event_id)` guarantees the row exists at most once, and the use case treats the
  refused duplicate as a successful no-op.
- **W8.5b.8** (α8.5b.3r) — notification queries never expose notifications belonging to another
  principal: every read/read-state method is scoped by `user_id` at the repository layer, and a
  foreign/missing id is indistinguishable (uniform 404). The read-side ownership invariant.
- **W8.5b.9** (α8.5b.3r) — read-state mutations modify only notification metadata and never alter
  projection identity, source-event linkage, or delivery provenance: `mark_read` / `mark_all_read`
  write only `read_at`, never `id`/`user_id`/`kind`/`title`/`body`/`payload`/`source_event_id`/
  `delivered_in_app_at` (mirror of W8.5b.6/7 — the read side cannot re-project or re-key).
- **W8.5b.10** (α8.5b.3r) — notification ordering is observational only; read-state mutations must
  not affect feed ordering. The feed is ordered purely by `(created_at, id)`, independent of
  `read_at`, so marking one read or all read never moves a notification or reshuffles the feed.

---

## Unfrozen surface (safe to extend)

> The **render composition layer** is unfrozen but **bounded** by
> [`ADR-0043`](../decisions/ADR-0043-render-composition-boundary.md): it may grow
> (audio mixing — shipped α8.4e; transitions, effects, quality tuning — later) provided
> composition stays a pure, reproducible `Timeline + MediaAssets + configuration → video`
> transform (**RC1–RC6**, incl. renderer purity) — never reading orchestration/provider
> state, never doing enrichment, and never mutating rendered media in place. Render
> **execution & scalability** (for future GPU/distributed rendering) is bounded by the
> performance invariants **RP1–RP9** (ADR-0043 Appendix A): no provider/orchestration I/O,
> deterministic/idempotent retries, bounded memory + temp storage, horizontal
> statelessness, and CPU/GPU/remote interchangeability behind `IRenderer`. A design
> boundary, not a freeze (no guard).

Everything **not** in the frozen list above is the intentional growth surface that
new slices plug into — additive by construction:

- **Concrete provider adapters** (`infrastructure/ai/providers/openai/…`,
  `…/fal/…`) — new models/providers behind the frozen ports.
- **Ingress + downstream use cases** — completion ingresses (poll/webhook),
  media ingestion, the render worker, and future consumers.
- **Ports + adapters for new concerns** — `IObjectStorage`, `IMediaDownloader`,
  `IRenderer`, `IWebhookVerifier`, and their infrastructure leaves.
- **Repositories** — additive, non-frozen methods (e.g. `get_ownership`,
  `find_paused_by_provider_job_id`, `get_by_storage_coords`, the render-job worker
  transitions), and new repositories entirely.
- **Routers, DI/container wiring, config, tests** — the composition layer.

New subscribers can attach to the existing event stream (`WorkflowRunSucceeded`,
`RenderJobSucceeded`, `ExportJobSucceeded`, …) without the orchestration core ever
knowing — the property the freeze was designed to preserve.

**Event projection pattern (established α8.5b.3).** `NotificationProjection` is the platform's
first *general* event projection and sets the precedent for all that follow (analytics, billing,
audit, search indexing, …): immutable domain events may be consumed by **multiple independent
downstream projections**, each of which (a) only reads a terminal event and writes its own
read/product state — never feeding back into the producer, (b) **owns its own persistence invariant
and idempotency strategy** so the *database* owns uniqueness and the subscriber stays stateless
(kin to `MediaAsset` storage-coords, `ExportJob` partial-unique, `Notification`
`(user_id, source_event_id)`), and (c) attaches behind `PublisherPort` without the producer's
knowledge. A future graph invariant — *a projection must never invoke another projection* (keep the
graph a fan-out `Event → {A, B, C}`, never a chain) — is noted for adoption once several projections
exist. See [`ADR-0041` §Event projection pattern](../decisions/ADR-0041-provider-runtime-contract.md).

---

## Deferred architecture guards

Architectural invariants that are **currently upheld by design** — no code path can
violate them today — but whose **automated enforcement is intentionally deferred**
until the corresponding risk actually appears. Recording them here converts
"currently true because of the design" into a conscious, tracked decision, so a
future contributor knows the guard was *deferred, not forgotten*.

Every architectural rule aims for three artefacts: **documentation** (ADR/contract),
**implementation** (the code follows it), and **enforcement** (CI / import-linter /
test that prevents regression). The guards below have the first two; the third
ships as part of the slice named under **Trigger** — proportional to the risk,
rather than guarding against code that does not yet exist.

| Guard | Reason deferred | Trigger |
|---|---|---|
| **The resolver must not consume verification output** (verification cannot influence routing / candidate ranking) | The resolver takes only immutable catalogue + runtime snapshots; verification output has no path into `resolve()`. Documented by ADR-0045 resolver purity and structurally impossible today (domain-isolation import contract). A guard now would defend against code that does not exist. | **Verification V2** — when verification gains richer confidence signals that someone might reasonably want to bias routing with. Ship an import-linter contract (`app.domain.resolver` must not import `app.domain.generation.verification`) **plus** a resolver regression test proving verification output cannot affect ranking. |
| **A dedicated "no provider-specific branching" semantic assertion in the planner** | Structural isolation already makes it impossible for the planner (`app.domain`) to import providers, and CS-8 bans provider *language* in planner output — the two existing guards cover the realistic failure modes. No observed pressure. | **First provider that requires planner changes** — if a provider ever motivates planner-side logic, add the explicit semantic assertion at that point. |

> This table is descriptive governance, not an invariant registry: an entry graduates
> out of it (and into an enforced import-linter contract / test) the moment its
> trigger slice lands.

---

## Remaining roadmap

| Slice | Scope |
|---|---|
| **α8.4f** | Render composition — transitions / crossfades / color grading / effects / subtitle burn-in. Blocked on the α6.4 Timeline **authoring** write paths (`transition_in_id`/`transition_out_id`/`effects`/subtitles); ADR-0043 RC1–RC6 |
| **α8.5a** | ✅ Export engine — delivery encodings (`ExportWorker` / `ProcessExportJob` / `IExporter`); shipped `v0.4.30` |
| **α8.5b.1** | ✅ Download serving — `DownloadExport` + `IDownloadDelivery` (`LocalStreamDelivery`); shipped `v0.4.31` |
| **α8.5b.2** | ✅ Storage backends & signed-URL delivery — `StorageResolver`/`DeliveryResolver` + `S3ObjectStorage` (S3/R2) + `S3RedirectDelivery` (fixed-TTL presigned `302`); write-active/read-persisted (E2); GCS/Azure plug in the same way; shipped `v0.4.32` |
| **α8.5b.3** | ✅ Notifications — `NotificationProjection` (2nd relay consumer) on `ExportJobSucceeded`/`ExportJobFailed`; exactly-once per recipient per source event, DB-enforced (partial `UNIQUE (user_id, source_event_id)`); in-app write path only; shipped `v0.4.33` |
| **α8.5b.3r** | ✅ Notifications read API — `GET /notifications` (keyset feed), `GET /notifications/unread-count`, `POST /notifications/{id}/read` + `/read-all` (action verbs); owner-scoped read-model completion on the α8.5b.3 projection (W8.5b.8/9/10); zero migration; archive/inbox features out; shipped `v0.4.34` |
| **α8.5b.4** | Notification channels — email (`INotifier` + provider/templates/retries), later push/websocket |
| **α8.5c** | Capability Catalogue & Provider Registry (tooling) — three design-time manifests + offline validator + CI Stage 0; *capability → providers*, score+strategy, static-only; runtime never reads the YAML (W8.5c.2). No runtime/migration/version bump |
| **α8.5x** | **AI Runtime & Generation Architecture (governance, ADR-0044 — Accepted)** — verify-driven, capability-first, local-first pipeline (Plan → Generate → Verify → Repair); requirements AR1–AR18 + invariants W8.5x.1–W8.5x.18; local execution/hardware abstraction. Carves out the **Minimum Runtime Contract MRC-1…MRC-8** as the Phase-1 MVP scope (the only runtime α8.5d/α8.5e + the first generation slice must ship); Phase 2 defers the AR depth. Governs α8.5d→α8.6. Docs-only |
| **α8.5d** | Seed — YAML → DB seeder + additive migration (DB = populated runtime source of truth; carries execution/hardware/mode metadata per ADR-0044 X-G; α8.5x) |
| **α8.5e** | Capability resolver — local-first / free-first selection (MRC-2/-3; ADR-0041 D2 realised; α8.5x) |
| **α8.5x-mrc** | MRC — the MVP: planner → character memory → scene-by-scene → generate → threshold verification → resume → asset cache → render → export, capturing AR18 provenance (α8.5x) |
| **α8.5x-mrc.4** | **Execution Runtime & Provenance (Increment 4; ADR-0046 — Accepted)** — makes the Execution plane persistent: generation ledger (`generations` + `generation_shots`), execution artefact registry (`generation_assets`, `parent_asset_id` lineage graph), model cache (`model_cache`), `generations.status` state machine, and outbox lifecycle events. Raw-SQL/ORM-less (migration 0012); freezes Execution-plane boundaries X1–X8 (`EXECUTION_RUNTIME_CONTRACT.md`). `generation_assets` is execution-owned; promotion to `media_assets` is deferred to an explicit `PublishGenerationAssets` |
| **α8.6** | Publishing — `PublishJob` + `SocialAccount` + destination OAuth (a new bounded context; destinations are not AI providers; its own parallel registry, shared α8.5c tooling; after the AR runtime) |

All remaining work is **downstream of / additive to the frozen orchestration
platform** (ADR-0042 Gate 1) and respects the render composition boundary
(ADR-0043 Gate 2) — new capabilities composed on stable seams, not redesigns of
the workflow engine. The α8.5x AI runtime (ADR-0044) keeps that discipline: the
planner sits *upstream* of the frozen runner, verify/repair *downstream*, and the
resolver is the ADR-0041 D2 extension point — zero freeze overrides.
