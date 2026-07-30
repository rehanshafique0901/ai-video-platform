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
| **Application version** | `0.4.52-phase3-alpha9.9` (code constant — matches the tag at the α9.9 finalize) |
| **Latest runtime tag** | `v0.4.52-phase3-alpha9.9` |
| **Phase** | Phase 3 — orchestration era (α7+) |
| **Orchestration core** | **Frozen** since `v0.4.23` (ADR-0042, 2026-07-22) |
| **Freeze overrides used to date** | **0** (α8.3b, α8.4a–e, α8.5a, α8.5b.1–3, α8.5b.3r, α8.5c–e, the α8.5x execution runtime + first generation slice, α8.7 Planner V2, α8.6a publishing account connections, α8.6b publish runtime, α8.6c destination adapters, the α8.8 asset promotion bridge, the α8.9a publish notifications, the α8.9b creator scheduling, the α8.9c creator dashboard, the α9.0 creator analytics foundation, the α9.1 AI caption & hashtag generation, the α9.2 media library foundation, the α9.3 publish thumbnail support, the α9.4 multi-destination publishing, the α9.5 notification delivery (email), the α9.6 TikTok destination adapter, the α9.7 generation ingress, the α9.8 worker runtime host, and the α9.9 execution adapter dispatch all shipped additively) |

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
| Publishing — creator scheduling | ✅ | α8.9b | second increment of the **α8.9 Creator Experience** milestone — lets a creator schedule a YouTube go-live via the existing pipeline. Adds the one missing **creator-facing ingress**: an optional `publish_at` on `PublishJobCreateRequest`, validated at the boundary (timezone-aware + strictly future, normalised to UTC — SC3), threaded through `CreatePublishJob.execute` into the already-wired `build_content_package(publish_at=…)` → the α8.6c YouTube mapping (`publish_at` ⇒ `privacyStatus=private` + `status.publishAt`). **Platform-native scheduling, not worker deferral**: the job still uploads immediately and `publish_jobs.scheduled_at` stays `None`, so the runtime is unchanged (SC1); idempotency preserved — a replay does **not** reschedule (SC5). **Strictly additive: no scheduler/cron/timer/loop, no migration, no new port, no ADR**; scheduling coverage extends **Stage 14** in place ([`PHASE3_ALPHA8_9b_PREFLIGHT.md`](../engineering/PHASE3_ALPHA8_9b_PREFLIGHT.md)) |
| Creator dashboard | ✅ | α8.9c | third + final increment of the **α8.9 Creator Experience** milestone (completes it) — a **read-only** owner-scoped `GET /api/v1/dashboard/summary` surfacing the caller's product state as scalar counts: publish-job counts by `PublishStatus` (+ total; every status always present), connected/total social accounts, the unread notification count, and the media total. A new `GetCreatorDashboard` use case composes these inside a **single** `IUnitOfWork` from the **already-existing** owner-scoped reads (`publish_jobs.list_for_owner`, `social_accounts.list_for_owner`, `notifications.count_unread`, `media.list_owned`) — all scope from `CurrentUserDep`, so a fresh caller sees all-zero (CD3/CD4). **Strictly additive: no new repository method, no new SQL, no migration, no new port, no ADR, no analytics subsystem** (the dormant `analytics_events` table stays untouched); CI Stage 17 ([`PHASE3_ALPHA8_9c_PREFLIGHT.md`](../engineering/PHASE3_ALPHA8_9c_PREFLIGHT.md)) |
| Creator analytics foundation | ✅ | α9.0 | activates the dormant, partitioned `analytics_events` table as a **fourth independent** downstream outbox consumer (ADR-0042 fan-out): a new `AnalyticsProjection` maps the publish + export lifecycle events to a stable `event_name` vocabulary + neutral property subset and, through the reused `RecordAnalyticsEvent` writer (fresh per event → own UoW; tenant resolved in-UoW), persists one owner-scoped row each. **Exactly-once is DB-owned** ([`ADR-0048`](../decisions/ADR-0048-analytics-consumer-idempotency.md)): `source_event_id = event.id`, `occurred_at = event.occurred_at` (deterministic, never `now()`), and the partial-unique `uq_analytics_events_source_event_id` over `(source_event_id, occurred_at)` — which includes the partition key so it is valid on / auto-propagates across the partitioned table (empirically verified vs PostgreSQL 17.10) — refuses a relay redelivery as a no-op. Exposed via a **read-only** owner-scoped `GET /api/v1/analytics/summary` (`GetCreatorAnalytics`: per-`event_name` counts + total, zero-filled over the full vocabulary; trailing-30d default window; naive/inverted window → 422). **Additive: migration `0015` (dedup column + unique index + owner-read index), one new `IAnalyticsRepository` port, ADR-0048; no producer or frozen-runtime change**; CI Stage 18 ([`PHASE3_ALPHA9_0_PREFLIGHT.md`](../engineering/PHASE3_ALPHA9_0_PREFLIGHT.md)) |
| AI caption & hashtag generation | ✅ | α9.1 | the **first real consumer** of the `Capability.LLM` seam — an **opt-in, advisory** `POST /api/v1/publish-metadata/suggestions` that suggests publish metadata (title / description / hashtags) for a creator's finished, owned export. A new **Publishing-owned** `IPublishMetadataGenerator` port (neutral, `ContentPackage`-free DTOs) is implemented by an AI-subsystem adapter `LlmPublishMetadataGenerator` over `Capability.LLM` (the deterministic mock by default) — the **sole publishing→AI bridge**, mechanically pinned one-way by a new import-linter contract ("AI plane never imports the Publishing bounded context"). The `GeneratePublishMetadata` use case validates ownership/readiness first (`404`→`422`), then calls the port **outside** the read UoW with a **mandatory deterministic template fallback** (PUB-9 preserved) on any AI failure/timeout. **Advisory only** ([`ADR-0049`](../decisions/ADR-0049-ai-publish-metadata-boundary.md) invariants): never a `PublishJob` prerequisite; graceful degradation; user edits always win; only final creator-selected metadata is persisted (provenance ephemeral); the destination adapter stays AI-unaware. **Strictly additive: no migration, no frozen-runtime change, one new port + one import-linter contract, ADR-0049**; CI Stage 19 ([`PHASE3_ALPHA9_1_PREFLIGHT.md`](../engineering/PHASE3_ALPHA9_1_PREFLIGHT.md)) |
| Media library foundation | ✅ | α9.2 | a deterministic, owner-scoped **Asset Library** over registered `media_assets` (ADR-0037 **CR-8**, no new ADR) — folders (self-referencing tree) + curated library entries carrying name / description / `text[]` tags / reuse counters, all sibling to the Media aggregate (never mutates `media_assets`; references one asset by id, `uq_library_assets_media_asset_id`). A new `ILibraryRepository` + 11 `library.*` use cases back `/api/v1/library/*`: folder + asset CRUD, keyset **browse**, tag GIN **ANY-of** filter, **version-fenced** asset OCC (404-before-412), folder-move **cycle rejection**, folder-delete **asset detachment** (SET NULL), and **idempotent** reuse recording. Browse/get **hide entries whose media is soft-deleted**. The pre-built `embedding vector(1536)` column and vector search are **deliberately out of scope** (deferred to a future increment + its own ADR). **Strictly additive: no migration, no new port beyond the repository, no ADR, no frozen-runtime change**; CI Stage 20 ([`PHASE3_ALPHA9_2_PREFLIGHT.md`](../engineering/PHASE3_ALPHA9_2_PREFLIGHT.md)). **Accepted limitation**: the frozen `uq_library_folders_parent_folder_id_name` index (default NULL-distinct, no `owner_user_id`) can't enforce per-owner **root-folder** uniqueness, so under the no-migration constraint it is **application-enforced** (`folder_name_conflicts`) — leaving a narrow, **non-corrupting** TOCTOU race for root folders only (READ COMMITTED); child-folder uniqueness stays DB-enforced. Permanent fix = a future per-owner partial-unique-index migration (no ADR) |
| Publishing — publish thumbnail support | ✅ | α9.3 | an **optional, best-effort** custom thumbnail for a publish ([`ADR-0050`](../decisions/ADR-0050-publish-thumbnail-source-and-delivery-boundary.md), **Option A — creator-supplied**). A creator may nominate one of their **own `image`** `media_assets` via an additive `thumbnail_media_asset_id` on `POST /api/v1/publish-jobs`; `CreatePublishJob` verifies ownership + `kind='image'` **before** queuing (`404` non-owned / `422` non-image) and captures the id **immutably** into the already-existing `ContentPackage.thumbnail_media_asset_id`. The frozen `IDestinationPublisher.publish` signature is **unchanged** — only the `UploadMedia` DTO gains an optional `thumbnail: UploadThumbnail` handle (additive, backward-compatible; existing adapters ignore it). `ProcessPublishJob` resolves + materialises the owned image **in the worker** (owner-scoped — the adapter never resolves or generates it) and attaches it; `YouTubeDestination` performs `thumbnails.set` **after** a durable `videos.insert` (**best-effort**: any failure is swallowed + logged and **never retried** — a retry would re-run `videos.insert` and risk a duplicate public video, preserving **PUB-11**). A missing/soft-deleted image or a materialisation failure degrades to **no thumbnail with the video still published** — the thumbnail is **advisory and never blocks or retries** the primary publish. **Strictly additive: no migration (`content_package.thumbnail_media_asset_id` already existed), no AI, no thumbnail generation, no lineage resolution, no new provider, no new ADR beyond ADR-0050, no frozen-runtime change beyond the additive `UploadMedia` evolution**; CI Stage 21 ([`PHASE3_ALPHA9_3_PREFLIGHT.md`](../engineering/PHASE3_ALPHA9_3_PREFLIGHT.md)) |
| Publishing — multi-destination publishing | ✅ | α9.4 | a single creator action that **fans out** one finished export publish to **N** of the caller's connected accounts — the capstone of the publishing workflow (captions α9.1 + thumbnails α9.3 + scheduling α8.9b compose once and apply to every channel). A new additive `POST /api/v1/publish-jobs/batch` (the single-create `POST /publish-jobs` is **unchanged**) accepts `social_account_ids[1..20]` (bounded + duplicate-free) plus the shared metadata overrides. **Orchestration only**: the new `CreatePublishJobs` composes the **unchanged** `CreatePublishJob` once per account — it duplicates **no** validation, idempotency, or persistence logic. A **shared** prerequisite failure (the export, or the optional thumbnail) aborts the whole request (fail-fast `404`/`422`); a **per-account** failure (account not owned / not connected / unsupported platform) is recorded as that item's outcome so one bad account never blocks the rest (classified from the existing error `details` keys — the single validation source is untouched). The response `data` is an ordered per-account outcome array (`created` / idempotent replay / neutral `error`) so callers never infer outcomes. **Idempotency (PUB-7) is preserved** — each account independently replays its own existing job. Every created job is an ordinary `PublishJob`, so scheduling, captions, thumbnails, notifications (α8.9a), and analytics (α9.0) all apply with **no** batch-specific logic. **Strictly additive: no migration, no ADR, no new port, no frozen-runtime change**; CI Stage 22 ([`PHASE3_ALPHA9_4_PREFLIGHT.md`](../engineering/PHASE3_ALPHA9_4_PREFLIGHT.md)) |
| Notification delivery — email | ✅ | α9.5 | fulfils the long-deferred **α8.5b.4** channel: the platform's **first outbound external-communication channel that is not a publish destination** ([`ADR-0051`](../decisions/ADR-0051-notification-delivery-email-idempotency-and-boundary.md)). A **dedicated leased poll worker** (`NotificationEmailWorker`, deliberately **off** the relay fan-out — the relay still owns only the in-app projection write) drains undelivered notifications FIFO and hands each to `ProcessNotificationEmail`, which takes a per-notification `notification_email:<id>` lease, resolves the recipient **owner-scoped**, sends **outside any transaction**, then **stamps** `delivered_email_at` (**send-then-stamp**, D1-C) — so a delivered row is never re-scanned. A transient failure earns a capped exponential backoff retry; a permanent failure or the attempt ceiling is **terminal** (D3). The application **owns** the `INotifier` port + neutral DTOs and infrastructure supplies the adapter (D5, strictly one-way): `LoggingNotifier` (mock-first, fail-soft default — keeps CI deterministic) and the config-gated `SmtpNotifier`, both **recipient-blind, PII-minimal leaves** (D4 — masked telemetry only), pinned by two new import-linter contracts (no persistence/use-case/domain imports; `aiosmtplib` confined to the adapter package). **Correctness never depends on provider-side deduplication** (ADR-0051 Appendix A is non-normative — no major provider offers deterministic email-send dedup): the lease + send-then-stamp model gives **at-least-once** delivery with a bounded, explicitly-accepted rare-duplicate window. **Strictly additive: no migration, no schema change, no relay change, no frozen-runtime change**; CI Stage 23 ([`PHASE3_ALPHA9_5_PREFLIGHT.md`](../engineering/PHASE3_ALPHA9_5_PREFLIGHT.md)). **Payload treatment**: retry/terminal bookkeeping lives in a reserved `payload["_email"]` namespace (success uses the pre-existing `delivered_email_at` column), stripped **centrally at the single repository row→entity boundary** so it can never reach any endpoint — an **implementation detail, never part of the public notification contract**; `NotificationPublic.payload` is behaviourally unchanged for existing consumers, and a future migration may relocate the bookkeeping into dedicated columns with **no** external behaviour change. **Accepted limitations**: the deliverable `delivered_email_at IS NULL` scan is **unindexed** (acceptable at current beta scale) and retry bookkeeping stays in the reserved namespace — no indexing/optimisation/schema work was done outside this slice |
| Publishing — TikTok destination adapter | ✅ | α9.6 | fulfils the deferred **α8.6d′** row: the platform's **second real publish destination**, proving the `IDestinationPublisher` seam generalises beyond its first implementation. `TikTokDestination` drives the Content Posting API "Direct Post" flow (`creator_info/query` → `video/init` → chunked `PUT` → `status/fetch`) as a **credential-blind leaf** (PUB-5 / ADR-0047 C4, pinned by the pre-existing import-linter contract), and `TikTokOAuthClient` fills the α8.6a `ISocialOAuthClient` seam — including TikTok's `client_key` naming, comma-separated scopes, `open_id` identity, and **rotating refresh tokens**, which the credential service re-encrypts unchanged. **The frozen `IDestinationPublisher.publish()` contract is untouched**: TikTok's *asynchronous* publish model is adapted **entirely inside the adapter** via bounded status polling, so **no use case, worker, scheduler, or API gained TikTok-specific logic** and **α9.4 multi-destination publishing now fans out across two real adapters with zero batch-side change**. **Exactly three adapter outcomes**: success (terminal publish inside the budget), permanent failure (terminal failure from TikTok), and an **indeterminate timeout** (budget expires with no terminal state) — the last is logged with reconciliation diagnostics and **never retried**, preserving **PUB-11**. Post-upload a transient status-poll failure is **absorbed and re-polled**, never surfaced as retryable (escaping retryable after transmission would re-upload). **The external identifier is stable**: `platform_post_id` always holds the durable `publish_id` minted at init and is **never** overwritten by a later public post id; **no post URL is invented** — TikTok's canonical URL requires the creator's username, which a credential-blind adapter never sees, so a disclosed public post id is captured as diagnostic metadata only. **Nothing degrades silently**: an unsupported (`UNLISTED`) or creator-unoffered visibility fails validation explicitly, and a `publish_at` schedule is **rejected** rather than ignored; `fail_reason="internal"` is treated as **permanent** despite TikTok documenting it as retryable, because it can only surface after our bytes were accepted and the no-duplicate invariant outranks a provider retry recommendation. Registration is **fail-soft** (unconfigured ⇒ absent from the registry ⇒ create-time validation failure, no HTTP client opened), keeping CI deterministic and network-free. **Strictly additive: no migration, no ADR, no schema change, no new port, no frozen-boundary change**; CI Stage 24 ([`PHASE3_ALPHA9_6_PREFLIGHT.md`](../engineering/PHASE3_ALPHA9_6_PREFLIGHT.md)). **Accepted limitation**: a publish that outlives the polling budget (slow moderation) settles `failed` with `ambiguous_upload_outcome` even if TikTok later publishes it — **reconciliation is deliberately out of scope** (no reconciliation worker, no webhook support in this slice) |
| Generation ingress (creator-triggered generation) | ✅ | α9.7 | closes the platform's largest product gap: until this slice the **core capability** — `GenerateVideo` — had **no HTTP surface at all** and could only be invoked from a script, and `generations` rows carried **no owner**. Governed by [`ADR-0052`](../decisions/ADR-0052-generation-ingress-ownership-execution-and-read-contract.md), which resolves the ownership deferral recorded by **ADR-0046 Q1** and **α8.8 AP9** ("project-asserted, generation-unowned"). **Owner-scoped generation jobs** (migration `0016`: `tenant_id` / `owner_user_id` / `idempotency_key` / a durable `request` JSONB, plus an owner keyset index and a partial-unique idempotency index) behind a dedicated top-level `/api/v1/generations` resource — create (`201`, or `200` on idempotent replay), poll, keyset-list, and cancel. **Queued execution model** (D2-B): `POST` records intent and returns in milliseconds; a new `GenerationWorker` reaps, then claims and runs each generation under a `generation:<id>` lease renewed by heartbeat for the life of the run. **One generation execution is one external spend opportunity** — job-level retries do not exist: `max_attempts = 1` is *structural* (claiming is a `queued → planning` CAS and nothing ever writes a row back to `queued`), and a run abandoned by a crashed worker is **terminalised to `failed` by the reaper, never re-run**. **Explicit idempotency-key contract** (D4): repeating a prompt is a legitimate second take, so only a client-supplied `idempotency_key` collapses two requests into one — the deliberate opposite of publishing's natural-key dedup (PUB-7). **Polling is the only read contract for v1** (D3) over a **curated** projection: provenance, resolver internals, adapter/provider identities, component versions and `final_video_asset_id` never cross the wire (the ADR-0051 read-model hygiene lesson applied to a second bounded context), with `promotable` as the derived next-action signal. **GEN-1** (below) splits the write boundary: ingress owns identity, the execution runtime owns execution state only. **Also fixes a latent authorisation gap** — `PromoteGenerationAssets` previously loaded a generation by id alone, so anyone who learned an id could promote another creator's output into their own project (harmless while ids were server-internal, exploitable the moment ingress made them client-visible); `IGenerationReader` is now owner-scoped and promotion requires **both** project membership **and** generation ownership. **Additive apart from migration `0016`: no ADR beyond ADR-0052, no frozen-runtime change, no change to the generation pipeline itself**; CI Stage 25 ([`PHASE3_ALPHA9_7_PREFLIGHT.md`](../engineering/PHASE3_ALPHA9_7_PREFLIGHT.md)). **Accepted limitations** (permanent architecture, frozen at this release): **ownerless legacy generations remain intentionally invisible** through the public API and are non-promotable — **ownership inference is permanently prohibited** (no heuristic, no attribution, no backfill; the rows stay intact for administrative inspection outside `/api/v1`); **only `queued` generations are cancellable** — once a worker has claimed one, `cancel` returns `409` rather than reporting a stop that did not happen, since mid-run cooperative cancellation needs the pipeline to poll a flag between shots and is a deliberate future capability; and **v1 identity is a seed plus a global style** — characters, locations, props and reference images belong to Identity-Runtime authoring (a separate slice), so the request codec rejects unknown keys to keep that extension additive |
| Worker runtime host (background execution) | ✅ | α9.8 | closes the gap every prior slice quietly depended on: **no background work had ever run outside a test.** Across α8.1–α9.7 the platform accumulated **seven** poll workers — relay, generation, render, export, enrichment, publish, email — and every one was a `run_once()` library primitive with **zero production callers**. The outbox never relayed, so notifications (α8.9a) and analytics (α9.0) were dormant behind a working fan-out; no video ever rendered or exported; no publish ever left the queue; no email was ever sent. Governed by [`ADR-0053`](../decisions/ADR-0053-worker-runtime-host.md). **`app/runtime/` is a delivery layer peer to `app/api/`** — the API turns HTTP requests into application calls, the runtime turns *elapsed time* into them; both consume the layers below and are consumed by nothing, pinned by a new import-linter contract so no use case can reach the scheduler even by accident. **The supervisor knows nothing about any worker**: a pass is an opaque coroutine and its result is handed straight back to the spec's own `found_work` predicate, so `worker_registry` is the *only* type-aware component (it holds the seven specs, the `--workers` selector, and the enablement rules). **Registration decides whether a worker exists, not whether its passes no-op**: `email_delivery_enabled=false` removes the worker outright, and an unrecognised selector name **fails startup** rather than booting healthy with a capability silently absent. **Shutdown is stop-claiming-then-bounded-drain** (D3): on `SIGTERM` no worker starts another pass, an idling worker wakes immediately instead of serving out its ceiling, and the single in-flight pass gets a per-worker budget sized by *work-item duration* (generation `900s` ≫ media `300s` > publish `180s` > relay/email `60s`) before being cancelled, logged, and reported — the process exits `75` (`EX_TEMPFAIL`) so an abandoned generation is visible to the orchestrator rather than looking like a clean shutdown. **Failure is handled in two tiers** (D5): a failed pass — including one whose container factory raises before a coroutine exists, or whose `found_work` predicate breaks — is logged, counted, and backed off toward a ceiling with the worker still registered and polling; a supervision task that dies anyway is **replaced**, and one that cannot be kept alive is **escalated** (critical log, flagged in the host result, stale liveness marker, exit `70`) rather than left quietly absent. **Replica safety stays a per-worker obligation** (D4): the host provides none, and each worker's existing claim mechanism (lease, CAS, or `FOR UPDATE SKIP LOCKED`) is what makes a second host safe — replication **preserves** each worker's accepted semantics rather than strengthening them, so ADR-0051's at-least-once email window is unchanged, not narrowed. **Strictly additive: no migration, no schema change, no new port, no frozen-runtime change, and no worker's business logic altered**; CI Stage 26 ([`PHASE3_ALPHA9_8_PREFLIGHT.md`](../engineering/PHASE3_ALPHA9_8_PREFLIGHT.md)) is the first test in the repository where background work executes **without a test calling `run_once()`**. **Also changed**: per-item isolation added to render, export, enrichment, and publish (all four let one unclassified exception discard every item behind it), and batch sizes for those four plus generation and email cut to **one item per pass** — where a work item is far longer than the queue scan that finds it, a multi-item pass would keep claiming long after the host asked it to stop. Only the relay still batches. **Accepted limitations**: a generation in flight during a deploy can still be lost if the drain budget or the orchestrator's grace period is exceeded (**GEN-2** forbids retrying it — bounded, logged, and materially improved by one in-flight generation per host); liveness markers are **opt-in** via `worker_liveness_dir`; and **no container/deployment artefacts ship in this slice** — the entrypoint, selector, and markers all exist, but actually deploying the worker process is a follow-up |
| Execution adapter dispatch | ✅ | α9.9 | closes the oldest gap between the two planes: since α8.5e the resolver has produced an ordered, scored, explainable candidate list, and `GenerateVideo` has passed the winner's `adapter_id` to a **single hard-wired Pollinations client that discards it**. Governed by [`ADR-0054`](../decisions/ADR-0054-execution-adapter-dispatch.md). Two defects followed from one missing seam. **Provenance could lie**: `generation_shots.adapter_used` was written from the resolver's *choice*, so a row could name a provider that never produced the bytes — a data-integrity fault, not merely debt. And **the Decision plane could recommend an adapter that does not exist in the build**: under `AUTO` it prefers the LOCAL tier and returns `comfyui.flux_schnell`, which has no code, with nothing anywhere to notice. **`ExecutableAdapters` is a third resolver input** beside the catalogue and runtime snapshots — the catalogue is manifest-derived and identified by its digest, the runtime snapshot is measurement-derived, and what a deployment can *construct* is build-derived, so folding it into either would make that object's identity or origin incoherent; the resolver receives **data, never a registry**, so it stays pure. **`not_executable` is the first eligibility check**, and the position is load-bearing rather than stylistic: only the first failing constraint is reported, so checking executability first is what makes each recorded reason a complete account of executability — which is precisely why **no executable-set payload has to be persisted** to explain a decision later. **`IImageAdapterRegistry`** (port in application, `ImageAdapterRegistry` in infrastructure, populated at the composition root) is a closed table on the `DestinationRegistry` pattern: **no runtime `importlib`**, no `import_path` loading, and an unregistered key raises a permanent `AdapterNotRegisteredError` rather than falling back to a default that would silently break the binding provenance is asserted from. **Producer identity comes from the dispatch binding** (DISP-2), captured **at invocation** rather than read off the artefact — an adapter's self-report is never the source, because today's sole adapter echoes back the id it was asked for, so a misbinding would produce an artefact that corroborates its own error. Capturing at invocation is also what makes the **rejected-image** case expressible: verification failure discards the bytes, and production and acceptance are different events, so the producer stays on record. A **construction failure records no producer at all** — no shot row, so nothing claims an adapter ran, while the decision fields written before dispatch stay populated. **`execution_result` is a decision field**, ruled so rather than renamed: it records whether resolution produced a selection, is written before any adapter is invoked, and `FAILURE`/`FALLBACK` are unreachable on that column by construction. **Only `candidates[0]` executes** — no walking, no authored fallback-chain execution, no re-scoring in Execution. **Strictly additive: no migration, no schema change, no frozen-runtime change**; Stage 13 is **rewritten in place** ([`PHASE3_ALPHA9_9_PREFLIGHT.md`](../engineering/PHASE3_ALPHA9_9_PREFLIGHT.md)) because its previous assertion passed only while the offline double echoed the id it was handed — it asserted the defect. **Visible behaviour change**: an adapter that is both unconstructible and disabled now reports `not_executable` where it previously reported `adapter_disabled`, and `candidate_list` is API-visible. **Accepted limitations**: the ledger still records the *pure* resolver's candidate list while `chosen_adapter` is the winner *after* the application-layer tier cascade, so a row can name a chosen adapter that is not the top of its own list — α9.9 improves this (every `not_executable` verdict is now in the recorded list) without closing it, since closing it means either losing the filtered candidates the ledger exists to keep or a migration; and **no fallback walking, no usage metering, and no second real adapter** — a second adapter is what would make walking worth building |

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
- **GEN-1** (α9.7) — **ingress owns identity; the execution runtime owns execution state.** Neither
  writes the other's columns. `IExecutionRuntimeStore.begin()` is an idempotent *state-initialisation*
  operation, not a generic upsert: it **may create** the `generations` row when absent (the
  direct-invocation path — demo script, integration tests — needs no ingress and stays supported),
  **may initialise runtime-owned execution fields** when ingress already created the row, **must
  never overwrite** ownership, the persisted `request`, `idempotency_key`, `created_at`, or any other
  ingress-owned metadata, **must never** let a queued generation be rebound to another owner or
  request, and **must remain safe** when called repeatedly for the same `generation_id`. The
  `ON CONFLICT (id) DO UPDATE` clause enumerates only runtime-owned columns, so the prohibitions
  hold by construction rather than by convention. The execution runtime therefore stays entirely
  **ownership-blind**: `GenerateVideoRequest` carries no tenant/owner field and no execution-plane
  module knows what a user is (the one-way posture ADR-0049 established for the AI plane).
- **GEN-2** (α9.7) — **one generation execution is one external spend opportunity.** There is no
  job-level retry and no requeue path: claiming is a `queued → planning` CAS and nothing ever writes
  a row back to `queued`, so `max_attempts = 1` is structural rather than a counter that could drift.
  A generation abandoned by a crashed worker is **terminalised to `failed`**, never re-run — spend
  that may already have been incurred must surface, not repeat. (Shot-level repair inside a run is
  unaffected: it is intra-run and already paid for.)
- **GEN-3** (α9.7) — **ownership is never inferred.** Owner-scoped reads are the sole supported read
  model. Generations written before ingress carry no owner, and no code path may guess, attribute
  heuristically, or backfill one; their invisibility through the public API is an intentional
  preservation of ownership correctness, not data loss (ADR-0052 D1).
- **HOST-1** (α9.8) — **worker registration is immutable for the lifetime of a process.** The host
  computes its enabled worker set once at startup and never re-reads it: no worker is added or
  removed while running, and a configuration change takes effect on the next process start, exactly
  as it already does for the API. Shutdown therefore drains a known set and liveness refreshes a
  known set. This is what makes "a disabled worker is not registered" meaningful — enablement is a
  registration-time question, never a per-pass one.
- **HOST-2** (α9.8) — **one worker's failure never suppresses another worker's scheduling.** Each
  worker gets its own task, its own idle and failure backoff state, and its own drain budget;
  nothing awaits across workers, and the stop signal is the only permitted cross-worker coupling.
  The guarantee holds through every failure tier: a raising pass, a broken `found_work` predicate, a
  container factory that cannot build a pass, a wedged worker consuming its whole drain budget, and
  a supervision task escalated after its restart bound is exhausted. Collapsing the per-worker tasks
  into a single loop, or letting any exception reach the aggregating `gather`, would satisfy every
  other requirement while silently reintroducing the coupling ADR-0053 D2-A rejected.
- **DISP-1** (α9.9) — **a resolution is well-formed only against a declared executable set.** The
  Decision plane therefore never returns an adapter the Execution plane cannot construct in that
  deployment. The set reaches the resolver as *data*, so the resolver stays pure, and every exclusion
  it causes is recorded as a candidate with `not_executable`, so the decision stays explainable
  without persisting the set itself (ADR-0054 D1).
- **DISP-2** (α9.9) — **a record that names an adapter as having executed names the adapter that
  produced the bytes, and is unset when nothing did.** Producer identity is asserted by the dispatch
  binding — the registry key under which Execution constructed and invoked the adapter — and never
  by an adapter's self-report, which may echo the requested identity. Decision records and execution
  records are never conflated (ADR-0054 D2).
- **DISP-3** (α9.9) — **Execution never follows a provider ordering the Decision plane did not
  produce.** The resolver's ordered candidate list is the only ordering; catalogue-authored fallback
  chains are metadata and never an execution ordering (ADR-0054 D3/D4).

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
> promotion bridge, the complete **α8.9 Creator Experience** — α8.9a publish
> notifications, α8.9b creator scheduling, α8.9c creator dashboard — the α9.0
> creator analytics foundation, the α9.1 AI caption & hashtag generation, the
> α9.2 media library foundation, the α9.3 publish thumbnail support, the
> α9.4 multi-destination publishing, the α9.5 notification delivery — email, the
> α9.6 TikTok destination adapter, the α9.7 generation ingress, and the
> α9.8 worker runtime host) have
> moved up into *Completed capability lifecycles*. Only genuinely future work remains below.

| Slice | Scope |
|---|---|
| **α8.4f** | Render composition — transitions / crossfades / color grading / effects / subtitle burn-in. Blocked on the α6.4 Timeline **authoring** write paths (`transition_in_id`/`transition_out_id`/`effects`/subtitles); ADR-0043 RC1–RC6 |
| **α8.5b.4′** | Notification channels — **email shipped in α9.5** (`INotifier` + adapters + bounded retries); only **push / websocket** remain, behind the same application-owned port |
| **α8.6d″** | Publishing — **further destinations** behind the proven `IDestinationPublisher` seam. The **second destination shipped in α9.6** (TikTok), retiring the original α8.6d′ row; Instagram / Facebook Reels remain, each gated on public-URL or resumable-upload prerequisites plus App Review. A destination **catalogue** stays deferred: two real adapters express their capability differences internally, so nothing upstream needs one yet |
| **Identity-Runtime authoring** | Characters / locations / props / reference images as durable, creator-authored world state. α9.7 deliberately scoped v1 generation identity to `seed` + `global_style`; the request codec rejects unknown keys so this extends the payload additively. Needs its own persistence and API surface |
| **Worker deployment artefacts** | α9.8 ships the worker *process* — entrypoint, selector, liveness markers, exit-code contract — but no container image, manifest, or probe wiring. Running one API deployment plus one or more worker deployments off the same image is a deployment change the code now supports and the repository does not yet describe |
| **Mid-run generation cancellation** | α9.7 ships **queued-only** cancel. Stopping a claimed run requires the generation pipeline to poll a cancellation flag between shots — a change to the execution plane, with its own spend-accounting question (what is owed for a part-run) — so it needs explicit design rather than an incremental patch |

All remaining work is **downstream of / additive to the frozen orchestration
platform** (ADR-0042 Gate 1) and respects the render composition boundary
(ADR-0043 Gate 2) — new capabilities composed on stable seams, not redesigns of
the workflow engine. The α8.5x AI runtime (ADR-0044) keeps that discipline: the
planner sits *upstream* of the frozen runner, verify/repair *downstream*, and the
resolver is the ADR-0041 D2 extension point — zero freeze overrides.
