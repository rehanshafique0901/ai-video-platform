# Asset Promotion Bridge (`PublishGenerationAssets`) — Grounding

> **Type:** Grounding (read-only facts). **No code, no schema, no baseline change.**
> Establishes the facts the pre-flight will build on. Nothing here is a design decision.
>
> **The one question:** *How does the Stage-13 AI generation runtime's output
> (`generation_assets`, Path B) reach the already-built export/publishing pipeline
> (`media_assets` → render/export → publish, Path A) — without violating any frozen
> architectural boundary?*
>
> **Governed by (frozen, must not be crossed without an ADR):** `ADR-0046` X1–X8
> (Execution-runtime boundaries; **X8** is the direct mandate for this slice),
> `EXECUTION_RUNTIME_CONTRACT.md` W8.6.1–8 (**W8.6.8**), `ADR-0037` (the `MediaAsset`
> canonical boundary — media are generation outputs, no version, direct ownership),
> `ADR-0043` RC1–RC6 (render composition boundary; **RC1**, **RC5**),
> `PUBLISHING_RUNTIME_CONTRACT.md` **PUB-1** (publish consumes the export delivery
> artifact), `ADR-0045` F4/F5 (Decision-plane raw-SQL persistence style).
>
> **Baseline (immutable):** `v0.4.38-phase3-alpha8.6c` (`app/main.py` version
> `0.4.38-phase3-alpha8.6c`; `git describe` → `v0.4.38-phase3-alpha8.6c`).
>
> **Roadmap label + version:** deferred to the pre-flight (this is not the α8.6d publish-
> notification slice; it is the execution→media bridge named `PublishGenerationAssets`).

---

## 0. The boundary being bridged

Two artefact pipelines exist in code today. They are **not connected**.

```
Path A — Workflow / platform (reaches export + publish)
  Timeline (clips → media_asset_id)
    → ProcessRenderJob            → render_jobs.output_media_asset_id   [master MediaAsset]
    → CreateExportJob(render_job) → export_jobs row
    → ProcessExportJob            → export_jobs.output_media_asset_id   [delivery MediaAsset]
    → CreatePublishJob(export_job)→ publish_jobs.source_media_asset_id  (PUB-1)
    → ProcessPublishJob           → uploads delivery bytes to a destination

Path B — AI generation runtime (does NOT reach media_assets / export / publish)
  GenerateVideo
    → generation_assets (kind ∈ {frame, reference, mask, audio, video, thumbnail, metadata})
    → generations.final_video_asset_id  (logical pointer to the final kind='video' asset)
    → object storage
```

`PublishGenerationAssets` is the **named but unbuilt** seam between them. It is reserved in
three places and implemented in none:

- `ADR-0046` **X8**: *"`generation_assets` is execution-owned. Promotion into the platform's
  `media_assets` library is an **explicit** future use case (`PublishGenerationAssets`),
  never a direct write."*
- `EXECUTION_RUNTIME_CONTRACT.md` **W8.6.8** (identical wording; frozen by ADR-0046).
- The `0012_execution_runtime` migration header and `ERD.md` (Cluster 12) both name it.

**Fact (verified):** grepping the repo, `PublishGenerationAssets` appears **only** in docs
(ADR-0046, the contract, the ERD, the 0012 header, and the discovery report). There is **no
module, class, port, route, subscriber, or test** referencing it.

---

## 1. Execution side — what already exists (Path B)

### 1.1 The tables (`0012_execution_runtime`, raw-SQL, ORM-less)

- **`generations`** — the execution aggregate + state machine + provenance head.
  - Carries `final_video_asset_id uuid` (a **logical** pointer to `generation_assets(id)`,
    **no FK** — avoids a circular dependency), plus the final render's storage coordinates
    mirrored on the row: `video_backend storage_backend`, `video_bucket`, `video_key`,
    `duration_seconds numeric(10,3)`, `width`, `height`.
  - **Ownership fact (critical):** `generations` has **no `tenant_id`, no `owner_user_id`,
    and no `project_id`** columns. It has `prompt`, `title`, `identity_id text` (a free-text
    identity handle), `target_platform`, dimensions, provenance versions, and status only.
- **`generation_assets`** — the canonical execution artefact registry.
  - `asset_kind generation_asset_kind` enum = `frame | reference | mask | audio | video |
    thumbnail | metadata`.
  - Physical coordinates: `storage_backend`, `storage_bucket`, `storage_key`
    (**UNIQUE together**), `mime_type NOT NULL`, `size_bytes`, `checksum_sha256 bytea`,
    `width`, `height`, `duration_ms`.
  - `parent_asset_id` self-reference → repair/upscale is a **lineage graph**, not an
    overwrite (ADR-0046 Q1).
  - **No link to `media_assets`** (by design — X8).

### 1.2 The store port + use case

- **`IExecutionRuntimeStore`** (`app/application/interfaces/execution_runtime_store.py`) —
  `begin / set_status / record_resolution / register_asset / record_shot / complete / fail`.
  - `register_asset(NewGenerationAsset) -> UUID` writes one `generation_assets` row.
  - `complete(*, generation_id, final_video_asset_id, storage_backend, storage_bucket,
    storage_key, duration_seconds, width, height)` marks the generation `COMPLETED` and sets
    the final-video pointer + coordinates.
  - A `NullExecutionRuntimeStore` no-op default exists (the use case can run without
    persistence).
- **`GenerateVideo`** (`app/application/use_cases/generation/generate_video.py`) consumes a
  **`GenerateVideoRequest`** (`.../generation/request.py`): `prompt`, `identity:
  IdentityProfile`, `generation_id`, `execution_mode`, `aspect_ratio`, `target_platform`,
  durations, dimensions, `fps`, similarity/attempt budgets. **It carries no `tenant_id`,
  `owner_user_id`, or `project_id`.**

### 1.3 The lifecycle events (internal, no consumers)

`app/application/use_cases/generation/events.py`, `AGGREGATE_TYPE = "generation"`:
`generation.started`, `generation.shot_generated`, `generation.verification_failed`,
`generation.repair_succeeded`, **`generation.video_rendered`**, **`generation.export_completed`**.
The module docstring states they *"have no external consumers yet; they exist so UI /
telemetry / analytics / notifications can subscribe later"* — i.e. `generation.export_completed`
is a natural (but currently unconsumed) promotion trigger.

### 1.4 Invocation fact (there is no runtime caller)

**`GenerateVideo` has no API router and no subscriber.** It is composed only by
`container.get_generate_video_use_case()` and invoked from `scripts/generate_demo.py` and
tests. There is **no `POST` endpoint** to trigger a generation, and nothing in
`app/main.py`'s router set exposes it. (Contrast: render, export, publish, and media all
have user-facing `POST` create endpoints.)

---

## 2. Target side — what already exists (Path A ingestion into `media_assets`)

### 2.1 The `MediaAsset` boundary (`ADR-0037`)

`app/domain/media/media_asset.py` — a slim, frozen projection of `media_assets`:

- **Ownership is direct and NOT NULL:** `tenant_id: UUID`, `owner_user_id: UUID`.
- Optional links: `project_id`, `scene_id`, `prompt_id`, `model_id` (all nullable).
- `source: str` ∈ `media_source` = `uploaded | stock | generated` — **`generated` already
  exists** and is the source used for produced artefacts.
- Physical fields (`storage_backend`/`bucket`/`key` unique, `mime_type`, `size_bytes`,
  `checksum_sha256` **required**, `width`/`height`/`duration_seconds`) are **immutable
  forever** (changing them means a *different* asset).
- **No `version`** (ADR-0037/ADR-0036): media are generation outputs, not editorial content;
  last-writer-wins on the mutable links.
- **No `generation_asset_id` column** exists on `media_assets` today (provenance back to
  Path B, if wanted, is currently only expressible through `source_metadata` JSON).

### 2.2 The closest precedent — `IngestGeneratedMedia` (α8.4a)

`app/application/use_cases/media/ingest_generated_media.py` is the existing "producing"
use case that lands **provider output → `media_assets(source='generated')`**. It is the
structural template a promotion use case would mirror:

- **Ownership is resolved from a project:** `ownership = await
  self._uow.projects.get_ownership(project_id)` → `(tenant_id, owner_user_id)`; a `None`
  ownership is a no-op.
- **Bytes are stored via `IStorageResolver.active().put(...)`** using a **deterministic key**
  (`{tenant}/{project}/{run}/{step}/{request_id}{ext}`), so re-delivery re-writes identical
  bytes and the `media_assets` storage-key uniqueness makes re-registration a **ConflictError
  = idempotent no-op**.
- **Registration:** `self._uow.media.add(tenant_id=…, owner_user_id=…, kind=…,
  source="generated", storage_backend/bucket/key=…, mime_type, size_bytes, checksum_sha256,
  project_id=…, scene_id=None, prompt_id=None, model_id=None, provider=…, width/height/
  duration_seconds, source_metadata={…provenance…})`.
- **I/O is outside any DB transaction** (download/store never hold a lock open); registration
  is a separate short transaction.
- Provenance is carried in **`source_metadata`** (e.g. `workflow_run_id`, `step_index`,
  `request_id`, `source_url`).

### 2.3 Object storage port

`IObjectStorage` (`app/application/interfaces/object_storage.py`): `put / get / exists /
delete` by opaque key; `get(key) -> bytes`. A promotion that **copies** execution bytes into
a media-namespaced key is expressible with `get` + `put`. Note: each adapter is scoped to a
single `backend`/`bucket` (its own properties); whether Path B (`generation_assets`) and
Path A (`media_assets`) share the same backend/bucket is an **infrastructure fact the
pre-flight must confirm** (it determines whether promotion is a within-store copy, a
cross-store copy, or a shared-object reference).

---

## 3. The reach into export/publish — the central architectural fact

Landing a `media_asset` **does not by itself reach export or publish.** The Path A chain is
strictly gated:

| Stage | Created by | Requires | Consumes | Produces |
|---|---|---|---|---|
| Render | `CreateRenderJob` (`POST /projects/{id}/render-jobs`) | project **Timeline** | Timeline clips' `media_asset_id` (RC1) | `render_jobs.output_media_asset_id` (master) |
| Export | `CreateExportJob` (`POST .../render-jobs/{render_job_id}/exports`) | a **succeeded `render_job_id`** with `output_media_asset_id` set | `render_jobs.output_media_asset_id` (the *only* legal source) | `export_jobs.output_media_asset_id` (delivery) |
| Publish | `CreatePublishJob` (`POST /publish-jobs`) | a **succeeded `export_job_id`** with `output_media_asset_id` set (**PUB-1**) | `export_jobs.output_media_asset_id` via `resolve_source` (join `export_jobs → render_jobs → projects`) | `publish_jobs.source_media_asset_id` |

**Verified facts:**

- `CreateExportJob` takes a **`render_job_id`**, not a `media_asset_id`; `export_jobs` has
  **no source-media column** (only the `render_job_id` FK). *(`create_export_job.py`,
  `0001_baseline.py` export_jobs DDL.)*
- `ProcessExportJob` resolves its source as `render_job.output_media_asset_id` — *"the only
  legal source"*. *(`process_export_job.py`.)*
- `CreatePublishJob` requires `export.status == SUCCEEDED and output_media_asset_id is not
  None` (PUB-1); `resolve_source` joins `export_jobs → render_jobs → projects` and returns
  the delivery asset id. *(`create_publish_job.py`, `publish_job_repository.py`.)*
- **There is no existing path** to create an export or publish job from an arbitrary finished
  video `media_asset` **without** going through `Timeline → Render → Export`. *(Verified: no
  such use case, endpoint, or column.)*

**RC1 / RC5 (frozen, ADR-0043):**

- **RC1** — *"Timeline is the sole composition input."* Anything that reaches the renderer
  does so as a Timeline clip referencing a `MediaAsset`.
- **RC5** — *"Rendered media is immutable downstream. Enrichment, export, and publishing
  **read** an output `MediaAsset`; they never re-encode, re-compose, or overwrite it. A new
  composition is a new render job producing a new output `MediaAsset`."*

**Consequence (fact, not a decision):** the generation runtime already produces a **finished,
composed MP4** (`generations.final_video_asset_id`, `kind='video'`). To reach *publish* via
the existing chain, that video must become an `export_jobs.output_media_asset_id`. The only
existing route to an export-delivery asset is `Timeline → Render → Export`, which would treat
the finished video as source footage and **re-render/re-encode** it. Whether the slice
(a) stops at the `media_assets` library (the literal X8 definition of `PublishGenerationAssets`),
(b) routes the promoted asset through Timeline→Render→Export, or (c) requires a new
export/publish entry that accepts a finished asset — is the **primary pre-flight question**
(§6.A). Grounding records only that **no (b)/(c) path exists today** and that any new entry
must be reconciled against RC1/RC5/PUB-1.

---

## 4. Frozen boundaries this slice must respect (restated)

| ID | Source | Constraint (fact) |
|---|---|---|
| **X8 / W8.6.8** | ADR-0046 / contract | Execution never writes `media_assets` directly; promotion is the explicit `PublishGenerationAssets` use case. The generation domain and content/platform domain stay decoupled. |
| ADR-0046 Q1 note | ADR-0046 | *"generation has no tenant/owner/project/publishing context yet."* — the ownership gap is a known, documented reality. |
| **ADR-0037** | ADR-0037 | `MediaAsset` is the canonical boundary: direct NOT-NULL ownership, `source ∈ {uploaded, stock, generated}`, immutable physical fields, no version. |
| **RC1 / RC5** | ADR-0043 | Timeline is the sole composition input; rendered media is immutable downstream (export/publish read, never re-encode). |
| **PUB-1** | Publishing contract | Publish consumes the export **delivery** artifact (`export_jobs.output_media_asset_id`); no auto-publish from export completion (PUB-2). |
| F4/F5 | ADR-0045 | Execution-runtime persistence is raw-SQL + repositories + validator allowlist (ORM-free), mirroring 0010/0011/0012. |

**Enforcement fact:** the X8 boundary is **governance/review-enforced** today (ADR-0046 +
the generic domain-isolation import-linter contract). There is currently **no dedicated
import-linter contract** forbidding `app.domain.generation` / execution modules from
importing `app.domain.media` (the publishing-domain isolation contract only pins
`app.domain.publishing`). Whether to add a mechanical guard for the execution→media boundary
is a pre-flight question.

---

## 5. What does **not** exist yet (the build surface)

Everything below is absent today. Grounding lists it as fact; the pre-flight decides which
of it is in scope.

| Missing seam | Layer | Notes |
|---|---|---|
| `PublishGenerationAssets` use case | application | The named, unbuilt seam (X8). Read `generation_assets`, resolve/copy bytes, register `media_assets(source='generated')`. |
| An **ownership/context source** for a generation | domain/persistence | `generations` + `GenerateVideoRequest` carry no `tenant_id/owner_user_id/project_id`; `media_assets` requires them (§1.1, §2.1). No mapping exists today. |
| A **`generation_assets` reader** on the store/repo side | infrastructure | `IExecutionRuntimeStore` today only *writes* assets (`register_asset`) + `complete`; there is no read/query-assets method. |
| A **cross-store byte path** (or shared-object reference) | infrastructure | `get` from execution coords → `put` to media coords; depends on the shared-store fact (§2.3). |
| A **provenance link** `media_assets ↔ generation_assets` | persistence | No column today; `source_metadata` is the existing (non-migration) precedent, a dedicated column would be an additive migration. |
| A **trigger/caller** | application/api | `GenerateVideo` has no API/subscriber (§1.4). Promotion needs either an explicit endpoint, a subscriber on `generation.export_completed`, or both. |
| A route into **export/publish** for a promoted asset | api/application | No existing path accepts a finished asset for export/publish (§3). |
| Tests + boundary enforcement | testing | Promotion happy-path + idempotency + a "no direct execution→media write" assertion/guard. |

---

## 6. Open questions for the pre-flight (surfaced, not decided)

Each has a concrete factual basis above. **None is resolved here.**

- **6.A — Slice boundary / depth.** Does the bridge stop at the `media_assets` library (the
  literal X8 definition), route the promoted video through `Timeline → Render → Export → Publish`
  (RC1/RC5), or introduce a new export/publish entry for a finished asset? (§3) — *the
  primary scoping decision.*
- **6.B — Ownership source.** Where do `tenant_id / owner_user_id / project_id` come from
  for a generation that carries none? Options grounded in the repo: supplied at promotion
  call-time (mirroring `IngestGeneratedMedia(project_id=…)`), or an additive
  ownership/context column/table on the execution side. (§1.1, §2.2) — *the single largest
  gap.*
- **6.C — Trigger.** Explicit use case + API endpoint, a subscriber on
  `generation.export_completed`, or both? (§1.3, §1.4)
- **6.D — Which assets promote.** The final video only (`final_video_asset_id`), or
  selected/all kinds (frames, thumbnail)? ADR-0046 Q1 notes most generation assets never
  become user media. (§1.1)
- **6.E — Byte handling.** Copy bytes across coordinates vs. reference a shared object —
  contingent on whether Path A and Path B share a storage backend/bucket. (§2.3)
- **6.F — Provenance link.** `source_metadata` (no migration, precedent) vs. a dedicated
  `media_assets.generation_asset_id` column (additive migration). (§2.1, §2.2)
- **6.G — Idempotency.** Adopt the deterministic-key + `ConflictError` no-op pattern from
  `IngestGeneratedMedia`. (§2.2)
- **6.H — Enforcement.** Add a mechanical import-linter guard for the execution→media
  boundary, or keep it governance-enforced (X8)? (§4)
- **6.I — ADR.** X8 already *names* this use case, so an ADR may not be required — **unless**
  6.A/6.B introduce a genuinely new decision (e.g. a new export-from-asset entry that touches
  RC1/RC5/PUB-1, or a new ownership model for the execution plane). To be judged in
  pre-flight per the workflow ("ADR only if genuinely required").
- **6.J — Roadmap label + version.** This is not α8.6d (publish notifications). The label and
  the next monotonic version are pre-flight decisions.

---

## 7. Non-goals (so the pre-flight stays scoped)

- **No new AI capability** — this slice moves *already-produced* artefacts; it does not
  change planning, generation, verification, or repair.
- **No caption/hashtag/thumbnail AI** — deterministic metadata only (deferred, separate
  slice).
- **No scheduling, analytics, notifications, or creator UX.**
- **No change to any frozen path** — the render/export/publish runtimes, the `MediaAsset`
  boundary, and the publishing credential boundary stay untouched except through their
  existing, sanctioned entry points.
- **No direct execution→`media_assets` write** — X8 is absolute; promotion is the only seam.

---

### Summary of established facts

The AI generation runtime (Path B, `generation_assets`) and the export/publishing pipeline
(Path A, `media_assets` → render → export → publish) are two disconnected pipelines. The
connecting use case — `PublishGenerationAssets` — is **named in three frozen documents and
built nowhere**. The execution plane produces a finished, composed video
(`generations.final_video_asset_id`) whose bytes already live in object storage, but the
plane carries **no ownership context** (`generations` and `GenerateVideoRequest` have no
tenant/owner/project) and **no runtime trigger** (`GenerateVideo` has no API/subscriber).
The target plane has a proven producing precedent (`IngestGeneratedMedia` →
`media_assets(source='generated')`, ownership from `projects.get_ownership`, deterministic-key
idempotency, I/O outside transactions) and the `generated` source already exists. Crucially,
**landing a `media_asset` does not by itself reach export or publish**: export requires a
succeeded `render_job` (Timeline-bound, RC1), publish requires a succeeded `export_job`
(PUB-1), and **no existing path** turns an arbitrary finished asset into an export-delivery
artifact. The single genuinely new design questions — the **slice depth** (library-only vs.
routed through render/export vs. a new finished-asset entry) and the **ownership source** —
are surfaced for the pre-flight (§6). Nothing in this document changes the repository or
pre-commits a design.
