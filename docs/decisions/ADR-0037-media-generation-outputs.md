# ADR-0037 — Media Assets Are Owner-Level Generation Outputs, Registered by Metadata

**Status:** Proposed (documents the pattern shipped in Phase 3 α6.2 — Media
CRUD). Flips to Accepted on merge of this ADR PR.
**Adopts:** the concurrency model established by **ADR-0036** (prompts as
generation inputs — no per-row OCC, no `projects.version` bump, excluded from
version snapshots/restore/diff). This ADR does **not** mutate ADR-0036; it
extends the same principle from generation *inputs* (prompts) to generation
*outputs* (media) and records the α6.2-specific ownership/routing and
create-semantics decisions.
**Refines / documents:** `docs/domain/MEDIA_AGGREGATE.md`,
`docs/domain/PROJECT_AGGREGATE.md` §6/§8 (the aggregate boundary + snapshot
exclusion), `API_CONTRACT.md` (new Media resource), and the α6.2 pre-flight
(`docs/engineering/PHASE3_ALPHA6_2_PREFLIGHT.md`, §7 Q1/Q2/Q3/Q8/Q14). Builds on
**ADR-0036** (prompts), **ADR-0035** (project version snapshots), **ADR-0034**
(authenticated endpoint pattern).
**Wave:** Phase 3, generation-pipeline slice α6.2 (Media Asset aggregate). Sets
the precedent for α6.3 (Timeline) and CR-8 (Asset Library).

---

## Context

α6.2 introduces the **Media Asset aggregate** — the first *generation-OUTPUT*
content — as a **register-by-metadata** CRUD surface backed by the existing
baseline `media_assets` table. The slice ships **zero migrations** (table, all
indexes, and the `(storage_backend, storage_bucket, storage_key)` unique
constraint exist in baseline `0001`).

Two load-bearing architectural questions had to be resolved, both keyed off the
physical schema:

1. **Ownership / routing.** Unlike `prompts` and `scenes` — whose ownership is
   *derived* through a parent (`project_id → projects.(tenant_id,
   owner_user_id)`) — `media_assets` carries its **own `tenant_id` +
   `owner_user_id`** (both `NOT NULL`) and only a **nullable `project_id`**. The
   schema is signalling that a media asset is an **owner-level** artefact that
   *may* be associated with a project/scene/prompt, not a project child. There is
   no `/media` route anywhere in the pre-α6.2 API_CONTRACT to inherit from.

2. **Concurrency.** Like `prompts`, `media_assets` has **no `version` column**
   (`UUIDPrimaryKeyMixin + TimestampMixin + SoftDeleteMixin`, deliberately
   **omitting** `VersionMixin` that the sibling `library_assets` *does* have). It
   is absent from `_VERSION_BUMP_TABLES` and carries only a `touch_updated_at`
   trigger — so there is no per-row optimistic-concurrency token, and α6.2 had to
   define concurrency **without inventing a migration**.

Without an ADR, a future contributor sees a table with its own owner columns, a
nullable project link, no `version`, an API with no `412`, and a version-restore
path that ignores media — and cannot tell whether that is a **decision** or an
**oversight** to be "fixed." This ADR promotes it from implemented convention to
recorded decision.

The physical facts α6.2 must honour (pre-flight §2, F1–F7):

- `media_assets`: **direct** `tenant_id` + `owner_user_id` (`NOT NULL`,
  `ON DELETE RESTRICT`); nullable `project_id` / `scene_id` / `prompt_id` (all
  `ON DELETE SET NULL`); nullable `model_id` (`ON DELETE RESTRICT`);
  `storage_backend` / `storage_bucket` / `storage_key` (`NOT NULL`, **UNIQUE**
  together); `mime_type`, `size_bytes` (`CHECK ≥ 0`), `checksum_sha256`
  (`bytea NOT NULL`); nullable `width` / `height` / `duration_seconds`; `kind`
  (`media_kind`), `source` (`media_source`), `source_metadata` (jsonb),
  timestamps, `deleted_at`. **No `version`.**
- `SET NULL` on the project/scene/prompt FKs fires **only on a hard parent
  `DELETE`** — and the API only ever *soft-deletes* those parents.
- `media_source = {generated, uploaded, stock}`; α6.2 registers already-held
  objects, so `generated` (no run behind it) is rejected on register until α8.

---

## Decision

### D1 — Media assets are generation outputs, outside the versioned content aggregate

Media assets adopt the ADR-0036 governing principle, applied to the output side:

> **Project versions capture editorial state, not generation artefacts. Media
> assets are registered generation outputs that do not participate in aggregate
> optimistic concurrency, snapshots, restore, or diff. A media asset may retain
> the prompt/model that produced it for provenance, independently of the current
> prompt record.**

The versioned Project aggregate is **{project root + default storyboard +
ordered scenes}** (ADR-0035). Scenes are *editorial state*; media assets are
*generation-pipeline artefacts* — a media asset is a pointer to a concrete,
immutable stored object, closer to a build artefact than to authored content.

### D2 — Owner-level ownership; top-level routing (α6.2 Q1 = Option A)

Because `tenant_id` + `owner_user_id` are **on the row** (not derived through a
project), the API is **top-level and owner-scoped**, not project-nested:

```
POST   /api/v1/media
GET    /api/v1/media            ?kind= ?source= ?project_id= ?scene_id=
GET    /api/v1/media/{id}
PATCH  /api/v1/media/{id}
DELETE /api/v1/media/{id}
```

`project_id` / `scene_id` / `source` are **optional validated links / filters**,
never route segments. The visibility gate is a **single, direct** row match —
`WHERE id = :id AND tenant_id = :t AND owner_user_id = :u AND deleted_at IS
NULL` — yielding a uniform `404` for a missing / soft-deleted / other-owner asset
(anti-enumeration). This matches the CR-8 library being top-level, and lets a
media asset exist with **no** project (uploaded / stock library media). A
project-nested route (Option B) was rejected: it would force `project_id`
non-null in practice and cannot represent project-less media.

### D3 — No per-row OCC; PATCH is last-writer-wins (α6.2 Q3, adopts ADR-0036)

Given D1 and the baseline's deliberate omission of `VersionMixin`, media takes
**no optimistic-concurrency control**:

- `PATCH /media/{id}` is a plain owner-gated update. **No `version` on the wire,
  no `412`.** Two racing edits: the last writer wins.
- A media mutation does **NOT** bump `projects.version`. The **Aggregate OCC
  Rule** (ADR-0035 D9) does not extend to media, precisely because media is not
  in the editorial aggregate.
- The use case still detects a **same-value no-op** (no write / no `updated_at`
  bump) and implements **tri-state PATCH** (`exclude_unset`): absent = unchanged;
  explicit `project_id: null` clears the link; `source_metadata` is non-nullable
  so an explicit `null` is a `422`.

### D4 — Media is excluded from version snapshots / restore / diff (adopts ADR-0036 D3)

The `project_versions` snapshot stays {project root + scenes}. Media assets are
**not** captured, **not** restored, **not** diffed — a **decision, not an
omission**. Restoring a project must not resurrect or delete generation outputs;
media reusability across projects (a single asset linked from many projects)
would in any case make snapshot capture ill-defined.

### D5 — Register-by-metadata, not generate (α6.2 Q2)

`POST /media` **registers** an object the client already holds: it supplies
storage coordinates (`storage_backend` / `storage_bucket` / `storage_key`),
`mime_type`, `size_bytes`, `checksum_sha256`, `kind`, `source`, and optional
links / descriptors. α6.2 makes **no** provider call, **no** byte upload, **no**
presigned URL, and does **not** fetch the object to verify the checksum. `source`
is restricted to `{uploaded, stock}`; **`generated` is rejected with `422`**
until the α8 provider seam can vouch for it (a `generated` asset with no run
behind it is unverifiable). Storage and generation are later slices.

### D6 — Storage identity is unique; duplicate register → `409` (α6.2 Q6, F4)

`(storage_backend, storage_bucket, storage_key)` is a UNIQUE constraint — a media
row *is* a pointer to a concrete stored object. Registering the same coordinates
twice is a **`409 CONFLICT`**, not a silent second row and not an upsert. The
repository catches the `IntegrityError` and maps it to `ConflictError`; the
unique constraint is the race-safe backstop behind the use case's pre-check
(same pattern as α2a unique-email).

### D7 — Link validation is `422`, not `404`; physical fields immutable (Q5/Q8)

Each **present** optional link is validated for the caller and a failure is a
**`422 VALIDATION_FAILED`, not a `404`** (the route target — the caller's own
media namespace — is fine; the *body* is invalid): `project_id` must be a live
project the caller owns; `scene_id` / `prompt_id` require `project_id` and must
be a live scene / prompt in **that** project; `model_id` must be an `ai_models`
row with status ≠ `retired` (`media.model_is_linkable` — the FK alone is
`RESTRICT` and would otherwise accept a since-retired model).

PATCH is **narrow** (Q8): **mutable** = the four links + `provider` +
`source_metadata`; **immutable forever** = `storage_backend`, `storage_bucket`,
`storage_key`, `checksum_sha256`, `mime_type`, `size_bytes`, `width`, `height`,
`duration_seconds`, `kind`, `source` (+ server-owned `id`, `owner_user_id`,
`tenant_id`). These describe the physical object — changing them means it is a
*different* asset (object-storage semantics). `extra="forbid"` on the DTO turns
any immutable/unknown key into a `422`.

### D8 — Link durability across parent soft-delete (F5)

Because `project_id` / `scene_id` / `prompt_id`'s `ON DELETE SET NULL` fires
**only on a hard parent `DELETE`**, and the API only ever *soft-deletes*
projects/scenes/prompts, a media asset's links **survive** a parent soft-delete
(and a project restore). This is asserted by a load-bearing repository
integration test, so the durability is a guaranteed property, not an accident.

---

## Alternatives Considered

1. **Project-nested routing `/projects/{id}/media` (α6.2 Q1 Option B).**
   *Rejected.* Strict parity with α5c/α6.1 nesting, but it forces `project_id`
   non-null in practice and cannot represent uploaded/library/stock media that
   isn't tied to a project — fighting the schema's direct-ownership + nullable
   `project_id` signal.

2. **Aggregate OCC — fence media PATCH/DELETE on `projects.version`.**
   *Rejected.* Media is reusable across projects and often has no project at all;
   coupling its edits to a single `projects.version` is ill-defined and would
   imply media belongs in snapshots (contradicting D4).

3. **Add a `version` column to `media_assets` (a migration).** *Rejected.* Breaks
   the no-migration discipline; media is a pointer to an immutable object, not
   high-contention co-edited content, so row-OCC buys nothing.

4. **Generate-on-register (call a provider from `POST /media`).** *Rejected for
   α6.2* (Q2): that is the α8 provider seam. α6.2 registers assets that already
   exist; `source = generated` is rejected until a run can vouch for it.

5. **Upsert / return-existing on duplicate storage coordinates.** *Rejected*
   (Q6): masks a client bug and is ambiguous about ownership; a duplicate is a
   `409`.

6. **Amend ADR-0036 instead of a new ADR (α6.2 Q14 alt).** *Rejected.* Keeps
   each decision record focused and immutable; ADR-0037 *adopts* ADR-0036's
   concurrency model by reference rather than editing the input-side record.

---

## Consequences

- **Positive — clean editorial/generation boundary, extended to outputs.**
  "Versioned editorial state" (project + scenes) vs "generation inputs"
  (prompts) vs "generation outputs" (media) is now explicit end-to-end. A
  contributor will not wire media into `projects.version` or the snapshot
  builder.
- **Positive — owner-level media library.** A media asset can be reused across
  projects and can exist with no project — the top-level owner-scoped surface
  supports the eventual asset-library UX without a redesign.
- **Positive — small, migration-free slice.** α6.2 is register + CRUD +
  ownership + link validation + filtering — not generation, not storage, not
  OCC/snapshot participation.
- **Contract — no `version` on the media wire.** Clients must not expect a
  `version` field or a `412` on media PATCH; a media PATCH is last-writer-wins.
  `MediaPublic` = `{id, kind, source, project_id, scene_id, prompt_id, model_id,
  provider, storage_backend, storage_bucket, storage_key, mime_type, size_bytes,
  width, height, duration_seconds, checksum_sha256 (hex), source_metadata,
  created_at, updated_at}`.
- **Contract — restore does not touch media.** A project restore neither
  captures nor rewrites media; media→project/scene/prompt links survive it (D8).
- **Precedent — timeline & library inherit this.** α6.3 clips reference
  `media_asset_id`; CR-8 library builds *over* registered media (with its own
  `VersionMixin`). Both treat media as an owner-level registered artefact.

---

## Pattern Reference (Examples)

- **Domain:** `app/domain/media/media_asset.py` (frozen `MediaAsset`, no
  `version`; `checksum_sha256` as `bytes`).
- **Repository:** `app/infrastructure/repositories/media_repository.py`
  (`MediaRepository`: `add` [unique→`ConflictError`], `list_owned` + filters,
  `get_owned`, `update_owned` — no OCC fence, `soft_delete_owned`,
  `model_is_linkable`). All owner-scoped (tenant + owner_user).
- **Use cases:** `app/application/use_cases/media/*` — `RegisterMedia`
  (link validation via `_links.validate_media_links` → `422`; duplicate →
  `409`), `ListMedia`, `GetMedia`, `UpdateMedia` (same-value no-op, tri-state,
  conditional re-validation), `DeleteMedia` (idempotent-by-404). None call
  `IProjectRepository.touch_version`.
- **DTOs / router:** `app/api/v1/schemas/media.py`,
  `app/api/v1/routers/media.py` (top-level `/media`; no `version`, no `412`).
- **F5 durability:** `tests/integration/.../test_media_repository.py`
  (media `project_id` / `scene_id` / `prompt_id` survive parent soft-delete).

New generation-pipeline aggregates copy these shapes rather than reinventing
them.

---

## Future Extensions

- **α6.3 — Timeline / tracks / clips** — `clips.media_asset_id` places a
  registered asset on the timeline (`Scene → Media → Clip → Timeline`); same
  outside-the-editorial-snapshot stance.
- **α6.4 — Render / export jobs** — outputs point back at `media_assets`
  (`output_media_asset_id`).
- **CR-8 — Asset Library** — `library_assets` (its **own** `VersionMixin`),
  folders, tags, embeddings/HNSW, usage counters over registered media.
- **α8+ — AI provider integration + object storage** — real generation:
  provider seam + storage populate `source = generated`, `provider`, `model_id`,
  `prompt_id`, and issue presigned upload/download URLs; register can then accept
  `generated` behind a verified run.
