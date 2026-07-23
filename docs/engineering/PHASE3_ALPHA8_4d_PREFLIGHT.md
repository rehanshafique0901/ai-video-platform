# Phase 3 — α8.4d Pre-flight: Derived-Preview Enrichment (preview clip + GIF + waveform)

> Status: **SIGNED OFF (2026-07-24).** Forks: **A1** (scope = derived-preview
> enrichment only; audio-mix / transitions / quality-tuning → new **α8.4e** render
> slice), **B1** (reuse the α8.4c `MediaEnrichmentWorker` — one enrichment ownership
> domain), **C1** (discrete neutral ports `IThumbnailer` / `IPreviewClipper` /
> `IGifPreviewer` / `IWaveformRenderer` — no "God" `IMediaEnricher`), **D1** (version
> the enrichment marker; claim assets whose version < current → deterministic
> backfill), **E** (recursion guard — derived assets never enriched), **F**
> (deterministic per-artifact keys). Invariants **W8.4c.1–3** carry over unchanged;
> new **W8.4d.1** (strengthened wording) adopted. **Additional signed-off structure:**
> the worker stays single, but internally becomes a **pipeline of independent
> enrichers** (implementation detail — no new platform abstraction, no new worker, no
> ADR). Companion to `PHASE3_ALPHA8_4c_PREFLIGHT.md`.

---

## 0. Mandatory gating question (answered first)

> **Does α8.4d require touching any ADR-0042 frozen module, checkpoint contract,
> orchestration state, provider protocol, or workflow lifecycle?**

**Answer: No.** α8.4d consumes an existing `MediaAsset`'s bytes and produces more
`MediaAsset`s + `source_metadata` — the exact α8.4c shape. Nothing upstream changes.
The freeze guard must stay green with **zero override markers** throughout the branch
(acceptance #1). If grounding ever turns this into a **Yes**, stop and revisit before
writing code.

---

## 1. The gating question is the scope filter (Fork A → A1)

Litmus test for every candidate: *"Does this change **what the render is**, or merely
**derive additional artifacts** from the finished render?"* If it changes the render,
it belongs in **α8.4e**.

| Deferred item | Pure transform of an existing MediaAsset? | Home |
|---|---|---|
| Preview clip (short trimmed/scaled MP4) | ✅ Yes — `video → shorter video` | **α8.4d** |
| GIF preview | ✅ Yes — `video → animated image` | **α8.4d** |
| Waveform image | ✅ Yes — `video/audio → image` | **α8.4d** |
| **Audio mixing** | ❌ No — mixes *multiple* tracks during **composition** | **α8.4e** |
| **Transitions / effects** | ❌ No — changes how clips are **composed** | **α8.4e** |
| **FFmpeg quality tuning / richer render filters** | ❌ No — changes the **render step's** output | **α8.4e** |

α8.4d = **derived-preview enrichment only**. The "No" items modify `IRenderer` /
render inputs and move to a dedicated **α8.4e** render-composition slice.

---

## 2. Thesis

α8.4c gave every generated video one derived thumbnail + a bitrate scalar. α8.4d adds
the remaining **derived-preview** artifacts, each a pure function of the parent
`MediaAsset` (W8.4c.3), produced by the **same** `MediaEnrichmentWorker` in one pass:

```
Generated video MediaAsset  (parent — sole source of truth)
        ↓  MediaEnrichmentWorker.run_once()   (α8.4c poll ingress, unchanged)
        ↓  EnrichGeneratedMedia (per asset, leased) — now a PIPELINE:
        │      for enricher in [thumbnail, preview, gif, waveform]:
        │          if enricher.applies: register derived MediaAsset (idempotent)
  + thumbnail        → derived MediaAsset(kind="image")   [α8.4c]
  + preview clip     → derived MediaAsset(kind="video")   [α8.4d]
  + GIF preview      → derived MediaAsset(kind="image")   [α8.4d]
  + waveform image   → derived MediaAsset(kind="image")   [α8.4d, if audio present]
  + parent.source_metadata.enrichment { …ids…, scalars, version, enriched_at }
```

---

## 3. Signed-off structure: an internal enricher pipeline (B1 + C1)

One worker, one lease, one materialization of the source bytes — but the use case
iterates a list of **independent enrichers**. Each enricher owns: applicability, its
neutral FFmpeg port, its deterministic storage key, and the derived-asset + metadata
it contributes. The use case orchestrates (materialize once → run each → register →
merge marker → settle); it does **not** hard-code artifact types.

```
Enricher (internal ABC — NOT a platform port)
  .origin           # "thumbnail" | "preview" | "gif" | "waveform"
  .produce(parent, source_path) -> DerivedArtifact | None   # None = not applicable

ThumbnailEnricher(IThumbnailer)     PreviewEnricher(IPreviewClipper)
GifEnricher(IGifPreviewer)          WaveformEnricher(IWaveformRenderer)
```

This is an **implementation detail** of α8.4d — no new platform abstraction, no new
worker, no ADR. It gives α8.4e/α8.5 room to add derived artifacts by appending an
enricher rather than growing a monolithic use case.

---

## 4. Grounding (what already exists / what must change)

- **`media_kind` enum** = `image, video, narration, subtitle, music, sound_effect,
  thumbnail`. Preview → `video`; GIF + waveform → `image`. All valid → **zero
  migration**. Derived kinds carry `source_metadata.origin` (`preview` / `gif` /
  `waveform`) + `parent_media_asset_id`, as thumbnails already do.
- **`EnrichGeneratedMedia`** already leases, materializes once, registers a derived
  asset under a deterministic key with `ConflictError` recovery, and augments the
  parent marker. α8.4d generalizes it into the pipeline above.
- **Recursion guard (Fork E — mandatory).** The α8.4c scan (`kind='video' AND
  source='generated' AND NOT (source_metadata ? 'enrichment')`) would re-claim a
  derived **preview video**. α8.4d adds `AND NOT (source_metadata ?
  'parent_media_asset_id')` so derived assets are **never** enrichment inputs.
- **Versioned claim (Fork D — D1).** Replace the presence check with a version check:
  claim where `COALESCE((source_metadata #>> '{enrichment,version}')::int, 0) <
  CURRENT_ENRICHMENT_VERSION`. α8.4c markers have **no** `version` → treated as `0` →
  re-claimed and **backfilled** with the full derived set. Method renamed
  `list_enrichable_generated_videos(*, target_version, limit)` (additive, non-frozen).

---

## 5. Invariants

- **W8.4c.1 / W8.4c.2 / W8.4c.3** — carry over unchanged (observational, parent-only,
  pure-function enrichment; more artifacts, same rules).
- **W8.4d.1 (new, strengthened) — Derived media is terminal.** A derived `MediaAsset`
  **SHALL NOT** participate as the source of further enrichment processing. Enrichment
  operates **exclusively** on primary generated or rendered `MediaAsset`s. Derived
  artifacts are observational outputs only. (Guarantees the derivation graph is a
  shallow tree, never a cycle — enforced by Fork E.)

---

## 6. Migration verdict

**Zero migration.** All derived artifacts are ordinary `media_assets` rows on the
existing `media_kind` enum; provenance + the versioned marker live in JSONB
`source_metadata`. Repository changes are additive / non-frozen (recursion guard +
versioned claim).

---

## 7. Behaviour: per-artifact isolation + settle rules

- Materialize the source **once**; run each applicable enricher over it.
- Register each produced artifact under its deterministic key (`ConflictError` →
  recover). Merge its id (`<origin>_media_asset_id`) + any scalars into the marker.
- `produce() → None` = **not applicable** (e.g. waveform with no audio stream) — not
  a failure; does not block the version bump (correctly terminal).
- **All applicable enrichers succeeded** → set `enrichment.version =
  CURRENT_ENRICHMENT_VERSION` → asset drops out of the scan.
- **Any enricher failed** (transient FFmpeg/storage error) → write the successful ids
  but **omit the version bump** → asset stays re-claimable; a later pass recovers the
  already-registered artifacts (idempotent) and retries the failed ones. FFmpeg + I/O
  run **outside** any DB transaction.

---

## 8. Test plan

- **Unit** — full derived set from one materialization; idempotent re-run (no
  duplicates, version bump); **backfill** (α8.4c-era marker, version 0 → re-claimed →
  gains previews); **recursion guard** (a derived video is never claimed); **per-
  artifact failure isolation** (one enricher raising doesn't block the others, leaves
  the asset re-claimable / version un-bumped); waveform-not-applicable is a clean
  no-op. Fakes for each new port.
- **Opt-in integration** — real FFmpeg preview / gif / waveform, skipped without the
  binary (α8.4b/α8.4c pattern).
- **Full gate** — ruff, black, mypy, import-linter, unit suite; **freeze guard green,
  zero overrides**.

---

## 9. Versioning

Runtime capability change → `0.4.28-phase3-alpha8.4d` and tag
`v0.4.28-phase3-alpha8.4d`. Standard two-commit release ritual.
