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
| **Application version** | `0.4.40-phase3-alpha8.9a` (code constant — matches the tag at the α8.9a finalize) |
| **Latest runtime tag** | `v0.4.40-phase3-alpha8.9a` |
| **Phase** | Phase 3 — orchestration era (α7+) |
| **Orchestration core** | **Frozen** since `v0.4.23` (ADR-0042, 2026-07-22) |
| **Freeze overrides used to date** | **0** (α8.3b, α8.4a–e, α8.5a, α8.5b.1–3, α8.5b.3r, α8.5c–e, the α8.5x execution runtime + first generation slice, α8.7 Planner V2, α8.6a publishing account connections, α8.6b publish runtime, α8.6c destination adapters, the α8.8 asset promotion bridge, and the α8.9a publish notifications all shipped additively) |

The project has crossed from *building the orchestration engine* to *building
capabilities on top of a stable platform*. Every slice since the freeze has been
strictly additive — no frozen path changed, no `Freeze-Override:` trailer used.

> **Tag/version note.** Milestone tagging lapsed after `v0.4.34-phase3-alpha8.5b3r`:
> α8.5c, α8.5d, α8.5e, the α8.5x execution runtime + first generation slice, and
> α8.7 Planner V2 all landed **folded into the single `v0.4.35-phase3-alpha8.7`
> baseline tag**. Per-slice tagging **resumed at α8.6a** (`v0.4.36-phase3-alpha8.6a`),
> and the FastAPI `version` constant in `backend/app/main.py` was **realigned** to match
> at that finalize (it previously lagged at `0.4.34-phase3-alpha8.5b3r`).
>
> **Release ordering.** α8.6 (Publishing) was intentionally completed *after* the α8.7
> baseline — the roadmap deferred Publishing until after the AI/AR runtime — so the
> numeric prefix moves forward (`0.4.36`) while the roadmap-milestone suffix steps back
> to `alpha8.6a`. Publishing is its own bounded context, not an α8.7 increment. (The
> `v0.4.35` tag annotation loosely applied "α8.6" to the *execution runtime*, which this
> document records authoritatively as **α8.5x**; **α8.6 is Publishing**.)

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

The orchestration pipeline is complete through rendering, export, and delivery, and
a persistent **generation runtime** (Planner → Storyboard → Resolver → … → Export)
now sits *upstream* of the frozen runner — see [`SYSTEM_MAP.md`](../engineering/SYSTEM_MAP.md)
for the end-to-end map. Each boundary is clean — providers know nothing about
rendering, rendering knows nothing about providers, orchestration doesn't know
FFmpeg exists, completion doesn't know storage exists, storage doesn't know
timelines exist:

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
| Capability catalogue & provider registry (design-time) | ✅ | α8.5c / α8.5d | three YAML manifests (`capabilities`/`providers`/`routing`) + optional `devices.yaml`, strict Pydantic loaders, offline validator, CI Stage 0; the runtime never reads the YAML (W8.5c.2); enriched for the resolver + planner (capability dependencies, feature matrix, resource/cost estimates, device profiles) |
| Catalogue seed → DB | ✅ | α8.5d | idempotent `YAML → validator → seeder → DB`; additive migration; the DB is the runtime source of truth, carrying execution/hardware/mode metadata (ADR-0044 X-G) |
| Capability resolver | ✅ | α8.5e | local-first / free-first, **explainable** candidate resolution over immutable catalogue + runtime snapshots; writes a resolution ledger (ADR-0041 D2 realised; AI-runtime core frozen by [`ADR-0045`](../decisions/ADR-0045-ai-runtime-core-freeze.md), [`RESOLVER_RUNTIME_CONTRACT.md`](../engineering/RESOLVER_RUNTIME_CONTRACT.md)) |
| Execution runtime & provenance | ✅ | α8.5x-mrc.4 (Increment 4) | persistent Execution plane: `generations` / `generation_shots` / `generation_assets` (+ `parent_asset_id` lineage), `model_cache`, the `generations.status` state machine, and transactional-outbox lifecycle events; raw-SQL / ORM-less (migration `0012`); Execution-plane boundaries X1–X8 frozen ([`ADR-0046`](../decisions/ADR-0046-execution-runtime-boundaries.md), [`EXECUTION_RUNTIME_CONTRACT.md`](../engineering/EXECUTION_RUNTIME_CONTRACT.md)) |
| First end-to-end generation slice | ✅ | α8.5x-mrc (Increment 5) | prompt → plan → storyboard → resolve → generate → verify → render → export, persisted with AR18 provenance; proven on an ephemeral Postgres + ffmpeg (CI Stage 13); golden-scenario regression, Pollinations wired purely as an `IImageGenerator` |
| Cinematic storyboard — Planner V2 | ✅ | α8.7 | `ShotIntent` value object + data-driven `StoryArcTemplate` (3/5/6 arcs); deterministic, position-independent shot ids + `blake2b` per-shot seeds; invariants CS-7 (adjacent shots differ) + CS-8 (no provider language in intent); Golden V1 frozen, Golden V2 active ([`CINEMATIC_STORYBOARD_CONTRACT.md`](../engineering/CINEMATIC_STORYBOARD_CONTRACT.md)) |
| Publishing — account connections (OAuth) | ✅ | α8.6a | first slice of the **Publishing** bounded context (credential + connection ownership only — no `PublishJob`/upload yet): `SocialAccount` aggregate, envelope-encrypted `social_credentials` (AES-256-GCM, per-record DEK wrapped by a fail-closed master key — the DB never holds a plaintext/usable token, ADR-0047 C1/C2), ports `ISocialCredentialStore`/`ISocialOAuthClient`/`IOAuthStateSigner` (Mock OAuth this slice), owner-scoped `/api/v1/social-accounts`; additive migration `0013`; import-linter crypto-confinement + bounded-context isolation; CI Stage 14; PUB-1…PUB-10 ([`PUBLISHING_RUNTIME_CONTRACT.md`](../engineering/PUBLISHING_RUNTIME_CONTRACT.md), [`ADR-0047`](../decisions/ADR-0047-publishing-credential-ownership.md)) |
| Publishing — publish runtime | ✅ | α8.6b | second Publishing slice (upload execution): user-initiated `PublishJob` (explicit `project_id`, DQ1) + poll-ingress `PublishWorker`, a faithful adaptation of the `ExportJob` execution model (DQ8) — dual lease (`publish_job:<id>` then `project_publish:<project_id>`, DQ5), version-fenced CAS, bounded capped-exponential-backoff retries with adapter-classified failures (DQ6), `(source_media_asset_id, social_account_id)` idempotency backstop (DQ2), and PascalCase terminal outbox events only (`PublishJobCreated`/`PublishJobSucceeded`/`PublishJobFailed`, DQ4/DQ7); **credential-blind** runtime consuming only the α8.6a `AuthorizedContext` (DQ3); `ContentPackage` (default visibility private) + Mock destination behind `IDestinationPublisher`/`IDestinationRegistry`; top-level `/api/v1/publish-jobs`; additive migration `0014`; import-linter credential-blind-leaves contract; CI Stage 14 ([`PUBLISHING_RUNTIME_CONTRACT.md`](../engineering/PUBLISHING_RUNTIME_CONTRACT.md), [`ADR-0047`](../decisions/ADR-0047-publishing-credential-ownership.md)) |
| Asset promotion bridge (`generation_assets` → `media_assets`) | ✅ | α8.8 | the **ADR-0046 X8** (`PublishGenerationAssets`) seam — connects the AI generation runtime (Path B, execution-owned `generation_assets`) to the platform media library (Path A, `media_assets(source='generated')`) that already feeds Render → Export → Publish. An explicit, user-initiated `PromoteGenerationAssets` use case, **library-only** (ends at `media_assets`; Render/Export/Publish/Timeline/orchestration untouched): request-time project-scoped ownership (mirrors `IngestGeneratedMedia`), byte **copy** under a deterministic key (copy never reference), recomputed checksum + provenance in `source_metadata`, storage-coordinate-uniqueness idempotency → `noop` replay; `POST /api/v1/media/promotions`; one new **read-only** `IGenerationReader` port (no existing port changed); **no schema migration**; import-linter X8 contract ("Execution Runtime never writes the media library") + CI Stage 15 ([ADR-0046](../decisions/ADR-0046-execution-runtime-boundaries.md), [`PHASE3_ASSET_PROMOTION_BRIDGE_PREFLIGHT.md`](../engineering/PHASE3_ASSET_PROMOTION_BRIDGE_PREFLIGHT.md)) |
| Publishing — destination adapters (YouTube) | ✅ | α8.6c | third Publishing slice (first real destination, **adapter-only — no migration, no port change, no runtime expansion**, EQ1–EQ5): two credential-blind infrastructure leaves behind the unchanged α8.6b ports — `YouTubeOAuthClient` (`ISocialOAuthClient`: consent URL / code exchange + channel-identity lookup / refresh / revoke) and `YouTubeDestination` (`IDestinationPublisher`: Data API v3 `videos.insert` resumable upload, deterministic `ContentPackage → snippet/status` mapping, Google-error → `DestinationError(retryable)` classification) — over a thin injected `httpx` transport (EQ2, no Google SDK); configuration-blind `Settings` + composition-root wiring that registers `"youtube"` only when credentials are set (**fail-soft**); **PUB-11** (ambiguous post-transmission outcome ⇒ permanent, never retried — no double-post, EQ3); network-free unit tests via `httpx.MockTransport` + an **opt-in live smoke test excluded from CI** (Stage 14 stays deterministic, EQ4); credential-blindness still enforced by the existing import-linter contracts (`AuthorizedContext` the only credential crossing into the adapter) ([`PUBLISHING_RUNTIME_CONTRACT.md`](../engineering/PUBLISHING_RUNTIME_CONTRACT.md) PUB-1…PUB-11, [`ADR-0047`](../decisions/ADR-0047-publishing-credential-ownership.md)) |
| Publishing — publish notifications | ✅ | α8.9a | first increment of the **α8.9 Creator Experience** milestone — fulfils the deferred **DQ7** fan-out. A new `PublishNotificationProjection` (a faithful twin of the export `NotificationProjection`) consumes only the terminal `PublishJobSucceeded`/`PublishJobFailed` outbox events and projects each into exactly one in-app notification (`publish.succeeded`/`publish.failed`) for the event's `requested_by_user_id`, reusing the existing `CreateNotification` writer (fresh per event → own UoW) and the DB-owned `(user_id, source_event_id)` unique index for exactly-once under relay redelivery; registered as a **third** independent consumer on the existing `InProcessPublisher` fan-out (producers + relay untouched, ADR-0042). **Strictly additive: no migration, no new port, no ADR**; carries no credential/URL/bytes (PUB-8 / ADR-0047 C8); CI Stage 16 ([`PHASE3_CREATOR_EXPERIENCE_PREFLIGHT.md`](../engineering/PHASE3_CREATOR_EXPERIENCE_PREFLIGHT.md)) |

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

> Shipped slices (α8.5a → α8.5b.3r, α8.5c → α8.5e, the α8.5x execution runtime +
> first generation slice, α8.7 Planner V2, α8.6a publishing account connections,
> α8.6b publish runtime, α8.6c destination adapters — YouTube, the α8.8 asset
> promotion bridge, and the α8.9a publish notifications) have moved up into
> *Completed capability lifecycles*. Only genuinely future work remains below.

| Slice | Scope |
|---|---|
| **α8.4f** | Render composition — transitions / crossfades / color grading / effects / subtitle burn-in. Blocked on the α6.4 Timeline **authoring** write paths (`transition_in_id`/`transition_out_id`/`effects`/subtitles); ADR-0043 RC1–RC6 |
| **α8.5b.4** | Notification channels — email (`INotifier` + provider/templates/retries), later push/websocket |
| **α8.9b / α8.9c** | Creator Experience — remaining increments: creator-set scheduling (an optional platform-native publish time mapped to the existing `ContentPackage.publish_at`) and a read-only owner-scoped creator dashboard aggregating existing product state (both scoped in [`PHASE3_CREATOR_EXPERIENCE_PREFLIGHT.md`](../engineering/PHASE3_CREATOR_EXPERIENCE_PREFLIGHT.md)) |
| **α8.6d′** | Publishing — a **second destination** behind the proven `IDestinationPublisher` seam (the publish-notifications half of the original α8.6d/DQ7 retired into **α8.9a**) |

All remaining work is **downstream of / additive to the frozen orchestration
platform** (ADR-0042 Gate 1) and respects the render composition boundary
(ADR-0043 Gate 2) — new capabilities composed on stable seams, not redesigns of
the workflow engine. The α8.5x AI runtime (ADR-0044) keeps that discipline: the
planner sits *upstream* of the frozen runner, verify/repair *downstream*, and the
resolver is the ADR-0041 D2 extension point — zero freeze overrides.
