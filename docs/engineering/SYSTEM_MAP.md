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
    destination adapter (YouTube — `YouTubeOAuthClient` + `YouTubeDestination`) in α8.6c.
```

…and the single request that flows through them, end to end:

```
  Prompt
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
  Publisher          MP4 → YouTube / TikTok / Instagram         Execution  ⟢ planned
```

Every arrow is a **stable seam** (a port or a pure function). New capability plugs
into a seam; it does not reshape the flow (ADR-0042, ADR-0045).

---

## 2. Stage by stage

| Stage | Plane | What it does | Code seam | Governed by | Status |
|---|---|---|---|---|---|
| **Prompt** | input | The request + the project *world state* (characters, locations, style, seed). | `GenerateVideoRequest`, `IdentityProfile` | [ADR-0044](../decisions/ADR-0044-ai-runtime-generation-architecture.md) | ✅ |
| **Planner** | Decision | Decomposes the prompt into a deterministic cinematic arc of `ShotIntent`s (establishing → … → closing); assigns semantic shot ids + derived seeds. Never names a provider. | `plan_from_prompt` (`domain/generation/planner.py`), `shot_intent.py` | [CINEMATIC_STORYBOARD_CONTRACT](./CINEMATIC_STORYBOARD_CONTRACT.md) · [ADR-0045](../decisions/ADR-0045-ai-runtime-core-freeze.md) | ✅ α8.7 |
| **Storyboard** | Decision | Turns each `ShotIntent` into a concrete `ShotPrompt` via the Prompt Builder (the sole place intent becomes generator-facing wording, CS-8). | `build_storyboard`, `prompt_builder.py` | [CINEMATIC_STORYBOARD_CONTRACT](./CINEMATIC_STORYBOARD_CONTRACT.md) | ✅ α8.7 |
| **Resolver** | Decision | Given a *capability* + constraints, returns explainable, ordered adapter candidates. Pure; reads immutable Knowledge + runtime snapshots. | `ICapabilityResolver` / `ResolverCapabilityResolver`; `domain/resolver` | [RESOLVER_RUNTIME_CONTRACT](./RESOLVER_RUNTIME_CONTRACT.md) · [ADR-0045](../decisions/ADR-0045-ai-runtime-core-freeze.md) | ✅ α8.5e |
| **Generation Runtime** | Execution | Composes the pipeline: executes the top eligible adapter per shot, drives the model cache for local tiers. No provider branching, never scores. | `GenerateVideo` (`application/use_cases/generation/generate_video.py`); `IImageGenerator` | [ADR-0044 (MRC)](../decisions/ADR-0044-ai-runtime-generation-architecture.md) · [ADR-0046](../decisions/ADR-0046-execution-runtime-boundaries.md) | ✅ α8.6 |
| **Verification** | Decision (pure) | A policy over *observed features* (extracted by infra): resolution, aspect, blank/blur/watermark, cross-shot similarity. Decides pass/fail; never sees raw bytes. | `verify_image` (`domain/generation/verification.py`); `IImageFeatureExtractor` | [ADR-0045](../decisions/ADR-0045-ai-runtime-core-freeze.md) | ✅ v1 |
| **Repair** | Decision (pure) | On failure, retries the *single* shot with a fresh derived seed up to a cap, else gives up (fails the run). | `decide_repair` (`domain/generation/repair.py`) | [ADR-0045](../decisions/ADR-0045-ai-runtime-core-freeze.md) | ✅ v1 |
| **Timeline gate** | Decision (pure) | Pre-render check: missing / duplicate / out-of-order frames, duration & aspect. Cheaply rejects a broken timeline before ffmpeg. | `verify_timeline` (`domain/generation/timeline_verification.py`) | [ADR-0043](../decisions/ADR-0043-render-composition-boundary.md) | ✅ α8.6 |
| **FFmpeg** | Execution | Renders accepted frames into an MP4; ffprobe measures the output for verification. | `FfmpegSlideshowRenderer`, `FfprobeVideoProbe` (`infrastructure/render/`) | [ADR-0043](../decisions/ADR-0043-render-composition-boundary.md) | ✅ α8.6 |
| **Execution Runtime** | Execution | Persists the whole run incrementally: `generations` + `generation_shots`, the `status` state machine, the resolution ledger, and lifecycle events via the transactional outbox. | `SqlExecutionRuntimeStore`; migration `0012` | [EXECUTION_RUNTIME_CONTRACT](./EXECUTION_RUNTIME_CONTRACT.md) · [ADR-0046](../decisions/ADR-0046-execution-runtime-boundaries.md) | ✅ α8.6 |
| **Asset Store** | Execution | Registers execution-owned artefacts (`generation_assets`, with a `parent_asset_id` lineage) and stores bytes behind object storage. Promotion to `media_assets` is the explicit `PromoteGenerationAssets` bridge (α8.8 — see the next row). | `generation_assets`; `IObjectStorage` | [ADR-0046](../decisions/ADR-0046-execution-runtime-boundaries.md) · [ADR-0037](../decisions/ADR-0037-media-generation-outputs.md) | ✅ α8.6 |
| **Asset Promotion Bridge** | Execution → Library | The **ADR-0046 X8** (`PublishGenerationAssets`) seam: promotes a completed generation's final video (`generation_assets`, Path B) into the platform media library (`media_assets(source='generated')`, Path A) that feeds Render → Export → Publish. User-initiated, **library-only**; **copies** bytes (never a shared reference), request-time project-scoped ownership, recomputed checksum + provenance, deterministic-key idempotency (`noop` replay). Reads through a new **read-only** `IGenerationReader` (no existing port changed); **no schema migration**. | `PromoteGenerationAssets` (`application/use_cases/media/`), `IGenerationReader`/`GenerationReader`, `POST /api/v1/media/promotions` | [ADR-0046](../decisions/ADR-0046-execution-runtime-boundaries.md) · [PHASE3_ASSET_PROMOTION_BRIDGE_PREFLIGHT](./PHASE3_ASSET_PROMOTION_BRIDGE_PREFLIGHT.md) | ✅ α8.8 |
| **Publisher** | Execution | Uploads the final MP4 to social platforms behind per-platform adapters. Its **account-connection foundation** (OAuth credential ownership: `SocialAccount`, envelope-encrypted tokens, owner-scoped `/api/v1/social-accounts`) shipped in **α8.6a**; the **publish runtime** (user-initiated `PublishJob` + poll-ingress `PublishWorker`, dual-lock serialization, bounded retries, terminal outbox events, `/api/v1/publish-jobs`) in **α8.6b** — credential-blind, consuming only the `AuthorizedContext` and uploading via `IDestinationPublisher`; and the **first real destination adapter** (YouTube — `YouTubeOAuthClient` + `YouTubeDestination`, Data API v3 resumable upload over a thin injected `httpx` transport, PUB-11 ambiguous-outcome safety) in **α8.6c** — adapter-only, no port/runtime change, still credential-blind (Mock remains the CI default; live smoke is opt-in). Downstream, the **α8.9a publish notifications** fan-out (`PublishNotificationProjection`) turns the terminal `PublishJobSucceeded`/`PublishJobFailed` events into in-app notifications for the requester — a separate consumer on the existing outbox fan-out, not part of the publish runtime. **α8.9b creator scheduling** adds an optional, boundary-validated `publish_at` on the create request that threads into `ContentPackage.publish_at` → the YouTube `status.publishAt` mapping (platform-native go-live; upload still immediate, `scheduled_at` untouched). | `SocialCredentialService`, `/social-accounts` (α8.6a); `CreatePublishJob`/`ProcessPublishJob`/`PublishWorker`, `IDestinationPublisher`/`IDestinationRegistry`, `/publish-jobs` (α8.6b); `YouTubeOAuthClient`/`YouTubeDestination` (α8.6c); `PublishNotificationProjection` (α8.9a); `PublishJobCreateRequest.publish_at` (α8.9b) | [PUBLISHING_RUNTIME_CONTRACT](./PUBLISHING_RUNTIME_CONTRACT.md) · [ADR-0047](../decisions/ADR-0047-publishing-credential-ownership.md) | ◑ α8.6a (connections) · ✅ α8.6b (runtime) · ✅ α8.6c (YouTube) · ✅ α8.9a (notifications) · ✅ α8.9b (scheduling) |
| **Creator Dashboard** | API / read | Read-only owner-scoped summary of the caller's product state — publish-job counts by status, connected/total social accounts, unread notifications, media total — composed inside a single UoW from the **already-existing** owner-scoped reads (no new repository method, no new SQL, no migration, no analytics). Completes the **α8.9 Creator Experience**. | `GetCreatorDashboard` (`application/use_cases/dashboard/`), `GET /api/v1/dashboard/summary` | [PHASE3_ALPHA8_9c_PREFLIGHT](./PHASE3_ALPHA8_9c_PREFLIGHT.md) | ✅ α8.9c |
| **Creator Analytics** | Outbox consumer + API / read | Activates the dormant, partitioned `analytics_events` table as a **fourth independent** downstream outbox consumer: `AnalyticsProjection` maps the publish + export lifecycle events → a stable `event_name` vocabulary + neutral properties, and the reused `RecordAnalyticsEvent` writer (own UoW/event; tenant resolved in-UoW) persists one owner-scoped row each. **Exactly-once is DB-owned** (`source_event_id = event.id`, `occurred_at = event.occurred_at`; partial-unique `(source_event_id, occurred_at)` including the partition key). Read via owner-scoped `GET /api/v1/analytics/summary` (`GetCreatorAnalytics`: zero-filled per-`event_name` counts + total, trailing-30d default, naive/inverted window → 422). Migration `0015`; one new `IAnalyticsRepository`; no producer/frozen-runtime change. | `AnalyticsProjection` / `RecordAnalyticsEvent` / `GetCreatorAnalytics` (`application/use_cases/analytics/`), `IAnalyticsRepository`/`AnalyticsRepository`, `GET /api/v1/analytics/summary`, migration `0015` | [ADR-0048](../decisions/ADR-0048-analytics-consumer-idempotency.md) · [PHASE3_ALPHA9_0_PREFLIGHT](./PHASE3_ALPHA9_0_PREFLIGHT.md) | ✅ α9.0 |
| **AI Publish Metadata** | Publishing → AI bridge (advisory) | The **first publishing→AI bridge**: an opt-in `POST /api/v1/publish-metadata/suggestions` that *suggests* publish metadata (title / description / hashtags) for a finished, owned export. A **Publishing-owned** `IPublishMetadataGenerator` port (neutral, `ContentPackage`-free DTOs) is implemented by the AI-subsystem `LlmPublishMetadataGenerator` over `Capability.LLM` (deterministic mock by default) — dependency is **strictly one-way** (AI never imports Publishing), pinned by a new import-linter contract. `GeneratePublishMetadata` validates ownership/readiness first (`404`→`422`), then calls the port **outside** the read UoW with a **mandatory deterministic template fallback** on any AI failure (PUB-9 preserved). Advisory only: never a `PublishJob` prerequisite; user edits win; only final metadata persisted (provenance ephemeral); destination adapter stays AI-unaware. No migration; no frozen-runtime change. | `IPublishMetadataGenerator` (`application/interfaces/`), `LlmPublishMetadataGenerator` (`infrastructure/ai/metadata/`), `GeneratePublishMetadata` (`application/use_cases/publishing/`), `POST /api/v1/publish-metadata/suggestions` | [ADR-0049](../decisions/ADR-0049-ai-publish-metadata-boundary.md) · [PHASE3_ALPHA9_1_PREFLIGHT](./PHASE3_ALPHA9_1_PREFLIGHT.md) | ✅ α9.1 |

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
  (the Generation Runtime end-to-end slice), **Stage 14** (publishing — account
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
  and **Stage 19** (the α9.1 AI publish-metadata suggestion — a deterministic suggestion over the
  mock LLM, deterministic-template fallback, owner isolation `404`, not-ready export `422`, and the
  real `POST /api/v1/publish-metadata/suggestions` end-to-end `200`/`401`/`404`/`422`)
  against an ephemeral Postgres + real ffmpeg.
  The α8.6c YouTube adapter adds **no** Stage 14 test: it is covered by **network-free unit
  tests in Stage 4** (via `httpx.MockTransport`) plus an **opt-in live smoke test excluded
  from CI**, so Stage 14 stays deterministic and offline (the Mock destination remains the
  CI default).

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
read surface — earns its own **Stage 18**; and the α9.1 AI publish-metadata suggestion — the
first publishing→AI bridge — earns its own **Stage 19** (current baseline
`v0.4.44-phase3-alpha9.1`).

---

## 6. Where to go deeper (governing documents)

**Freezes (ADRs — enforce boundaries):**
- [ADR-0042](../decisions/ADR-0042-orchestration-platform-freeze.md) — orchestration platform is frozen.
- [ADR-0043](../decisions/ADR-0043-render-composition-boundary.md) — render composition is a pure `Timeline + assets → video` transform.
- [ADR-0044](../decisions/ADR-0044-ai-runtime-generation-architecture.md) — AI runtime & generation architecture (Plan→Generate→Verify→Repair; Minimum Runtime Contract).
- [ADR-0045](../decisions/ADR-0045-ai-runtime-core-freeze.md) — AI runtime core freeze (the three planes; Decision-plane invariants F1–F7).
- [ADR-0046](../decisions/ADR-0046-execution-runtime-boundaries.md) — Execution Runtime boundaries (X1–X8; ships with Increment 4).

**Contracts (engineering — implementation reference):**
- [CINEMATIC_STORYBOARD_CONTRACT](./CINEMATIC_STORYBOARD_CONTRACT.md) — Planner v2 (α8.7): ShotIntent, story arcs, CS-7/CS-8.
- [RESOLVER_RUNTIME_CONTRACT](./RESOLVER_RUNTIME_CONTRACT.md) — α8.5e resolver.
- [EXECUTION_RUNTIME_CONTRACT](./EXECUTION_RUNTIME_CONTRACT.md) — α8.6 Increment 4 persistence.
- [PROVIDER_RUNTIME_DATA_MODEL](./PROVIDER_RUNTIME_DATA_MODEL.md) — the Knowledge-plane catalogue.
- [AI_RUNTIME_PLANES](./AI_RUNTIME_PLANES.md) — why Knowledge/Decision/Execution are separate.

**Data:**
- [Database ERD](../database/ERD.md) — clusters incl. Execution Runtime & Provenance (Cluster 12).
- [Content Generation Pipeline blueprint](../architecture/CONTENT_GENERATION_PIPELINE.md) — the Phase-3 architectural blueprint this runtime realises.
