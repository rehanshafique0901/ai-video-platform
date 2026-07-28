# α9.2 — Media Library Foundation — Grounding

> **Status:** Read-only grounding. **Facts only** — no design, no schema changes, no
> implementation. This document establishes what exists, what is missing, and whether a
> genuine architectural decision (ADR) is required before the pre-flight.
>
> **Baseline:** `v0.4.44-phase3-alpha9.1` (frozen). `main @ 693d584`, working tree clean,
> `main == origin/main`. Nothing in the repository was modified to produce this document.
>
> **Method:** Direct inspection of `backend/` (domain / application / infrastructure / api),
> Alembic migrations `0001`–`0015`, ORM models, engineering contracts, ADR-0037, and the
> pgvector guard tests. Every "exists / does not exist" claim is grounded in a named file,
> table, column, index, port, route, or ADR clause.

---

## 1. Slice selection (why this slice, now)

The authoritative discovery report (`docs/engineering/NEXT_VERTICAL_SLICES_DISCOVERY.md`)
ranked a **top 5**. Cross-referenced against everything shipped since that report's baseline
(`α8.6c`):

| Rank | Slice | Status |
|---|---|---|
| #1 | Asset promotion bridge (§2.10) | **Shipped — α8.8** |
| #2 | Publish notifications (§2.1) | **Shipped — α8.9a** |
| #3 | Scheduling (§2.2) | **Shipped — α8.9b** |
| #4 | AI caption & hashtag generation (§2.3) | **Shipped — α9.1** |
| #5 | **Media library (§2.9)** | **Not implemented — this slice** |

Runners-up creator dashboard (§2.8 → α8.9c) and analytics (§2.9-minor → α9.0) also shipped.

**Media Library (§2.9) is the highest-ranked remaining top-5 slice.** No re-ranking is
warranted:

- The only candidate with arguably higher *raw* product value — **Additional destinations
  (§2.6, TikTok/Instagram)** — was *deliberately* excluded from the discovery's "start-now"
  set because its dominant risk is **external** (platform app-review / content-publishing API
  approval) and native OAuth is awkward without a UI. That external blocker is unchanged at
  α9.1; it does not fit the additive / deterministic / zero-external-blocker discipline.
- Media Library's value is **reinforced** by work since discovery: α8.8 now routes AI-generated
  output into `media_assets`, so assets accumulate with **still no way to browse, organise,
  search, or reuse** them (`ListMedia` is unpaginated, filter-only, no search — verified §3.4).

---

## 2. Grounding objectives → verdicts (at a glance)

| # | Objective | Verdict |
|---|---|---|
| 1 | Library schema exists in DB/ORM | **EXISTS** — `0001_baseline.py`; ORM in `models/media.py` |
| 2 | Any library code above ORM (domain/repo/use case/API) | **DOES NOT EXIST** |
| 3 | Migration genuinely required for a core slice | **NO** — tables + all indexes in `0001` |
| 4 | Media context reusable as substrate | **YES** — `MediaAsset`, `IMediaRepository`, `/api/v1/media` |
| 5 | Reusable seams (pagination / envelope / owner-scope / UoW / DI / CI) | **ALL EXIST** |
| 6 | Embedding-generation port/adapter exists | **DOES NOT EXIST** (only dormant `Vector(1536)` columns) |
| 7 | pgvector used in any runtime query today | **NO** (HNSW/GIN indexes created but unused by code) |
| 8 | Genuine architectural decision requiring an ADR | **NO — for the deterministic foundation scope** (see §7) |

---

## 3. What exists — the Media substrate to build over

### 3.1 `MediaAsset` aggregate — `app/domain/media/media_asset.py`
Frozen dataclass; generation output, **not** editorial content (ADR-0037). Owner-scoped
(`tenant_id` + `owner_user_id`). Physical-object fields and `kind`/`source` immutable after
register. **No `version` / no OCC** (media is last-writer-wins — ADR-0037 D3).

`kind ∈ {image, video, narration, subtitle, music, sound_effect, thumbnail}`,
`source ∈ {generated, uploaded, stock}` (`infrastructure/db/enums.py`).

### 3.2 `IMediaRepository` — `app/application/interfaces/repositories.py` (L728–929)
Owner-scoped surface: `add`, `list_owned` (filters `kind/source/project_id/scene_id`; **not
paginated**), `get_owned`, `update_owned` (last-writer-wins), `soft_delete_owned`,
`get_by_storage_coords`, `model_is_linkable`, `list_enrichable_generated_videos`. Concrete:
`infrastructure/repositories/media_repository.py` (SQLAlchemy `select`, excludes
`deleted_at IS NOT NULL`).

### 3.3 Media API — `app/api/v1/routers/media.py` (prefix `/media`)
`POST /media`, `POST /media/promotions`, `GET /media`, `GET /media/{id}`, `PATCH /media/{id}`,
`DELETE /media/{id}`. Schemas in `schemas/media.py`. Use cases in
`application/use_cases/media/` (register/get/list/update/delete + α8 ingest/enrich/promote).

### 3.4 `ListMedia` gap (confirmed)
`application/use_cases/media/list_media.py` docstring: *"Not paginated in α6.2."* Repository
`list_owned` has **no `limit`/`after`** and **no search** (`q`, `tags`). There is no way to
browse-by-page, tag, organise into folders, search, or track reuse today.

---

## 4. What exists — the pre-built Library schema (dormant)

All three tables + every index were created in **`alembic/versions/0001_baseline.py`**; ORM
in `app/infrastructure/db/models/media.py` (L116–213). No later migration touches them.

### 4.1 `library_folders` (`LibraryFolder`, L116–149)
`id`, `tenant_id`→`tenants` (RESTRICT), `owner_user_id`→`users` (RESTRICT),
`parent_folder_id`→`library_folders` (CASCADE, nullable), `name` (Text NOT NULL),
`created_at/updated_at/deleted_at`.
Constraints: `CheckConstraint(id <> parent_folder_id)`; partial-unique
`uq_library_folders_parent_folder_id_name (parent_folder_id, name) WHERE deleted_at IS NULL`;
`ix_library_folders_tenant_id_parent_folder_id`.

### 4.2 `library_assets` (`LibraryAsset`, L152–195) — **has `VersionMixin` (OCC)**
`id`, `tenant_id` (RESTRICT), `owner_user_id` (RESTRICT), `media_asset_id`→`media_assets`
(**RESTRICT, NOT NULL**), `library_folder_id`→`library_folders` (SET NULL, nullable),
`name` (NOT NULL), `description` (nullable), `tags text[]` (NOT NULL default `'{}'`),
`embedding vector(1536)` (nullable, dormant), `usage_count int` (NOT NULL default 0),
`last_used_at` (nullable), **`version`** (VersionMixin → OCC/412), timestamps + soft-delete.
Constraints/indexes: `uq_library_assets_media_asset_id` (**one library entry per media asset**),
`ix_library_assets_tenant_id_owner_user_id`, partial `ix_library_assets_last_used_at`,
GIN `ix_library_assets_tags_gin`, HNSW `ix_library_assets_embedding_hnsw (vector_cosine_ops)`
(last two emitted directly by `0001`).

### 4.3 `library_asset_projects` (`LibraryAssetProject`, L198–213)
M:N junction. Composite PK `(library_asset_id, project_id)`, both FKs CASCADE;
`first_used_at` (NOT NULL default `now()`). Tracks which projects reused an asset.

### 4.4 No code above the ORM (all confirmed missing)
`LibraryAsset`/`LibraryFolder` **domain aggregate** — **DOES NOT EXIST**.
`ILibraryRepository` — **DOES NOT EXIST**. `/library` routes — **DOES NOT EXIST**.
`application/use_cases/library/` — **DOES NOT EXIST**. Only ORM classes + an import smoke test
reference these tables.

---

## 5. Reusable seams (mirror targets — all present)

- **Keyset pagination:** `application/pagination.py` (`Cursor(created_at,id)`, `Page[T]`,
  base64url `encode/decode_cursor`, bad token → 422). Canonical use case:
  `use_cases/projects/list_projects.py`; repo predicate `project_repository.py` (`tuple_(...) <
  (created_at,id)`, `limit+1`).
- **Envelope + owner-scope:** `api/v1/helpers.py` (`envelope`, `meta.next_cursor`),
  `CurrentUserDep` (`deps.py`). Representative router: `routers/notifications.py`
  (`?limit=&cursor=`), `routers/projects.py` (tenant+owner).
- **Repository + UoW wiring:** add `IXxxRepository` to `interfaces/repositories.py`; declare
  `xxx: IXxxRepository` on `IUnitOfWork` (`interfaces/unit_of_work.py`); `cast(...)` in
  `SqlAlchemyUnitOfWork.__aenter__` (`infrastructure/uow/sqlalchemy_unit_of_work.py`); mirror
  in test UoW (`tests/integration/conftest.py`).
- **OCC/412:** established for `projects` (VersionMixin) — directly applicable to
  `library_assets.version` for `PATCH`/move/rename.
- **DI + routing:** `container.get_*_use_case()` factories → `deps.py`
  `Annotated[..., Depends(...)]` → `main.py` `include_router(..., prefix="/api/v1")`.
- **Tests + CI:** unit under `tests/unit/application/use_cases/<domain>/`; integration under
  `tests/integration/{infrastructure/repositories,api}`; each slice adds one numbered
  `ci_gate.py` stage (`requires_db=True`) — latest is **Stage 19**; next would be **Stage 20**.
- **Migration convention:** head is `0015_analytics_events_source_event_id`; a *new* migration
  (only if we add indexes/columns) would be `0016_<desc>.py`.

---

## 6. Embeddings / vector search — the deferral boundary

- **No embedding-generation port or adapter exists** anywhere in `app/` (searched
  `IEmbed*`, `embedding`, `pgvector`, `Vector(`). The `embedding vector(1536)` column and its
  HNSW index are **dormant** — created in `0001`, never written or queried by code.
- **pgvector guard:** `tests/test_metadata.py::test_pgvector_columns_are_scoped` allows exactly
  `{("agent_memory","embedding"), ("library_assets","embedding")}` and asserts *"Add a new ADR
  before introducing additional embedding columns."* Using the **already-approved**
  `library_assets.embedding` needs **no** ADR; adding new embedding columns would.
- **Consequence for scope:** *populating* `embedding` and doing vector/semantic search requires
  a **new, non-deterministic embedding provider** (external cost, latency, provenance,
  determinism concerns) — analogous to the α9.1 AI-plane integration and its ADR-0049. That is
  the natural ADR trigger. It is **cleanly separable** and **deferred** from this slice.

---

## 7. Architectural-decision check → **no ADR required (foundation scope)**

The proposed foundation slice — folders (create/rename/move/list/soft-delete), library entries
(add media→library, list/browse with **keyset pagination**, get, rename/describe, move between
folders, tags add/remove, **tag-filtered browse via the existing GIN index**, usage tracking,
project-association tracking), all **owner-scoped**, reusing OCC/envelope/pagination — surfaces
**no genuine architectural decision**, because:

1. **The library is already architecturally sanctioned.** ADR-0037 "Future Extensions"
   explicitly reserves **CR-8 — Asset Library**: *"`library_assets` (its own `VersionMixin`),
   folders, tags, embeddings/HNSW, usage counters over registered media."* The bounded-context
   shape (library wraps registered media 1:1 via `uq_library_assets_media_asset_id`) is
   pre-decided, not a new decision.
2. **No schema change.** Tables + all indexes exist in `0001`. No migration → no
   `test_metadata`/`validate_schema`/ERD boundary is crossed. (Doc drift in `schema.md` §13 /
   `INDEX_STRATEGY.md` is pre-existing and only needs the documentation-sync step, not an ADR.)
3. **No new plane, port class, or external dependency.** It is a standard owner-scoped
   CRUD+browse slice mirroring Projects/Notifications/Media — the exact pattern shipped many
   times with zero freeze overrides.
4. **No frozen contract touched.** ADR-0046 X8 (execution must not write `media_assets`) is
   irrelevant — the library only *reads* existing `media_assets` and writes `library_*`.
   ADR-0037 D-clauses are respected (library is a sibling *over* media, with its own OCC).
5. **The one thing that *would* need an ADR — the embedding/vector-search plane — is
   explicitly deferred** (§6) and delivers a clean, self-contained later increment.

**Therefore, per the standard workflow, grounding does not uncover a genuine architectural
decision requiring an ADR, and this proceeds automatically to the α9.2 pre-flight.** The
pre-flight will fix the exact scope (including the vector-search deferral), the API shape,
folder/tag/OCC semantics, the media↔library FK/soft-delete interaction, the migration
assessment (expected: none), the test plan, and the CI stage.

---

## 8. Open questions for the pre-flight (design, not architecture)

1. **Add-to-library trigger:** explicit `POST /library/assets {media_asset_id}` (recommended,
   owner opt-in) vs. auto-mirror on media register. `uq_library_assets_media_asset_id` enforces
   one entry per asset → idempotent add / 409-or-return-existing semantics to decide.
2. **Soft-delete interaction:** `media_asset_id` is RESTRICT NOT NULL, but media uses *soft*
   delete (`deleted_at`), so RESTRICT never fires. Decide whether library browse hides entries
   whose underlying media is soft-deleted (join filter) — a query rule, not a schema change.
3. **Folder tree depth / move validation:** self-parent already blocked by
   `CheckConstraint`; cycle prevention on move is an application rule to specify.
4. **Tag search semantics:** ANY-of vs ALL-of tag match over the GIN index; casing/normalisation.
5. **Usage tracking:** when `usage_count`/`last_used_at`/`library_asset_projects` are stamped
   (e.g. on timeline/clip use vs. explicit "mark used") — scope to a deterministic trigger.
6. **Scope line:** confirm vector/semantic search + embedding generation are **out** of α9.2
   (own future increment + ADR).

---

## 9. What this document is not

No design, no schema, no API shapes, no ADR, no code. It selects the slice per the authoritative
roadmap, verifies the baseline, and establishes that a deterministic Media Library **foundation**
is additive, migration-free, and free of any new architectural decision — clearing the path to
the α9.2 pre-flight.
