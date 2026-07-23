# Phase 3 — α8.4c Pre-flight: Media Enrichment (thumbnail + metadata)

> Status: **SIGNED OFF (2026-07-24).** Forks: **A1** (scope split — thumbnail +
> bitrate/metadata only; previews/waveform/audio-mix/GIF/transitions/quality →
> α8.4d), **B2** (dedicated **poll worker**, not a relay subscriber), **C1**
> (derived `MediaAsset(kind=image)` + JSONB enrichment), **D1** (new `IThumbnailer`
> port, not an `IRenderer` extension), **E1** (deterministic key, `ConflictError` =
> success). Invariants **W8.4c.1** (strengthened), **W8.4c.2**, **W8.4c.3** (new)
> adopted. Gating criterion confirmed: zero ADR-0042 frozen changes, zero overrides.
> Companion to `PHASE3_ALPHA8_4b_PREFLIGHT.md` / `PHASE3_ALPHA8_4a_PREFLIGHT.md`.

---

## 0. Gating criterion (acceptance #1)

**α8.4c must touch zero ADR-0042 frozen modules and zero `_paused`/checkpoint
contracts.** The freeze guard (`backend/scripts/check_frozen_platform.py --base main`)
must stay green with **zero override markers** throughout the branch. If the design
appears to need a frozen change, **stop and treat it as a design issue** first.

**Verdict (grounded):** enrichment is a pure `MediaAsset → derived MediaAsset(s) +
augmented metadata` transform. It reads a produced `MediaAsset`, runs FFmpeg over
its bytes, and writes only new/derived media artifacts + the parent's own
`source_metadata`. It never reads or writes the runner, completion engine,
dispatcher, provider ports, usage recorder, relay, lock manager, workflow registry,
render-job rows, or any checkpoint. **Zero frozen paths involved.**

---

## 1. Thesis

α8.4b ends the pipeline at an output `MediaAsset(kind="video", source="generated")`
carrying `width/height/duration/codec` from `ffprobe`. α8.4c **enriches** any such
generated video — the smallest useful "make the output presentable" step:

```
Generated video MediaAsset  (un-enriched)
        ↓  discovered by scan
  MediaEnrichmentWorker.run_once()      ← poll worker, symmetric with
        ↓                                  CompletionEngine.poll_once() /
  EnrichRenderedMedia (per asset, leased)  RenderWorker.run_once()
        ↓  FFmpeg (IThumbnailer)
  + thumbnail  → derived MediaAsset(kind="image", source="generated")
  + bitrate/probed extras → augment the PARENT asset's source_metadata
        ↓
  Enriched generated video (+ linked thumbnail, marked enriched)
```

Everything heavier (previews, GIF previews, waveform, audio mixing,
transition/effect improvements, FFmpeg quality tuning) is **deferred to α8.4d** so
α8.4c stays comparable in size to α8.4a / α8.3b.

---

## 2. Grounding (what already exists)

- **`MediaAsset` has no thumbnail/preview/bitrate columns** (`db/models/media.py`):
  the only extensible field is `source_metadata` (JSONB). → thumbnails become their
  **own** `MediaAsset` rows; scalar enrichment lives in `source_metadata`. **Zero
  migration.**
- **`IMediaRepository.update_owned`** already permits partial updates of the mutable
  columns — including `source_metadata` — with **no version fence** (ADR-0037). →
  augmenting + marking the parent is an existing, additive capability.
- **`IMediaRepository.add`** enforces `(storage_backend, storage_bucket,
  storage_key)` uniqueness (`ConflictError`) → deterministic-key idempotency.
- **`IMediaRepository.get_by_storage_coords`** (α8.4b) → idempotent recovery of an
  already-registered derived thumbnail.
- **`MediaAsset` carries its own `tenant_id` / `owner_user_id`** → the enricher
  resolves ownership straight off the parent asset (no `get_ownership`, no
  render-job read — supports W8.4c.3).
- **`IObjectStorage`** (local FS; α8.4a) + the α8.4b FFmpeg binary config are reused
  as-is.
- **`IDistributedLockManager`** → the `media_enrichment:<media_asset_id>` lease, the
  same exactly-once primitive `CompletionEngine`/`RenderWorker` use.

**Trigger reconciliation (B2 + W8.4c.3).** A poll worker needs a *claimable set*.
Because W8.4c.3 forbids depending on render-job history, the worker does **not**
react to the `RenderJobSucceeded` payload; it **scans the media table** for
un-enriched generated videos (a bounded, shrinking set — assets drop out once
marked enriched). A `RenderJobSucceeded` (or any future generated-video producer)
merely *creates* an un-enriched asset the worker later discovers. This makes
enrichment a pure function of the `MediaAsset` and independent of what produced it.

---

## 3. Proposed shape (α8.4c scope only)

- **`IThumbnailer` neutral port** + `FfmpegThumbnailer` adapter — configuration-blind
  (reuses `render_ffmpeg_path` / `render_ffprobe_path` / `render_timeout_seconds`):
  extract one frame at `t` → image bytes + `(width, height, mime)`, and probe the
  source `bitrate`. Kept **separate from `IRenderer`** (D1). All engine failures →
  a neutral `ThumbnailError`.
- **`IMediaRepository.list_unenriched_generated_videos(*, limit)`** — additive,
  non-frozen scan: `kind='video' AND source='generated' AND deleted_at IS NULL AND
  NOT (source_metadata ? 'enrichment')`, oldest first, capped. The claimable set.
- **`EnrichGeneratedMedia` use case** — per parent asset: acquire the
  `media_enrichment:<id>` lease → load the parent (must be a live generated video,
  not already enriched) → materialize bytes from `IObjectStorage` → extract one
  thumbnail + probe bitrate (FFmpeg, **outside any DB txn**) → store the thumbnail
  under a **deterministic** key in `parent_media_asset_id` → register a
  `MediaAsset(kind="image", source="generated")` cross-linked to the parent
  (`source_metadata.origin="thumbnail"`, `parent_media_asset_id=…`) → `update_owned`
  the parent's `source_metadata` with an `enrichment` object
  (`{thumbnail_media_asset_id, bitrate, enriched_at}`) → release lease.
  `ConflictError` on the derived add → recover via `get_by_storage_coords` (E1).
- **`MediaEnrichmentWorker.run_once()`** — scans the claimable set (limit
  `enrichment_batch_size`) and hands each to `EnrichGeneratedMedia`; each asset
  settled independently under its own lease. Poll-driven (library-only, D11), same
  as the other workers.
- **Config** — `enrichment_thumbnail_at_seconds` (default `1.0`),
  `enrichment_batch_size` (default `10`); FFmpeg paths/timeout reused.
- **DI** — lazy `FfmpegThumbnailer`; `get_media_enrichment_worker()` factory (fresh
  UoW per call), symmetric with `get_render_worker()`.

**Scope note (deliberate).** The scan enriches *all* generated video assets, not
only render outputs — both α8.4b render outputs and α8.4a-ingested provider videos
are `generated` videos, and a thumbnail is uniformly useful. This falls out of
W8.4c.3 (parent is the sole source of truth) and adds no complexity.

---

## 4. Design forks (signed off)

- **Fork A → A1.** α8.4c = thumbnail + bitrate/metadata only. Previews, GIF
  previews, waveform, audio mix, transition improvements, FFmpeg quality tuning →
  **α8.4d**.
- **Fork B → B2 (changed from the recommendation).** A dedicated **poll worker**
  (`MediaEnrichmentWorker.run_once()`), **not** a relay subscriber. Preserves the
  established principle: *PublisherPort subscribers orchestrate work; they never
  perform media processing.* FFmpeg belongs to workers; the relay stays
  deterministic. Symmetric with `CompletionEngine.poll_once()` /
  `RenderWorker.run_once()`.
- **Fork C → C1.** Parent `MediaAsset` + derived `MediaAsset(kind="image")` + JSONB
  enrichment. No schema changes.
- **Fork D → D1.** New `IThumbnailer` port (do **not** extend `IRenderer` —
  Timeline→Video and Video→Image are different capabilities).
- **Fork E → E1.** Deterministic storage key; `ConflictError` == success.

---

## 5. Invariants

- **W8.4c.1 (strengthened) — media enrichment is observational and downstream.** It
  may derive additional media artifacts and augment the owning `MediaAsset`'s
  `source_metadata`, but it **never** mutates orchestration state, checkpoints,
  provider state, workflow/render lifecycle, Timeline definitions, or renderer
  inputs. (Prevents enrichment from becoming "smart rendering.")
- **W8.4c.2 — the enricher consumes only `MediaAsset` bytes + identifiers.** Never
  provider outputs, URLs, checkpoints, request IDs, provider job IDs, webhook
  payloads, or Timeline internals. (Mirror of W8.4b.2.)
- **W8.4c.3 (new) — derived media is reproducible from its parent `MediaAsset`
  alone.** Media enrichment must never depend on provider payloads, workflow
  checkpoints, Timeline state, or render-job history once the parent `MediaAsset`
  exists. `MediaAsset → Thumbnail` is a **pure function** of the parent — thumbnails
  can be regenerated years later (e.g. after an FFmpeg upgrade) without the workflow
  that produced the video.

---

## 6. Migration verdict

**Zero migration.** Thumbnails are ordinary `MediaAsset` rows; enrichment metadata
+ the `enrichment` marker are JSONB `source_metadata`. No new columns, no new
tables. New repository surface is additive and non-frozen; `update_owned` /
`get_by_storage_coords` already exist.

---

## 7. Test plan

- **Unit** — `EnrichGeneratedMedia` (happy path: thumbnail registered as a linked
  image asset + parent `enrichment` metadata written; idempotent re-run = no
  duplicate; parent-missing / non-video / non-generated / already-enriched no-ops;
  lease-held skip) against fakes with a **fake `IThumbnailer`**;
  `MediaEnrichmentWorker.run_once` (drain / batch cap / empty scan); the
  `list_unenriched_generated_videos` fake filter.
- **Opt-in integration** — a real `FfmpegThumbnailer` frame extract + bitrate probe,
  skipped when the binary is absent (α8.4b pattern).
- **Full gate** — ruff, black, mypy, import-linter, unit suite; **freeze guard
  green, zero overrides**.

---

## 8. Versioning

Runtime capability change → `0.4.27-phase3-alpha8.4c` and tag
`v0.4.27-phase3-alpha8.4c`. Standard two-commit release ritual.
