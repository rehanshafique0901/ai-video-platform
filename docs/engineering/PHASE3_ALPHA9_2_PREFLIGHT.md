# α9.2 — Media Library Foundation — Pre-flight (design, pre-implementation)

> **Status:** Approved-for-review. Design blueprint for α9.2, grounded by
> [`PHASE3_ALPHA9_2_GROUNDING.md`](./PHASE3_ALPHA9_2_GROUNDING.md). **No code yet** — per the
> established workflow this pre-flight **stops for review before implementation.**
> **Baseline:** `v0.4.44-phase3-alpha9.1` (frozen; `main @ 693d584`).
> **Architectural-decision check:** the design stays entirely within existing
> ports-&-adapters / DI / API / pagination / OCC patterns and the boundary ADR-0037 already fixed
> for the Asset Library (Future Extensions **CR-8**) — **no new architectural decision is
> introduced, no ADR required** (see §11). Vector/semantic search + embedding generation — the one
> element that *would* need an ADR — is explicitly **out of scope / deferred** (see §1, §10).

---

## 1. Scope (what ships in α9.2)

A **deterministic, owner-scoped Media Library** built *over* already-registered `media_assets`,
turning the dormant `library_folders` / `library_assets` / `library_asset_projects` tables (all in
`0001_baseline.py`) into a usable browse / organise / tag / reuse surface. Strictly additive; **no
migration** (schema + all indexes already exist — grounding §3–§4).

**In scope:**
- **Folders:** create, rename, move (reparent), list (owner-scoped, keyset-paginated), soft-delete.
- **Library entries:** add a registered media asset to the library (one entry per asset,
  `uq_library_assets_media_asset_id`), browse (keyset-paginated; filter by folder and by tags via
  the existing GIN index), get, update (name/description/tags/folder — **OCC/412 fenced**),
  soft-delete.
- **Reuse tracking:** record that a library asset was used in a project — increment `usage_count`,
  stamp `last_used_at`, upsert `library_asset_projects` (idempotent per `(asset, project)`).

**Explicitly OUT of scope (deferred to a future increment + its own ADR):**
- **Vector / semantic search and embedding *generation*.** No embedding provider/adapter exists
  (grounding §6); populating `library_assets.embedding` needs a **new, non-deterministic external
  provider** — the natural ADR trigger. The `embedding` column + HNSW index stay **dormant and
  untouched** this slice. (No new pgvector column → `test_metadata` guard untouched.)
- **Pagination-index migration** (see §10 — deferred, additive-if-needed optimisation).

---

## 2. Domain entities (new — `app/domain/library/`)

Frozen dataclasses mirroring `MediaAsset` / `Project` (no ORM, no I/O). `LibraryAsset` carries
`version` (it wraps a `VersionMixin` row — ADR-0037 CR-8); `LibraryFolder` does not.

```python
# app/domain/library/library_folder.py
@dataclass(frozen=True, slots=True)
class LibraryFolder:
    id: UUID
    tenant_id: UUID
    owner_user_id: UUID
    parent_folder_id: UUID | None
    name: str
    created_at: datetime
    updated_at: datetime

# app/domain/library/library_asset.py
@dataclass(frozen=True, slots=True)
class LibraryAsset:
    id: UUID
    tenant_id: UUID
    owner_user_id: UUID
    media_asset_id: UUID
    library_folder_id: UUID | None
    name: str
    description: str | None
    tags: tuple[str, ...]
    usage_count: int
    last_used_at: datetime | None
    version: int
    created_at: datetime
    updated_at: datetime
    # `embedding` is deliberately NOT modelled — dormant this slice.
```

---

## 3. Repository port + UoW wiring (mirrors `IMediaRepository` / `IProjectRepository`)

New `ILibraryRepository` in `app/application/interfaces/repositories.py`; declared
`library: ILibraryRepository` on `IUnitOfWork`; `cast(...)` in `SqlAlchemyUnitOfWork.__aenter__`;
mirrored in the test UoW (`tests/integration/conftest.py`). Concrete
`app/infrastructure/repositories/library_repository.py` (SQLAlchemy `select`/`insert`/`update`,
excludes `deleted_at IS NOT NULL`). All methods **owner-scoped** (`tenant_id` + `owner_user_id`).

```python
class ILibraryRepository(ABC):
    # --- folders ---
    async def add_folder(self, *, tenant_id, owner_user_id, parent_folder_id, name) -> LibraryFolder: ...
    async def get_folder(self, folder_id, tenant_id, owner_user_id) -> LibraryFolder | None: ...
    async def list_folders(self, tenant_id, owner_user_id, *, parent_folder_id: UUID | None | _Unset,
                           limit: int, after: tuple[datetime, UUID] | None) -> list[LibraryFolder]: ...
    async def update_folder(self, folder_id, tenant_id, owner_user_id,
                            changes: Mapping[str, Any]) -> LibraryFolder | None: ...
    async def soft_delete_folder(self, folder_id, tenant_id, owner_user_id) -> bool: ...
    # --- assets ---
    async def add_asset(self, *, tenant_id, owner_user_id, media_asset_id, library_folder_id,
                        name, description, tags) -> LibraryAsset: ...            # ConflictError on dup media
    async def get_asset(self, asset_id, tenant_id, owner_user_id) -> LibraryAsset | None: ...
    async def list_assets(self, tenant_id, owner_user_id, *, folder_id: UUID | None | _Unset,
                          tags: tuple[str, ...] | None, limit: int,
                          after: tuple[datetime, UUID] | None) -> list[LibraryAsset]: ...
    async def update_asset(self, asset_id, tenant_id, owner_user_id, *, expected_version: int,
                           changes: Mapping[str, Any]) -> LibraryAsset | None: ...   # None = CAS miss
    async def soft_delete_asset(self, asset_id, tenant_id, owner_user_id) -> bool: ...
    async def record_use(self, asset_id, tenant_id, owner_user_id, *, project_id) -> LibraryAsset | None: ...
```

- **`list_assets` tag filter:** ANY-of match over `tags` using the existing GIN index
  (`library_assets.tags && :tags` array-overlap); keyset order `(created_at desc, id desc)` +
  `limit+1` (mirrors `list_projects`). `folder_id` uses a three-state sentinel (unset = all,
  `None` = unfiled, UUID = that folder).
- **`update_asset` (OCC):** CAS `... WHERE id=:id AND version=:expected_version` returning the
  bumped row; `None` on miss (stale version / concurrent delete) → **412** in the use case
  (mirrors `update_project`).
- **`record_use`:** upsert `library_asset_projects (asset,project) ON CONFLICT DO NOTHING`;
  increment `usage_count`, set `last_used_at = now()`. Idempotent per pair (junction PK).

---

## 4. Use cases (new — `app/application/use_cases/library/`)

One use case per operation, mirroring the Projects/Media style (each opens the UoW, is
owner-scoped, commits only on write):

| Use case | Writes? | Notable rules |
|---|---|---|
| `CreateLibraryFolder` | yes | parent (if given) must be owner's live folder → else 404/422; name unique per parent → 409 |
| `ListLibraryFolders` | no | keyset page; optional `parent_folder_id` filter |
| `GetLibraryFolder` | no | 404 if missing/not-owned/soft-deleted |
| `UpdateLibraryFolder` | yes | rename / reparent; **cycle guard** (target ≠ self and ≠ descendant → 422); self-parent also blocked by DB `CheckConstraint` |
| `DeleteLibraryFolder` | yes | soft-delete; children `library_folder_id` handled per §7 |
| `AddLibraryAsset` | yes | `media_asset_id` must be caller's live media (`uow.media.get_owned`) → else 404; folder (if given) must be owner's → else 404; duplicate media entry → **409**; default `name`← media-derived |
| `ListLibraryAssets` | no | keyset page; `folder_id` + `tags` filters (GIN) |
| `GetLibraryAsset` | no | 404 gate |
| `UpdateLibraryAsset` | yes | OCC-fenced (404-before-412); mutable = `name`/`description`/`tags`/`library_folder_id` |
| `DeleteLibraryAsset` | yes | soft-delete; idempotent |
| `RecordLibraryAssetUse` | yes | asset + project must be owner's; idempotent upsert (§3) |

Cross-context read is one-directional and legal: Library reads Media via the existing
`uow.media.get_owned` (Library sits *over* Media — ADR-0037 CR-8); it never writes `media_assets`.

---

## 5. API surface (new router `app/api/v1/routers/library.py`, prefix `/library`)

Authenticated via `CurrentUserDep`; envelope + keyset `?limit=&cursor=` exactly like
`routers/projects.py` / `routers/notifications.py`. Schemas in `app/api/v1/schemas/library.py`
(`LibraryFolder*`, `LibraryAsset*` request/public DTOs + `_to_public`).

| Method / path | Handler | Success | Failures |
|---|---|---|---|
| `POST /library/folders` | create folder | 201 | 404 (parent), 409 (name), 422 |
| `GET /library/folders` | list (`?parent_folder_id=&limit=&cursor=`) | 200 | 422 (bad cursor) |
| `GET /library/folders/{id}` | get | 200 | 404 |
| `PATCH /library/folders/{id}` | rename/move | 200 | 404, 409, 422 (cycle) |
| `DELETE /library/folders/{id}` | soft-delete | 204 | 404 |
| `POST /library/assets` | add media→library | 201 | 404 (media/folder), 409 (dup), 422 |
| `GET /library/assets` | browse (`?folder_id=&tags=&limit=&cursor=`) | 200 | 422 |
| `GET /library/assets/{id}` | get | 200 | 404 |
| `PATCH /library/assets/{id}` | update (body carries `version` fence) | 200 | 404, **412**, 409, 422 |
| `DELETE /library/assets/{id}` | soft-delete | 204 | 404 |
| `POST /library/assets/{id}/uses` | record use (`{project_id}`) | 200 | 404 |

`PATCH /library/assets/{id}` mirrors `update_project`: body `{ version, name?, description?, tags?,
library_folder_id? }`, `model_dump(exclude_unset=True, exclude={"version"})` → tri-state changes,
`expected_version=body.version`, 404-before-412.

---

## 6. DI + router registration (mirrors α8.9c/α9.0 factories)

- `app/core/container.py`: one `get_*_use_case()` factory per use case (fresh UoW per call), e.g.
  `get_add_library_asset_use_case() -> AddLibraryAsset: return AddLibraryAsset(uow=get_unit_of_work())`.
- `app/api/v1/deps.py`: `XxxDep = Annotated[Xxx, Depends(container.get_xxx_use_case)]` grouped under
  a `# ---- Use-case dependencies (Slice α9.2 — Media Library) ----` comment.
- `app/main.py`: `from app.api.v1.routers import ... library` + `include_router(library.router,
  prefix="/api/v1")`.
- No new config, no new external dependency, no provider registry involvement.

---

## 7. Semantics to fix (design rulings, not architecture)

1. **Add-to-library trigger:** **explicit** `POST /library/assets` (owner opt-in). Duplicate media
   → **409 CONFLICT** (not silent return), consistent with the storage-coords 409 precedent
   (ADR-0037 D6).
2. **Underlying media soft-deleted:** `media_asset_id` is `RESTRICT NOT NULL`, but media uses *soft*
   delete, so RESTRICT never fires. **Library browse/get join-filters out entries whose media is
   soft-deleted** (`media_assets.deleted_at IS NULL`), so a deleted asset silently disappears from
   the library without an integrity error. (Query rule; no schema change.)
3. **Folder move cycle guard:** application walks the ancestor chain (owner-scoped) to forbid moving
   a folder under itself or a descendant → 422; DB `CheckConstraint("id <> parent_folder_id")`
   backstops self-parent.
4. **Folder soft-delete:** soft-deletes only the target folder; its `library_assets` keep their now
   dangling `library_folder_id` **or** are set to `NULL` (unfiled). Ruling: **set contained assets’
   `library_folder_id = NULL`** (assets are not deleted with the folder — reuse must survive
   reorganisation). Child *folders* are not cascade-deleted in v1 (owner must empty first) — keeps
   the operation bounded and deterministic.
5. **Tag semantics:** tags are lower-cased + trimmed + de-duplicated on write; browse `?tags=`
   filter is **ANY-of** (array overlap) over the GIN index. Empty tag list clears tags.
6. **Reuse trigger:** explicit `POST /library/assets/{id}/uses {project_id}` in v1 (deterministic,
   idempotent). Automatic stamping from the timeline/clip path is deferred (additive follow-up).

---

## 8. Failure semantics (summary)

| Condition | Result |
|---|---|
| Not owned / missing / soft-deleted (any entity) | `404 NOT_FOUND` (uniform, α5b visibility-before-concurrency) |
| Stale `version` on asset PATCH (or concurrent bump/delete race) | `412 VERSION_CONFLICT` |
| Duplicate media in library / folder-name collision under parent | `409 CONFLICT` |
| Empty patch / bad field / missing `version` / bad cursor / cycle move | `422` (Pydantic/FastAPI or explicit) |
| Unauthenticated | `401` via `CurrentUserDep` |

---

## 9. Testing plan

**Unit (`pytest -m unit`, `tests/unit/application/use_cases/library/`):** each use case against a
`FakeLibraryRepository` + `FakeUnitOfWork` — happy path, owner isolation (→404), OCC 412 on stale
version, duplicate-media 409, folder-name 409, cycle-move 422, tag normalisation, idempotent
`record_use`, multipage keyset walk (mirrors `test_list_projects`). Plus DTO validation tests
(`schemas/library.py`).

**Integration (new CI Stage 20, `requires_db=True`,
`tests/integration/infrastructure/library/`):** seed a unique user + media asset(s) + project via a
committed UoW; exercise the real `LibraryRepository` + endpoints and assert: add→browse round-trip;
keyset pagination + `next_cursor`; tag GIN filter (ANY-of); folder create/move/soft-delete with the
`library_folder_id=NULL` rule; OCC 412 under a concurrent bump; owner isolation (another user’s
asset → 404); soft-deleted-media entry hidden from browse; idempotent `record_use` (usage_count and
junction). FK-safe `_cleanup` in `finally` (mirrors `dashboard/test_creator_dashboard.py`).
Determinism confirmed by repeated + reordered runs.

**CI gate:** add **Stage 20** "media library integration" to `backend/scripts/ci_gate.py`
(docstring + `_stages()`), pointing at `tests/integration/infrastructure/library/`.

---

## 10. Migration assessment → **none**

- **No table/column/enum change.** `library_folders`, `library_assets`, `library_asset_projects`
  and every index (incl. `tags` GIN, `last_used_at` partial, folder partial-unique) already exist
  in `0001_baseline.py`. The `embedding` column + HNSW index remain **dormant/untouched** → the
  `test_metadata` pgvector guard and `validate_schema` pgvector check stay green **with no edits**.
- **ERD/schema-validator:** derive from the *unchanged* ORM → no drift introduced. (The pre-existing
  `schema.md §13` / `INDEX_STRATEGY.md` documentation drift noted in grounding §5 is corrected in the
  **documentation-sync** step, not implementation — no ADR.)
- **Keyset pagination index (deferred, additive):** `library_assets` browse orders by
  `(created_at,id)` filtered by `(tenant_id, owner_user_id)`; the existing
  `ix_library_assets_tenant_id_owner_user_id` backs the filter. A dedicated
  `(tenant_id, owner_user_id, created_at, id)` index (as projects added in `0008`) is a **future
  additive optimisation** — omitted here to keep α9.2 migration-free (consistent with
  `INDEX_STRATEGY.md`’s "add when profiling shows need"). Purely a performance decision; no
  correctness or boundary impact.

---

## 11. Architectural-decision check → **no new decision, no ADR**

Every element is a standard, precedented pattern — domain dataclasses, an ABC repository port on the
UoW, per-operation use cases, `CurrentUserDep` endpoints, keyset pagination, OCC/412, owner-scoping —
operating **inside** the boundary ADR-0037 already fixed for the Asset Library (Future Extensions
**CR-8**: "`library_assets` (its own `VersionMixin`), folders, tags, embeddings/HNSW, usage counters
over registered media"). No new plane, no external service, no schema change, no frozen contract
touched (ADR-0046 X8 is irrelevant — Library only *reads* `media_assets`). The rulings in §7 are
additive, reversible product decisions, not architectural boundaries. The **only** element that would
require an ADR — the embedding/vector-search plane — is explicitly **deferred** (§1). **No stop
required beyond this pre-flight review.**

---

## 12. Files touched (all additive unless noted)

**New:** `app/domain/library/{__init__,library_folder,library_asset}.py`;
`app/application/use_cases/library/*.py` (11 use cases + `__init__`);
`app/infrastructure/repositories/library_repository.py`;
`app/api/v1/schemas/library.py`; `app/api/v1/routers/library.py`; unit + integration test modules.
**Edited (additive):** `app/application/interfaces/repositories.py` (`ILibraryRepository`);
`app/application/interfaces/unit_of_work.py` (`library:`); `app/infrastructure/uow/sqlalchemy_unit_of_work.py`
(wire repo); `app/core/container.py` (factories); `app/api/v1/deps.py` (deps);
`app/main.py` (include router); `backend/scripts/ci_gate.py` (Stage 20); `CHANGELOG.md`; docs
(`schema.md §13` + `INDEX_STRATEGY.md` drift fix + `SYSTEM_MAP.md` + `PLATFORM_STATUS.md` at the
documentation-sync step).
**Not touched:** `media_assets` schema/ORM, all generation/render/export/publish runtimes, the
dormant `embedding` column/HNSW index, the provider registry.

---

## 13. Stop

Pre-flight complete; grounding surfaced **no** genuine architectural decision, so no ADR was
authored. Per the established workflow, **stopping for review before implementation.** On approval I
will implement α9.2 exactly as specified, then run the full ephemeral PostgreSQL gate and open the
`-dev` release-review PR.
