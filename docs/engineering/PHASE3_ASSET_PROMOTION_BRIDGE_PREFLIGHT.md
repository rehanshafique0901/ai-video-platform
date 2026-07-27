# Phase 3 — Pre-flight: Asset Promotion Bridge (`PromoteGenerationAssets`)

> **Status: DRAFT — awaiting sign-off.** Input: `PHASE3_ASSET_PROMOTION_BRIDGE_GROUNDING.md`
> (approved). Governing artifacts: **ADR-0046** X1–X8 (Execution-runtime boundaries — **X8**
> is the direct mandate), `EXECUTION_RUNTIME_CONTRACT.md` **W8.6.8**, **ADR-0037** (the
> `MediaAsset` canonical boundary), **ADR-0043** RC1/RC5 (render composition boundary),
> `PUBLISHING_RUNTIME_CONTRACT.md` **PUB-1**, **ADR-0045** F4/F5 (Decision-plane raw-SQL
> persistence). Baseline (immutable): `v0.4.38-phase3-alpha8.6c`.
>
> **The one question this slice answers:** *How does a completed AI-generation output
> (`generations.final_video_asset_id`, Path B) become an owned `media_assets` library entry
> (Path A's asset store) — the exact X8 seam — while leaving the execution runtime, render,
> export, publish, ports, contracts, and schema untouched?*
>
> **Objective (per approved direction):** *promote completed AI-generated outputs into
> `media_assets` (the X8 seam). Do not expand into export, publish, Timeline, Render, or new
> runtime flows. Strictly additive.*
>
> **Locked from grounding + approved rulings (AR1–AR3):**
> - **AR1 — Slice depth:** the bridge stops at the `media_assets` library. It does **not**
>   create render/export/publish jobs, touch the Timeline, or add any runtime flow.
> - **AR2 — Ownership:** resolved **at promotion request time** from the authenticated caller
>   (adapting the `IngestGeneratedMedia(project_id=…)` pattern). **No ownership columns are
>   added to execution-runtime tables.**
> - **AR3 — Additivity:** existing orchestration, execution runtime, render, export, publish,
>   contracts, ADRs, and the frozen baseline remain untouched.
>
> **§10 records the design decisions AP1–AP9. §11 is the ADR assessment. Nothing is
> implemented until this pre-flight is approved.**

---

## 0. Gates (answered first)

### Gate 1 — ADR-0046 X8 / W8.6.8 (execution-owned artefacts)
> Does this write `media_assets` from the Execution Runtime?

**No — and that is the whole point.** X8 mandates that promotion be an **explicit use case**
(`PublishGenerationAssets`), never a direct write from the Execution Runtime. This slice
builds exactly that use case (named `PromoteGenerationAssets` — §2), lives **outside** the
execution plane (`app/application/use_cases/media/`), and reads execution artefacts through a
**new read-only port**. The Execution Runtime (`GenerateVideo`, `IExecutionRuntimeStore`,
`SqlExecutionRuntimeStore`, the generation repositories) is **not modified**. A **new
import-linter contract** (§8) mechanically forbids the execution plane from importing the
media context, cementing X8 for the first time.

### Gate 2 — ADR-0037 (`MediaAsset` canonical boundary)
> Does the promoted asset respect the media boundary?

**Yes.** The promoted row is an ordinary `media_assets` entry: `source='generated'` (the
enum value already exists and is already used by `IngestGeneratedMedia` and the export
worker), direct NOT-NULL ownership, immutable physical fields, **no version**. No new media
column, no enum change.

### Gate 3 — ADR-0043 RC1/RC5 (render composition boundary)
> Does this touch render composition or re-encode rendered media?

**No.** Promotion **copies bytes verbatim** into the media library; it never composes,
trims, re-times, or re-encodes (RC5 is about the render/export/publish read-path, which is
untouched). A promoted video reaches export/publish **only** through the existing, unchanged
Path A (`Timeline → Render → Export`, RC1) — this slice adds no shortcut around it (AR1).

### Gate 4 — PUB-1 / publishing runtime
> Does this change publishing?

**No.** Publishing still consumes `export_jobs.output_media_asset_id` (PUB-1). This slice
neither creates publish jobs nor auto-publishes. It lands an asset in the library only.

### Gate 5 — Migration / schema
> Is a migration required?

**No.** Provenance is carried in `media_assets.source_metadata` (JSONB, exists —
`IngestGeneratedMedia` precedent). Idempotency uses the existing
`uq_media_assets_storage_backend_storage_bucket_storage_key` constraint. No new table,
column, enum, or `EXPECTED_ENUM_COUNT` change. The `upgrade → downgrade → upgrade` roundtrip
is unaffected.

---

## 1. Positioning (what this slice *is* / *is not*)

```
Path B (unchanged)                          THE BRIDGE (new, additive)              Path A (unchanged)
GenerateVideo                                                                       media_assets library
  → generation_assets (kind='video')  ──▶  PromoteGenerationAssets  ──▶  media_assets(source='generated')
  → generations.final_video_asset_id        (read exec · copy bytes ·          (owned, project-linked)
       (COMPLETED)                            register owned media)                       │
                                                                                          ▼
                                                                            (later, via existing Path A only:
                                                                             Timeline → Render → Export → Publish)
```

**Is:** one new use case `PromoteGenerationAssets`; one new **read-only** port
`IGenerationReader` + a raw-SQL `GenerationReader` (ORM-less, F4/F5); a byte-copy from the
execution artefact's persisted storage backend into the active media store under a
deterministic key; registration of a `media_assets(source='generated')` row stamped with the
caller's ownership; one new authenticated, owner-scoped API endpoint; a container factory +
API deps wiring; a new import-linter contract cementing X8; unit + ephemeral-Postgres
integration tests.

**Is not:** any change to the execution runtime, render, export, publish, Timeline, or
orchestration; any migration; any change to `IExecutionRuntimeStore`, `IMediaRepository`,
`IProjectRepository`, `IStorageResolver`, or any domain type; a render/export/publish trigger;
a subscriber/auto-promotion; promotion of non-final artefacts (frames/masks/audio/thumbnail);
any AI metadata, scheduling, analytics, or creator UX.

---

## 2. The use case — `PromoteGenerationAssets`

**Name.** ADR-0046 X8 names the seam `PublishGenerationAssets`. To avoid colliding with the
publishing/destination "publish" verb (α8.6), the class is named **`PromoteGenerationAssets`**
and its docstring records that it *is* the ADR-0046 X8 `PublishGenerationAssets` seam. (AP1.)

**Location.** `app/application/use_cases/media/promote_generation_assets.py` — the
"producing-into-media" family, alongside `IngestGeneratedMedia` (its structural twin).

**Signature (design):**

```
async def execute(
    self, *,
    generation_id: UUID,
    project_id: UUID,
    tenant_id: UUID,        # from the authenticated caller
    owner_user_id: UUID,    # from the authenticated caller
) -> PromoteGenerationAssetsResult
```

**Flow (mirrors `IngestGeneratedMedia`'s phased, transaction-safe shape):**

1. **Authorize + resolve context (read).** In a UoW read: verify the `project_id` is owned by
   the caller (`uow.projects.get_owned(project_id, tenant_id, owner_user_id)` → `None` ⇒
   `NotFoundError` → 404). *(Ownership is the caller's; the project is a validated link — AR2 /
   AP2.)*
2. **Load the promotable output (read, new port).**
   `reader.load_final_video(generation_id)` → `PromotableGenerationVideo | None`.
   - `None` ⇒ `NotFoundError` (unknown generation) → 404.
   - `status != 'completed'` or `final_video_asset_id is None` ⇒ `ValidationFailedError`
     ("generation has no promotable final video") → 422.
3. **Copy bytes OUTSIDE any DB transaction (AP4).**
   `src = storage.resolve(video.storage_backend).get(key=video.storage_key)` →
   deterministic media key (§3) →
   `stored = storage.active().put(key=…, data=src, content_type=video.mime_type)`;
   `checksum = sha256(src)`, `size = len(src)`.
4. **Register the owned media (write, short txn).** `uow.media.add(tenant_id, owner_user_id,
   kind="video", source="generated", storage coords from `stored`, mime_type, size_bytes,
   checksum_sha256, project_id, scene_id=None, prompt_id=None, model_id=None,
   provider=video.chosen_provider, width, height, duration_seconds=video.duration_ms/1000,
   source_metadata={…provenance…})` → commit → return the asset.
5. **Idempotent replay (AP5).** If `add` raises `ConflictError` (deterministic key already
   registered on a prior attempt), re-read the existing asset via
   `uow.media.get_by_storage_coords(...)` and return it as an idempotent no-op (`status="noop"`).

**Result DTO:** `PromoteGenerationAssetsResult(status: str /* "promoted" | "noop" */,
media_asset_id: UUID, generation_id: UUID, generation_asset_id: UUID)`.

**Field mapping (`generation_assets` → `media_assets`):**

| media_assets | value |
|---|---|
| `tenant_id`, `owner_user_id` | authenticated caller (AR2) |
| `kind` | `"video"` (the final render; AP7) |
| `source` | `"generated"` |
| `storage_backend/bucket/key` | the **active** media store coords for the copied bytes |
| `mime_type` | generation video's `mime_type` |
| `size_bytes`, `checksum_sha256` | computed from the copied bytes (guarantees NOT-NULL checksum) |
| `width`, `height` | generation video's `width`/`height` |
| `duration_seconds` | `duration_ms / 1000` (nullable) |
| `project_id` | the validated owned project link |
| `scene_id`/`prompt_id`/`model_id` | `None` |
| `provider` | `generations.chosen_provider` (nullable) |
| `source_metadata` | `{"origin":"generation_promotion","generation_id":…,"generation_asset_id":…,"chosen_adapter":…,"seed":…}` |

---

## 3. The new read-only port + reader (the only new port)

**Why a new port.** No existing use-case-facing port reads `generation_assets`. The UoW
exposes no generation repository; `IExecutionRuntimeStore` is **write-only** (`begin /
set_status / register_asset / record_shot / complete / fail`) and runs on its own
`session_factory`, not the UoW. Extending the write port would broaden a frozen seam;
instead this slice adds a **separate, additive, read-only** port and leaves
`IExecutionRuntimeStore` untouched (AP3).

**Port** — `app/application/interfaces/generation_reader.py`:

```
class IGenerationReader(ABC):
    async def load_final_video(self, generation_id: UUID) -> PromotableGenerationVideo | None: ...
```

**DTO** — `PromotableGenerationVideo` (frozen): `generation_id`, `status`,
`final_video_asset_id`, `chosen_provider`, `chosen_adapter`, `seed`, `title`,
`target_platform`, and the final video asset's `storage_backend/bucket/key`, `mime_type`,
`size_bytes`, `checksum_sha256`, `width`, `height`, `duration_ms`.

**Implementation** — `app/infrastructure/generation/generation_reader.py`: a raw-SQL reader
(ORM-less, ADR-0045 F4/F5, mirroring `SqlExecutionRuntimeStore`) that `SELECT`s
`generations JOIN generation_assets ON generation_assets.id = generations.final_video_asset_id`
in one query. Built with the same `async_sessionmaker` the execution store uses; read-only
(no writes, no events). Injected into the use case.

---

## 4. Bytes: copy, never reference (AP4)

The generation video's bytes already live in object storage, so a reference (a `media_assets`
row pointing at the execution object's coordinates) would avoid a copy — but it is
**rejected**: `generation_assets` are `ON DELETE CASCADE` from `generations` and are
execution-owned/repairable; a shared object would couple the media library's durability to
execution-plane lifecycle and break the X8 decoupling. Promotion therefore **copies** the
bytes into the **active** media store under a media-namespaced deterministic key:

```
{tenant_id}/{project_id}/generation/{generation_id}/{generation_asset_id}{ext}
```

Reads use `storage.resolve(persisted_backend)` (so a cross-backend generation object is read
where it actually lives, W8.5b.5); writes use `storage.active()` (the configured media write
backend). All storage I/O happens **outside** any DB transaction (no lock held across
network/file I/O) — the `IngestGeneratedMedia` discipline.

---

## 5. Idempotency (AP5)

The deterministic key makes re-promotion safe: the second attempt writes identical bytes and
`media.add` raises `ConflictError` on the existing
`uq_media_assets_storage_backend_storage_bucket_storage_key` constraint; the use case catches
it and returns the already-promoted asset via `get_by_storage_coords(...)` (`status="noop"`).
No duplicate media, no new constraint, no migration — the exact ingestion/render precedent.

---

## 6. API surface (the trigger)

Media is **top-level and owner-scoped** (α6.2 Q1 — not nested under a project). The promotion
endpoint follows suit:

- **`POST /api/v1/media/promotions`** → **201** (first promotion) / **200** (idempotent
  replay), authenticated via `CurrentUserDep`.
- **Body:** `{ generation_id: UUID, project_id: UUID }`.
- **Response:** the created/existing `MediaPublic` (the promoted video asset), reusing the
  existing media DTO + envelope.
- **Errors (existing centralized handlers):** `404` unknown generation / foreign or unknown
  project; `422` generation not completed / no final video; `409` never surfaces to the
  client (mapped internally to the idempotent replay).

No subscriber, no auto-promotion (AR1) — promotion is always user-initiated, which is what
makes request-time ownership (AR2) well-defined.

---

## 7. Composition wiring (`app/core/container.py` + `app/api/v1/deps.py`)

- **Container:** a `get_promote_generation_assets_use_case()` factory composing the new
  `GenerationReader` (from the execution `async_sessionmaker`), the UoW factory, and the
  `IStorageResolver` — mirroring `get_ingest_generated_media_use_case()`.
- **API deps:** a `PromoteGenerationAssetsDep` in `deps.py` (mirroring `RegisterMediaDep`).
- **Router:** the new endpoint added to `app/api/v1/routers/media.py` (already the media
  router); no new router registration in `main.py` beyond what already includes media.

No change to `ProcessPublishJob`, render/export workers, the execution runtime, or any other
composition path.

---

## 8. Enforcement — a new import-linter contract cements X8 (AP1)

Today X8 is governance/review-enforced only (grounding §4). This slice adds the first
**mechanical** guard, additive to `backend/pyproject.toml`:

- **"The Execution Runtime never writes the media library (X8)"** — `forbidden`:
  `source_modules = ["app.application.use_cases.generation",
  "app.infrastructure.generation"]`, `forbidden_modules = ["app.domain.media",
  "app.application.use_cases.media", "app.infrastructure.repositories.media_repository"]`.

The bridge itself is unaffected — `PromoteGenerationAssets` lives in `use_cases.media` (a
*consumer* of both sides), and the read path is the `IGenerationReader` port, so the execution
plane still imports nothing from the media context. A unit test additionally asserts the
execution runtime performs no media writes.

---

## 9. Testing & CI (full ephemeral-Postgres gate)

- **Unit (use case, fakes):** happy path (promoted asset shape + provenance + ownership);
  generation-not-found → 404; not-completed / no-final-video → 422; foreign/unknown project →
  404; idempotent replay (`ConflictError` → existing asset, `status="noop"`); byte-copy uses
  `resolve(persisted_backend)` for read and `active()` for write.
- **Unit (reader):** `load_final_video` maps the joined row → DTO; returns `None` for unknown
  / no-final-video generations.
- **Integration (ephemeral Postgres):** seed a `project` (+ owner/tenant), a `completed`
  `generations` row with a `generation_assets(kind='video')` final asset and real bytes in a
  local object store; run promotion → assert one `media_assets(source='generated')` row with
  correct ownership, project link, coordinates, checksum, and `source_metadata`; re-run →
  idempotent (still one row). This lands under `tests/integration/infrastructure/…` and is
  added to the existing gate stage that runs the generation + media integration suites.
- **Enforcement:** `lint-imports` green with the new X8 contract; the no-media-write test.
- **Migration roundtrip:** unchanged (no new migration) — `upgrade → downgrade → upgrade` and
  `validate_schema.py` must still pass with zero drift.

The complete ephemeral-Postgres gate (all stages: migration roundtrip, integration suites,
unit suites, `ruff`/`black`/`mypy`, `lint-imports`, schema validation) must pass before
release review.

---

## 10. Design decisions (AP1–AP9)

- **AP1 — Explicit promotion use case, X8 mechanically guarded.** `PromoteGenerationAssets`
  (the ADR-0046 X8 `PublishGenerationAssets` seam) is the sole path; a new import-linter
  contract forbids the execution plane from importing the media context (§8).
- **AP2 — Request-time ownership from the authenticated caller.** `(tenant_id,
  owner_user_id)` come from the session; `project_id` is required and validated as owned
  (`get_owned`) and used as the media link. This is the user-initiated adaptation of
  `IngestGeneratedMedia(project_id=…)` — same principle (resolve at request time, nothing
  stored on execution tables, AR2), with the authoritative owner being the caller because
  promotion is user-triggered (unlike the headless post-workflow ingestion).
- **AP3 — One new read-only port only.** `IGenerationReader`; `IExecutionRuntimeStore`,
  `IMediaRepository`, `IProjectRepository`, `IStorageResolver`, `IObjectStorage` are all
  reused unchanged (`add`, `get_owned`, `get_ownership`, `get_by_storage_coords`, `active`,
  `resolve`, `get`, `put` already exist).
- **AP4 — Copy bytes, never reference** the execution object (§4).
- **AP5 — Idempotent by deterministic key** via the existing storage-coords constraint (§5).
- **AP6 — Additive & migration-free** (Gate 5): provenance via `source_metadata`, no schema
  change.
- **AP7 — Final video only.** This slice promotes the `completed` generation's
  `final_video_asset_id` (`kind='video'`). Thumbnail/frames/audio/other kinds are deferred.
- **AP8 — Reaches the library, not export/publish** (AR1): no render/export/publish/Timeline
  changes; a promoted video reaches export/publish only through the existing Path A.
- **AP9 — Project-asserted, generation-unowned (the known limitation).** Because
  `generations` carry no ownership (ADR-0046 Q1), promotion authorizes the **project** (owned
  by the caller) but cannot bind the **generation** to an owner. Binding generation ownership
  is deferred to the future slice that gives `GenerateVideo` a user-facing trigger. Current
  mitigation: Path B has **no** user-facing creation path today (generations are produced
  only by scripts/tests), and `generation_id` is a random UUID. Recorded as a documented
  constraint the reviewer may elect to escalate (§11).

---

## 11. ADR assessment (per the workflow: "ADR only if genuinely required")

**Conclusion: no new ADR is required.** Rationale:

- The use case is **already sanctioned** by ADR-0046 X8 — this slice *implements* the named
  seam rather than deciding a new boundary.
- **No frozen contract changes:** no schema/migration, no port change to any existing seam,
  no change to execution/render/export/publish or ADR-0037/0043/0046/0047/PUB-1.
- The slice **strengthens** an existing decision (the first mechanical X8 guard, §8) — a
  consequence of ADR-0046, not a new decision.
- The one genuinely-new consideration — **promotion authorization** (AP9: project-asserted,
  generation-unowned) — is a documented **consequence** of ADR-0046 Q1 ("generation has no
  tenant/owner/project context yet"), not a new architectural choice, and is not yet
  exploitable (no user creates generations).

**Reviewer override:** if you consider "who may promote which generation" a boundary worth
**freezing now** (rather than at the future generation-trigger slice), that is the single
thing that would warrant a small ADR. Per your instruction I would then **stop and propose it
before implementation**. My recommendation is to record AP9 as a documented constraint in
this pre-flight and defer the freeze to the slice that actually establishes generation
ownership — but this is your call at review.

---

## 12. Increment plan (implementation order — on approval)

1. **Read port + reader** — `IGenerationReader` (`application/interfaces/`) + raw-SQL
   `GenerationReader` (`infrastructure/generation/`) + unit tests.
2. **Use case** — `PromoteGenerationAssets` (`application/use_cases/media/`) + unit tests
   (happy / not-found / not-completed / foreign-project / idempotent-replay).
3. **API** — request schema, `PromoteGenerationAssetsDep`, `POST /api/v1/media/promotions`.
4. **Wiring** — `get_promote_generation_assets_use_case()` container factory.
5. **Enforcement** — the X8 import-linter contract (§8) + the no-media-write test.
6. **Integration test** — ephemeral-Postgres promotion (promote + idempotent replay).
7. **Version → `-dev`** — bump `app/main.py` to the `-dev` slice version (§13).
8. **Full ephemeral-Postgres gate** (all stages green) → single `-dev` feature commit →
   release-review PR → **stop** (no finalise/tag without approval).

---

## 13. Versioning & roadmap label

- **Roadmap label (proposed):** **α8.8 — Asset Promotion Bridge** (the next free α8
  increment; explicitly **not** α8.6d / publish notifications). Confirmable at review.
- **App version:** **`0.4.39-phase3-alpha8.8`**, held at **`0.4.39-phase3-alpha8.8-dev`**
  throughout implementation (the `0.4.39` monotonic part is authoritative regardless of the
  label).

---

## 14. Non-goals / explicitly deferred

- **No export/publish/render/Timeline changes** — a promoted video reaches them only via the
  existing Path A (AR1/AP8).
- **No subscriber / auto-promotion** — always user-initiated (AR2).
- **No promotion of non-final artefacts** — frames/masks/audio/thumbnail/metadata deferred
  (AP7).
- **No generation ownership model / no user-facing generation trigger** — deferred to a
  future slice; AP9 documents the interim limitation.
- **No migration, no new domain type, no change to existing ports** — one new read-only port
  only.
- **No AI metadata, scheduling, analytics, notifications, or creator UX.**
- **No change to any frozen path, contract, or ADR** — strictly additive (AR3).

---

> **Objective restated:** promote completed AI-generated outputs into `media_assets` (the X8
> seam), strictly additively, leaving execution/render/export/publish/Timeline unchanged. On
> approval, implementation proceeds in the §12 order on a feature branch held at the `-dev`
> version; the full ephemeral-Postgres gate must pass before the `-dev` release review,
> followed by the normal review → finalise → tag → documentation-sync workflow. **Stop after
> this pre-flight and wait for review.**
