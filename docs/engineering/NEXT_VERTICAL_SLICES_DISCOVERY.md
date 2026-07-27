# Next Vertical Slices — Discovery Report

> **Status:** Read-only discovery. **Facts only** — no design, no implementation, no planning
> beyond discovery. This document does **not** select a slice or authorise work.
>
> **Baseline:** `v0.4.38-phase3-alpha8.6c` (frozen). Nothing in the repository was modified to
> produce this report; only this file was created.
>
> **Method:** Direct inspection of the repository at `/Users/rehanshifque/dev/ai-video-platform`
> (domain / application / infrastructure / api layers, Alembic migrations `0001`–`0014`, ORM
> models, engineering contracts, and ADRs). Every "exists / does not exist" claim below is
> grounded in a named file, port, table, column, migration, event, route, or contract section.

---

## 0. How to read this

**Estimated implementation size** (relative to prior α8.x slices):

| Size | Meaning (rough) |
|---|---|
| **Very Small** | one leaf/use case behind an existing port; no migration |
| **Small** | a few use cases + API; no migration or trivial additive one |
| **Medium** | new use cases + repository + API + tests; possibly one additive migration |
| **Large** | new bounded context or cross-context bridge; migration + contract/ADR |
| **Very Large** | multiple contexts, external integrations, or new external-service surface |

**Product impact:** Low · Medium · High · Transformational.

**Global fact that colours every slice:** there is **no frontend / UI package in this repository**
(backend only — confirmed: repo root has `backend/` + `docs/`, no `frontend/`, no `.tsx` app tree).
Therefore every "UI" line below is **"does not exist in repo"**, and all product value is delivered
as **backend enablers** (APIs, read models, workers) that a future UI (ROADMAP Phase 5) would consume.

---

## 1. Repository facts snapshot (shared groundwork)

**Architecture:** three planes — Knowledge (`what can exist`), Decision (`what should happen`,
pure/frozen), Execution (`what did happen`) — see `docs/engineering/SYSTEM_MAP.md`.

**Frozen contracts (must not be broken by additive slices):**
`ADR-0042` (orchestration platform freeze), `ADR-0043` (render composition boundary RC1–RC6),
`ADR-0044` (AI runtime architecture AR1–AR18), `ADR-0045` (Decision-plane freeze F1–F7),
`ADR-0046` (Execution-runtime boundaries X1–X8), `ADR-0047` (publishing credential ownership).

**Two artefact pipelines exist today (important):**

- **Path A — Workflow/Platform:** provider adapters → `WorkflowRun` → `WorkflowRunSucceeded`
  event → `GeneratedMediaIngestionSubscriber` → `IngestGeneratedMedia` → **`media_assets`**
  (`source='generated'`) → Timeline clips → `RenderJob` → `ExportJob`
  (`export_jobs.output_media_asset_id`) → `PublishJob`. **This path reaches export + publish.**
- **Path B — AI Generation Runtime:** `GenerateVideo`
  (`application/use_cases/generation/generate_video.py`, the Stage-13 slice) → **`generation_assets`**
  (migration `0012`, execution-owned, `parent_asset_id` lineage) → object storage. **This path does
  NOT reach `media_assets`, export, or publish in code today.**
- The documented bridge between them — **`PublishGenerationAssets`** — is named in
  `ADR-0046` (X8) and `EXECUTION_RUNTIME_CONTRACT.md` (W8.6.8) but **is not implemented** (no module,
  class, port, route, or test references it). See slice **§2.10**.

**Transactional outbox events emitted today** (and who consumes them):

| Event type (string) | Emitter | Consumer(s) today |
|---|---|---|
| `WorkflowRunSucceeded` (+ other `WorkflowRun*`) | `workflow/_events.py` | `GeneratedMediaIngestionSubscriber` |
| `ExportJobCreated/Succeeded/Failed` | `export/_events.py` | `NotificationProjection` (terminal only) |
| `RenderJobCreated/Canceled/Succeeded/Failed` | `render/_events.py` | **none** |
| `PublishJobCreated/Succeeded/Failed` | `publishing/_events.py` | **none** (DQ7 deferred) |
| `generation.started/shot_generated/…/export_completed` | `generation/events.py` | **none** |

Verified: `notification_projection.py` → `_HANDLED_EVENT_TYPES = {EVENT_EXPORT_JOB_SUCCEEDED,
EVENT_EXPORT_JOB_FAILED}` (export only).

**Schema-exists-but-no-code (Phase-2 baseline `0001` created the tables; no domain/app/API on top):**

| Feature | Tables (ORM) | Code on top? |
|---|---|---|
| DB templates | `templates` (`models/templates.py`) | **none** |
| Billing | `plans`, `subscriptions`, `invoices`, `credit_ledger` (`models/billing.py`) | **none** |
| Usage metering | `usage_records`, `cost_reconciliations` (`models/usage.py`) | **partial** — `UsageRecorderService` writes rows; `credits_consumed` hard-wired to `0`; **no read API, no ledger debit** |
| Analytics | `analytics_events` (partitioned; `models/analytics.py`) | **none** |
| Media library | `library_assets` (pgvector `embedding vector(1536)` + HNSW; `library_folders`; `library_asset_projects`) | **none** |
| Agent memory | `agent_memory` (pgvector) | **none** |
| Feature flags / config | `feature_flags`, `feature_flag_overrides`, `system_settings`, `tenant_settings`, `provider_settings` | **none** (not on `IUnitOfWork`) |
| RBAC roles | `roles`, `roles_users` (seeded: `owner/admin/editor/viewer/billing/support`) | **write-only** — `RegisterUser` assigns `owner`; no role checks anywhere |

(Embedding columns are guarded by `backend/tests/test_metadata.py`: only `library_assets.embedding`
and `agent_memory.embedding` are permitted — a new ADR is required to add more.)

**Reusable cross-cutting seams for new slices:** keyset pagination (`application/pagination.py`),
response envelope (`api/v1/helpers.py`), owner scoping (`CurrentUserDep`, `api/v1/deps.py`),
transactional outbox + `InProcessPublisher` fan-out, distributed locks
(`distributed_locks`, `SqlAlchemyDistributedLockManager`), worker/lease poll-ingress pattern
(`ExportWorker`/`PublishWorker`), the `ExportJob`/`PublishJob` OCC+CAS state-machine template.

---

## 2. Candidate slices (facts)

Grouped for readability. Each entry follows the requested 8-point structure.

### 2.1 Publish notifications (α8.6d — the deferred DQ7 consumer)

1. **User value.** Creators learn the outcome of a publish (succeeded/failed) instead of polling
   `GET /publish-jobs`. Benefits every publishing user; closes the explicit DQ7 deferral.
2. **Existing groundwork.** Emitters already exist: `PublishJobSucceeded`/`PublishJobFailed`
   (`publishing/_events.py`). The **entire in-app notification stack is proven** for the export
   equivalent: `NotificationProjection` (`use_cases/notifications/notification_projection.py`),
   `CreateNotification`, `Notification` domain entity, `notifications` table (`models/notifications.py`),
   `NotificationRepository`, read API `GET /api/v1/notifications` (+ unread-count, read, read-all),
   `NotificationPublic` DTO, and `source_event_id` idempotency (`0009`). Payload already carries
   `requested_by_user_id`.
3. **Missing pieces.** *Domain:* none. *Application:* add `Publish*` to `_HANDLED_EVENT_TYPES` +
   content mappers (new `kind` values e.g. `publish.succeeded`/`publish.failed`); the projection is
   already registered on the publisher. *Infrastructure:* none. *API:* none (existing read API
   surfaces it). *Persistence:* none. *UI:* n/a. *Testing:* projection + wiring tests.
4. **Architectural impact.** Extends the Notifications context; additive; no migration; no ADR; no
   frozen contract touched.
5. **Size.** **Very Small.**
6. **Product impact.** **Medium.**
7. **Dependencies.** α8.6b (publish events), α8.5b.3 (notification projection).
8. **Risk.** Lowest of any candidate — the pattern is already shipped for exports; main care is
   deterministic content templates + idempotent projection (already the norm).

### 2.2 Scheduling (publish at a future time)

1. **User value.** Schedule posts for optimal times — a headline feature for any social publishing
   tool. Benefits all creators.
2. **Existing groundwork.** Substrate is largely present: `publish_jobs.scheduled_at` column +
   claim index `ix_publish_jobs_status_scheduled_at` (migration `0014`); `PublishJobRepository.list_claimable`
   already filters `status='queued' AND (scheduled_at IS NULL OR scheduled_at <= now)`;
   `reschedule_for_retry` already writes `scheduled_at` (retry backoff, DQ6);
   `ContentPackage.publish_at` already maps to YouTube `status.publishAt` (+ forces `private`) in
   `YouTubeDestination._build_request_body` (tested).
3. **Missing pieces.** *Domain:* validation of user `publish_at`/`scheduled_at`. *Application:* wire
   user intent into `CreatePublishJob` (today it hard-codes `scheduled_at=None` and does not pass
   `publish_at`). *Infrastructure:* an **external cadence** to call `PublishWorker.run_once()` —
   `PUBLISHING_RUNTIME_CONTRACT.md` §7/§14 state there is deliberately **no in-repo scheduler**.
   *API:* add `publish_at`/`scheduled_at` to `PublishJobCreateRequest`. *Persistence:* **none**
   (columns exist). *UI:* n/a. *Testing:* create→due-scan→publish E2E.
4. **Architectural impact.** Extends Publishing; additive; **no migration**; behaviour already
   described in the contract (no new ADR); the "no time scheduler" note is a known operational gap,
   not a code change.
5. **Size.** **Small.**
6. **Product impact.** **High.**
7. **Dependencies.** α8.6b runtime, α8.6c destination.
8. **Risk.** The runtime relies on an external process to call `run_once()` on a cadence; without a
   deployed scheduler, scheduled jobs never drain. Deciding worker-delay vs platform-side schedule
   (`scheduled_at` vs `publish_at`) semantics is the main design question.

### 2.3 AI caption & hashtag generation

1. **User value.** Auto-writes titles/descriptions/hashtags — directly serves the product thesis
   ("create social videos without editing/marketing skill"). Benefits all publishing users.
2. **Existing groundwork.** Publishing metadata is a stable, pure surface today: `ContentPackage`
   (`domain/publishing/content_package.py`) with `title`/`description`/`tags`/`visibility` and a
   deterministic `build_content_package` (PUB-9); `CreatePublishJob` already accepts optional
   overrides; stored as `content_package` JSONB. The **LLM capability already exists** in the
   generation plane: `Capability.LLM`, `GenerateTextRequest`/`GenerateTextResponse`
   (`interfaces/providers.py`), `MockLLMProvider`, and the `StepCommandDispatcher` routing.
3. **Missing pieces.** *Domain:* optional metadata provenance VO. *Application:* a new
   `GeneratePublishMetadata`-style use case that calls the LLM **through an infrastructure adapter**
   (must NOT import the AI `ProviderRegistry`/resolver into `domain.publishing` — PUB-3/PUB-4,
   enforced by import-linter); a deterministic fallback. *Infrastructure:* an LLM-metadata adapter.
   *API:* opt-in flag / preview endpoint. *Persistence:* **none** (JSONB). *UI:* n/a. *Testing:*
   deterministic + LLM-path tests.
4. **Architectural impact.** Extends Publishing via an adapter; additive; **no migration**;
   `PUBLISHING_RUNTIME_CONTRACT.md` §5/§14 call this "its own later slice with its own contract"
   → **a new engineering contract is expected**; must preserve PUB-4 (destinations/metadata are not
   AI providers).
5. **Size.** **Medium.**
6. **Product impact.** **High.**
7. **Dependencies.** α8.6b (`ContentPackage`), the α8.x provider/LLM plane.
8. **Risk.** Keeping the credential-blind / PUB-4 separation intact while reaching into the AI plane;
   non-determinism vs the current deterministic guarantee (needs an explicit fallback + provenance).

### 2.4 Thumbnail generation / custom thumbnails

1. **User value.** Better click-through with a chosen/derived thumbnail. Benefits publishing users.
2. **Existing groundwork.** `ContentPackage.thumbnail_media_asset_id` field already exists (JSONB
   round-trip). The **media enrichment pipeline is shipped** (α8.4c/d): `IThumbnailer`
   (`interfaces/thumbnailer.py`), `FfmpegThumbnailer`, `ThumbnailEnricher`, `EnrichGeneratedMedia`
   (writes `source_metadata.enrichment.thumbnail_media_asset_id`), `MediaEnrichmentWorker`.
3. **Missing pieces.** *Domain:* rules for choosing the thumbnail on the export-delivery asset.
   *Application:* resolve `thumbnail_media_asset_id` in `CreatePublishJob`; extend `ProcessPublishJob`
   to materialise thumbnail bytes. *Infrastructure:* YouTube `thumbnails.set` call in
   `YouTubeDestination` (no thumbnail API usage today). *API:* accept `thumbnail_media_asset_id` on
   create. *Persistence:* **none** (JSONB). *UI:* n/a. *Testing:* publish-with-thumbnail E2E.
4. **Architectural impact.** Extends Publishing + reuses Media enrichment; additive; **no migration**;
   contract §14 defers custom upload but permits reusing the enrichment thumbnail.
5. **Size.** **Small–Medium.**
6. **Product impact.** **Medium.**
7. **Dependencies.** α8.4c enrichment, α8.6b/c publishing.
8. **Risk.** Platform-specific thumbnail APIs (size/format/verification rules); a second upload phase
   adds a new ambiguous-outcome surface (PUB-11-style reasoning).

### 2.5 Multi-destination publishing (one artifact → several destinations)

1. **User value.** Publish once to many channels in a single action. High value for cross-posting
   creators.
2. **Existing groundwork.** The model is already one job per `(source_media_asset_id,
   social_account_id)`; the partial-unique idempotency index permits **N independent jobs** for the
   same export to different accounts. So N sequential `POST /publish-jobs` calls already work; the
   per-project `project_publish` lock serialises them.
3. **Missing pieces.** *Domain:* optional `PublishBatch` grouping. *Application:* a batch/fan-out use
   case + build-metadata-once semantics. *Infrastructure:* batch repo ops. *API:*
   `social_account_ids: list[...]` or a batch endpoint + grouped status. *Persistence:* optional
   `publish_batch_id`/correlation column (only if grouped status is required). *UI:* n/a. *Testing:*
   one-action-N-destinations E2E.
4. **Architectural impact.** Extends Publishing; additive; migration **optional** (only for batch
   correlation); likely a small contract addendum (v1 is explicitly single-destination).
5. **Size.** **Small.**
6. **Product impact.** **High** (compounds with §2.6).
7. **Dependencies.** α8.6b/c; strongly compounded by additional destinations (§2.6).
8. **Risk.** Low mechanically; partial-failure UX (some destinations succeed, some fail) is the real
   product decision.

### 2.6 Additional publishing destinations (TikTok / Instagram / X …)

1. **User value.** For a social-video tool, the destination set **is** the product. TikTok/Instagram
   Reels are arguably the highest-demand channels.
2. **Existing groundwork.** The port is proven end-to-end by YouTube: `IDestinationPublisher` +
   `IDestinationRegistry` + `ISocialOAuthClient`, the credential-blind `AuthorizedContext`, the thin
   injected `httpx` transport pattern, `DestinationRegistry` wiring in
   `container._get_destination_registry()`, `platform` is free-text (no enum change), and
   `CreatePublishJob` already gates on `supported_platforms()`. PUB-11 ambiguous-outcome handling is
   a reusable template. Config pattern (`youtube_oauth_*`) is established.
3. **Missing pieces.** *Infrastructure:* per-platform `IDestinationPublisher` + `ISocialOAuthClient`
   implementations + config + registration. *Domain/Application/API/Persistence:* **none** required
   (port is sufficient; contract §12 "adapters only", §14 defers a YAML catalogue until ≥2 real
   destinations). *UI:* n/a. *Testing:* per-platform network-free unit tests via `MockTransport`.
4. **Architectural impact.** Extends Publishing; additive; **no migration**; per-platform OAuth scope
   notes may warrant small ADR addenda under ADR-0047.
5. **Size.** **Medium** per destination.
6. **Product impact.** **High → Transformational** (depending on which platforms).
7. **Dependencies.** α8.6c (the proven adapter shape).
8. **Risk.** **External, not architectural** — TikTok / Meta (Instagram) require app registration,
   review, and content-publishing API approval; OAuth + upload semantics differ per platform; native
   OAuth (PKCE/redirect) is awkward without a UI. This risk lives outside the codebase.

### 2.7 Notification delivery — email / push

1. **User value.** Reach creators off-platform (email now; push later). Benefits all users.
2. **Existing groundwork.** `notifications.delivered_email_at` column already exists (currently always
   NULL; intentionally omitted from `NotificationPublic`). In-app projection + repository are shipped.
3. **Missing pieces.** *Application:* **`INotifier` port does not exist** (planned name in docs) — a
   delivery use case/worker + a repository method to stamp `delivered_email_at`. *Infrastructure:*
   SMTP/SES/SendGrid adapter + template rendering (no adapter dir exists); push adapter (FCM/APNs).
   *Persistence:* email needs **no migration** (column exists); **push needs new columns/table**
   (`delivered_push_at`, device tokens). *API:* channel-preference endpoints. *UI:* n/a. *Testing:*
   delivery worker + adapter fakes.
4. **Architectural impact.** Extends Notifications; additive; email = no migration; push = migration;
   a small ADR for external-send idempotency/retries is appropriate. This is the roadmap's **α8.5b.4**.
5. **Size.** **Medium** (email) / **Large** (email + push + preferences).
6. **Product impact.** **High.**
7. **Dependencies.** α8.5b.3 notifications; complements §2.1.
8. **Risk.** External deliverability (SPF/DKIM, provider accounts); at-least-once send idempotency to
   avoid duplicate emails; PII handling.

### 2.8 Creator dashboard (aggregated read model)

1. **User value.** A single overview (recent projects, job statuses, publish outcomes, counts) —
   the natural landing surface. Benefits all users.
2. **Existing groundwork.** Rich per-entity list/read endpoints already exist to aggregate over:
   `ListProjects` (keyset), `ListPublishJobs`, `ListRenderJobs`, `ListMedia`, `ListNotifications`
   (keyset), `ListWorkflowRuns` (+ `WorkflowRunSummary` DTO precedent), `ListSocialAccounts`. Envelope
   + pagination + owner-scoping helpers are reusable.
3. **Missing pieces.** *Domain:* read-model types. *Application:* a `GetCreatorDashboard`/overview
   use case (aggregation queries). *Infrastructure:* SQL aggregates (or a materialised view).
   *API:* `GET /api/v1/dashboard` (does not exist). *Persistence:* optional rollup table/view.
   *UI:* n/a. *Testing:* aggregate endpoint tests. **Known gap:** there is **no export-jobs list
   endpoint** (`GetExportJob` only), which a "recent exports" widget would need.
4. **Architectural impact.** New read-only application slice (no new aggregate root); additive;
   migration optional (only for materialised read models → optional CQRS-lite ADR).
5. **Size.** **Small–Medium.**
6. **Product impact.** **Medium–High.**
7. **Dependencies.** All prior list endpoints.
8. **Risk.** Aggregation performance (deferred indexes in `INDEX_STRATEGY.md` are gated on this);
   scope creep into analytics.

### 2.9 Media library (browse / search / reuse assets)

1. **User value.** Find, organise, tag, and reuse generated/uploaded assets across projects
   (folders, tag + semantic search). Benefits any repeat creator.
2. **Existing groundwork.** **Schema fully pre-built** in `0001`: `library_assets` (1:1 to
   `media_assets`, `tags text[]` with GIN index, `embedding vector(1536)` with HNSW cosine index,
   `usage_count`, `last_used_at`, `version`), `library_folders` (tree, partial-unique name),
   `library_asset_projects` (M:N). The Media context is shipped: `MediaAsset` aggregate,
   `IMediaRepository`, `/api/v1/media` CRUD, ingestion + enrichment.
3. **Missing pieces.** *Domain:* `LibraryAsset`/`LibraryFolder` aggregates. *Application:* library
   CRUD, folder tree, tag mgmt, **embedding generation** (no embedding port exists), vector/tag
   search, and **pagination for `ListMedia`** (today explicitly not paginated, filters only
   `kind/source/project_id/scene_id`, no search). *Infrastructure:* `ILibraryRepository` + GIN/HNSW
   queries + an embedding provider adapter. *API:* `/library/*`, and `?q=/?tags=/?cursor=` on media.
   *Persistence:* **none** for core (tables + indexes exist). *UI:* n/a. *Testing:* library + search.
4. **Architectural impact.** Extends Media (library wraps media 1:1); additive; **likely no migration**
   for core (schema pre-declared as CR-8); must respect ADR-0037 (media aggregate) + ADR-0042.
5. **Size.** **Medium** (CRUD/folders/tags) → **Large** (with semantic/vector search + embeddings).
6. **Product impact.** **High.**
7. **Dependencies.** α6.2 Media, α8.4 ingestion/enrichment.
8. **Risk.** Embedding provider choice/cost for vector search; adding embedding columns beyond the two
   approved requires an ADR (guarded by `test_metadata.py`).

### 2.10 Asset promotion bridge (`generation_assets` → export/publish path)

1. **User value.** Makes the **AI generation runtime's output actually reach export + publish in
   code**. Today `GenerateVideo` (Path B) writes `generation_assets`, which is **not** connected to
   the `media_assets`→render→export→publish path (Path A). This is the connective tissue of the
   end-to-end pipeline. Benefits every user of the AI generation runtime.
2. **Existing groundwork.** The seam is **named and reserved** but unbuilt: `PublishGenerationAssets`
   in `ADR-0046` (X8) + `EXECUTION_RUNTIME_CONTRACT.md` (W8.6.8) + the `0012` migration header.
   Execution side exists: `IExecutionRuntimeStore`/`SqlExecutionRuntimeStore`, `generation_assets`
   (with `parent_asset_id` lineage), `generation.export_completed` event, object storage. Target side
   exists: `IngestGeneratedMedia`/`RegisterMedia` (Path A registration into `media_assets`),
   `IObjectStorage`.
3. **Missing pieces.** *Domain:* promotion/lineage mapping. *Application:* the
   **`PublishGenerationAssets` use case does not exist** (verified: zero references) — read
   `generation_assets`, copy bytes, register `media_assets`; optional subscriber on
   `generation.export_completed`. *Infrastructure:* a `generation_assets` reader + cross-store copy.
   *API:* promote/list-generation-artefacts route. *Persistence:* likely a provenance link
   (`media_assets.generation_asset_id` or structured `source_metadata`) — **not in schema today**.
   *UI:* n/a. *Testing:* promotion + no-direct-write enforcement.
4. **Architectural impact.** **Cross-context bridge** (Execution → Media); additive **only if** done
   via the explicit use case (ADR-0046 X8 forbids Execution writing `media_assets` directly);
   promoted assets must then enter the Timeline/render path to reach export (ADR-0043 RC5); likely a
   small additive migration; ADR **already exists** (X8) so probably no new ADR, just a contract.
5. **Size.** **Large** (a copy use case is Medium, but wiring promoted assets through Timeline→render
   to actually reach export/publish is the larger part).
6. **Product impact.** **Transformational** (turns two disconnected pipelines into one).
7. **Dependencies.** α8.5x execution runtime, α8.4 media/ingestion, α8.5 render/export, α8.6 publish.
8. **Risk.** Highest architectural subtlety: must not violate ADR-0046 X8 (no direct Execution→media
   writes) and must route through the frozen render/export boundary (ADR-0043) rather than
   short-circuiting it.

### 2.11 Verification V2

1. **User value.** Higher-quality generated shots (identity consistency, blur/brightness, watermark
   ML) → fewer bad outputs reach the final video. Benefits generation-runtime users.
2. **Existing groundwork.** Pure Decision-plane v1: `verify_image` + `verify_timeline`
   (`domain/generation/`), `IImageFeatureExtractor` + `PillowFeatureExtractor`,
   `VERIFIER_VERSION="verifier/1.0"`, `generation_shots.verification` JSONB, `generation.verification_failed`
   event, tests. `ObservedImage` already carries **unused** richer fields (`blur_score`,
   `average_brightness`, `perceptual_hash`, `watermark_probability`, …). ADR-0044 AR4 names visual
   verification (`IVerifier`, CLIP/SigLIP) as the future target.
3. **Missing pieces.** *Domain:* new pure checks. *Application:* pluggable extractor/verifier
   selection + thresholds. *Infrastructure:* ML-backed extractors (CLIP/face). *API:* none
   (`GenerateVideo` has no HTTP route). *Persistence:* JSONB suffices. *UI:* n/a. *Testing:* ML-path
   + richer-field checks.
4. **Architectural impact.** Extends the Decision plane; **additive under ADR-0045** *iff* policy
   stays pure and bytes stay in the extractor (Execution/infra); no migration; new ADR **only** if a
   plane boundary (F2 "Execution never scores") is crossed. Bump `VERIFIER_VERSION`.
5. **Size.** **Medium** (policy + extractors) → **Large** (real ML models).
6. **Product impact.** **Medium–High** (quality, not a new user-facing capability).
7. **Dependencies.** α8.x generation runtime.
8. **Risk.** Introducing ML inference cost/latency; keeping determinism/purity of the Decision plane.

### 2.12 Repair V2

1. **User value.** Smarter recovery from bad shots (beyond re-seed retry) → higher yield. Benefits
   generation-runtime users.
2. **Existing groundwork.** Pure v1: `decide_repair` (ACCEPT/RETRY/GIVE_UP, seed-bump,
   `DEFAULT_MAX_ATTEMPTS=3`), `REPAIR_VERSION="repair/1.0"`, `generation_shots.repair_count`/`attempts`
   JSONB, `generation.repair_succeeded` event, `generation_assets.parent_asset_id` lineage (schema
   present, unused by repair). ADR-0044 AR5 names bounded repair strategies as the future target.
3. **Missing pieces.** *Domain:* strategy selection (failure-type → action; inpaint/upscale/adapter
   switch). *Application:* repair orchestrator beyond seed bump; capability-resolver integration for
   repair-specific capabilities; register child `generation_assets` per attempt. *Infrastructure:*
   repair adapters. *API:* none. *Persistence:* JSONB suffices. *UI:* n/a. *Testing:* multi-strategy
   + lineage.
4. **Architectural impact.** Extends the Decision plane + Execution adapters; **additive under
   ADR-0045/0046** *iff* strategies stay capability-driven and new artefacts register via
   `generation_assets`; no migration; new ADR only if a plane boundary is crossed. Bump `REPAIR_VERSION`.
5. **Size.** **Medium–Large.**
6. **Product impact.** **Medium.**
7. **Dependencies.** α8.x generation runtime; compounds with §2.11.
8. **Risk.** Cost of new repair strategies; keeping repair pure/deterministic while orchestrating
   side-effects in Execution.

### 2.13 Template system

1. **User value.** Reusable project/generation templates (a gallery of starting points) → faster,
   guided creation. Benefits new + repeat creators.
2. **Existing groundwork.** **Schema exists, feature not built:** `templates` table (`models/templates.py`
   — `name/description/category/tags/body(jsonb)/is_public/is_system/version`, partial-unique name,
   `ix_templates_category_is_public`). No domain/app/API/repository. (Unrelated: in-memory
   `StoryArcTemplate` in `shot_intent.py` is a generation-arc concept, not this table.)
3. **Missing pieces.** *Domain:* `Template` entity + invariants. *Application:* CRUD + "create project
   from template" + gallery. *Infrastructure:* `TemplateRepository`. *API:* `/api/v1/templates/*`.
   *Persistence:* **none** for core (table exists; **doc drift:** `schema.md` §28 shows a stale shape).
   *UI:* n/a. *Testing:* all.
4. **Architectural impact.** New Templates context (or extend Projects); additive; **no migration**
   for core; ADR for the `body` JSON contract + gallery/instantiation rules.
5. **Size.** **Medium.**
6. **Product impact.** **Medium–High** (accelerates the creator funnel).
7. **Dependencies.** Projects aggregate.
8. **Risk.** Defining the `body` schema + project-instantiation contract; reconciling doc drift.

### 2.14 Creator Workflow / Creator UX (backend enablers)

1. **User value.** A guided "prompt → finished, published video" flow with a single high-level
   command + progress. This is the product's core promise.
2. **Existing groundwork.** The pieces exist but are **not fused into one guided command**: Projects
   → Scenes → Prompts → Media → Timeline → RenderJob → ExportJob → PublishJob all have APIs; workflow
   orchestration exists (`WorkflowRun` + `/workflow-runs/*`, registered `generate-image`/`generate-video`
   workflows); `GenerateVideo` exists **but has no HTTP route** (container-only, test-only). See the
   two-pipeline fact (§1 / §2.10).
3. **Missing pieces.** *Application:* a high-level "create video" command + progress polling that ties
   generation → media → timeline → render → export → publish. *API:* `/generations/*` or
   `/projects/{id}/generate` (does not exist). *Domain/Persistence:* possibly draft/wizard state.
   *UI:* n/a. *Testing:* E2E creator-flow API.
4. **Architectural impact.** Extends Workflow + Generation; additive API surface; may be largely a
   composition/orchestration slice **and depends on §2.10** to make Path B reach export/publish.
5. **Size.** **Large** (it is essentially "wire the whole funnel behind one API", incl. §2.10).
6. **Product impact.** **Transformational.**
7. **Dependencies.** Nearly everything, especially §2.10 (asset promotion) and a UI (out of repo).
8. **Risk.** Largest scope; overlaps several contexts; blocked in practice by the Path A/Path B split.

### 2.15 Team / collaboration

1. **User value.** Multiple users per tenant, roles, sharing, invitations. Benefits business/agency
   users (the `business` plan seeds `team_collab: true`, `seats: 5`).
2. **Existing groundwork.** **Partial schema:** `tenants`, `users` (`tenant_id` NOT NULL), `roles`,
   `roles_users` (seeded roles). `RegisterUser` = one new tenant per signup + assigns `owner`.
   **No membership/invitation tables, no role checks** anywhere; scoping is `tenant_id AND
   owner_user_id` (owner-only visibility). `PROJECT_AGGREGATE.md` §2 already anticipates relaxing
   `owner_user_id` under RBAC.
3. **Missing pieces.** *Domain:* `Role` entity, membership/invitation aggregates, permission model.
   *Application:* invite/accept/remove, list members, role checks, shared-project access. *Infra:*
   membership/invitation repos + role queries. *API:* `/teams`, `/members`, `/invitations`.
   *Persistence:* **new tables** (`tenant_invitations`, possibly `tenant_members`). *UI:* n/a.
   *Testing:* RBAC + multi-user scoping.
4. **Architectural impact.** Extends Identity; **new migration**; ADR for shared-tenant access;
   changes the owner-scoping behaviour across many read paths (broad blast radius).
5. **Size.** **Large.**
6. **Product impact.** **High** (for the business segment; lower for solo creators).
7. **Dependencies.** Identity/auth foundation.
8. **Risk.** Touches ownership/scoping in nearly every list/read use case; security-sensitive.

### 2.16 Brand kits

1. **User value.** Per-brand fonts/colours/logos/style presets applied to generations/renders.
   Benefits businesses/agencies.
2. **Existing groundwork.** **Nothing exists** — no `brand_kits` table, ORM, or code (verified: zero
   matches). Adjacent only: `tenant_settings` (generic KV JSONB), `projects.style`/`projects.settings`.
3. **Missing pieces.** Everything: domain, application, infrastructure, API, persistence (new tables,
   likely FKs to `media_assets` for logos), tests.
4. **Architectural impact.** New Brand context (or extend Configuration/Projects); **new migration**;
   new ADR (schema + scope + default-kit-on-create); render/generation integration to actually apply
   the kit.
5. **Size.** **Large** (greenfield + must plug into generation/render to have effect).
6. **Product impact.** **Medium** (High for the business segment).
7. **Dependencies.** Media (logos), and generation/render to apply styling.
8. **Risk.** Only valuable once it influences output — requires touching the generation/render path.

### 2.17 Mobile API support

1. **User value.** Native mobile clients (create/manage/publish on the go). Benefits mobile-first
   creators.
2. **Existing groundwork.** The generic `/api/v1` REST surface is already mobile-consumable: JWT
   access+refresh (`auth/*`), envelope + `request_id`, keyset pagination, OCC `version`/`412`,
   body-level idempotency keys, full CRUD across projects/media/publish/notifications/social-accounts.
3. **Missing pieces.** *Persistence/Infra:* device/push registration (`device_tokens`) — none.
   *API:* mobile-friendly OAuth (PKCE/custom-scheme; today social connect is browser-redirect),
   mobile aggregate endpoints (overlaps §2.8), realtime/websocket (none). *UI:* the mobile app itself
   (out of repo). *Testing:* device/push.
4. **Architectural impact.** Cross-cutting additive API; **new tables** for push/devices; ADR for
   mobile auth + push. Strongly overlaps §2.7 (push) and §2.8 (aggregates).
5. **Size.** **Large** (as a program; individual pieces are smaller).
6. **Product impact.** **Medium–High** (depends on go-to-market).
7. **Dependencies.** Auth; §2.7 push; §2.8 dashboard.
8. **Risk.** Mostly external (native OAuth, store review); not a single vertical slice but a bundle.

### 2.18 Billing / credits + usage read API (grounded addition)

1. **User value.** Plans, credit balances, subscription lifecycle, and usage visibility — required to
   **monetise** (ROADMAP M1 "billing live"). Benefits the business.
2. **Existing groundwork.** **Full schema exists** (`plans/subscriptions/invoices/credit_ledger` with
   an append-only balance trigger; `usage_records` partitioned; `ai_models`/`ai_model_pricing`
   seeded). Usage is **partially wired**: `UsageRecorderService`/`accounting.py`/`UsageRecordRepository`
   write rows on terminal provider calls — but `credits_consumed` is hard-wired to `0` and there is
   **no ledger debit and no read API**.
3. **Missing pieces.** *Domain:* `Plan`/`Subscription`/`CreditBalance`/entry types. *Application:*
   subscribe/cancel/upgrade, credit grant/debit, pre-flight credit checks, invoice webhooks, usage
   summaries; wire `credits_consumed` + `credit_ledger`. *Infrastructure:* billing repos + a payment
   provider adapter (Stripe). *API:* `/billing/*`, `/usage/*` (contract-documented, absent in code).
   *Persistence:* **none** for core tables; possibly Stripe-specific columns. *UI:* n/a. *Testing:*
   ledger immutability, balance invariants, subscription lifecycle.
4. **Architectural impact.** New Billing context atop existing tables + extend Usage; migration mostly
   unneeded for core; **new ADR** (credit-debit timing, Stripe integration); external payment
   dependency.
5. **Size.** **Very Large.**
6. **Product impact.** **High** (Transformational for commercialisation; not creator-feature value).
7. **Dependencies.** Usage recorder (partial), identity/plans seed.
8. **Risk.** External payment integration; financial correctness (immutable ledger, idempotent
   webhooks, credit races); highest correctness stakes.

**Minor grounded candidates (noted, not ranked):** Analytics (`analytics_events` partitioned table
exists, zero code; terminal job events available to subscribe → new Analytics context); Feature-flag /
configuration service (tables exist, not on `IUnitOfWork`, no code); Export-jobs list endpoint (a
small gap — only `GetExportJob` exists — that a dashboard needs).

---

## 3. Ranked recommendation — top 5

Ranking weighs **(existing groundwork) × (product value) × (additive safety / low external risk)**,
biased toward slices that convert *already-built substrate* into *user-visible value* without touching
frozen contracts. All are grounded in §2.

### #1 — Asset promotion bridge (`PublishGenerationAssets`) — §2.10
- **Why now.** It is the **missing connective tissue of the pipeline just completed**: the AI
  generation runtime (Path B, `generation_assets`) does not reach export/publish (Path A,
  `media_assets`) in code. The seam is already named and reserved (ADR-0046 X8 / W8.6.8). Until it
  exists, "Prompt → … → Export → Publish" is two pipelines, not one.
- **Why not later.** Every other creator-facing publishing feature (scheduling, captions, multi-dest)
  ultimately wants to publish *AI-generated* output; without this bridge they only apply to Path A.
- **Expected user impact.** Transformational — unifies the platform.
- **Expected architectural complexity.** Large; highest subtlety (must honour ADR-0046 X8 + route
  through the ADR-0043 render/export boundary). Likely a small additive provenance migration.

### #2 — Publish notifications (α8.6d) — §2.1
- **Why now.** The publish runtime already emits `PublishJobSucceeded`/`PublishJobFailed` with **no
  consumer** (DQ7 deferral). The in-app projection is already proven for exports; adding publish is
  the smallest possible slice that closes an open loop.
- **Why not later.** Publishing without outcome feedback is a visibly incomplete MVP.
- **Expected user impact.** Medium (completes the publish feedback loop).
- **Expected architectural complexity.** Very Small; additive; no migration; no frozen contract.

### #3 — Scheduling — §2.2
- **Why now.** A headline social feature whose substrate is **already ~80% present** (`scheduled_at`
  column + claim scan + retry rescheduling + `publish_at`→YouTube mapping). The remaining work is a
  create-path field + an external run-once cadence.
- **Why not later.** High-demand, low-cost; disproportionate value per unit of work.
- **Expected user impact.** High.
- **Expected architectural complexity.** Small; no migration; the only real dependency is an external
  scheduler process (a deployment concern the contract already flags).

### #4 — AI caption & hashtag generation — §2.3
- **Why now.** Directly serves the product thesis ("create social videos without marketing skill").
  The LLM capability + mock already exist; metadata lives in JSONB (no migration).
- **Why not later.** It is the highest-leverage *content* feature for reach; compounds with every
  destination and with scheduling.
- **Expected user impact.** High.
- **Expected architectural complexity.** Medium; additive; needs its own contract and careful PUB-4
  separation (do not couple `domain.publishing` to the AI provider plane).

### #5 — Media library — §2.9
- **Why now.** The **schema is fully pre-built** (including pgvector `embedding`/HNSW + tag GIN); as
  generated/uploaded assets accumulate there is currently **no way to browse, search, or reuse** them
  (`ListMedia` is unpaginated, filter-only, no search).
- **Why not later.** Asset sprawl grows with usage; reuse/organisation is a repeat-creator retention
  feature and unlocks templates/brand kits later.
- **Expected user impact.** High.
- **Expected architectural complexity.** Medium for CRUD/folders/tags (no migration); Large if
  semantic/vector search + an embedding provider are included (embedding columns beyond the two
  approved need an ADR).

**Strong runners-up (deliberately just outside the top 5):**
- **Additional destinations (TikTok/Instagram)** — §2.6 — likely the single highest *product* value
  for a social-video tool, but the dominant risk is **external** (platform app-review/API approval),
  which is why it sits just outside a "start now" list.
- **Creator dashboard** — §2.8 — cheap, additive, high perceived value; needs the small export-list gap.
- **Notification delivery (email)** — §2.7 — natural companion to #2, but adds an external-service
  dependency (SMTP/provider) the in-app slice does not.

---

## 4. What this report is not

No design, no schema, no API shapes, no ADRs, no estimates beyond the coarse size buckets requested,
and no selection. Sizes/impact are assessments grounded in the facts in §1–§2; the actual next slice,
its grounding, and its pre-flight remain to be chosen and opened intentionally per the standard
workflow (select → grounding → pre-flight → ADR-if-needed → implementation → full gate → `-dev`
review → finalise).

**Noted documentation drift (facts, not fixed here):** `schema.md` §28 / ERD templates cluster show a
stale `templates` shape (`kind/content/preview_media_asset_id`) vs the deployed ORM
(`category/body/tags/is_public/is_system`); `schema.md` §24 lists `analytics_events` columns not in the
baseline ORM. These are pre-existing and out of scope for this read-only exercise.
