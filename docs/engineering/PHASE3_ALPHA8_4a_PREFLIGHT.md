# α8.4a — Generated Media Ingestion (pre-flight)

> Status: **SIGNED OFF (2026-07-22).** All forks approved: α8.4a/α8.4b split ✅, event-driven
> subscriber via `WorkflowRunSucceeded` (A1) ✅, no `_paused`/checkpoint change (Fork 0) ✅, neutral
> `IMediaDownloader` (B1) ✅, `IObjectStorage` + local FS first (C1) ✅, deterministic-key idempotency
> (D1) ✅, minimal metadata (E1) ✅. Invariants **W8.4.1** + **W8.4.2** adopted.
> First integration slice that turns a *completed* run's provider output into a durable
> asset. Built entirely on the ADR-0042 frozen platform (α8.4 is "outputs", not orchestration).

## 1. Thesis

When a workflow run reaches `succeeded`, its final checkpoint already holds the provider's
opaque output envelope — including an `image_ref` / `video_ref` **URL** (α8.1/α8.2). Today that
URL is never fetched: it lives only in the checkpoint and expires. **α8.4a** adds a strictly
**downstream** consumer that, after a run succeeds, downloads the referenced bytes, writes them
through a new object-storage port, and registers a `MediaAsset` with `source='generated'` — the
first thing the platform *produces*.

It is deliberately **not** the whole of α8.4. FFmpeg, thumbnails, timeline rendering, the render
worker, and `render_jobs.output_media_asset_id` population are **α8.4b** (see §7). α8.4a proves
the *ingestion seam* — download + storage + registration — with zero FFmpeg and zero migration.

## 2. Grounding (what already exists — verified)

- **`MediaAsset` aggregate + `media_assets` table already support generated media.** Columns
  `storage_backend`/`storage_bucket`/`storage_key` (UNIQUE together), `mime_type`, `size_bytes`,
  `checksum_sha256`, `width`/`height`/`duration_seconds`, `source` enum incl. **`generated`**,
  `kind` incl. **`thumbnail`**, `source_metadata` JSONB, plus `project_id`/`scene_id`/`prompt_id`/
  `model_id`/`provider` links. `source_backend` enum already has `local|s3|r2|azure_blob|gcs`.
  (`app/domain/media/media_asset.py`, `app/infrastructure/db/models/media.py`, baseline `0001`.)
  **There is no separate `GeneratedMedia` type — we reuse `MediaAsset`.**
- **`IMediaRepository.add(...)` exists** and enforces the storage-key uniqueness (→ `ConflictError`).
  The α6.2 `RegisterMedia` use case is **register-by-metadata only** and the wire DTO restricts
  `source` to `uploaded|stock` (`generated` is blocked at the API — minted server-side in α8).
  α8.4a adds a **new server-side use case**, it does **not** widen `POST /media`.
- **No storage abstraction exists.** No `IObjectStorage`/blob/presign port or adapter anywhere.
  α8.4a introduces it. (OpenAI's `gpt-image-1` base64 path and provider comments already point at
  "α8.4 storage".)
- **No FFmpeg / media processing exists** (only the string `"ffmpeg"` default in the render DTO).
  → entirely α8.4b.
- **Outbox → relay → publisher already exist.** `WorkflowRunSucceeded` (`EVENT_WORKFLOW_RUN_SUCCEEDED`)
  is emitted for **both** the synchronous runner path and the async resume path. `RelayService`
  drains `event_outbox` (`FOR UPDATE SKIP LOCKED`, at-least-once, dedupe on `event.id`) and fans out
  through the in-process `PublisherPort` — which currently has **no real subscriber**. This is the
  natural, already-built trigger seam.
- **`render_jobs` linkage is dormant.** `workflow_run_id` and `output_media_asset_id` columns exist
  but are never written by app code, and there is no render worker. → α8.4b.
- **Frozen surface (ADR-0042):** the runner, completion engine, resume, dispatcher, provider ports/
  registry/DTOs, usage recorder, relay *service*, lock manager, and workflow registry/aggregate are
  frozen. Media domain, media use cases, `IMediaRepository`, routers, the container, and any **new**
  port/adapter are **not** frozen.

## 3. Proposed shape (α8.4a)

```
WorkflowRunSucceeded (outbox)
   │  RelayService.relay_once()  →  PublisherPort fan-out   (both already exist, unchanged)
   ▼
[NEW] GeneratedMediaSubscriber            ← the first real in-process event handler
   ▼
[NEW] IngestGeneratedMedia (use case)     ← read succeeded run's checkpoint output(s)
   │      for each image_ref/video_ref:
   │        1. download bytes            → [NEW] IMediaDownloader (httpx GET)
   │        2. compute sha256 + size + mime
   │        3. put(bytes, key)           → [NEW] IObjectStorage (local fs adapter first)
   │        4. IMediaRepository.add(source='generated', storage coords, links)  (exists)
   ▼
MediaAsset(source='generated')  ← the platform's first produced artifact
```

New, non-frozen pieces:
- `IObjectStorage` port + `LocalObjectStorage` adapter (deterministic key, config-blind root).
- `IMediaDownloader` port + `HttpMediaDownloader` adapter (injected httpx client; streams to bytes/temp, caps size).
- `IngestGeneratedMedia` application use case (reads the checkpoint, orchestrates download→store→register; idempotent).
- `GeneratedMediaSubscriber` registered on the in-process publisher for `WorkflowRunSucceeded`.
- Container wiring + config keys (storage root/bucket, download timeout + max bytes).

## 4. Design forks (for sign-off)

### Fork 0 — **[MANDATORY, per ADR-0042 AR-2] Does α8.4a change the `_paused`/checkpoint contract?**
**Proposed answer: NO.** Ingestion is a **read-only consumer** of the *already-written* `succeeded`
checkpoint `output` (the same `image_ref`/`video_ref` α8.1/α8.2 persist). It adds no field to the
`_paused` handoff, changes no checkpoint producer, and touches no frozen module. Therefore α8.4a
proceeds additively with **no ADR and no `schema_version`** work. (If a *future* slice wanted an
in-workflow "register media" step, that would change the checkpoint/registry contract → dedicated
ADR first. Not α8.4a.)

### Fork A — trigger: where does ingestion run relative to the frozen pipeline?
- **A1 (recommended): in-process subscriber on `WorkflowRunSucceeded`** via the existing
  `PublisherPort`/`RelayService`. Reuses at-least-once delivery + `event.id` dedupe; no new scan, no
  new run state. Introduces the platform's **first real event consumer** — architecturally the
  intended use of the outbox/relay built in α7.3.
- **A2: explicit `IngestGeneratedMedia` poller** (a `poll_once`-style scan of succeeded-but-not-yet-
  ingested runs). Avoids the first subscriber, but needs a new "ingested?" marker + scan query.
- **A3 (rejected): inline in completion/runner** — touches frozen modules and makes orchestration
  parse provider payloads (violates **G4** provider-agnostic orchestration + the freeze).

### Fork B — who downloads the provider bytes? (G9: "providers own external communication")
- **B1 (recommended for α8.4a): a neutral `IMediaDownloader`** (httpx GET the ref URL). α8.1's
  OpenAI image URLs and α8.2's Fal result URLs are directly fetchable; keep the downloader provider-
  agnostic and inject the client (config-blind, W8.1.1).
- **B2: extend the provider lifecycle with `download()/fetch_result()`** — but `ports.py` /
  `provider_dispatcher.py` are **frozen**, so this requires a **dedicated ADR**. Only pursue if a
  provider's result URL needs provider-specific auth that a neutral GET can't satisfy. **Deferred.**

### Fork C — storage port + first backend
- **C1 (recommended): `IObjectStorage` + `LocalObjectStorage` (filesystem) first.** Config-blind
  (injected root dir + bucket name), matching the enum's `local` backend. `put(key, bytes, mime)` →
  storage coords; `get`/`exists`/`delete` for completeness. **S3/R2/Supabase deferred** to when a
  deploy target needs them (adapter swap, no use-case change).

### Fork D — idempotency & deterministic key
- **D1 (recommended): deterministic storage key** from `tenant/project/run_id/step_index/request_id`
  + extension. Re-ingesting the same output hits the `(backend, bucket, key)` unique constraint →
  `IMediaRepository.add` raises `ConflictError` → the use case returns the **existing** asset
  (idempotent replay, mirroring α7.5's usage pattern). At-least-once relay redelivery is thus safe.

### Fork E — metadata/probing depth in α8.4a
- **E1 (recommended): minimal.** Compute `size_bytes` + `checksum_sha256` from the downloaded bytes;
  derive `mime_type` from `Content-Type`/extension. Leave `width`/`height`/`duration_seconds`
  **NULL** — real probing (FFprobe) and thumbnails are **α8.4b**. No FFmpeg dependency in α8.4a.

## 5. ADR-0042 freeze compliance

No `FROZEN_PATHS` entry is touched (whole diff = new ports + adapters + one use case + one
subscriber + container wiring + tests + media-repo reuse). **G4 preserved:** orchestration never
downloads/stores/registers media; ingestion is strictly downstream and reads only the opaque
`output`. Proposed new invariant **W8.4.1 — "Generated-media ingestion is strictly downstream of the
frozen completion pipeline; the runner/completion/dispatcher never download, store, or register
media."** And **W8.4.2 — "Generated-media ingestion is *observational*: it may create downstream
artifacts (storage objects, `MediaAsset` rows, logs, metrics) but must never mutate `WorkflowRun`,
`WorkflowCheckpoint`, workflow steps, `UsageRecord`, or any orchestration decision."** Acceptance
criterion (as in α8.3b): `check_frozen_platform.py --base main` stays green with **zero override
markers** throughout the branch.

## 6. Migration verdict

**Zero migration.** `media_assets` already has every column ingestion needs (`source='generated'`,
storage coords, checksum, mime, size, nullable dimensions). No new table, column, enum value, or
index.

## 7. Scope / non-goals

**In (α8.4a):** `IObjectStorage` + local FS adapter; `IMediaDownloader` + httpx adapter;
`IngestGeneratedMedia` use case; `GeneratedMediaSubscriber` on `WorkflowRunSucceeded`; container +
config wiring; register `MediaAsset(source='generated')`.

**Out (→ α8.4b):** FFmpeg / FFprobe, thumbnails, dimension/duration probing, timeline rendering, the
**render worker**, `render_jobs.output_media_asset_id` / `workflow_run_id` population, and non-local
storage backends (S3/R2/Supabase). **Out (later phases):** export/publishing (α8.5), CDN, cleanup/GC.

## 8. Test plan (unit)

- **Downloader:** httpx `MockTransport` — success (bytes + content-type), size-cap exceeded, HTTP
  error → typed failure; no network.
- **Storage:** `LocalObjectStorage` over `tmp_path` — put/get/exists/delete, deterministic key,
  overwrite semantics.
- **`IngestGeneratedMedia`:** reads a succeeded run's checkpoint `output` → downloads → stores →
  `MediaAsset(source='generated')` with correct links + checksum; **idempotent re-ingest** returns
  the existing asset (ConflictError path); a run with no media ref → **noop**; a `failed`/non-terminal
  run → noop; download failure → surfaced, no partial `MediaAsset`.
- **Subscriber wiring:** a `WorkflowRunSucceeded` event routed through the in-process publisher
  invokes `IngestGeneratedMedia` exactly once; dedupe on redelivery.
- **Freeze guard:** green, zero overrides.

## 9. Version

Runtime capability → version bump **`0.4.25-phase3-alpha8.4a`** (tag `v0.4.25-phase3-alpha8.4a`),
following the α8.x cadence (each deployable capability increments the patch line).

## 10. Questions for sign-off

1. **Fork 0:** confirm α8.4a is additive and does **not** touch the checkpoint contract (no ADR needed).
2. **Fork A:** A1 (event subscriber) vs A2 (poller)? (Recommend A1.)
3. **Fork B:** B1 neutral downloader (recommend) — accept deferring provider `download()` (B2) to an ADR if/when an auth'd result URL appears?
4. **Fork C:** `IObjectStorage` + local FS first (recommend); S3/R2/Supabase deferred?
5. **Fork D/E:** deterministic-key idempotency + minimal metadata (no FFmpeg) in α8.4a?
6. **Scope split:** approve carving α8.4 into **α8.4a (ingestion, this doc)** and **α8.4b (FFmpeg +
   render worker + thumbnails + render-job linkage)**?
7. **Invariant:** adopt **W8.4.1**; keep the freeze guard green with zero overrides as an acceptance criterion.
