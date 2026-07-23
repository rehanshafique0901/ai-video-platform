# Phase 3 — α8.5a Pre-flight: Export Engine — render output → delivery encoding

> Status: **SIGNED OFF.** First slice of the **delivery** stage (downstream of render +
> enrichment). Companion to `PHASE3_ALPHA8_4e_PREFLIGHT.md`.
>
> **Rulings:** Gate 1 (ADR-0042) PASS · Gate 2 (ADR-0043) PASS (RC5 immutable master +
> RC6 determinism) · **A1** (export engine only) · **B1** (`CreateExportJob` +
> `ProcessExportJob` + `ExportWorker.run_once()`) · **C1** (discrete `IExporter` +
> `FfmpegExporter`) · **D** (source = render output only) · **E** (reuse partial-unique
> dedup + deterministic key) · **F tightened** — *same-orientation only* (format / codec /
> bitrate / resolution ladder); **cross-orientation exports deferred** ("export preserves
> presentation semantics; it only changes delivery characteristics") · **W8.5.1 + W8.5.2 +
> new W8.5.3** (master/delivery hierarchy).

---

## 0. Gates (answered first)

### Gate 1 — ADR-0042 (orchestration freeze)

> **Does α8.5a touch any frozen orchestration module, checkpoint contract, orchestration
> state, provider protocol, or workflow lifecycle?**

**Answer: No.** Export reads a **finished render-output `MediaAsset`** and produces a new
**delivery `MediaAsset`**. It touches only the `export_jobs` lifecycle, `media_assets`
(read source / create output), object storage, and export events. Freeze guard stays green,
**zero overrides**.

### Gate 2 — ADR-0043 (render composition boundary)

> **Does α8.5a change how media is composed?**

**No — export is *below* the render boundary.** It is a **delivery transcode** of an
already-composed output, not a composition change. The one relevant principle is **RC5
(rendered media is immutable downstream)**: export **reads** the render output and emits a
*new* asset; it never re-composes, re-times, or overwrites it. Export uses FFmpeg for
transcoding, but that is *delivery encoding*, not `Timeline → video` composition, so it
gets its own port (Fork C) rather than reaching into `IRenderer`. (RP1–RP9 are render-layer
execution invariants; α8.5a mirrors their spirit — no provider I/O, deterministic,
idempotent, bounded temp — but they formally govern the render layer, not export.)

---

## 1. Positioning (what export *is*)

Export is a **user-requested, delivery-time transcode** of a finished render output into a
requested `(format, quality, orientation)`. Like enrichment it is a **pure downstream
transform of an existing `MediaAsset`**; unlike enrichment it is **request-driven** (an
explicit `export_jobs` row with `requested_by_user_id` + params) and tracked by its **own
self-versioned aggregate**.

```
render_job.output_media_asset_id  (finished, immutable — RC5)
        ↓  IExporter (FFmpeg transcode: format / quality / orientation)
delivery MediaAsset(kind=video|image, source="export")
        ↓  export_job.succeeded (output_media_asset_id, file_size_bytes)
```

---

## 2. Grounding (what exists / what changes)

- **`export_jobs` table is fully modeled and unused at the app layer (zero migration).**
  `ExportJob` (`db/models/jobs.py`) — self-versioned (`VersionMixin`), FK
  `render_job_id` (CASCADE), `requested_by_user_id`, `format`/`quality`/`orientation`/
  `status` enums, `output_media_asset_id`, `download_count`, `last_downloaded_at`,
  `file_size_bytes`, `finished_at`. **No** `IExportRepository`, use case, worker, or router
  exists yet — greenfield application code.
- **Enums exist** — `export_status` (`queued/running/succeeded/failed/canceled`, mirrors
  `render_status`), `export_format` (`mp4/mov/gif/webm`), `export_quality`
  (`sd/hd_1080p/qhd_2k/uhd_4k`), `export_orientation` (`horizontal/vertical/square`).
- **Idempotency is built into the schema.** Partial-unique
  `uq_export_jobs_render_job_id_format_quality_orientation` where
  `status IN ('queued','running','succeeded')` (ADR-0030 W1.1, migration `0003`) — at most
  one active-or-fulfilled export per `(render_job, format, quality, orientation)`;
  `failed`/`canceled` excluded so retries are allowed.
- **The worker pattern is established.** `RenderWorker.run_once()` + `ProcessRenderJob`
  (claim → `render_job:<id>` lease → `queued→running` CAS → transform outside any txn →
  deterministic key + `ConflictError` recovery → settle + event) is the exact shape to
  mirror. `IObjectStorage`, `CreateRenderJob`, and the render events are reusable analogues.
- **The source is a finished `MediaAsset`.** `render_jobs.output_media_asset_id` (set by
  α8.4b/α8.4e) is the export input — read-only (RC5). Export never reads the Timeline.

---

## 3. Design forks (for sign-off)

- **Fork A — Scope split (recommended: α8.5a = export engine only).** Split the roadmap's
  "export & publishing":
  - **α8.5a** — export engine: request use case + poll worker + `IExporter` (transcode) →
    delivery `MediaAsset` + events.
  - **α8.5b** — publishing (social platforms) + notifications (a distinct concern with
    external providers + OAuth — its own slice).
  - **Storage providers (S3/R2/…)** are *already* abstracted by `IObjectStorage`; new
    backends are additive infra, **not** required for α8.5a (local storage suffices).
  - Download **serving** (`download_count` / `last_downloaded_at`) → defer to a thin
    follow-up (α8.5a produces the artifact; serving it is a separate read path). *Fork A′.*

- **Fork B — Trigger (recommended: poll worker).** `CreateExportJob` (user request) inserts
  a `queued` `export_jobs` row; **`ExportWorker.run_once()` + `ProcessExportJob`** claim and
  transcode — transcoding is CPU-bound, so it runs behind a poller, **not** the relay
  fan-out (same rule as render/enrichment). *Alternative:* a `RenderJobSucceeded` subscriber
  auto-exports — rejected (export is user-chosen, with params; not every render is exported).

- **Fork C — Neutral port (recommended: discrete `IExporter`).** A new
  `IExporter` (`MediaAsset` source + `(format, quality, orientation)` → delivery file +
  probed facts), FFmpeg leaf `FfmpegExporter` (configuration-blind, W8.1.1). **Not** a reuse
  of `IRenderer` (`Timeline → video` ≠ `MediaAsset → delivery encoding`) and **not**
  `IThumbnailer`. Keeps the discrete-port discipline (no "God" media adapter).

- **Fork D — Source (recommended: the render output only).** Export consumes
  `render_job.output_media_asset_id` (a finished, owned, live `MediaAsset`) and nothing else
  — never the Timeline, provider state, checkpoints, or render-job history (RC5 + W8.5.x).

- **Fork E — Idempotency (recommended: reuse the built-in dedup + deterministic key).**
  `CreateExportJob` relies on the partial-unique index: a duplicate active/fulfilled request
  returns the existing job (`ConflictError` → recover). `ProcessExportJob` writes a
  **deterministic** output key so a re-run hits `media_assets` uniqueness → recover-existing
  (identical to render). No new idempotency machinery.

- **Fork F — Encoding semantics (SIGNED OFF: *tightened* — delivery-only, same-orientation).**
  α8.5a changes **delivery characteristics only**, never presentation:
  - **Allowed:** format conversion, codec conversion, bitrate ladder, **resolution ladder**
    (`quality` → `sd`/`hd_1080p`/`qhd_2k`/`uhd_4k`); `format` → container/codec
    (`mp4`=h264/aac, `webm`=vp9/opus, `mov`=h264/aac, `gif`=palette). Deterministic scaling
    **within the master's own orientation** (`horizontal→horizontal`, `vertical→vertical`,
    `square→square`). Audio is carried through (α8.4e produced it); `gif` drops audio.
  - **Deferred:** **cross-orientation exports** (`horizontal→vertical`, etc.) and any
    letterbox/pillarbox/crop/smart-reframe/AI-reframe. Even deterministic scale-and-pad
    changes *presentation* semantics → product policy, not delivery. A later slice may add a
    reframe policy without expanding the meaning of "export".
  - **Guard:** if a requested `orientation` differs from the master's orientation,
    `CreateExportJob` rejects it (`422`, out of α8.5a scope). All allowed params are pure
    functions of the request (RC6/RP9).

---

## 4. Proposed invariants

- **W8.5.1 (new) — Export is downstream, delivery-only.** Export **reads** a finished
  render-output `MediaAsset` and produces a **new** delivery `MediaAsset`; it never
  re-composes, re-times, mutates, or overwrites the source (RC5), and never reads or mutates
  orchestration state, checkpoints, provider state, workflow/render lifecycle, or Timeline
  definitions. Its only writes are `export_jobs` lifecycle fields, the delivery
  `MediaAsset`, storage objects, and export events.
- **W8.5.2 (new) — Export consumes only a `MediaAsset` + request params.** Never provider
  outputs, URLs, checkpoints, request IDs, provider job IDs, or webhooks (mirror of
  W8.4b.2 / W8.4c.2).
- **W8.5.3 (new) — The rendered `MediaAsset` is the canonical master; exports are
  replaceable delivery artifacts.** An exported `MediaAsset` may be regenerated at any time
  from the master render (same master + same `(format, quality, orientation)` ⇒ functionally
  equivalent delivery, RC6). Deleting/regenerating a delivery artifact never affects the
  master. This establishes an explicit hierarchy — one master render, N replaceable delivery
  encodings (MP4 / MOV / WEBM / GIF) — which aligns with RC5, simplifies storage lifecycle,
  and lets export **profiles** evolve later without ever touching the master.

---

## 5. Migration verdict

**Zero migration.** `export_jobs`, all four enums, and the partial-unique dedup index
already exist (migrations `0001` + `0003`). α8.5a is application code only: repository
(additive), use case, worker, port + adapter, events, DI, tests.

---

## 6. Test plan

- **Unit** — `CreateExportJob` inserts a `queued` job + dedups via the partial-unique index
  (`ConflictError` → existing job); `ProcessExportJob` claims (lease + CAS), transcodes via a
  **fake `IExporter`**, registers the delivery `MediaAsset`, settles `succeeded`
  (`output_media_asset_id` + `file_size_bytes`) and emits `ExportJobSucceeded`; deterministic
  key idempotency (re-run recovers, no dupe); failure path (`ExportJobFailed`); canceled
  mid-export (no resurrection); non-`succeeded` render output → guarded.
- **Opt-in integration** — real `FfmpegExporter`: mp4 → webm / gif / mov; a quality + an
  orientation (pad) roundtrip; skipped without the binary (α8.4b–e pattern).
- **Full gate** — ruff, black, mypy, import-linter, unit; **freeze guard green, zero
  overrides**.

---

## 7. Versioning

Runtime capability → `0.4.30-phase3-alpha8.5a`, tag `v0.4.30-phase3-alpha8.5a`. Standard
two-commit release ritual.

---

## 8. Deliverable (signed off)

α8.5a **provides:** `CreateExportJob`, `ProcessExportJob`, `ExportWorker`, `IExporter`,
`FfmpegExporter`, deterministic export keys, export lifecycle + events, delivery
`MediaAsset`s — **zero migrations**, freeze guard green, **zero ADR-0042 overrides**,
RC1–RC6 + RP1–RP9 compliant.

α8.5a **explicitly excludes:** publishing, notifications, download service,
storage-provider implementations, smart reframe, **orientation changes**, and any creative
transformation.

> **Crisp definition:** α8.5a adds a delivery **export engine** that transcodes a completed
> render's master `MediaAsset` into requested `(format, quality)` delivery encodings **within
> the master's orientation**, via a discrete `IExporter`, following the platform's
> claim → lease → transform → idempotent-settle → event worker model — entirely downstream of
> the ADR-0042 frozen surface and within ADR-0043 RC5/RC6.
