# Media Asset Aggregate

> **Convention.** This is a domain design document (companion to
> `docs/domain/PROJECT_AGGREGATE.md`, `docs/domain/SCENE_AGGREGATE.md`, and
> `docs/domain/PROMPT_AGGREGATE.md`). It defines the **Media Asset** aggregate —
> its identity, boundary, **direct owner-level ownership**, the deliberate
> *no-OCC / no-snapshot* stance, the **register-by-metadata** create boundary,
> and its position as the first **generation-output** content in the model. It is
> the design authority for Phase 3 **α6.2** (Media CRUD). Read it alongside the
> α6.2 pre-flight (`docs/engineering/PHASE3_ALPHA6_2_PREFLIGHT.md`) and
> **ADR-0037** (which adopts **ADR-0036**'s concurrency model).
>
> **Grounding.** Every schema claim is checked against the live ORM
> (`backend/app/infrastructure/db/models/media.py`) and the baseline migration
> (`backend/alembic/versions/0001_baseline.py`), not an idealised model. Where
> the baseline diverges from a conceptual sketch, the baseline wins — decisively
> here: `media_assets` carries its **own** `tenant_id` + `owner_user_id` and a
> **nullable** `project_id`, and has **no `version` column**. Those two facts are
> the whole thesis of this aggregate.

---

## 1. Purpose & position in the model

A **Media Asset** is a **generation output**: a registered pointer to a concrete,
immutable stored object (an image / video / narration / subtitle / music / sound
effect / thumbnail) that lives in some storage backend. It is closer to a *build
artefact* than to the project's authored editorial content. Unlike prompts and
scenes, a media asset is an **owner-level** artefact — it belongs to a user
(tenant + owner), and *may optionally* be linked to a project, a scene, a prompt
(provenance), and/or a model.

Position in the hierarchy (baseline schema fact):

```
User / Tenant  (direct owner of media)
  └── Media Asset  (media_assets.tenant_id + owner_user_id, NOT NULL)   ← α6.2
         · optional project_id (SET NULL, nullable — may be project-less)
         · optional scene_id   (SET NULL)
         · optional prompt_id  (SET NULL — provenance: which prompt drove it)
         · optional model_id   (RESTRICT — ai_models registry link)

Prompt (α6.1) ──drives──▶ Media Asset (α6.2) ──placed as──▶ Clip ──on──▶ Timeline (α6.3)
```

The critical separation this document establishes:

> **Scenes are editorial state. Prompts are generation inputs. Media assets are
> generation outputs.**

Scenes belong to the versioned Project aggregate (row-OCC + version snapshots).
Prompts (ADR-0036) and media (ADR-0037) do **not** — they are production-pipeline
artefacts with their own lifecycle. Media is additionally **owner-level and
reusable across projects**, which is why it is not a project child.

---

## 2. Aggregate boundary

### 2.1 Inside the α6.2 Media Asset (the slim domain surface)

The domain `MediaAsset` is a slim, frozen view of the physical row:

- `id` — durable UUID, server-minted.
- `tenant_id` / `owner_user_id` — **direct** ownership, on the row, `NOT NULL`
  (contrast prompts/scenes, whose ownership is *derived* through a project).
  Internal identity/authorization fields — **not** client-supplied (they come
  from `CurrentUserDep`) and **omitted** from the public DTO (caller-implicit).
- `project_id` / `scene_id` / `prompt_id` — optional links (all nullable).
  `NULL` = an owner-level asset not tied to that entity. **Mutable** (re-link)
  via the narrow PATCH.
- `model_id` — optional link to an `ai_models` row (which model produced / should
  produce it), validated as existing + not `retired`. **Mutable.**
- `provider` — optional free-text provider label. **Mutable.**
- `kind` — one of the 7 `media_kind` enum values (`image, video, narration,
  subtitle, music, sound_effect, thumbnail`). **Immutable** after register.
- `source` — one of `media_source` (`uploaded, stock`; `generated` is α8).
  **Immutable** after register.
- Physical-object fields — `storage_backend` / `storage_bucket` / `storage_key`
  (unique together), `mime_type`, `size_bytes`, `checksum_sha256` (raw 32 bytes,
  surfaced as 64-char hex in the DTO), `width` / `height` / `duration_seconds`.
  **Immutable forever** — they describe the concrete stored object.
- `source_metadata` — free-form JSONB bag (default `{}`). **Mutable.**
- `created_at` / `updated_at` — timestamps; `updated_at` is trigger-owned.

### 2.2 Physical-but-not-on-the-public-DTO

- `tenant_id` / `owner_user_id` — carried in the domain entity (for scoping) but
  omitted from `MediaPublic` (caller-implicit).
- `deleted_at` — soft-delete tombstone; internal, never on the wire.

### 2.3 The defining absence: **no `version` column**

`MediaAsset` is built from `UUIDPrimaryKeyMixin + TimestampMixin +
SoftDeleteMixin` — **no `VersionMixin`** (whereas the sibling `LibraryAsset`
*does* have it). The table is absent from `_VERSION_BUMP_TABLES` and carries only
a `touch_updated_at` trigger. There is **no per-row concurrency token** on a
media asset, by design (see §4) — the same posture as `prompts`.

---

## 3. Ownership, scoping & anti-enumeration

Every endpoint is authenticated (`CurrentUserDep`) and runs a **single, direct
visibility gate** (contrast the two-level project→child gate of α5c/α6.1, because
ownership is on the media row itself):

- **Owner gate** — `MediaRepository.get_owned(media_id, tenant, owner)` matches
  `WHERE id = :id AND tenant_id = :t AND owner_user_id = :u AND deleted_at IS
  NULL`; `None` → uniform `404 NOT_FOUND` (missing / soft-deleted / not the
  caller's). A `media_id` owned by another user is indistinguishable from "never
  existed."

`project_id` / `scene_id` / `source` are **filters**, never part of the access
key. The optional links are validated as **`422 VALIDATION_FAILED`, not `404`**:
a foreign/unknown `project_id`, a `scene_id` / `prompt_id` without (or outside)
that project, or an unknown/retired `model_id` means the *request body* is
invalid — the route target (the caller's own media namespace) is fine. `404` is
reserved for the owner visibility gate.

**Storage identity is unique.** `(storage_backend, storage_bucket, storage_key)`
is a UNIQUE constraint; registering the same coordinates twice is a **`409
CONFLICT`** (§6), never a silent second row.

---

## 4. Concurrency: last-writer-wins (no OCC), and why (ADR-0037 adopts ADR-0036)

Because a media asset has no `version` column (§2.3), α6.2 resolves its
concurrency model **within the existing schema** (no migration): a `PATCH` is a
plain owner-gated update. There is **no version fence on the wire**, **no
`412`**, and a mutation does **not** bump `projects.version`. Two racing edits:
the last writer wins.

This is deliberate, not a shortcut:

- The baseline's omission of `VersionMixin` on `media_assets` (while
  `library_assets` *has* it) is a **signal**: media is a registered pointer to an
  immutable object, not concurrency-guarded editorial content. α6.2 follows the
  signal instead of fighting it.
- Media is **reusable across projects** and may have no project at all; coupling
  its edits to a single `projects.version` (the Aggregate OCC Rule token) is
  ill-defined and would imply media belongs in version snapshots — which it does
  not (§5).

`update_owned` still detects a **same-value no-op** at the use-case layer (no
write, no spurious `updated_at` bump) and supports **tri-state PATCH** (absent =
unchanged; explicit `project_id: null` clears a link; a value sets it —
`source_metadata` is non-nullable so an explicit `null` is `422`). The physical
fields are **immutable** — presence of one in the patch is a `422` (`extra=
"forbid"`).

---

## 5. Exclusion from version snapshots (ADR-0037 adopts ADR-0036)

The `project_versions` snapshot boundary is **{project root + default storyboard
+ ordered scenes}** (ADR-0035) and nothing more. Media assets are **excluded** —
not captured, restored, or diffed. Restore's silence on media is a **decision,
not an omission**.

The governing principle (ADR-0037, adopting ADR-0036):

> **Project versions capture editorial state, not generation artefacts. Media
> assets are registered generation outputs that do not participate in aggregate
> optimistic concurrency, snapshots, restore, or diff. A media asset may retain
> the prompt/model that produced it for provenance, independently of the current
> prompt record.**

Consequences:

- Restoring a project to an old version does **not** resurrect or delete
  generation outputs. A media asset reused across several projects makes snapshot
  capture ill-defined in any case.
- Provenance flows *forward*: a media asset retains `prompt_id` / `model_id` /
  `source_metadata`, independent of the current prompt record.
- This extends the ADR-0036 precedent from generation *inputs* (prompts) to
  *outputs* (media); timeline (α6.3) inherits the same stance.

### 5.1 Link durability across parent soft-delete / restore (F5)

`project_id` / `scene_id` / `prompt_id` are `ON DELETE SET NULL`, but **SET NULL
fires only on a hard `DELETE`** of the parent. Projects / scenes / prompts are
only ever **soft-deleted**, and a version restore soft-deletes then revives under
the same `id`. Therefore a media asset's links **survive** a parent soft-delete
and a project restore — never silently nulled by editorial operations. This is
covered by a load-bearing repository integration test.

---

## 6. Lifecycle & the register-by-metadata boundary

```
        register                        DELETE
  ∅ ───────────▶  live  ───────────────────────▶  soft-deleted
                   │  PATCH (narrow, last-writer-  (deleted_at set)
                   └──  wins, no version fence)     GET/PATCH/DELETE → 404
```

- **register** — `POST /media`; the client supplies storage coordinates for an
  object it **already holds**. α6.2 makes **no** provider call, **no** byte
  upload, **no** presigned URL, and does **not** fetch the object to verify the
  checksum. `source` is restricted to `{uploaded, stock}`; `generated` → `422`
  (α8). Duplicate storage coordinates → `409`. Identity + ownership are
  server-owned. `201`.
- **live** — normal state (`deleted_at IS NULL`); readable, listable, patchable
  (narrow mutable subset).
- **soft-deleted** — owner-scoped soft delete (`DELETE /media/{id}` → `204`), no
  version fence, **idempotent-by-404**: a second delete — and any `GET`/`PATCH`
  after delete — is `404`; deleting another user's asset or an unknown id is the
  same `404`.

Listing (`GET /media`) is side-effect-free, newest-first (`created_at` desc, `id`
desc), soft-delete-excluded, owner-scoped, with optional `?kind=<enum>`,
`?source=<str>`, `?project_id=<uuid>`, `?scene_id=<uuid>` filters (combined = AND;
bad enum / non-UUID → `422`). Not paginated in α6.2.

---

## 7. Structured-log posture

Media lifecycle events are logged with identifiers and *field names* only —
**never** sensitive values. `storage_key` (may encode a signed path),
`checksum_sha256`, and `source_metadata` **values** are never logged:

- `media.registered` (INFO) — `media_id`, `kind`, `source`, `storage_backend`,
  `project_id`, `scene_id`, `prompt_id`, `model_id`, `size_bytes`,
  `owner_user_id`, `tenant_id`, `ip`, `request_id`.
- `media.register_rejected` (WARN) — `reason` (`duplicate_storage` /
  `foreign_link` / `bad_source`), `storage_backend`, `owner_user_id`, `ip`,
  `request_id`.
- `media.updated` (INFO) — `media_id`, `changed_fields`, `ip`, `request_id`.
- `media.update_rejected` (WARN) — `reason` (`not_visible` / `immutable_field`),
  `media_id`, `ip`, `request_id`.
- `media.deleted` (INFO) — `media_id`, `owner_user_id`, `ip`, `request_id`.
- `media.delete_rejected` (WARN) — `reason` (`not_visible`), `media_id`, `ip`,
  `request_id`.

---

## 8. Open evolution (explicitly out of α6.2)

- **Object storage.** No byte upload, no presigned/signed URLs, no download
  proxy, no storage-backend SDK. The client supplies coordinates for an object it
  already placed. Deferred.
- **Provider generation.** No model calls; nothing *produces* bytes.
  `source = generated` end-to-end is α8 (register can accept `generated` behind a
  verified run then).
- **Checksum verification.** α6.2 stores the client-supplied `checksum_sha256`
  as-is; it does not fetch the object to verify the digest.
- **Re-keying.** Storage coordinates + checksum + size are immutable after
  register; "the object moved buckets" is not a use case yet.
- **Asset Library (CR-8).** `library_assets` (its **own** `VersionMixin`),
  folders, tags, embeddings/HNSW, usage counters — a separate later slice over
  registered media.
- **Timeline placement.** `clips.media_asset_id` (α6.3).

---

## 9. Change log

| Date | Change |
|---|---|
| 2026-07-13 | Initial authoring for Phase 3 α6.2 (Media CRUD). Establishes the generation-output identity, direct owner-level ownership + single-level visibility gate, register-by-metadata boundary, storage-identity uniqueness (→ 409), the no-OCC / last-writer-wins concurrency model, narrow PATCH (mutable links + provider + source_metadata; physical fields immutable), exclusion from version snapshots (F5 link durability), and the lifecycle. Adopts ADR-0037 (which adopts ADR-0036). |
