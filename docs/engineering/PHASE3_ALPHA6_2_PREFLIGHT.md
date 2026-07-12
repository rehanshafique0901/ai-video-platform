# Phase 3 Slice α6.2 — Media Asset Aggregate — Pre-flight

> Status: **SIGNED OFF — α6.2 READY.** **Q1 = top-level `/media`** (owner-scoped,
> `project_id` an optional link/filter); **Q2 = register-by-metadata only** (no
> provider/storage/presigned URLs); **Q3 = reuse ADR-0036** (no OCC, no
> `projects.version` bump, excluded from snapshots/restore/diff); **Q8 = narrow
> PATCH** (mutable = the four links + `source_metadata`; storage coords /
> checksum / mime / size / dimensions immutable forever); **Q14 = new ADR-0037**
> (adopts ADR-0036's concurrency model, does not mutate it). **Q4–Q7, Q9–Q13
> accepted as drafted.** Branch `phase3/alpha6.2-media`; implementation follows
> §10.
>
> Mirrors the α5/α6.1 discipline: ground in the physical schema → lock
> decisions → sign-off → branch → implement → CI → merge → tag. Read-only
> planning artefact per `docs/engineering/RUNBOOK_WAVE.md` §1.
>
> **Predecessors.**
> α5c (`v0.4.7`) Scenes CRUD+reorder · α5d.1–3 (`v0.4.8`–`v0.4.10`) Version
> capture/read/restore/diff/branch + **Aggregate OCC Rule** · α6.1 (`v0.4.11`)
> **Prompt aggregate — generation-input CRUD** + **ADR-0036**.
>
> **Companion design docs.**
> * `docs/decisions/ADR-0036-prompts-generation-inputs.md` — the governing
>   principle: *"Project versions capture editorial state, not generation
>   inputs… Generated media may retain the prompt used for provenance
>   independently of the current prompt record."* **Q3 reuses this verbatim for
>   the generation-OUTPUT side.**
> * `docs/domain/PROMPT_AGGREGATE.md` — the immediate sibling; α6.2 is its
>   downstream (`media_assets.prompt_id` links a produced asset back to the
>   prompt that drove it).
> * `docs/domain/PROJECT_AGGREGATE.md` §6/§8 — parent aggregate; media is
>   **outside** the versioned snapshot (same as prompts).
>
> **Reference implementations to mirror.**
> * Owner-scoped soft-delete, idempotent-by-404 → α5c `DeleteScene` /
>   `soft_delete_owned`; α6.1 `DeletePrompt`.
> * Optional-link validation (foreign link → `422`, not `404`) → α6.1
>   `CreatePrompt` (`scenes.get_owned_scene`, `prompts.model_is_linkable`).
> * Tri-state PATCH via `model_fields_set`, `extra="forbid"` DTOs → α5b/α5c/α6.1.
> * DI factories + `deps` aliases + UoW child-repo wiring (real +
>   `_TestUnitOfWork` + `FakeUnitOfWork`) → α6.1 `.prompts`.
>
> **Baseline versioning.** `main` is at `0.4.11` (tag
> `v0.4.11-phase3-alpha6.1`). First α6.2 commit bumps `app/main.py` →
> `"0.4.12-phase3-alpha6.2-dev"`; release tag `v0.4.12-phase3-alpha6.2` drops
> `-dev` on merge.

---

## Section 1 — Scope

### 1.1 One-line thesis

α6.2 introduces the **Media Asset aggregate** — the first *generation-OUTPUT*
content — as a **register-by-metadata** CRUD surface backed by the existing
`media_assets` table. Unlike prompts/scenes, `media_assets` carries its **own
`tenant_id` + `owner_user_id`** (direct ownership) and a **nullable
`project_id`** (a media asset may or may not belong to a project). α6.2 does
**not** call any provider, upload bytes, or issue presigned URLs — the client
registers an asset it already holds storage coordinates for (`source ∈
{uploaded, stock}`); actual generation (`source = generated`) is α8. α6.2 ships
**zero migrations** (table + all indexes + the `(backend,bucket,key)` unique
constraint exist in baseline `0001`) and **reuses ADR-0036** (no OCC, no
`projects.version` bump, out of version snapshots).

### 1.2 What's in *(endpoint prefix pending Q1)*

1. **`POST …/media`** — register a media asset: required storage coordinates
   (`storage_backend`, `storage_bucket`, `storage_key`), `mime_type`,
   `size_bytes`, `checksum_sha256` (hex), `kind`, `source`; optional links
   (`project_id`, `scene_id`, `prompt_id`, `model_id`), optional dimensions
   (`width`, `height`, `duration_seconds`), optional `provider`,
   `source_metadata`. `201` + `MediaPublic`; duplicate storage coords → `409`.
2. **`GET …/media`** — list the caller's live assets, newest-first, filters
   `?kind=` `?source=` `?project_id=` `?scene_id=`. `200` + `{ data, meta }`.
3. **`GET …/media/{media_id}`** — single asset. `200` / `404`.
4. **`PATCH …/media/{media_id}`** — partial update of the **mutable** subset
   (Q8; storage coords / checksum / size are immutable). `200` / `404` / `422`.
5. **`DELETE …/media/{media_id}`** — owner-scoped soft delete, unconditional,
   `204`, idempotent-by-404.
6. **Domain:** `app/domain/media/media_asset.py` — frozen `MediaAsset` entity.
7. **`IMediaRepository`** + `SqlAlchemyMediaRepository`: `add`, `list_owned`
   (+ filters), `get_owned`, `update_owned`, `soft_delete_owned`,
   `model_is_linkable` (or reuse). All **owner-scoped** (tenant + owner_user).
8. **Use cases** (`app/application/use_cases/media/`): `RegisterMedia`,
   `ListMedia`, `GetMedia`, `UpdateMedia`, `DeleteMedia`.
9. **DTOs** (`app/api/v1/schemas/media.py`): `MediaRegisterRequest`,
   `MediaUpdateRequest` (tri-state, `extra="forbid"`), `MediaPublic`.
10. **Router** `app/api/v1/routers/media.py` (prefix per Q1); mounted in `main`.
11. **DI**: container factories + `deps.py` aliases; `.media` on the UoW
    (+ `_TestUnitOfWork`, + `FakeUnitOfWork`).
12. **Fakes**: `FakeMediaRepository`.
13. **Docs**: `API_CONTRACT.md` new Media subsection + resource-map row;
    `CHANGELOG.md`; version bump; `ROADMAP.md`; `docs/domain/MEDIA_AGGREGATE.md`;
    **ADR-0037** (or extend ADR-0036) recording the generation-output boundary.

### 1.3 Non-goals (explicit, will NOT ship in α6.2)

* **Provider generation** — no model calls, no inference; nothing *produces*
  bytes. `source = generated` end-to-end is α8.
* **Object storage** — no byte upload, no presigned/signed URLs, no download
  proxy, no storage-backend SDK. The client supplies storage coordinates for an
  object it already placed. `storage_key` is opaque text to α6.2.
* **Asset Library (CR-8)** — `library_assets`, `library_folders`,
  `library_asset_projects`, tags, embeddings/HNSW, usage counters are a
  **separate later slice**. α6.2 touches only the raw `media_assets` table.
* **Timeline placement** — clips referencing `media_asset_id` are α6.3.
* **Media in version snapshots** — media is a generation artefact, **not**
  captured/restored/diffed (ADR-0036 precedent, Q3).
* **Checksum verification** — α6.2 stores the client-supplied `checksum_sha256`
  as-is; it does not fetch the object to verify the digest (no storage access).
* **Re-keying** — storage coordinates + checksum + size are immutable after
  register (Q8); "the object moved buckets" is not a use case yet.
* **Migrations** — none. If the slice appears to need one, stop and re-scope.

### 1.4 Anti-scope-creep envelope

* *"Let /media generate an image from a prompt."* — No; that is α8 (provider
  seam). α6.2 registers assets that already exist.
* *"Issue a presigned upload URL."* — No; object-storage integration is
  deferred. α6.2 is metadata-only.
* *"Promote a media asset into the reusable library."* — No; CR-8 library is a
  later slice with its own aggregate (`library_assets` has its **own**
  `VersionMixin`, unlike `media_assets`).
* *"Add a `version` column so media gets OCC."* — No; that is a migration. Q3
  resolves concurrency within the existing schema (ADR-0036 precedent).
* *"Snapshot media into the project version."* — No; media is a generation
  output, out of the editorial snapshot.

---

## Section 2 — Foundational facts (grounded in the physical schema)

Read straight off `0001_baseline.py` + `models/media.py` + `enums.py`. These
are **not** decisions — they are the constraints α6.2 must honour.

### F1 — `media_assets` columns (baseline `0001`)
```
id                uuid PK default gen_random_uuid()
tenant_id         uuid NOT NULL REFERENCES tenants(id)   ON DELETE RESTRICT   -- DIRECT owner
owner_user_id     uuid NOT NULL REFERENCES users(id)     ON DELETE RESTRICT   -- DIRECT owner
kind              media_kind NOT NULL
project_id        uuid          REFERENCES projects(id)  ON DELETE SET NULL   -- NULLABLE
scene_id          uuid          REFERENCES scenes(id)    ON DELETE SET NULL   -- NULLABLE
prompt_id         uuid          REFERENCES prompts(id)   ON DELETE SET NULL   -- NULLABLE (provenance)
model_id          uuid          REFERENCES ai_models(id) ON DELETE RESTRICT   -- NULLABLE
provider          text                                                        -- NULLABLE
storage_backend   storage_backend NOT NULL
storage_bucket    text NOT NULL
storage_key       text NOT NULL
mime_type         text NOT NULL
size_bytes        bigint NOT NULL   CHECK (size_bytes >= 0)
width             integer                                                     -- NULLABLE
height            integer                                                     -- NULLABLE
duration_seconds  numeric(10,3)                                               -- NULLABLE
checksum_sha256   bytea NOT NULL
source            media_source NOT NULL
source_metadata   jsonb NOT NULL DEFAULT '{}'::jsonb
created_at        timestamptz NOT NULL DEFAULT now()
updated_at        timestamptz NOT NULL DEFAULT now()
deleted_at        timestamptz                                                 -- soft delete
UNIQUE (storage_backend, storage_bucket, storage_key)   -- uq_media_assets_storage_...
```
Indexes: `ix_media_assets_tenant_id_kind_created_at (tenant_id, kind,
created_at)`, `ix_media_assets_project_id`, `ix_media_assets_prompt_id`,
`ix_media_assets_checksum_sha256`.

### F2 — **Direct ownership** (contrast prompts/scenes).
`tenant_id` + `owner_user_id` are **on the row** and **NOT NULL**. Ownership is
**not** derived through `project_id` (which is nullable). → The owner gate reads
`WHERE id = :id AND tenant_id = :t AND owner_user_id = :u AND deleted_at IS
NULL`, **not** a project join. This is the α6.2-specific access pattern and the
crux of **Q1/Q4**. The leading index `(tenant_id, kind, created_at)` is built
for exactly this owner-scoped, kind-filtered, newest-first listing.

### F3 — **No row-OCC `version` column.**
`MediaAsset` uses `UUIDPrimaryKeyMixin + TimestampMixin + SoftDeleteMixin` — **no
`VersionMixin`** (whereas the sibling `LibraryAsset` **does** have it).
`media_assets` is **absent** from `_VERSION_BUMP_TABLES`; it has only
`tg_media_assets_biu_touch_updated_at` and is **not** in `_IMMUTABLE_TABLES`. →
No per-row concurrency token — same posture as `prompts` (Q3, ADR-0036).

### F4 — Storage identity is unique + mandatory.
`(storage_backend, storage_bucket, storage_key)` is a **UNIQUE** constraint and
all three + `mime_type` + `size_bytes` + `checksum_sha256` are **NOT NULL**. →
A media row *is* a pointer to a concrete stored object. Registering the same
coordinates twice must be a **`409 CONFLICT`** (Q6), not a silent second row.
`size_bytes >= 0` is a CHECK (Q7).

### F5 — Links degrade softly; `model_id` is RESTRICT.
`project_id` / `scene_id` / `prompt_id` are `ON DELETE SET NULL`; `model_id` is
`ON DELETE RESTRICT` (a model referenced by media cannot be hard-deleted — but
`ai_models` is a system registry, not user-deletable here). As with α6.1 F6,
**SET NULL fires only on a hard `DELETE`** of the parent; projects/scenes/
prompts are **soft-deleted**, so a media asset's links **survive** parent
soft-delete. Relevant to Q5 and the link-durability integration test.

### F6 — Downstream references are SET NULL (hard-delete only).
`clips.media_asset_id`, `render_jobs.output_media_asset_id`,
`export_jobs.output_media_asset_id` are `ON DELETE SET NULL`;
`library_assets.media_asset_id` is `ON DELETE RESTRICT`. α6.2 **soft-deletes**
(sets `deleted_at`), which trips **none** of these — downstream rows keep
pointing at a now-hidden asset. α6.2 does not create any of those rows, so this
is a forward-compatibility note, not a live interaction.

### F7 — `media_kind` (7) & `media_source` (3) & `storage_backend` (5) enums.
`media_kind = {image, video, narration, subtitle, music, sound_effect,
thumbnail}`; `media_source = {generated, uploaded, stock}`; `storage_backend =
{local, s3, r2, azure_blob, gcs}`. DTOs validate against exactly these sets.
`generated` is **rejected** on register in α6.2 (Q2 — no provider produced it).

---

## Section 3 — Implementation decisions (α6.2-specific, proposed)

> These follow from §2 + the α6.1 precedent. Load-bearing choices escalate to
> **Q1–Q14** in §7; the rest are mechanical mirrors.

### D1 — New `media` bounded context
Add `domain/media`, `use_cases/media`, `schemas/media.py`, `routers/media.py`,
`SqlAlchemyMediaRepository`. Router prefix decided by **Q1**.

### D2 — **Owner** visibility gate (single-level, direct — contrast α6.1)
Because ownership is on the row (F2), the gate is one level:
`get_owned(media_id, tenant_id, owner_user_id)` → live row or `None` → `404`
(anti-enumeration). *If Q1 = project-nested*, add a project gate in front
(project owned → else 404) and require `media.project_id == route project`.

### D3 — Slim domain over the physical row
Domain `MediaAsset = {id, tenant_id(internal), owner_user_id(internal), kind,
project_id, scene_id, prompt_id, model_id, provider, storage_backend,
storage_bucket, storage_key, mime_type, size_bytes, width, height,
duration_seconds, checksum_sha256, source, source_metadata, created_at,
updated_at}`. Frozen dataclass; `checksum_sha256` carried as `bytes` internally,
surfaced as lowercase hex in the DTO (Q7).

### D4 — DELETE = unconditional soft delete, `204`, idempotent-by-404
Exactly α6.1 D4: `soft_delete_owned` sets `deleted_at` on the caller's own live
asset; `False` → `404`. First delete `204`; repeat / subsequent `GET`/`PATCH`
→ `404`. No `projects.version` bump (ADR-0036).

### D5 — `updated_at` trigger-owned; no version co-set
`tg_media_assets_biu_touch_updated_at` sets `updated_at`; the repo never
hand-sets it and there is no `version` to co-bump (F3).

### D6 — List is side-effect-free, filtered, newest-first
Owner-scoped, ordered `(created_at DESC, id DESC)`, optional `?kind=`,
`?source=`, `?project_id=`, `?scene_id=` (validated; bad enum/UUID → `422`).
Uses the `(tenant_id, kind, created_at)` index. Empty → `200 []`.

### D7 — Register maps DB constraints to HTTP
`(backend,bucket,key)` unique violation → `409 CONFLICT` (D7/Q6); `size_bytes <
0`, bad enum, bad hex checksum, oversize metadata → `422` (Q7); foreign/unknown
link → `422` (Q5). Owner + tenant come from `CurrentUserDep`, never the body.

> **Concurrency (Q3), routing/ownership (Q1/Q4), create semantics (Q2), link
> validation (Q5), mutable-field set (Q8), and DTO shape (Q11) are decided in
> §7.**

---

## Section 4 — Acceptance criteria (behavioural, provisional on §7)

**A1. Register happy.** `POST …/media` with valid storage coords + `mime_type`
+ `size_bytes` + `checksum_sha256` (hex) + `kind` + `source` → `201` +
`MediaPublic`; links echoed (or `null`).
**A2. Register duplicate storage coords** (same backend+bucket+key) → `409
CONFLICT` (F4/Q6).
**A3. Register with links.** Valid `project_id` (owned), `scene_id` (live scene
in that project), `prompt_id` (live prompt in that project), `model_id` (exists,
not retired) → linked; any foreign/unknown/soft-deleted → `422` (Q5).
**A4. Register validation.** Missing required field, bad `kind`/`source`/
`storage_backend` enum, `source = generated` (Q2), `size_bytes < 0`, malformed
`checksum_sha256` (not 64-hex), forbidden field (`id`, `owner_user_id`,
`tenant_id`) → `422`.
**A5. Register link-consistency.** `scene_id`/`prompt_id` supplied **without**
`project_id` (or with a `project_id` they don't belong to) → `422` (Q5).
**A6. List happy + newest-first**, owner-scoped, `(created_at, id)` DESC.
**A7. List filters.** `?kind=`, `?source=`, `?project_id=`, `?scene_id=` narrow
correctly (AND-combined); bad enum/UUID → `422`.
**A8. List excludes soft-deleted; excludes other owners; empty → `200 []`.**
**A9. Get single happy / 404** (unknown / other owner / soft-deleted /
[other project iff Q1 = nested]).
**A10. PATCH happy.** Mutable field changed → `200`; `updated_at` advances.
**A11. PATCH partial** (absent unchanged); explicit-null on nullable clears;
immutable field (`storage_key`/`checksum_sha256`/`size_bytes`) present → `422`;
empty patch → `422`.
**A12. PATCH not-owned/unknown/soft-deleted → 404.**
**A13. DELETE happy / idempotent-by-404.** First `204`; second `404`;
`GET`/`PATCH` after → `404`; not-owned/unknown → `404`; no auth → `401`;
non-UUID path → `422`.

**Engineering (E1–E6):** CI gate green; **no new migration**; no new
`noqa`/`type: ignore`; unit coverage ≥ 80%; `import-linter` layering kept
(media mirrors prompts); schema validator + ERD unchanged (table + indexes +
unique constraint already exist).

---

## Section 5 — Test matrix (provisional)

### 5.1 Unit — use cases (fakes)
`RegisterMedia` happy / duplicate→409 / each-link-valid / each-link-foreign→422
/ scene-without-project→422 / generated-source→422 / bad-checksum→422;
`ListMedia` ordered / kind+source+project+scene filters / empty / owner
isolation; `GetMedia` happy / not-visible→404; `UpdateMedia` real-change /
same-value-no-op / immutable-field→422 / not-visible→404 / explicit-null-clears;
`DeleteMedia` happy / idempotent→404; owner+tenant scoping threaded on all.

### 5.2 Repository integration (real DB, SAVEPOINT rollback)
`add` (with/without links) · unique `(backend,bucket,key)` violation surfaces
(→ use case maps 409) · `list_owned` order + soft-delete exclusion + owner
isolation + all filters · `get_owned` cross-owner isolation · `update_owned`
real change (`updated_at` advances; no version) · `update_owned` foreign/
soft-deleted → None · `soft_delete_owned` happy / wrong-owner / already-deleted
· **F5 link durability**: media `scene_id`/`prompt_id` survive scene/prompt
*soft-delete* (SET NULL does not fire) — the load-bearing schema-interaction
test · `model_is_linkable` exists-and-not-retired.

### 5.3 HTTP integration — `test_media.py`
Register→token→(create-project→scene→prompt)→call; cover A1–A13 end-to-end
(201/200/204/404/422/409/401, owner isolation, filters, tri-state PATCH,
immutable-field rejection, idempotent-by-404).

---

## Section 6 — Structured-log catalogue (α6.2 additions)

| Event | Level | Fields |
|---|---|---|
| `media.registered` | INFO | `media_id`, `kind`, `source`, `storage_backend`, `project_id`, `scene_id`, `prompt_id`, `model_id`, `size_bytes`, `owner_user_id`, `tenant_id`, `ip`, `request_id` |
| `media.register_rejected` | WARN | `reason` (`duplicate_storage` / `foreign_link` / `bad_source`), `storage_backend`, `owner_user_id`, `ip`, `request_id` |
| `media.updated` | INFO | `media_id`, `changed_fields`, `ip`, `request_id` |
| `media.update_rejected` | WARN | `reason` (`not_visible` / `immutable_field`), `media_id`, `ip`, `request_id` |
| `media.deleted` | INFO | `media_id`, `owner_user_id`, `ip`, `request_id` |
| `media.delete_rejected` | WARN | `reason` (`not_visible`), `media_id`, `ip`, `request_id` |

* **No secrets/opaque blobs in logs** — `storage_key`, `checksum`, and
  `source_metadata` **values** are never logged (field names only). `storage_key`
  can encode a signed path, so it is treated as sensitive.

---

## Section 7 — Decisions & Open Questions (SIGN-OFF NEEDED)

### Q1 — Routing & ownership shape ★ load-bearing
`media_assets` has **direct** `tenant_id`+`owner_user_id` and a **nullable**
`project_id` (F2). There is **no `/media` route in the current API_CONTRACT**
(media appears only as AI-gen output + the CR-8 library). So we must choose:

| Option | Route | Ownership gate | Handles orphan (no-project) media? |
|---|---|---|---|
| **A — Top-level, owner-scoped** ★ | `/api/v1/media`, `/media/{id}` | direct row gate (tenant+owner) | **Yes** — `project_id` is an optional link |
| **B — Project-nested** | `/api/v1/projects/{id}/media` | project gate → then `media.project_id == route` | **No** — every asset must have a project |
| **C — Both** | nested for create/list under a project + top-level for cross-project browse | mixed | Yes, but two surfaces = bigger slice |

**Recommendation: A (top-level `/media`, owner-scoped).** The schema's direct
ownership + nullable `project_id` are a deliberate signal that a media asset is
an **owner-level** artefact that *may* be associated with a project, a scene,
and/or a prompt — not a project child like scenes. `project_id` becomes a
validated optional **link/filter** (`?project_id=`), not a route segment. This
also matches the CR-8 library being top-level (`/library/assets`). If the
reviewer prefers strict parity with α5c/α6.1 nesting, **B** is viable but forces
`project_id` non-null in practice and cannot represent uploaded/library media
that isn't tied to a project.

### Q2 — Create semantics: register-by-metadata, not generate ★ load-bearing
**Recommendation: register-by-metadata.** The client supplies storage
coordinates for an object it already holds; α6.2 makes **no** provider or
storage calls. `source` is restricted to `{uploaded, stock}` on register;
`generated` is **rejected with `422`** until the α8 provider seam can vouch for
it (a `generated` asset with no run behind it is unverifiable). Revisit in α8.

### Q3 — Concurrency, given no `version` column (F3) ★ pairs with ADR-0036
**Recommendation: reuse ADR-0036 (Option A / last-writer-wins).** Media is a
generation **output** — a pointer to an immutable stored object — not
concurrency-guarded editorial content. No per-row OCC, no `projects.version`
bump, **out of** version snapshots/restore/diff. This is the exact posture the
baseline encodes (no `VersionMixin`, absent from `_VERSION_BUMP_TABLES`) and
extends the ADR-0036 principle from inputs (prompts) to outputs (media).

### Q4 — Owner gate is direct (not project-derived)
**Recommendation: direct owner gate** — `WHERE id AND tenant_id AND
owner_user_id AND deleted_at IS NULL`. `owner_user_id`/`tenant_id` come from
`CurrentUserDep`. Anti-enumeration `404` for another owner's asset. (If Q1 = B,
prepend the project gate.)

### Q5 — Optional-link validation (project / scene / prompt / model)
**Recommendation: validate each present link; map failure to `422`.**
* `project_id` — must be a **live project owned by the caller** (`projects.
  get_owned`) → else `422`.
* `scene_id` — requires `project_id` present; must be a **live scene in that
  project** (`scenes.get_owned_scene`) → else `422`.
* `prompt_id` — requires `project_id` present; must be a **live prompt in that
  project** (`prompts.get_owned`) → else `422`.
* `model_id` — must be **linkable** (exists + not `retired`) → else `422`
  (reuse the α6.1 `model_is_linkable` gate; either lift it to a shared helper or
  add `media.model_is_linkable`).
Cross-link rule: `scene_id`/`prompt_id` **without** `project_id`, or belonging
to a different project, → `422` (A5).

### Q6 — Duplicate storage coordinates → `409 CONFLICT` (F4)
**Recommendation: yes.** Pre-check with a scoped `SELECT` **and** rely on the
unique constraint as the race-safe backstop (catch `IntegrityError` → map to
`CONFLICT`, consistent with how α2a handled unique email). Do **not** upsert or
silently return the existing row.

### Q7 — Field bounds & representations
**Recommendation:**
* `checksum_sha256` — DTO accepts a **64-char lowercase hex** string; decode to
  32 bytes for storage; surface as hex in `MediaPublic`. Bad length/charset →
  `422`.
* `size_bytes` — `int >= 0` (mirrors the DB CHECK).
* `mime_type` — non-empty, `len ≤ 255`, `type/subtype` shape (light regex).
* `storage_bucket`/`storage_key` — non-empty, `len ≤ 1024`; `storage_key`
  opaque.
* `width`/`height` — optional `int > 0`; `duration_seconds` — optional `≥ 0`,
  `numeric(10,3)`.
* `provider` — optional text `len ≤ 255`.
* `source_metadata` — optional object, default `{}`, must be a dict; cap
  serialized size (e.g. ≤ 16 KiB). `extra="forbid"` on the DTO.

### Q8 — Mutable field set on PATCH — **DECIDED: narrow PATCH**
`media_assets` describes a physical stored object, so most fields are
**immutable**. **Decision:** keep a **narrow** PATCH.
* **Mutable** = the four **associations** `project_id` / `scene_id` /
  `prompt_id` / `model_id` (re-link, tri-state, same validation as Q5) +
  `source_metadata` + `provider`. (`tags` are **not** on `media_assets` — they
  live on `library_assets`, deferred to CR-8.)
* **Immutable forever** = `storage_backend`, `storage_bucket`, `storage_key`,
  `checksum_sha256`, `mime_type`, `size_bytes`, `width`, `height`,
  `duration_seconds`, `kind`, `source`, `id`, `owner_user_id`, `tenant_id` —
  these describe the physical object; changing them means it is a *different*
  asset (mirrors object-storage semantics).
Presence of an immutable field in the patch → `422`. Empty patch → `422`.

### Q9 — DELETE semantics
**Recommendation:** unconditional owner-scoped **soft** delete, `204`,
idempotent-by-404 (D4). No hard delete (downstream FKs + audit).

### Q10 — List pagination
**Recommendation:** full ordered array + filters (mirror α6.1 Q9); defer cursor.
A prolific tenant may accrue many assets — flag cursor as the first α6.x
follow-up if needed.

### Q11 — `MediaPublic` fields
**Recommendation:** `{id, kind, source, project_id, scene_id, prompt_id,
model_id, provider, storage_backend, storage_bucket, storage_key, mime_type,
size_bytes, width, height, duration_seconds, checksum_sha256 (hex),
source_metadata, created_at, updated_at}`. Omit `owner_user_id`/`tenant_id`
(caller-implicit) and `deleted_at`. *(Open sub-question: is exposing
`storage_bucket`/`storage_key` to the client acceptable pre-signed-URL, or
should they be withheld until a download seam exists? Recommendation: expose —
they are the caller's own coordinates — but flag for review.)*

### Q12 — `source_metadata` role
**Recommendation:** free-form client dict for now (generation params, EXIF,
etc.), validated as a dict with a size cap (Q7). No server-owned keys in α6.2.

### Q13 — One cohesive slice? **Recommendation: yes** — full Media CRUD
(register/list/get/patch/delete) as one PR → `v0.4.12`. *(If Q8 chooses
register-only, PATCH drops out and the slice shrinks accordingly.)*

### Q14 — Companion docs: `MEDIA_AGGREGATE.md` + ADR-0037 — **DECIDED: new ADR-0037**
**Decision:** (a) a concise `docs/domain/MEDIA_AGGREGATE.md` mirroring
`PROMPT_AGGREGATE.md` (identity, direct ownership, no-OCC rationale, snapshot
exclusion, storage-identity uniqueness, register-by-metadata boundary); (b) a
**new ADR-0037 — "Media asset ownership"** that does **not** mutate ADR-0036 and
explicitly states it *adopts the concurrency model established by ADR-0036*,
while recording Q1 (top-level ownership/routing) and Q2 (register-not-generate).
Each ADR stays focused; the α6.1 decision record remains immutable.

---

## Section 8 — File inventory (provisional)

### 8.1 New files
| Path | LOC est. | Purpose |
|---|---:|---|
| `backend/app/domain/media/__init__.py` | ~3 | package |
| `backend/app/domain/media/media_asset.py` | ~90 | frozen `MediaAsset` entity |
| `backend/app/infrastructure/repositories/media_repository.py` | ~200 | `SqlAlchemyMediaRepository` (add/list/get/update/soft-delete/model_is_linkable) |
| `backend/app/application/use_cases/media/__init__.py` | ~6 | exports |
| `backend/app/application/use_cases/media/register_media.py` | ~120 | `RegisterMedia` (link + duplicate validation) |
| `backend/app/application/use_cases/media/list_media.py` | ~55 | `ListMedia` (filters) |
| `backend/app/application/use_cases/media/get_media.py` | ~40 | `GetMedia` |
| `backend/app/application/use_cases/media/update_media.py` | ~110 | `UpdateMedia` (iff Q8 keeps PATCH) |
| `backend/app/application/use_cases/media/delete_media.py` | ~55 | `DeleteMedia` |
| `backend/app/api/v1/schemas/media.py` | ~140 | `MediaRegisterRequest` / `MediaUpdateRequest` / `MediaPublic` |
| `backend/app/api/v1/routers/media.py` | ~150 | router (prefix per Q1) |
| `backend/tests/unit/application/use_cases/media/test_*.py` | ~380 | unit matrix (§5.1) |
| `backend/tests/integration/infrastructure/repositories/test_media_repository.py` | ~240 | repo matrix (§5.2) |
| `backend/tests/integration/api/test_media.py` | ~400 | HTTP matrix (§5.3) |
| `docs/domain/MEDIA_AGGREGATE.md` | ~130 | companion (Q14) |
| `docs/decisions/ADR-0037-media-generation-outputs.md` | ~90 | Q1/Q2/Q3 precedent (Q14) |

### 8.2 Modified files
| Path | Change |
|---|---|
| `backend/app/main.py` | version → `0.4.12-phase3-alpha6.2-dev`; mount media router |
| `backend/app/application/interfaces/repositories.py` | add `IMediaRepository` |
| `backend/app/application/interfaces/unit_of_work.py` | add `.media` |
| `backend/app/infrastructure/uow/sqlalchemy_unit_of_work.py` | instantiate `MediaRepository` |
| `backend/app/core/container.py` | 5 use-case factories |
| `backend/app/api/v1/deps.py` | 5 `*MediaDep` aliases |
| `backend/tests/integration/conftest.py` | `.media` on `_TestUnitOfWork` |
| `backend/tests/unit/application/use_cases/auth/_fakes.py` | `FakeMediaRepository` (+ `FakeUnitOfWork.media`) |
| `API_CONTRACT.md` | new Media resource-map row + subsection (Q1 prefix) |
| `CHANGELOG.md` | `[Unreleased]` α6.2 entry |
| `ROADMAP.md` | Phase 3 row α6.2 |
| `docs/domain/PROJECT_AGGREGATE.md` | §8 diagram: Media α6.2; §6 note (media outside snapshot) |

> **UoW note (α5c/α6.1 lesson).** The real `UnitOfWork`, the integration
> `_TestUnitOfWork`, and `FakeUnitOfWork` must all gain `.media` or every media
> use-case test fails at attribute access.

### 8.3 Deliberately NOT touched
No migration; no ORM change (table + indexes + unique constraint exist);
`library_assets`/`library_folders`/`library_asset_projects` untouched (CR-8,
later); `clips`/timeline untouched (α6.3); version ledger code untouched (Q3 —
media out of snapshot); no provider/storage code (α8).

---

## Section 9 — Reviewer sign-off

**Reviewer verdict — 2026-07-13: ✅ Approved.**

| Question | Decision |
|---|---|
| **Q1 — Routing / ownership** | ✅ **Top-level `/media`** — owner-scoped; media is a user-level artefact, `project_id`/`scene_id`/`source` are optional links & filters. Project nesting would fight the schema. |
| **Q2 — Create semantics** | ✅ **Register-by-metadata only** — α6.2 knows nothing about S3/Supabase/Azure/GCS/presigned URLs/AI providers. Storage + generation are later slices. |
| **Q3 — Concurrency** | ✅ **Reuse ADR-0036** — media is a generated artefact, not editorial state: no OCC, no `projects.version` bump, excluded from snapshots/restore/diff. |
| **Q8 — PATCH** | ✅ **Narrow PATCH.** Mutable = `project_id`, `scene_id`, `prompt_id`, `model_id` links + `source_metadata` (`tags` deferred to CR-8). Immutable forever = `storage_backend`, `storage_bucket`, `storage_key`, `checksum_sha256`, `mime_type`, `size_bytes`, `width`, `height`, `duration_seconds`, `kind`, `source` — these describe the physical object; changing them means it is a *different* asset. |
| **Q14 — ADR** | ✅ **New ADR-0037 — "Media asset ownership."** Does **not** mutate ADR-0036; explicitly states it *adopts the concurrency model established by ADR-0036*. Each ADR stays focused. |
| **Everything else (Q4–Q7, Q9–Q13)** | ✅ Accept as drafted. |

**Architecture note (reviewer).** The aggregate responsibilities are now clean:
Projects + Scenes = versioned **editorial state** (Aggregate OCC, immutable
snapshots); Prompts = generation **inputs** (last-writer-wins, out of
snapshots); Media = generation **outputs** (registered artefacts, reusable
across projects, out of snapshots). Timeline (α6.3) becomes the composition
layer that references scenes + media without owning either.

Branch cut authorised: **`phase3/alpha6.2-media`** (single slice) — follow §10.

---

## Section 10 — Implementation order (once approved)

1. Cut `phase3/alpha6.2-media` off fresh `main`; bump `app/main.py` →
   `0.4.12-phase3-alpha6.2-dev`.
2. `MediaAsset` domain entity (`domain/media/media_asset.py`).
3. `IMediaRepository` + wire `.media` on the UoW (+ `_TestUnitOfWork`, +
   `FakeUnitOfWork`).
4. `SqlAlchemyMediaRepository` (add/list/get/update/soft-delete/model_is_linkable)
   + `FakeMediaRepository`.
5. Use cases + unit tests (§5.1); `pytest -m unit` + mypy green.
6. DTOs + container factories + deps + `routers/media.py`; mount it.
7. Repo integration (§5.2, incl. the F5 link-durability + unique-conflict tests)
   + HTTP (§5.3).
8. Docs: API_CONTRACT (new row + subsection), CHANGELOG, ROADMAP,
   PROJECT_AGGREGATE §6/§8, MEDIA_AGGREGATE.md, ADR-0037.
9. Local CI gate green (no migration → stages 5/6/7 unchanged).
10. Commit (chore(docs) + feat(media)), push, PR, merge, tag
    `v0.4.12-phase3-alpha6.2`; pivot to **α6.3 (timeline / tracks / clips)**.

---

## Section 11 — Post-α6.2 roadmap (dependency order)

* **α6.3 — Timeline / tracks / clips** — `clips.media_asset_id` places a
  registered asset on the timeline (`Scene → Media → Clip → Timeline`).
* **α6.4 — Render / export jobs** — orchestrate existing data; outputs point
  back at `media_assets` (`output_media_asset_id`).
* **CR-8 — Asset Library** — `library_assets` (its own `VersionMixin`),
  folders, tags, embeddings/HNSW over registered media.
* **α8+ — AI provider integration** — real generation: provider seam + object
  storage populate `source = generated`, `provider`, `model_id`, `prompt_id`,
  and emit `reason = generated` project versions end-to-end.
