# System Map — How One Prompt Becomes One Exported Video

**Status:** Engineering overview / navigation map. **Not a contract** — it does not
add invariants; it *routes* you to the ADRs and contracts that do. Written after
**α8.7** (Planner v2). Companion to
[`AI_RUNTIME_PLANES.md`](./AI_RUNTIME_PLANES.md), which explains *why* the planes
exist; this map shows *where a request goes*.

If you are new to the generation pipeline, read this first, then follow the links
into the governing documents for the stage you care about.

> **SYSTEM_MAP is a navigation document.** It does not define architectural rules or
> behavioural guarantees — those remain the responsibility of the **ADRs** (*why the
> architecture exists*) and the **engineering contracts** (*what each subsystem must
> do*). This map only shows *how everything connects*. Where this document and an
> ADR/contract ever disagree, the ADR/contract is authoritative.
>
> ```
> ADRs        → why the architecture exists
>   Contracts → behavioural guarantees per subsystem
>     SYSTEM_MAP → how everything connects
>       Code
> ```

---

## Architecture at a glance

*Read this in under a minute; the rest of the document is detail.*

Three planes, each with its own mutability model (full rationale:
[`AI_RUNTIME_PLANES.md`](./AI_RUNTIME_PLANES.md)):

```
  Knowledge plane   — what CAN exist       provider catalogue, device profiles
        │
        ▼
  Decision plane    — what SHOULD happen   planner · storyboard · resolver ·
        │                                  verification · repair · timeline
        ▼
  Execution plane   — what DID happen      generation · ffmpeg · execution
                                           runtime · asset store · publisher*

  * Publishing is an Execution-plane capability, not a separate plane. Its
    account-connection foundation (OAuth credential ownership) shipped in α8.6a, the
    publish runtime (`PublishJob` + `PublishWorker`) in α8.6b, and the first real
    destination adapter (YouTube — `YouTubeOAuthClient` + `YouTubeDestination`) in α8.6c,
    with a **second** destination (TikTok — `TikTokOAuthClient` + `TikTokDestination`) in
    α9.6 behind the same unchanged seam.
```

…and the single request that flows through them, end to end:

```
  Prompt          ← POST /api/v1/generations (α9.7: owned, queued, worker-executed)
    ↓  Planner
    ↓  Storyboard
    ↓  Resolver
    ↓  Generation
    ↓  Verification
    ↓  Repair
    ↓  Timeline
    ↓  FFmpeg
    ↓  Execution Runtime
    ↓  Asset Store
    ↓  Publisher   (planned)
```

Everything below expands these two pictures: the annotated flow (§1), the per-stage
table with code seams + governing docs (§2), and the deeper references (§6).

---

## 1. The pipeline at a glance

One `GenerateVideoRequest` flows top-to-bottom. The **plane** column shows which
mutability model owns each step (Knowledge = *what can exist*, Decision = *what
should happen*, Execution = *what did happen*).

```
                                                        plane
  Prompt  (GenerateVideoRequest + IdentityProfile)      input
    │
    ▼
  Planner            prompt → GenerationPlan (ShotIntent arc)   Decision
    │
    ▼
  Storyboard         plan → ShotPrompt[] (Prompt Builder)       Decision
    │
    ▼
  Resolver           capability → ordered candidates            Decision
    │                   ▲ reads Knowledge (catalogue) + runtime state
    ▼
 ┌───────────────── Generation Runtime (per shot) ─────────────┐
 │  Image Generator   adapter_id + prompt + seed → bytes        │  Execution
 │        │                                                     │
 │        ▼                                                     │
 │  Verification      observed features → pass / fail           │  Decision (pure)
 │        │                                                     │
 │        ▼                                                     │
 │  Repair            fail → retry w/ fresh seed | give up      │  Decision (pure)
 └──────────────────────────────┬──────────────────────────────┘
                                 ▼
  Timeline gate      frames → duplicate/order/duration check    Decision (pure)
    │
    ▼
  FFmpeg             frames → MP4  →  ffprobe verifies output    Execution
    │
    ▼
  Execution Runtime  status machine + ledger + outbox events    Execution
    │
    ▼
  Asset Store        frames + final video (execution-owned)     Execution
    │
    ▼
  Publisher          MP4 → YouTube ✅ / TikTok ✅ / Instagram   Execution
```

Every arrow is a **stable seam** (a port or a pure function). New capability plugs
into a seam; it does not reshape the flow (ADR-0042, ADR-0045).

---

## 2. Stage by stage

| Stage | Plane | What it does | Code seam | Governed by | Status |
|---|---|---|---|---|---|
| **Generation Ingress** | API → Execution (queue) | **How a creator actually starts a generation.** Until α9.7 the pipeline below had *no HTTP entry point* — `GenerateVideo` could only be invoked from a script — and `generations` rows carried **no owner**. A dedicated top-level `/api/v1/generations` resource now queues **owner-scoped generation jobs** (create → `201`, or `200` on idempotent replay; poll; keyset-list; cancel), and `GenerationWorker` executes them. `POST` records intent and returns in **milliseconds**; a run takes minutes, so execution is a **queued background** concern (ADR-0052 D2-B): the worker reaps abandoned runs, then claims each under a `generation:<id>` lease renewed by heartbeat. **One execution is one external spend opportunity** — claiming is a `queued → planning` CAS and nothing ever writes a row back to `queued`, so `max_attempts = 1` is structural and a crashed run is **terminalised to `failed`, never re-run**. **Idempotency is explicit creator intent** (D4): repeating a prompt is a legitimate second take, so only a client-supplied `idempotency_key` collapses two requests. **Polling is the v1 read contract** (D3) over a **curated** projection — provenance, resolver internals, adapter/provider identities, component versions and `final_video_asset_id` never cross the wire; `promotable` is the derived next-action signal. Migration `0016` adds the ingress-owned `tenant_id` / `owner_user_id` / `idempotency_key` / `request` columns, resolving the ADR-0046 Q1 + α8.8 AP9 ownership deferral. **Accepted limitations**: ownerless legacy generations stay intentionally invisible and non-promotable (**ownership is never inferred**), and **only `queued` generations are cancellable** (a claimed one returns `409` — mid-run cancellation needs the pipeline to poll a flag between shots and is a deliberate future capability). | `CreateGeneration`/`GetGeneration`/`ListGenerations`/`CancelGeneration`, `GenerationWorker` (`application/use_cases/generation/`), `IGenerationJobStore`/`SqlGenerationJobStore`, `IGenerationRunner`/`SessionScopedGenerationRunner`, `/api/v1/generations`, migration `0016` | [ADR-0052](../decisions/ADR-0052-generation-ingress-ownership-execution-and-read-contract.md) · [PHASE3_ALPHA9_7_PREFLIGHT](./PHASE3_ALPHA9_7_PREFLIGHT.md) | ✅ α9.7 |
| **Identity Runtime** | Knowledge | **Where a creator's *world* comes from.** An owner-scoped authoring context — named characters with stable appearance, a location, recurring props, a project look and a stable seed — behind `/api/v1/identities`, so the six shots of a video are about the same people in the same place. Deliberately **not** the α2a *authentication* identity context (`User`/`Tenant`/`Session`), which is why the package is `identity_runtime` while the resource stays `identities`. Naming a world on a generation **snapshots it whole** into `generations.request` at acceptance: the run carries a *value*, not a reference, so editing or deleting that world later cannot change what a past generation executed or would replay as (**IDENT-1**), and `generations.identity_id` deliberately has **no foreign key**. Identity is **Knowledge, never measurement** (**IDENT-4**) and **world state, never policy** (**IDENT-3**) — it is not a resolver input and never influences casting, ordering or routing. A field is authorable only when a path in the deployment consumes it (**IDENT-2**), which is why reference images are **refused** rather than accepted and discarded. | `IdentityProfile` + children (`domain/identity_runtime/`), `IIdentityRepository`/`IdentityRepository`, `identity.*` use cases (`application/use_cases/identity/`), `/api/v1/identities`, migration `0017` | [ADR-0055](../decisions/ADR-0055-identity-runtime-authoring.md) · [PHASE3_ALPHA10_0_PREFLIGHT](./PHASE3_ALPHA10_0_PREFLIGHT.md) | ✅ α10.0 |
| **Prompt** | input | The request + the project *world state* (characters, locations, style, seed). Reached either from **generation ingress** (α9.7 — the creator-facing path, where the request is persisted verbatim in `generations.request` so a worker can rebuild it exactly) or by direct invocation (demo script, integration tests). Since **α10.0** that world state is authorable and durable: a request may name an Identity Runtime profile, whose snapshot travels in the payload as `SPEC_VERSION = 2` (v1 rows still decode unchanged, and a request that names no world behaves exactly as before). | `GenerateVideoRequest`, `IdentityProfile`, `GenerationRequestSpec` (`request_codec.py`) | [ADR-0044](../decisions/ADR-0044-ai-runtime-generation-architecture.md) · [ADR-0052](../decisions/ADR-0052-generation-ingress-ownership-execution-and-read-contract.md) · [ADR-0055](../decisions/ADR-0055-identity-runtime-authoring.md) | ✅ · ✅ α10.0 (authored worlds) |
| **Planner** | Decision | Decomposes the prompt into a deterministic cinematic arc of `ShotIntent`s (establishing → … → closing); assigns semantic shot ids + derived seeds. Never names a provider. | `plan_from_prompt` (`domain/generation/planner.py`), `shot_intent.py` | [CINEMATIC_STORYBOARD_CONTRACT](./CINEMATIC_STORYBOARD_CONTRACT.md) · [ADR-0045](../decisions/ADR-0045-ai-runtime-core-freeze.md) | ✅ α8.7 |
| **Storyboard** | Decision | Turns each `ShotIntent` into a concrete `ShotPrompt` via the Prompt Builder (the sole place intent becomes generator-facing wording, CS-8). | `build_storyboard`, `prompt_builder.py` | [CINEMATIC_STORYBOARD_CONTRACT](./CINEMATIC_STORYBOARD_CONTRACT.md) | ✅ α8.7 |
| **Resolver** | Decision | Given a *capability* + constraints, returns explainable, ordered adapter candidates. Pure; reads immutable Knowledge + runtime snapshots **+ the deployment's executable adapter set** (α9.9), so it cannot recommend an adapter this build has no code for. | `ICapabilityResolver` / `ResolverCapabilityResolver`; `domain/resolver`; `ExecutableAdapters` | [RESOLVER_RUNTIME_CONTRACT](./RESOLVER_RUNTIME_CONTRACT.md) · [ADR-0045](../decisions/ADR-0045-ai-runtime-core-freeze.md) · [ADR-0054](../decisions/ADR-0054-execution-adapter-dispatch.md) | ✅ α8.5e |
| **Adapter dispatch** | Execution | The seam that turns a *resolved* adapter into a *running* one. A closed adapter-id → generator table populated at the composition root — no runtime `importlib`, no `import_path` loading, and an unknown key is a permanent failure rather than a fallback. Its keys are simultaneously the executable set the resolver is given and the authority on what produced an artefact: provenance is asserted from the **binding**, captured at invocation, never from what an adapter reports about itself. | `IImageAdapterRegistry` / `ImageAdapterRegistry` (`infrastructure/generation/registry.py`) | [ADR-0054](../decisions/ADR-0054-execution-adapter-dispatch.md) · [PHASE3_ALPHA9_9_PREFLIGHT](./PHASE3_ALPHA9_9_PREFLIGHT.md) | ✅ α9.9 |
| **Generation Runtime** | Execution | Composes the pipeline: dispatches the top eligible adapter per shot through the adapter registry, drives the model cache for local tiers. No provider branching, never scores, and **never walks** — only `candidates[0]` is invoked. | `GenerateVideo` (`application/use_cases/generation/generate_video.py`); `IImageAdapterRegistry` → `IImageGenerator` | [ADR-0044 (MRC)](../decisions/ADR-0044-ai-runtime-generation-architecture.md) · [ADR-0046](../decisions/ADR-0046-execution-runtime-boundaries.md) · [ADR-0054](../decisions/ADR-0054-execution-adapter-dispatch.md) | ✅ α8.6 |
| **Verification** | Decision (pure) | A policy over *observed features* (extracted by infra): resolution, aspect, blank/blur/watermark, cross-shot similarity. Decides pass/fail; never sees raw bytes. | `verify_image` (`domain/generation/verification.py`); `IImageFeatureExtractor` | [ADR-0045](../decisions/ADR-0045-ai-runtime-core-freeze.md) | ✅ v1 |
| **Repair** | Decision (pure) | On failure, retries the *single* shot with a fresh derived seed up to a cap, else gives up (fails the run). | `decide_repair` (`domain/generation/repair.py`) | [ADR-0045](../decisions/ADR-0045-ai-runtime-core-freeze.md) | ✅ v1 |
| **Timeline gate** | Decision (pure) | Pre-render check: missing / duplicate / out-of-order frames, duration & aspect. Cheaply rejects a broken timeline before ffmpeg. | `verify_timeline` (`domain/generation/timeline_verification.py`) | [ADR-0043](../decisions/ADR-0043-render-composition-boundary.md) | ✅ α8.6 |
| **FFmpeg** | Execution | Renders accepted frames into an MP4; ffprobe measures the output for verification. | `FfmpegSlideshowRenderer`, `FfprobeVideoProbe` (`infrastructure/render/`) | [ADR-0043](../decisions/ADR-0043-render-composition-boundary.md) | ✅ α8.6 |
| **Execution Runtime** | Execution | Persists the whole run incrementally: `generations` + `generation_shots`, the `status` state machine, the resolution ledger, and lifecycle events via the transactional outbox. Since **α9.7** its lifecycle **begins with an already-created `queued` generation supplied by ingress** — it is no longer responsible for establishing that a generation exists, only for executing one. **GEN-1**: `begin()` is an idempotent *state-initialisation* operation, not a generic upsert — it creates the row when absent (the direct-invocation path is unchanged), initialises runtime-owned fields when ingress created one, and can **never** overwrite ownership, the persisted request, the idempotency key, or `created_at`. **Ingress owns identity; the runtime owns execution state**, and the runtime stays entirely ownership-blind. | `SqlExecutionRuntimeStore`; migrations `0012`, `0016` | [EXECUTION_RUNTIME_CONTRACT](./EXECUTION_RUNTIME_CONTRACT.md) · [ADR-0046](../decisions/ADR-0046-execution-runtime-boundaries.md) · [ADR-0052](../decisions/ADR-0052-generation-ingress-ownership-execution-and-read-contract.md) | ✅ α8.6 · ✅ α9.7 (GEN-1) |
| **Asset Store** | Execution | Registers execution-owned artefacts (`generation_assets`, with a `parent_asset_id` lineage) and stores bytes behind object storage. Promotion to `media_assets` is the explicit `PromoteGenerationAssets` bridge (α8.8 — see the next row). | `generation_assets`; `IObjectStorage` | [ADR-0046](../decisions/ADR-0046-execution-runtime-boundaries.md) · [ADR-0037](../decisions/ADR-0037-media-generation-outputs.md) | ✅ α8.6 |
| **Asset Promotion Bridge** | Execution → Library | The **ADR-0046 X8** (`PublishGenerationAssets`) seam: promotes a completed generation's final video (`generation_assets`, Path B) into the platform media library (`media_assets(source='generated')`, Path A) that feeds Render → Export → Publish. User-initiated, **library-only**; **copies** bytes (never a shared reference), request-time project-scoped ownership, recomputed checksum + provenance, deterministic-key idempotency (`noop` replay). Reads through a new **read-only** `IGenerationReader` (no existing port changed); **no schema migration**. **α9.7 closed a latent authorisation gap here**: promotion previously authorised only the *project*, loading the generation by id alone — so anyone who learned an id could promote another creator's output into their own project (harmless while ids were server-internal, exploitable once ingress made them client-visible). `IGenerationReader` is now **owner-scoped**, and promotion requires **both** project membership **and** generation ownership. The original AP9 "project-asserted, generation-unowned" posture is superseded. | `PromoteGenerationAssets` (`application/use_cases/media/`), `IGenerationReader`/`GenerationReader`, `POST /api/v1/media/promotions` | [ADR-0046](../decisions/ADR-0046-execution-runtime-boundaries.md) · [ADR-0052](../decisions/ADR-0052-generation-ingress-ownership-execution-and-read-contract.md) · [PHASE3_ASSET_PROMOTION_BRIDGE_PREFLIGHT](./PHASE3_ASSET_PROMOTION_BRIDGE_PREFLIGHT.md) | ✅ α8.8 · ✅ α9.7 (owner-scoped) |
| **Publisher** | Execution | Uploads the final MP4 to social platforms behind per-platform adapters. Its **account-connection foundation** (OAuth credential ownership: `SocialAccount`, envelope-encrypted tokens, owner-scoped `/api/v1/social-accounts`) shipped in **α8.6a**; the **publish runtime** (user-initiated `PublishJob` + poll-ingress `PublishWorker`, dual-lock serialization, bounded retries, terminal outbox events, `/api/v1/publish-jobs`) in **α8.6b** — credential-blind, consuming only the `AuthorizedContext` and uploading via `IDestinationPublisher`; and the **first real destination adapter** (YouTube — `YouTubeOAuthClient` + `YouTubeDestination`, Data API v3 resumable upload over a thin injected `httpx` transport, PUB-11 ambiguous-outcome safety) in **α8.6c** — adapter-only, no port/runtime change, still credential-blind (Mock remains the CI default; live smoke is opt-in). Downstream, the **α8.9a publish notifications** fan-out (`PublishNotificationProjection`) turns the terminal `PublishJobSucceeded`/`PublishJobFailed` events into in-app notifications for the requester — a separate consumer on the existing outbox fan-out, not part of the publish runtime. **α8.9b creator scheduling** adds an optional, boundary-validated `publish_at` on the create request that threads into `ContentPackage.publish_at` → the YouTube `status.publishAt` mapping (platform-native go-live; upload still immediate, `scheduled_at` untouched). **α9.6** adds the **second real destination** (TikTok — `TikTokOAuthClient` + `TikTokDestination`, Content Posting API "Direct Post" over the same thin injected `httpx` transport), proving the seam generalises: TikTok's *asynchronous* publish model is adapted **entirely inside the adapter** via bounded status polling, so the frozen `publish()` contract, the worker, the scheduler, and the API are all unchanged — and **α9.4 multi-destination publishing now fans out across two real adapters with no batch-side change**. | `SocialCredentialService`, `/social-accounts` (α8.6a); `CreatePublishJob`/`ProcessPublishJob`/`PublishWorker`, `IDestinationPublisher`/`IDestinationRegistry`, `/publish-jobs` (α8.6b); `YouTubeOAuthClient`/`YouTubeDestination` (α8.6c); `PublishNotificationProjection` (α8.9a); `PublishJobCreateRequest.publish_at` (α8.9b); `TikTokOAuthClient`/`TikTokDestination` (α9.6) | [PUBLISHING_RUNTIME_CONTRACT](./PUBLISHING_RUNTIME_CONTRACT.md) · [ADR-0047](../decisions/ADR-0047-publishing-credential-ownership.md) | ◑ α8.6a (connections) · ✅ α8.6b (runtime) · ✅ α8.6c (YouTube) · ✅ α8.9a (notifications) · ✅ α8.9b (scheduling) · ✅ α9.6 (TikTok) |
| **Creator Dashboard** | API / read | Read-only owner-scoped summary of the caller's product state — publish-job counts by status, connected/total social accounts, unread notifications, media total — composed inside a single UoW from the **already-existing** owner-scoped reads (no new repository method, no new SQL, no migration, no analytics). Completes the **α8.9 Creator Experience**. | `GetCreatorDashboard` (`application/use_cases/dashboard/`), `GET /api/v1/dashboard/summary` | [PHASE3_ALPHA8_9c_PREFLIGHT](./PHASE3_ALPHA8_9c_PREFLIGHT.md) | ✅ α8.9c |
| **Creator Analytics** | Outbox consumer + API / read | Activates the dormant, partitioned `analytics_events` table as a **fourth independent** downstream outbox consumer: `AnalyticsProjection` maps the publish + export lifecycle events → a stable `event_name` vocabulary + neutral properties, and the reused `RecordAnalyticsEvent` writer (own UoW/event; tenant resolved in-UoW) persists one owner-scoped row each. **Exactly-once is DB-owned** (`source_event_id = event.id`, `occurred_at = event.occurred_at`; partial-unique `(source_event_id, occurred_at)` including the partition key). Read via owner-scoped `GET /api/v1/analytics/summary` (`GetCreatorAnalytics`: zero-filled per-`event_name` counts + total, trailing-30d default, naive/inverted window → 422). Migration `0015`; one new `IAnalyticsRepository`; no producer/frozen-runtime change. | `AnalyticsProjection` / `RecordAnalyticsEvent` / `GetCreatorAnalytics` (`application/use_cases/analytics/`), `IAnalyticsRepository`/`AnalyticsRepository`, `GET /api/v1/analytics/summary`, migration `0015` | [ADR-0048](../decisions/ADR-0048-analytics-consumer-idempotency.md) · [PHASE3_ALPHA9_0_PREFLIGHT](./PHASE3_ALPHA9_0_PREFLIGHT.md) | ✅ α9.0 |
| **AI Publish Metadata** | Publishing → AI bridge (advisory) | The **first publishing→AI bridge**: an opt-in `POST /api/v1/publish-metadata/suggestions` that *suggests* publish metadata (title / description / hashtags) for a finished, owned export. A **Publishing-owned** `IPublishMetadataGenerator` port (neutral, `ContentPackage`-free DTOs) is implemented by the AI-subsystem `LlmPublishMetadataGenerator` over `Capability.LLM` (deterministic mock by default) — dependency is **strictly one-way** (AI never imports Publishing), pinned by a new import-linter contract. `GeneratePublishMetadata` validates ownership/readiness first (`404`→`422`), then calls the port **outside** the read UoW with a **mandatory deterministic template fallback** on any AI failure (PUB-9 preserved). Advisory only: never a `PublishJob` prerequisite; user edits win; only final metadata persisted (provenance ephemeral); destination adapter stays AI-unaware. No migration; no frozen-runtime change. | `IPublishMetadataGenerator` (`application/interfaces/`), `LlmPublishMetadataGenerator` (`infrastructure/ai/metadata/`), `GeneratePublishMetadata` (`application/use_cases/publishing/`), `POST /api/v1/publish-metadata/suggestions` | [ADR-0049](../decisions/ADR-0049-ai-publish-metadata-boundary.md) · [PHASE3_ALPHA9_1_PREFLIGHT](./PHASE3_ALPHA9_1_PREFLIGHT.md) | ✅ α9.1 |
| **Media Library** | API / read + write (over Media) | A deterministic, owner-scoped **Asset Library** over registered `media_assets` (ADR-0037 **CR-8**): folders (self-referencing tree) + curated library entries (name / description / `text[]` tags / reuse counters). A **sibling** to the Media aggregate — never mutates `media_assets`, references one asset by id (`uq_library_assets_media_asset_id`). `ILibraryRepository` + 11 `library.*` use cases back `/api/v1/library/*`: folder + asset CRUD, keyset **browse**, tag GIN **ANY-of** filter, **version-fenced** asset OCC (404-before-412), folder-move **cycle rejection**, folder-delete **asset detachment** (SET NULL), **idempotent** reuse recording; browse/get **hide entries whose media is soft-deleted**. The pre-built `embedding vector(1536)` column / vector search are **out of scope** (future increment + own ADR). No migration; no new port beyond the repository; no ADR; no frozen-runtime change. **Accepted limitation**: per-owner **root-folder** name uniqueness is **application-enforced** (`folder_name_conflicts`) because the frozen `uq_library_folders_parent_folder_id_name` index (default NULL-distinct, no `owner_user_id`) can't constrain root rows without a migration — a narrow, non-corrupting root-only TOCTOU race remains (child uniqueness stays DB-enforced); permanent fix = a future per-owner partial-unique-index migration (no ADR). | `LibraryFolder`/`LibraryAsset` (`domain/library/`), `ILibraryRepository`/`LibraryRepository`, `library.*` use cases (`application/use_cases/library/`), `/api/v1/library/*` | [ADR-0037](../decisions/ADR-0037-media-generation-outputs.md) · [PHASE3_ALPHA9_2_PREFLIGHT](./PHASE3_ALPHA9_2_PREFLIGHT.md) | ✅ α9.2 |
| **Publish Thumbnail Support** | Publishing (additive, best-effort) | An **optional, best-effort** custom thumbnail for a publish ([`ADR-0050`](../decisions/ADR-0050-publish-thumbnail-source-and-delivery-boundary.md), **Option A — creator-supplied**). A creator nominates one of their **own `image`** `media_assets` via an additive `thumbnail_media_asset_id` on `POST /api/v1/publish-jobs`; `CreatePublishJob` verifies ownership + `kind='image'` **before** queuing (`404`/`422`) and captures it **immutably** into the existing `ContentPackage.thumbnail_media_asset_id`. The frozen `IDestinationPublisher.publish` signature is **unchanged** — only `UploadMedia` gains an optional `thumbnail: UploadThumbnail` (additive; existing adapters ignore it). `ProcessPublishJob` resolves + materialises the owned image **in the worker** (owner-scoped — the adapter never resolves/generates it); `YouTubeDestination` calls `thumbnails.set` **after** a durable `videos.insert` (**best-effort**: failures swallowed + logged, **never retried** — preserves **PUB-11**, no duplicate video). A missing/soft-deleted image or a materialisation failure ⇒ **video still publishes with no thumbnail** (advisory — never blocks/retries the primary publish). No migration; no AI; no thumbnail generation; no lineage; no new provider; no frozen-runtime change beyond the additive `UploadMedia` handle. | `UploadThumbnail`/`UploadMedia.thumbnail` (`application/interfaces/destination_publisher.py`), `CreatePublishJob`/`ProcessPublishJob` (`application/use_cases/publishing/`), `YouTubeDestination`/`MockDestination` (`infrastructure/publishing/destinations/`), `thumbnail_media_asset_id` on `POST /api/v1/publish-jobs` | [ADR-0050](../decisions/ADR-0050-publish-thumbnail-source-and-delivery-boundary.md) · [PHASE3_ALPHA9_3_PREFLIGHT](./PHASE3_ALPHA9_3_PREFLIGHT.md) | ✅ α9.3 |
| **Multi-Destination Publishing** | Publishing (additive orchestration) | A single creator action that **fans out** one finished export publish to **N** of the caller's connected accounts (the capstone of the publishing workflow — captions α9.1 + thumbnails α9.3 + scheduling α8.9b compose once, apply to every channel). A new additive `POST /api/v1/publish-jobs/batch` (`social_account_ids[1..20]`, bounded + duplicate-free; the single-create `POST /publish-jobs` is **unchanged**). **Orchestration only**: `CreatePublishJobs` composes the **unchanged** `CreatePublishJob` once per account — no duplicated validation / idempotency / persistence. A **shared** prerequisite failure (export / optional thumbnail) aborts the whole request (`404`/`422`); a **per-account** failure (not owned / not connected / unsupported) is recorded as that item's outcome so one bad account never blocks the rest (classified from the existing error `details`). The response is an ordered per-account outcome array (`created` / idempotent replay / neutral `error`). Idempotency (PUB-7) preserved per account; every created job is an ordinary `PublishJob`, so scheduling / captions / thumbnails / notifications / analytics apply with no batch-specific logic. No migration; no ADR; no new port; no frozen-runtime change. | `CreatePublishJobs` (`application/use_cases/publishing/create_publish_jobs.py`), `PublishJobBatchCreateRequest`/`PublishJobBatchItemPublic` (`api/v1/schemas/publish_jobs.py`), `POST /api/v1/publish-jobs/batch` (`routers/publish_jobs.py`) | [PHASE3_ALPHA9_4_PREFLIGHT](./PHASE3_ALPHA9_4_PREFLIGHT.md) | ✅ α9.4 |
| **Notification Delivery (Email)** | Outbound channel (additive, out-of-band) | The platform's **first outbound external-communication channel that is not a publish destination** — fulfils the deferred **α8.5b.4**. A **dedicated leased poll worker** (`NotificationEmailWorker`, deliberately **off** the relay fan-out, which still owns only the in-app projection write) drains undelivered notifications FIFO; `ProcessNotificationEmail` takes a per-notification `notification_email:<id>` lease, resolves the recipient **owner-scoped**, sends **outside any transaction**, then stamps `delivered_email_at` (**send-then-stamp**) — a delivered row is never re-scanned. Transient failure ⇒ capped exponential backoff retry; permanent failure / attempt ceiling ⇒ **terminal**. The application **owns** `INotifier` + neutral DTOs; infrastructure supplies the adapter (strictly one-way): `LoggingNotifier` (mock-first, fail-soft default — keeps CI deterministic) and the config-gated `SmtpNotifier`, both **recipient-blind, PII-minimal leaves** (masked telemetry only) pinned by two new import-linter contracts (`aiosmtplib` confined to the adapter package). **Correctness never depends on provider-side deduplication** (ADR-0051 Appendix A, non-normative): lease + send-then-stamp give **at-least-once** delivery with a bounded, accepted rare-duplicate window. **No migration / no schema change / no relay change / no frozen-runtime change.** Retry + terminal bookkeeping lives in a reserved `payload["_email"]` namespace (success uses the pre-existing `delivered_email_at` column), **stripped centrally at the single repository row→entity boundary** so it can never reach any endpoint — an implementation detail, never part of the public notification contract (`NotificationPublic.payload` behaviourally unchanged; a future migration may move it to dedicated columns with no external change). **Accepted limitations**: the `delivered_email_at IS NULL` deliverable scan is unindexed (fine at beta scale) and retry bookkeeping stays in the reserved namespace. | `INotifier`/`EmailMessage`/`NotifierDeliveryError` (`application/interfaces/notifier.py`), `LoggingNotifier`/`SmtpNotifier` (`infrastructure/notifications/`), `NotificationEmailWorker`/`ProcessNotificationEmail` (`application/use_cases/notifications/`), `list_email_deliverable`/`mark_email_delivered`/`record_email_delivery_failure` (`INotificationRepository`) | [ADR-0051](../decisions/ADR-0051-notification-delivery-email-idempotency-and-boundary.md) · [PHASE3_ALPHA9_5_PREFLIGHT](./PHASE3_ALPHA9_5_PREFLIGHT.md) | ✅ α9.5 |
| **TikTok Destination Adapter** | Publishing (additive, second destination) | The platform's **second real publish destination**, proving the `IDestinationPublisher` seam generalises beyond its first implementation. `TikTokDestination` drives the Content Posting API "Direct Post" flow (`creator_info/query` → `video/init` → chunked `PUT` → `status/fetch`) as a **credential-blind leaf** (PUB-5 / ADR-0047 C4, pinned by the pre-existing import-linter contract); `TikTokOAuthClient` fills the α8.6a `ISocialOAuthClient` seam, handling TikTok's `client_key` naming, comma-separated scopes, `open_id` identity, and **rotating refresh tokens** (re-encrypted by the unchanged credential service). **The frozen `publish()` contract is untouched** — the *asynchronous* publish model is adapted **entirely inside the adapter** via bounded polling, so no use case / worker / scheduler / API gained TikTok-specific logic, and **α9.4 batch publishing now spans two real adapters unchanged**. **Exactly three outcomes**: success, permanent failure, and an **indeterminate timeout** logged with reconciliation diagnostics and **never retried** (**PUB-11**); a post-upload transient poll failure is **absorbed and re-polled**, never escaping as retryable. **Stable identity**: `platform_post_id` always holds the durable `publish_id` and is never overwritten by a later public post id; **no post URL is invented** (TikTok's canonical URL needs the username a credential-blind adapter never sees). Unsupported/unoffered visibility and `publish_at` schedules **fail explicitly**, never silently degrade; `fail_reason="internal"` is **permanent** despite provider guidance, since it can only surface after bytes were accepted. Registration is **fail-soft** (unconfigured ⇒ create-time validation failure, no HTTP client opened). No migration; no ADR; no schema change; no new port; no frozen-boundary change. **Accepted limitation**: a publish outliving the poll budget settles `failed` even if TikTok later publishes it — reconciliation is out of scope (no worker, no webhooks). | `TikTokDestination` (`infrastructure/publishing/destinations/tiktok.py`), `TikTokOAuthClient` (`infrastructure/publishing/oauth/tiktok_oauth_client.py`), `tiktok_*` settings (`core/config.py`), fail-soft registration (`core/container.py`) | [PHASE3_ALPHA9_6_GROUNDING](./PHASE3_ALPHA9_6_GROUNDING.md) · [PHASE3_ALPHA9_6_PREFLIGHT](./PHASE3_ALPHA9_6_PREFLIGHT.md) | ✅ α9.6 |
| **Worker Runtime Host** | Delivery (peer to the API) | **What makes every row above actually happen.** Each stage in this map that ends in a `run_once()` poll worker — relay, generation, render, export, enrichment, publish, email — was, until α9.8, a library primitive with **no production caller**: correct, tested, and never turning. `app/runtime/` supplies the caller. It is a **delivery layer peer to `app/api/`**: the API turns HTTP requests into application calls, the runtime turns *elapsed time* into them; both consume the layers below and are consumed by nothing (a new import-linter contract pins the direction). **`WorkerHost` schedules; it never decides** — a pass is an opaque coroutine and its result goes straight back to the spec's own `found_work` predicate, so `worker_registry` is the only component that knows a `RelayResult` has `fetched` while a `PublishPollResult` has `scanned`. Deployment is **one image, many process classes**: `--workers generation` runs a GPU node, no argument runs everything enabled, and an unrecognised name **fails startup** rather than booting healthy with a capability absent. **Shutdown stops claiming immediately, then drains within per-worker budgets** sized by work-item duration (generation `900s` ≫ media `300s` > publish `180s` > relay/email `60s`); a pass that outlives its budget is cancelled and reported (`exit 75`), and cancelling a generation pass stops the **pipeline**, not merely the wait for it. **Failure has two tiers** (D5): a failed pass is contained and backed off with the worker still polling; a supervision task that dies is replaced, and one that cannot be kept alive is **escalated** (`exit 70`) rather than left quietly absent. **HOST-1** freezes registration for the process lifetime and **HOST-2** keeps every worker's failure to itself. **Replica safety remains each worker's own obligation** (D4) — the host supplies none, and replication *preserves* each worker's accepted semantics rather than strengthening them. | `WorkerHost`/`WorkerSpec`/`Liveness` (`app/runtime/`), `worker_registry`, `scripts/run_worker.py` | [ADR-0053](../decisions/ADR-0053-worker-runtime-host.md) · [PHASE3_ALPHA9_8_PREFLIGHT](./PHASE3_ALPHA9_8_PREFLIGHT.md) | ✅ α9.8 |

Legend: ✅ implemented · ⟢ planned.

---

## 3. The three planes in one breath

- **Knowledge** — *what can exist*: the provider catalogue (`capabilities/providers/
  routing/devices` YAML → `0010` tables), authored offline, seeded, read-only at
  request time. See [`PROVIDER_RUNTIME_DATA_MODEL.md`](./PROVIDER_RUNTIME_DATA_MODEL.md).
- **Decision** — *what should happen*: Planner, Storyboard, Resolver, Verification,
  Repair, Timeline. **Pure functions**; same inputs ⇒ identical output. Frozen by
  [ADR-0045](../decisions/ADR-0045-ai-runtime-core-freeze.md).
- **Execution** — *what did happen*: Generation Runtime, FFmpeg, Execution Runtime,
  Asset Store, Publisher. Stateful, side-effecting, records provenance. Frozen by
  [ADR-0046](../decisions/ADR-0046-execution-runtime-boundaries.md).

Full rationale: [`AI_RUNTIME_PLANES.md`](./AI_RUNTIME_PLANES.md).

The α8.7 result is the cleanest illustration of the separation: the storyboard went
from six near-identical scenes to a genuine establishing→closing arc, and Stage 13
turned green **without touching the generator, verifier, repair loop, renderer, or
persistence** — only the Planner (a Decision-plane change) improved.

---

## 4. The golden thread — provenance & events

Two things flow *alongside* the pipeline so any run is explainable and replayable
long after the catalogue changes:

- **Provenance** (`GenerationProvenance`, persisted on `generations`): the chosen
  adapter/provider/tier, the full ranked `candidate_list`, `catalogue_version`,
  `manifest_digest`, and a **version stamp for every component** — `planner`,
  `storyboard`, `prompt_builder`, `verifier`, `repair`, `renderer`, `resolver`,
  `score_schema` (see `domain/generation/versions.py`). A defect found later is
  attributable to an exact component revision.
- **Lifecycle events** (transactional outbox): `generation.started`,
  `generation.shot_generated` (×N), `generation.video_rendered`,
  `generation.export_completed` — the run is observable from the database alone.

---

## 5. How the pipeline maps to CI

Validation mirrors the plane boundaries (see [`../CI_QUALITY_GATE.md`](../CI_QUALITY_GATE.md)):

- **Decision plane** → fast pure unit tests (planner, storyboard, verification,
  repair, timeline, resolver scoring) + the byte-for-byte **Golden V2** regression.
- **Knowledge plane** → provider manifest validation + seed round-trip (Stage 0, Stage 11).
- **Execution plane** → **Stage 12** (runtime infrastructure, frozen), **Stage 13**
  (the Generation Runtime end-to-end slice, and since α9.9 the authoritative test of
  adapter dispatch: unit tests cannot verify that the registry dispatched to the adapter
  the resolver intended, so a wrong binding failing closed and `AUTO` cascading past
  adapters this build cannot execute are both proven here), **Stage 14** (publishing — account
  connections α8.6a + the publish runtime α8.6b, extended in place for the α8.9b creator
  scheduling ingress: `publish_at` validation + a scheduled publish that persists
  `publish_at` and still runs to `succeeded`), **Stage 15** (the α8.8 asset
  promotion bridge — promote + idempotent replay of `generation_assets` → `media_assets`),
  **Stage 16** (the α8.9a publish notifications — publish terminal events →
  in-app notifications: success/failure, exactly-once under redelivery, read-API visibility),
  **Stage 17** (the α8.9c creator dashboard — `GET /api/v1/dashboard/summary` aggregating
  committed owner-scoped state across publish jobs / social accounts / notifications / media,
  plus owner isolation + the auth gate), and **Stage 18** (the α9.0 creator analytics — the
  outbox projection writing one owner-scoped `analytics_events` row per publish/export
  lifecycle event, exactly-once under redelivery via the `(source_event_id, occurred_at)`
  unique index, owner isolation, and `GET /api/v1/analytics/summary` visibility + `422`/`401`),
  **Stage 19** (the α9.1 AI publish-metadata suggestion — a deterministic suggestion over the
  mock LLM, deterministic-template fallback, owner isolation `404`, not-ready export `422`, and the
  real `POST /api/v1/publish-metadata/suggestions` end-to-end `200`/`401`/`404`/`422`),
  **Stage 20** (the α9.2 media library — the real `LibraryRepository` + `library.*` use cases
  honouring `(parent, name)` / `media_asset_id` uniqueness, version-fenced OCC, keyset pagination,
  the tag GIN ANY-of browse, hiding entries with soft-deleted media, folder-move cycle rejection,
  folder-delete asset detachment, and idempotent reuse — plus the real `/api/v1/library/*`
  endpoints for an authenticated owner),
  **Stage 21** (the α9.3 publish thumbnail support — an owned image carried through
  `CreatePublishJob` → worker materialisation → the credential-blind destination as an
  `UploadThumbnail`; best-effort when the image is soft-deleted before publish; ownership `404`
  / non-image `422`; plus the additive request field's HTTP-contract validation),
  **Stage 22** (the α9.4 multi-destination publishing — `POST /publish-jobs/batch` fanning one
  export out to N connected accounts as N distinct `publish_jobs` the worker drains; shared
  prerequisite fail-fast — unknown export → `404`, zero jobs; per-account isolation so a bad
  account never blocks the rest; plus HTTP auth + empty/over-cap/duplicate shape validators),
  and **Stage 23** (the α9.5 notification email delivery — the leased worker sending via the mock
  `LoggingNotifier` and stamping `delivered_email_at` send-then-stamp, then never re-scanning;
  transient failure recording backed-off retry bookkeeping and permanent failure / attempt ceiling
  recording a terminal state in the reserved `payload["_email"]` namespace; and the proof that the
  reserved namespace is present in the stored row yet **stripped from the read API**),
  and **Stage 24** (the α9.6 TikTok destination inside the real publish runtime — the happy
  path recording the durable `publish_id` as the stable identifier; a retryable pre-upload
  failure requeuing with backoff and emitting no `PublishJobFailed`; the **PUB-11 indeterminate
  timeout** settling `failed` with attempts still remaining, proving permanent classification
  rather than attempt exhaustion stopped it; and a rotated refresh token reaching the stored
  **encrypted** credential with replay intact),
  and **Stage 25** (the α9.7 generation ingress — the full queue → claim → execute → poll path;
  **GEN-1** proved against a real ingress row, with `begin()` deliberately passed a *different*
  prompt and seed and every ingress-owned column asserted byte-for-byte unchanged, plus
  convergence under repeated calls; owner isolation across two tenants; legacy ownerless rows
  invisible **and** non-promotable while still physically present; the **F3** fix, where the same
  generation reads as absent for a non-owner; DB-enforced create idempotency; keyset paging with
  no duplicates or gaps; queued-only cancel preventing execution while a claimed one is refused;
  and the reaper terminalising an abandoned run to `failed` **without re-running it**),
  and **Stage 26** (the α9.8 worker runtime host — the first stage in which background work
  executes **without a test calling `run_once()`**: a real `WorkerHost` schedules real workers
  against the live database and drains a queued generation to `completed` and an outbox event to
  stamped, honours a mid-flight stop by finishing the in-flight item and claiming nothing further,
  and — across two hosts contending over one queue with `batch_size=1` to force contention —
  **demonstrates** ADR-0053 D4 replica safety rather than asserting it)
  against an ephemeral Postgres + real ffmpeg.
  The α8.6c YouTube adapter adds **no** Stage 14 test: it is covered by **network-free unit
  tests in Stage 4** (via `httpx.MockTransport`) plus an **opt-in live smoke test excluded
  from CI**, so Stage 14 stays deterministic and offline (the Mock destination remains the
  CI default). Stage 24 follows the same discipline — `httpx.MockTransport` throughout, TikTok
  unconfigured in CI, and its own opt-in live smoke test excluded from the gate.

Governance principle: infrastructure stages stay stable; each **new bounded context**
earns its **own** stage — **Stage 14** is the Publishing stage, extended in place across the
persistence-bearing Publishing slices (α8.6a connections, α8.6b publish runtime, and the
α8.9b creator-scheduling ingress) rather than expanding Stage 13 into a catch-all; the
adapter-only α8.6c slice keeps its determinism by staying in Stage 4; the α8.8 asset
promotion bridge earns its own **Stage 15**; the α8.9a publish notifications fan-out
earns its own **Stage 16** (the notifications context reacting to publish events — a
downstream consumer, not the publish runtime); the α8.9c creator dashboard — a new
read surface composed across bounded contexts — earns its own **Stage 17**; the α9.0
creator analytics — a dormant table activated as a downstream outbox consumer plus a new
read surface — earns its own **Stage 18**; the α9.1 AI publish-metadata suggestion — the
first publishing→AI bridge — earns its own **Stage 19**; the α9.2 media library — a new
owner-scoped bounded context sibling to the Media aggregate — earns its own **Stage 20**;
the α9.3 publish thumbnail support — an additive, best-effort extension of the Publishing plane
(a new destination-delivery artifact) — earns its own **Stage 21**; the α9.4 multi-destination
publishing — an additive fan-out orchestration over the existing publish runtime — earns its own
**Stage 22**; the α9.5 notification email delivery — a new **outbound external channel** driven by
its own dedicated leased worker (not the relay) — earns its own **Stage 23**; and the α9.6 TikTok
destination — the **second real destination** exercising the publish runtime through a different
provider protocol (asynchronous publish adapted inside the adapter) — earns its own **Stage 24**;
and the α9.7 generation ingress — the **first creator-facing entry point into the generation
plane**, introducing generation ownership, a queue, and a worker — earns its own **Stage 25**;
and the α9.8 worker runtime host — the process that makes **all seven** poll workers actually run,
and the first stage where background work executes without a test driving it — earns its own
**Stage 26** (current baseline `v0.4.51-phase3-alpha9.8`).

---

## 6. Where to go deeper (governing documents)

**Freezes (ADRs — enforce boundaries):**
- [ADR-0042](../decisions/ADR-0042-orchestration-platform-freeze.md) — orchestration platform is frozen.
- [ADR-0043](../decisions/ADR-0043-render-composition-boundary.md) — render composition is a pure `Timeline + assets → video` transform.
- [ADR-0044](../decisions/ADR-0044-ai-runtime-generation-architecture.md) — AI runtime & generation architecture (Plan→Generate→Verify→Repair; Minimum Runtime Contract).
- [ADR-0045](../decisions/ADR-0045-ai-runtime-core-freeze.md) — AI runtime core freeze (the three planes; Decision-plane invariants F1–F7).
- [ADR-0046](../decisions/ADR-0046-execution-runtime-boundaries.md) — Execution Runtime boundaries (X1–X8; ships with Increment 4).
- [ADR-0053](../decisions/ADR-0053-worker-runtime-host.md) — worker runtime host (α9.8; process topology, scheduling, bounded drain, HOST-1/HOST-2, replica safety as a per-worker obligation).
- [ADR-0052](../decisions/ADR-0052-generation-ingress-ownership-execution-and-read-contract.md) — generation ingress: ownership, execution model & read contract (α9.7; resolves the ADR-0046 Q1 ownership deferral).
- [ADR-0054](../decisions/ADR-0054-execution-adapter-dispatch.md) — execution adapter dispatch (α9.9; executability as a Decision-plane input, decision vs execution provenance, DISP-1/DISP-2/DISP-3).

**Contracts (engineering — implementation reference):**
- [CINEMATIC_STORYBOARD_CONTRACT](./CINEMATIC_STORYBOARD_CONTRACT.md) — Planner v2 (α8.7): ShotIntent, story arcs, CS-7/CS-8.
- [RESOLVER_RUNTIME_CONTRACT](./RESOLVER_RUNTIME_CONTRACT.md) — α8.5e resolver.
- [EXECUTION_RUNTIME_CONTRACT](./EXECUTION_RUNTIME_CONTRACT.md) — α8.6 Increment 4 persistence.
- [PROVIDER_RUNTIME_DATA_MODEL](./PROVIDER_RUNTIME_DATA_MODEL.md) — the Knowledge-plane catalogue.
- [AI_RUNTIME_PLANES](./AI_RUNTIME_PLANES.md) — why Knowledge/Decision/Execution are separate.

**Data:**
- [Database ERD](../database/ERD.md) — clusters incl. Execution Runtime & Provenance (Cluster 12).
- [Content Generation Pipeline blueprint](../architecture/CONTENT_GENERATION_PIPELINE.md) — the Phase-3 architectural blueprint this runtime realises.
