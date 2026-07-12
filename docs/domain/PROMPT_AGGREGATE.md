# Prompt Aggregate

> **Convention.** This is a domain design document (companion to
> `docs/domain/PROJECT_AGGREGATE.md` and `docs/domain/SCENE_AGGREGATE.md`). It
> defines the **Prompt** aggregate — its identity, boundary, the deliberate
> *no-OCC / no-snapshot* stance, and its position as the first
> **generation-input** content in the model. It is the design authority for
> Phase 3 **α6.1** (Prompt CRUD). Read it alongside the α6.1 pre-flight
> (`docs/engineering/PHASE3_ALPHA6_1_PREFLIGHT.md`) and **ADR-0036**.
>
> **Grounding.** Every schema claim is checked against the live ORM
> (`backend/app/infrastructure/db/models/scenes.py`, where the `Prompt` model
> lives) and the baseline migration
> (`backend/alembic/versions/0001_baseline.py`), not an idealised model. Where
> the baseline diverges from a conceptual sketch, the baseline wins — and here
> it wins decisively: the baseline gave `prompts` **no `version` column**, and
> that omission is the whole thesis of this aggregate.

---

## 1. Purpose & position in the model

A **Prompt** is a **generation input**: authored text (`text_content`) of a
typed `kind` (image / video / motion / …) that later drives media generation.
It is closer to an *inference request / generation parameter* than to the
project's authored editorial content. A prompt is owned by a project and may
*optionally* be scoped to a single scene.

Position in the hierarchy (baseline schema fact):

```
Project  (α5a/α5b — root, owner/tenant-scoped)
  ├── Storyboard → Scene            (α5c — editorial content, versioned)
  └── Prompt  (prompts.project_id, CASCADE)   ← α6.1: the aggregate this doc defines
         · optional scene_id (SET NULL, immutable after create)
         · optional model_id  (SET NULL — ai_models registry link)
         Prompt ──drives──▶ Media Asset  (α6.2) ──placed as──▶ Clip ──on──▶ Timeline (α6.3)
```

The critical separation this document establishes:

> **Scenes are editorial state. Prompts are generation inputs.**

Scenes belong to the versioned Project aggregate (row-OCC + version snapshots).
Prompts do **not** — they are a production-pipeline artefact with their own
lifecycle. This is the boundary that keeps "restore my project to last Tuesday"
from also rewinding every experimental prompt edit.

---

## 2. Aggregate boundary

### 2.1 Inside the α6.1 Prompt (the slim domain surface)

The domain `Prompt` is a slim, frozen view of the physical row:

- `id` — durable UUID, server-minted.
- `project_id` — the owning project (ownership is *derived* through it; there
  is no `tenant_id` / `owner_user_id` on the row — F3).
- `scene_id` — optional link to a **live scene in the same project**. Set at
  create, **immutable** thereafter (no re-parenting in α6.1). `NULL` = a
  project-level prompt.
- `kind` — one of the 8 `prompt_kind` enum values
  (`image, video, animation, negative, camera, motion, lighting, style` —
  modality/aspect kinds, **not** chat-style `system`/`user` roles).
- `text_content` — the prompt body (`1 ≤ len ≤ 10000`, whitespace-stripped).
- `model_id` — optional link to an `ai_models` row ("which model this prompt is
  written for"), validated as existing + not `retired`.
- `extra` — free-form JSONB bag (default `{}`).
- `created_at` / `updated_at` — timestamps; `updated_at` is trigger-owned.

### 2.2 Physical-but-not-in-the-domain-surface

- `generated_by_agent` — server-owned provenance for AI-authored prompts.
  Stays `NULL` in α6.1 (human-authored); the AI-authorship seam is α8. **Not**
  client-supplied and **not** on the public DTO.
- `deleted_at` — soft-delete tombstone; internal, never on the wire.

### 2.3 The defining absence: **no `version` column**

`Prompt` is built from `UUIDPrimaryKeyMixin + TimestampMixin +
SoftDeleteMixin` — **no `VersionMixin`**. The table is absent from
`_VERSION_BUMP_TABLES` and carries only a `touch_updated_at` trigger. There is
**no per-row concurrency token** on a prompt, by design (see §4).

---

## 3. Ownership, scoping & anti-enumeration

Every endpoint is authenticated (`CurrentUserDep`) and runs a **two-level
visibility gate** (mirroring the α5c scene gate):

1. **Project gate** — `ProjectRepository.get_owned(project_id, tenant, owner)`;
   `None` → uniform `404 NOT_FOUND` (missing / soft-deleted / not the caller's).
2. **Prompt gate** — the prompt must be live **and** have
   `project_id == resolved project`; else the same `404`. A `prompt_id` under
   another user's project is indistinguishable from "never existed."

There is no uniqueness constraint on prompts (F4): multiple prompts of the same
`kind` on the same scene/project are legal. No dedupe, no collision path.

The scene/model links are validated as **`422 VALIDATION_FAILED`, not `404`**:
a foreign/unknown/soft-deleted `scene_id`, or an unknown/retired `model_id`,
means the *request body* is invalid — the route-target project is fine. `404`
is reserved for the two-level visibility gate.

---

## 4. Concurrency: last-writer-wins (no OCC), and why (ADR-0036)

Because a prompt has no `version` column (§2.3), α6.1 resolves its concurrency
model **within the existing schema** (no migration): a `PATCH` is a plain
project-gated update. There is **no version fence on the wire**, **no `412`**,
and a mutation does **not** bump `projects.version`. Two racing edits: the last
writer wins.

This is deliberate, not a shortcut:

- The baseline's omission of `VersionMixin` on `prompts` (while `scenes` *has*
  it) is a **signal**: prompts are generation inputs, not concurrency-guarded
  editorial rows. α6.1 follows the signal instead of fighting it.
- Prompts are low-contention authored text — usually created fresh, rarely
  co-edited — so last-writer-wins is an acceptable model.
- Coupling prompt edits to `projects.version` (the Aggregate OCC Rule token)
  would mean a prompt edit invalidates a concurrent *scene* edit's token and
  vice-versa, and would imply prompts belong in version snapshots — which they
  do not (§5).

`update_owned` still detects a **same-value no-op** at the use-case layer (no
write, no spurious `updated_at` bump) and supports **tri-state PATCH** (absent =
unchanged; explicit `model_id: null` clears the link; a value sets it —
`text_content`/`kind` are non-nullable so an explicit `null` is `422`).

---

## 5. Exclusion from version snapshots (ADR-0036)

The `project_versions` snapshot boundary is **{project root + default
storyboard + ordered scenes}** (ADR-0035) and nothing more. Prompts are
**excluded** — not captured, restored, or diffed. Restore's silence on prompts
is a **decision, not an omission**.

The governing principle, recorded verbatim in **ADR-0036**:

> **Project versions capture editorial state, not generation inputs. Prompts
> are mutable generation inputs that do not participate in aggregate optimistic
> concurrency, snapshots, restore, or diff. Generated media may retain the
> prompt used for provenance independently of the current prompt record.**

Consequences:

- Restoring a project to an old version does **not** resurrect old prompt
  wording. Users restore structure, ordering, timing, narrative — not every
  experimental prompt iteration.
- Provenance is preserved *downstream*, not in the editorial ledger: a media
  asset generated from a prompt can retain a `prompt_id` / prompt snapshot
  (α6.2), independent of the current prompt record.
- This precedent extends to later generation aggregates (media α6.2, timeline
  α6.3): they, too, stay outside the editorial snapshot.

### 5.1 Link durability across scene soft-delete / restore (F6)

`prompts.scene_id` is `ON DELETE SET NULL`, but **SET NULL fires only on a hard
`DELETE`** of the parent scene. Scenes are only ever **soft-deleted** (α5c), and
a version restore soft-deletes then revives scenes **under the same `id`**.
Therefore a prompt's `scene_id` link **survives** both a scene soft-delete and a
project restore — the link is never silently nulled by editorial operations.
This is covered by a load-bearing repository integration test.

---

## 6. Lifecycle

```
        create                         DELETE
  ∅ ───────────▶  live  ───────────────────────▶  soft-deleted
                   │  PATCH (last-writer-wins,      (deleted_at set)
                   └──  no version fence)           GET/PATCH/DELETE → 404
```

- **create** — `POST …/prompts`; identity + provenance server-owned. `201`.
- **live** — normal state (`deleted_at IS NULL`); readable, listable,
  patchable.
- **soft-deleted** — owner-scoped soft delete (`DELETE …/prompts/{id}` → `204`),
  no version fence, **idempotent-by-404**: a second delete — and any
  `GET`/`PATCH` after delete — is `404`; deleting another user's prompt or an
  unknown id is the same `404`.

Listing (`GET …/prompts`) is side-effect-free, newest-first
(`created_at` desc, `id` desc), soft-delete-excluded, with optional
`?kind=<enum>` and `?scene_id=<uuid>` filters (combined = AND; bad enum /
non-UUID → `422`). Not paginated in α6.1.

---

## 7. Structured-log posture

Prompt lifecycle events are logged with identifiers and *field names* only —
**never** content values (`text_content` / `extra` values are never logged):

- `prompt.created` (INFO) — `prompt_id`, `project_id`, `scene_id`, `kind`,
  `model_id`, `owner_user_id`, `tenant_id`, `ip`, `request_id`.
- `prompt.updated` (INFO) — `prompt_id`, `project_id`, `changed_fields`, `ip`,
  `request_id`.
- `prompt.update_rejected` (WARN) — `reason` (`not_visible`), `prompt_id`, `ip`,
  `request_id`.
- `prompt.deleted` (INFO) — `prompt_id`, `project_id`, `owner_user_id`, `ip`,
  `request_id`.
- `prompt.delete_rejected` (WARN) — `reason` (`not_visible`), `prompt_id`, `ip`,
  `request_id`.

---

## 8. Open evolution (explicitly out of α6.1)

- **Prompt history / audit.** If prompts ever need history, add a
  `prompt_runs` / `prompt_revisions` table — *without* changing the meaning of
  project snapshots. Choosing "no OCC, no snapshot" now does **not** foreclose
  richer history later; it just keeps it out of the editorial ledger.
- **Re-parenting.** Moving a prompt between scenes (`scene_id` mutation) is not
  a use case yet.
- **Model capability matching.** α6.1 validates `model_id` existence + not
  `retired` only; kind/capability compatibility belongs to generation (α6.2).
- **AI-authored prompts.** `generated_by_agent` population is α8.
- **Templates / variable interpolation / cursor pagination.** Separate
  features, untouched.

---

## 9. Change log

| Date | Change |
|---|---|
| 2026-07-12 | Initial authoring for Phase 3 α6.1 (Prompt CRUD). Establishes the generation-input identity, the two-level ownership gate, the no-OCC / last-writer-wins concurrency model, the exclusion from version snapshots (F6 link durability), and the lifecycle. Records the ADR-0036 governing principle verbatim. |
