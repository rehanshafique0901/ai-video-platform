# Phase 3 · α8.4b — Render Engine (Timeline → FFmpeg → output MediaAsset) · PRE-FLIGHT

> Status: **SIGNED OFF (2026-07-23).** Forks approved as proposed: **E** (scope
> split — core render loop only; thumbnails/metadata → α8.4c), **A** (poll worker),
> **B** (neutral `IRenderer`), **C/D** (deterministic output key + idempotency),
> **F** (live Timeline; defer version-binding). Invariants **W8.4b.1** (strengthened)
> and **W8.4b.2** adopted. Gating criterion confirmed: zero ADR-0042 frozen changes,
> zero overrides. Companion to `PHASE3_ALPHA8_4a_PREFLIGHT.md` (ingestion, shipped
> `v0.4.25-phase3-alpha8.4a`).

---

## 0. Gating criterion (ADR-0042) — verified *before* design

**Question:** can α8.4b be implemented touching **zero** ADR-0042 frozen modules
and **zero** `_paused`/checkpoint contract?

**Answer: yes.** Grounding confirms every surface α8.4b needs is *outside* the
freeze:

| α8.4b needs | Path | Frozen? |
| --- | --- | --- |
| `RenderJob` aggregate | `app/domain/render/*` | ❌ not frozen |
| `IRenderJobRepository` (+ new worker transitions) | `app/application/interfaces/repositories.py` (render block) | ❌ |
| Render use cases (new worker) | `app/application/use_cases/render/*` | ❌ |
| Timeline read surface | `app/domain/timeline/*`, `ITimelineRepository` | ❌ |
| `IObjectStorage` / `IMediaDownloader` (α8.4a) | `app/application/interfaces/*`, `app/infrastructure/{storage,media}/*` | ❌ |
| `IMediaRepository.add` | media repo | ❌ |
| `IDistributedLockManager` | `app/application/interfaces/locks.py` | ✅ **frozen port — consumed, not modified** |

The frozen set is only: workflow runner / resume / completion / workflow events,
provider ports+registry+dispatcher, usage recorder, relay, lock manager **impl**,
and the workflow domain enums/registry/aggregate. α8.4b **consumes** the frozen
lock-manager *port* (allowed — that is its public API) and touches none of the
frozen files. It never reads or writes `_paused`, checkpoints, or any workflow
state. **Acceptance criterion:** `check_frozen_platform.py --base main` stays green
with **zero override markers** for the whole branch — if implementation ever
pressures a frozen file, stop and revisit the design.

---

## 1. Thesis

α8.4a made a finished provider job *become a `MediaAsset`*. α8.4b makes a
**Timeline of `MediaAsset`s become a rendered video** `MediaAsset`. It ships the
first **render worker**: it claims a `queued` `render_jobs` row under the
`render_job:<id>` lock, loads the Timeline (tracks + clips), resolves each
`clip.media_asset_id` to stored bytes, composes them with **FFmpeg**, stores the
output via `IObjectStorage`, registers a `MediaAsset(kind=video, source=generated)`,
sets `render_jobs.output_media_asset_id`, and settles the job `succeeded`.

Dependency graph the freeze was meant to produce (α8.4b lives at the bottom, blind
to everything above `MediaAsset`):

```
Provider → Completion → GeneratedMediaIngestion → MediaAsset
                                                      │
                                             Timeline (clips ref MediaAsset)
                                                      │
                                        RenderJob worker → FFmpeg → output MediaAsset
```

FFmpeg never learns about providers, checkpoints, webhooks, completion, or
orchestration. It only knows Timelines and MediaAssets.

---

## 2. Grounding (what already exists)

- **`RenderJob` aggregate (α7.1)** — `app/domain/render/render_job.py`. Carries
  `timeline_id`, `workflow_run_id?`, `pipeline`/`pipeline_version`, `queue`,
  `priority`, `status`, `started_at?`, `finished_at?`, `progress` (decimal-as-str),
  `error?`, **`output_media_asset_id?`**, `idempotency_key?`, self-versioned. The
  worker-set fields are `NULL`/default at create — α8.4b is exactly the code that
  fills them.
- **`RenderStatus` (α7.1)** — `queued → running → {succeeded|failed}`; cancel from
  `queued|running`. The docstring already reserves `running/succeeded/failed` as
  "producible only by the background render worker (α8.x)".
- **`IRenderJobRepository` (α7.1)** — has `add` / `get_by_project_and_key` /
  `list_by_project` / `get_owned` / `cancel`. **No worker transitions yet** →
  α8.4b adds them (non-frozen): a claim/scan for queued jobs + `mark_running` /
  `update_progress` / `mark_succeeded` / `mark_failed` (status-guarded CAS).
- **`RenderJobCreated` event (α7.1)** — already emitted to the outbox on create
  (`create_render_job.py`). `RenderJobCanceled` too.
- **Timeline aggregate (α6.3)** — `Timeline` = tracks + clips. `Clip` has
  `media_asset_id?`, `start_seconds`/`end_seconds` (placement), and
  `source_start_seconds`/`source_end_seconds` (trim window). `ITimelineRepository`
  exposes `get_by_project` / `list_tracks` / `list_clips`.
- **`IObjectStorage` + `IMediaDownloader` (α8.4a)** — the storage + fetch seams to
  reuse for reading source bytes and writing the render output. No new storage
  abstraction needed (blueprint §10 "Storage Providers" == this port).
- **`IMediaRepository.add` (α6.2)** — already accepts `source="generated"`,
  `kind="video"`/`"thumbnail"`, storage coords, and is idempotent on the
  storage-key uniqueness (`ConflictError`).
- **Blueprint §9** — the canonical render flow (load timeline → resolve clips →
  compose/trim/mix/subtitle → FFmpeg mux → storage → `media_assets(kind=video,
  source=generated)` → set `output_media_asset_id` → `RenderFinished`).

**Producer-uniqueness check (as requested):** today **`IngestGeneratedMedia` is the
sole producer of `MediaAsset(source="generated")`** (grep confirms only
`ingest_generated_media.py`). α8.4b's render *output* becomes a **second** generated
producer (`kind=video`), and thumbnails (if in scope) a third (`kind=thumbnail`) —
all distinguished by `kind` + `source_metadata.origin` (`"provider"` vs `"render"`
vs `"thumbnail"`). Critically, this does **not** violate the dependency-cleanliness
goal: render's **input** path consumes `MediaAsset`s (via `clip.media_asset_id`),
**never** provider outputs or checkpoints. The enum is unchanged (no migration).

---

## 3. Proposed shape (all additive, non-frozen)

- **`IRenderer` port + neutral DTOs** (`app/application/interfaces/renderer.py`) —
  `render(spec: RenderSpec) -> RenderResult`. `RenderSpec` = ordered resolved
  clips (local input paths + trim windows + placement) + output target; neutral,
  FFmpeg-agnostic. `RenderResult` = output path + probed basics (duration, width,
  height, codec, size). Raises a neutral `RenderError`.
- **`FfmpegRenderer` adapter** (`app/infrastructure/render/ffmpeg_renderer.py`) —
  invokes the `ffmpeg`/`ffprobe` binaries in a subprocess in a temp workspace.
  Configuration-blind (binary path + timeouts injected). All failures → neutral
  `RenderError`. (Unit tests use a fake renderer; the real binary is exercised in
  an integration test that skips when `ffmpeg` is absent — same discipline as the
  provider adapters.)
- **`ProcessRenderJob` use case** (`app/application/use_cases/render/process_render_job.py`)
  — the worker body for **one** job: acquire `render_job:<id>` lease → CAS
  `queued → running` → load timeline + resolve clips (materialize source bytes to
  a temp dir via `IObjectStorage`/`IMediaDownloader`) → `IRenderer.render(...)` →
  `IObjectStorage.put(output)` → `IMediaRepository.add(kind=video,
  source=generated)` → set `output_media_asset_id` + CAS `running → succeeded` →
  emit `RenderJobSucceeded`. On failure → CAS `running → failed` + `RenderJobFailed`.
  Everything render-side; **no workflow/orchestration state touched**.
- **`RenderWorker.run_once()`** (poll ingress) — scans `queued` jobs oldest-first
  and processes each under its own lease (mirrors `CompletionEngine.poll_once()`).
  Library-only/synchronous — **no Celery/Redis/daemon** (consistent with α8.1–α8.3).
- **New non-frozen repo methods** on `IRenderJobRepository`: a queued-scan/claim +
  `mark_running` / `update_progress` / `mark_succeeded(output_media_asset_id)` /
  `mark_failed(error)` (status-guarded CAS, exactly-once via the lease + CAS).
- **New events**: `RenderJobSucceeded` / `RenderJobFailed` (additive; enables
  future downstream consumers — export, notifications — without touching render).
- **Config**: `ffmpeg_binary_path`, `ffprobe_binary_path`, `render_workspace_dir`,
  `render_timeout_seconds`, `render_lock_owner`, `render_lease_seconds`.
- **DI wiring**: renderer + worker factories; reuse the α8.4a storage/downloader.

---

## 4. Design forks (for sign-off — not yet decided)

- **Fork E — scope split (recommended).** The blueprint's α8.4b bundle (compose +
  transitions/effects + thumbnails + metadata) is several concerns. Propose:
  - **α8.4b (this slice):** the core render loop — timeline → FFmpeg compose
    (concat/trim/basic mix) → single output video → register `MediaAsset` +
    `output_media_asset_id` → `RenderJobSucceeded`. Duration/dimensions/codec probed
    from the output are cheap and included.
  - **α8.4c (next):** thumbnails (`kind=thumbnail`) + poster/preview derivation.
  - **α6.4 (separate, per blueprint):** transitions/effects write paths.
  Keeps "one architectural concern per slice."
- **Fork A — worker trigger: poller vs event subscriber.** Recommend **poller**
  (`RenderWorker.run_once()` scanning `queued`), like `CompletionEngine.poll_once()`.
  Rationale: renders are long/CPU-heavy; running them inside the relay's synchronous
  fan-out (as α8.4a ingestion does for fast downloads) would block the outbox. The
  `RenderJobCreated` event stays available for *light* future consumers.
- **Fork B — FFmpeg boundary: neutral `IRenderer` port.** Recommend yes — keeps the
  use case ffmpeg-agnostic and unit-testable with a fake; the real binary lives in
  one adapter (integration-tested, skipped without ffmpeg).
- **Fork C — input materialization.** Render needs local files for ffmpeg. Recommend
  the worker materializes each clip's source to a temp workspace via `IObjectStorage`
  (local/`generated`) — with `IMediaDownloader` as the fallback for non-local
  coords. α8.4b targets **locally-stored generated media** first; broad backends
  ride the same port later.
- **Fork D — output idempotency + registration.** Deterministic output key in
  `render_job_id` → a retried render re-writes identical coords and the
  `media_assets` uniqueness `ConflictError` is treated as already-rendered
  (idempotent), reusing/looking up the existing asset for `output_media_asset_id`.
- **Fork F — reproducibility (§14).** Render the **live** Timeline for α8.4b (note
  as accepted scope); frozen-`project_version` binding is a later decision and does
  not block this slice.

---

## 5. New invariants (adopted)

- **W8.4b.1 — the render worker is a pure Timeline → Media transform.** It reads
  only the `RenderJob`, its `Timeline` (tracks/clips), and the referenced
  `MediaAsset`s, and writes only render artifacts (output object, output
  `MediaAsset`, `render_jobs` lifecycle fields, render events). It **neither reads
  nor mutates orchestration state, checkpoints, provider state, workflow status, or
  the completion lifecycle** — extending W8.4.1/W8.4.2 to the render path.
- **W8.4b.2 — the renderer consumes only `MediaAsset` identifiers and Timeline
  data.** It **never** consumes provider-specific outputs, URLs, checkpoints,
  request IDs, provider job IDs, or webhook payloads. This makes the renderer
  completely provider-agnostic and preserves the dependency graph
  `Provider → Completion → GeneratedMediaIngestion → MediaAsset → Timeline →
  Renderer → output MediaAsset`.

---

## 6. Migration verdict

**Zero migration expected.** `render_jobs`, `media_assets`, `timeline_*`, and the
`media_source`/`render_status` enums all already exist with the needed columns
(`output_media_asset_id`, `progress`, `kind=video`, `source=generated`). The only
new persistence surface is additive repo *methods* on existing tables (render CAS
transitions), mirroring how α8.3 added `resume_run`/`list_paused`.

---

## 7. Test plan

- **Unit** (no ffmpeg, no DB): `ProcessRenderJob` with a **fake `IRenderer`** +
  fake storage/downloader/repos — happy path (register output + `output_media_asset_id`
  + `succeeded`), render failure → `failed`, missing/empty timeline → `failed`
  cleanly, idempotent re-run (ConflictError → no duplicate), lease contention →
  no double-processing, and a **W8.4b.1 assertion** that no workflow/checkpoint
  surface is touched. `RenderWorker.run_once()` scan/claim ordering.
- **Repo unit**: the new render CAS transitions (status guards, exactly-once).
- **Integration (opt-in)**: `FfmpegRenderer` against the real binary on a tiny
  fixture timeline — **skipped when `ffmpeg` is unavailable** (like provider
  adapters), so CI stays green without the binary.
- **Gate**: ruff/black/mypy/import-linter + `-m unit` + freeze guard green, zero
  overrides.

---

## 8. Versioning

Runtime capability change → **`0.4.26-phase3-alpha8.4b`** (feature branch
`phase3/alpha8.4b-render-engine`, `-dev` during work), following the standard
release ritual.

---

## 9. Sign-off questions

1. **Fork E** — split thumbnails/metadata into α8.4c and keep α8.4b to the core
   render loop? (recommended)
2. **Fork A** — poller (`RenderWorker.run_once()`) over event-subscriber for the
   trigger? (recommended)
3. **Fork B** — neutral `IRenderer` port + `FfmpegRenderer` adapter? (recommended)
4. **Fork C/D** — temp-workspace materialization via `IObjectStorage`, deterministic
   output key for idempotency? (recommended)
5. **Fork F** — render the live Timeline for now (defer version-binding)? (recommended)
6. Adopt **W8.4b.1**?
7. Confirm the gating criterion: **zero ADR-0042 frozen changes, zero overrides**.
