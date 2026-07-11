# Scene Aggregate

> **Convention.** This is a domain design document (companion to
> `docs/domain/PROJECT_AGGREGATE.md`). It defines the **Scene** aggregate —
> its identity, boundary, ordering model, lifecycle, and the deliberate
> gap between the **fat physical table** and the **slim domain model**
> exposed by the API. It is the design authority for Phase 3 **α5c**
> (Scene CRUD). Read it before the α5c pre-flight
> (`docs/engineering/PHASE3_ALPHA5C_PREFLIGHT.md`).
>
> **Grounding.** Every schema claim here is checked against the live ORM
> (`backend/app/infrastructure/db/models/scenes.py`) and the baseline
> migration (`backend/alembic/versions/0001_baseline.py`), not against an
> idealised model. Where the baseline diverged from earlier conceptual
> sketches, the baseline wins (it has already saved us from bypassing
> Storyboards).

---

## 1. Purpose & position in the model

A **Scene** is one shot in a video's editorial shot list: a titled,
timed unit carrying the spoken/written content that later drives media
generation. Scenes are the first piece of *real project content* — until
they exist, a Project is little more than a titled settings row.

Position in the hierarchy (baseline schema fact):

```
Project  (α5a/α5b — root, owner/tenant-scoped)
  └── Storyboard  (storyboards.project_id, CASCADE) ← α5c: implicit, one default per project
        └── Scene  (scenes.storyboard_id, CASCADE)  ← α5c: the aggregate this doc defines
              └── Prompt  (prompts.scene_id, SET NULL)  ← α5d
        Scene ──generates──▶ Media Asset ──placed as──▶ Clip ──on──▶ Timeline  (α6/α6b)
```

The Scene is **not** the timeline. Scene ordering is editorial and lives
with the storyboard; the timeline is a *downstream* consumer of media
assets that scenes generate (see §7). This keeps rendering decoupled from
the shot list.

---

## 2. The Storyboard intermediary (implicit in the α5c API)

### 2.1 Why Storyboard exists

The baseline keys scenes to a **Storyboard**, not directly to a Project
(`scenes.storyboard_id`, not `scenes.project_id`). A Storyboard is a
*generated shot list* carrying provenance (`generated_by ∈
{'system','user'}`, `generated_at`, optional `project_version_id`). This
lets a project eventually hold **several** storyboards — e.g. multiple AI
regenerations of the same script — without schema change.

### 2.2 α5c policy: one implicit default storyboard

α5c does **not** expose storyboards. It presents scenes directly under the
project (`/projects/{id}/scenes`) and resolves them through the project's
**default storyboard**:

* **Default = the earliest live storyboard** for the project, ordered
  `(created_at ASC, id ASC)`. Deterministic and index-backed
  (`ix_storyboards_project_id_created_at`).
* **Auto-create on first need.** If a project has no live storyboard when
  a scene is first created (or listed), α5c lazily creates one with
  `generated_by = 'system'`, `project_version_id = NULL`.
* **Race-safety.** Because `storyboards` has **no** `is_default` column and
  **no** per-project uniqueness, two concurrent "first scene" writes could
  otherwise create two storyboards and split scenes across them. α5c
  serialises default-storyboard creation by taking a **row lock on the
  parent `projects` row** (`SELECT … FOR UPDATE`) inside the same
  transaction before the get-or-create. This guarantees exactly one
  default storyboard per project with no migration.

### 2.3 Future: multi-storyboard, zero API breakage

When regeneration ships, `/projects/{id}/storyboards` and
`/projects/{id}/storyboards/{sid}/scenes` can be added; the α5c
`/projects/{id}/scenes` shorthand continues to mean "the default
storyboard's scenes". No α5c endpoint changes shape.

---

## 3. Scene identity & addressing

* **Identity:** server-assigned `id uuid` (`gen_random_uuid()`), stable and
  opaque. Scenes are addressed by UUID under a project:
  `/projects/{project_id}/scenes/{scene_id}`.
* **No slug** (same posture as Project α5a D15). Scenes are ordered, not
  named-addressed.
* **`scene_number` is not the address** and is never surfaced raw — it is
  an internal ordering key (§5). Clients see a computed 1-based
  `position`, never `1000`/`2000`.
* **Storyboard is implicit** — `storyboard_id` is not part of the α5c
  public representation (the default storyboard is resolved server-side).

### 3.1 Identity is stable across Version restores (contract, α5c-forward)

A Scene's `id` is a **durable identity**, minted once
(`gen_random_uuid()`) and **never regenerated** for the life of the scene.
When Project Versions arrive (post-α5c), a snapshot captures scene
**content keyed by the existing scene `id`**, and a restore re-materialises
that content **under the same `id`** — it does **not** mint new scene
identities. This is a forward contract documented now (α5c has no restore
yet) because durable scene identity is load-bearing for nearly every future
feature — comments, analytics, AI regeneration, cross-version diffing, and
collaboration all key off a stable scene id; re-minting on restore would
silently break every such reference. **Implication:** never treat a scene
`id` as ephemeral or derived, in α5c or later.

---

## 4. Aggregate boundary — slim domain over a fat table (D2)

The physical `scenes` table is deliberately **fat** (cinematography +
audio columns). α5c keeps the table untouched and exposes a **slim** Scene
domain model. The rich columns stay physical, remain `NULL` for
α5c-created scenes, and become the storage for future child configs
**without a migration or an API change**.

### 4.1 Inside the α5c Scene (the slim write/read surface)

| Domain field | Column | Notes |
|---|---|---|
| `id` | `id` | server UUID |
| `title` | `title` (`text NOT NULL`) | **required** on create; `1 ≤ len ≤ 200` (app rule) |
| `duration_seconds` | `duration_seconds` (`numeric(8,3) NOT NULL`, CHECK `> 0`) | **required** on create; `0 < d < 100000` (fits `numeric(8,3)`); Decimal↔float at the repo edge |
| `narration` | `narration` (`text`) | optional — the scene's spoken script/voiceover text (this is the "script") |
| `subtitle` | `subtitle` (`text`) | optional — on-screen caption |
| `version` | `version` (`integer`, trigger-bumped) | OCC fence |
| `created_at` / `updated_at` | mixins (trigger-touched) | audit |
| `deleted_at` | `deleted_at` | soft-delete marker (not exposed as a value; drives visibility) |
| *(ordering)* | `scene_number` (`integer NOT NULL`) | internal; surfaced only as computed `position` |
| *(context)* | `storyboard_id` (`uuid NOT NULL`) | internal; the project's default storyboard |

**`ScenePublic` (API projection):** `id`, `project_id` (from the path
context), `position` (computed 1-based), `title`, `duration_seconds`,
`narration`, `subtitle`, `version`, `created_at`, `updated_at`. It
**omits** `storyboard_id`, raw `scene_number`, `deleted_at`, and every
deferred column below.

### 4.2 Physical-but-deferred (present in the table, NOT in α5c)

`emotion`, `camera_angle`, `camera_motion`, `lens`, `lighting`, `weather`,
`location`, `animation`, `transition_in`, `music_mood`, `extra` (JSONB).
These remain in the table, are never written or read by α5c, and are the
landing zone for α5d child configs:

```
Scene (α5c: title/duration/narration/subtitle)
  ├── VoiceConfig      → emotion, narration, (voice_* in extra)     (α5d)
  ├── CameraConfig     → camera_angle/motion, lens                  (α5d)
  ├── AnimationConfig  → animation, transition_in                   (α5d)
  └── MusicConfig      → music_mood                                 (α5d)
```

Each child aggregate can first map onto these columns and later migrate to
its own table with **no** change to `/projects/{id}/scenes`.

### 4.3 Not on the table at all (needs a future migration)

* **`status`** — there is **no** status column. α5c does **not**
  fabricate one (that would be a migration, which D2 forbids for this
  slice). Scene lifecycle in α5c is `live → soft-deleted`, expressed by
  `deleted_at`, not a status enum. A richer `status` (draft/ready/…) is a
  deliberate later slice.
* **`prompt`** — prompts are a **separate** aggregate (`prompts` table,
  `prompts.scene_id`), arriving in α5d. A Scene does not embed a prompt.

> **Reconciliation note (surfaced to the reviewer).** The α5c-approval
> sketch listed a slim Scene of `{id, storyboard_id, scene_number, title,
> script, prompt, duration, status, deleted_at, version}`. Mapped onto the
> real baseline: **`script` → `narration`** (existing column),
> **`prompt` → the `prompts` child table (α5d)**, **`status` → does not
> exist (deferred, needs a migration)**. α5c therefore ships
> `title / duration_seconds / narration / subtitle` as the core content
> surface — the honest intersection of "slim editorial content" and
> "columns that already exist".

---

## 5. Ordering model — `scene_number` as a sparse gap key (D3)

* **Reuse `scene_number`** (already `integer NOT NULL`, already covered by
  the partial-unique index `uq_scenes_storyboard_id_scene_number` WHERE
  `deleted_at IS NULL`). **No new `position` column, no migration.**
* **Sparse gaps.** Scenes are numbered `1000, 2000, 3000, …` (step
  `1000`). Appending a scene uses `max(scene_number) + 1000` (or `1000`
  for the first). Inserting between two scenes uses the **midpoint**
  (between `1000` and `2000` → `1500`; between `1000` and `1500` →
  `1250`).
* **Uniqueness.** `(storyboard_id, scene_number)` is unique among **live**
  scenes (partial index). Soft-deleting a scene **frees** its number.
* **Display position is computed.** The API returns a dense 1-based
  `position` derived from the live scenes sorted by `scene_number ASC`.
  Clients never see the raw key; the "single source of truth" is the
  sorted order, not a duplicated position column.
* **Rebalance (rare).** If a midpoint insert has no integer room (adjacent
  keys differ by `1`), α5c reassigns the storyboard's live scenes to a
  fresh `1000, 2000, 3000, …` sequence in one transaction. Because the
  version-bump trigger fires on every touched row, a rebalance bumps those
  scenes' `version`; this is an accepted, infrequent trade-off (documented
  so a concurrent editor understands a `412` after a rebalance is "reorder
  happened, re-read").

---

## 6. Lifecycle & concurrency

```
        create (append)              PATCH (content)          move (reorder)
   ──────────────────────▶  live  ─────────────────────▶  live  ───────────▶ live
                             │  ▲   version-fenced CAS      (scene_number CAS)
                     DELETE  │  │
                (soft, 204)  ▼  │  GET/PATCH/DELETE after delete → 404
                        soft-deleted  (deleted_at set; scene_number freed)
```

* **OCC everywhere content changes.** `PATCH` and `move` carry a required
  `version` fence and use the α4/α5b compare-and-swap
  (`UPDATE … WHERE version = :expected`), hand-setting `version = version +
  1` exactly as `ProjectRepository.update_owned` does. The baseline
  `bump_version()` trigger is **guarded** (`IF NEW.version = OLD.version`),
  so the hand-set `+1` makes it no-op and the net increment is exactly +1.
* **404-before-412** (α5b pattern, two-level). Every scene endpoint first
  establishes **project** visibility (owned + live → else `404`) and then
  **scene** visibility (belongs to the project's default storyboard + live
  → else `404`), and only then applies the version fence (`412`).
  Existence never leaks via a `412`.
* **Soft delete** (`deleted_at = now()`), owner-scoped, **unconditional**
  (no version fence — mirrors α5b Project DELETE), returns `204`.
  **Idempotent-by-404**: a second `DELETE`, and any `GET`/`PATCH`/`move`
  after delete, returns `404`. Deleting frees the `scene_number` and the
  remaining scenes' computed positions close up automatically (no
  renumber needed).
* **Create appends.** α5c create always appends to the end; repositioning
  is done via the reorder (`move`) endpoint. (Insert-at-position on create
  is a deferred convenience.)

---

## 7. Relationship to Media & Timeline (downstream — D4)

The baseline pipeline is `Scene → Media Asset → Clip → Timeline`:

* A **Scene** is an editorial unit; it later **generates** one or more
  **Media Assets** (`media_assets`, α6).
* **Clips** (`clips.media_asset_id`, `ON DELETE SET NULL`) place assets on
  **Tracks** inside the single per-project **Timeline** (`timelines`,
  1:1 with project, α6b).
* The **Timeline never owns scenes** and never owns scene ordering. So
  soft-deleting a scene in α5c cannot break a timeline: clips reference
  *assets*, not scenes, and no α5c-era scene has generated assets yet.
* `prompts.scene_id` is `ON DELETE SET NULL`, but soft delete does not fire
  FK actions; α5d prompt handling treats a soft-deleted scene as invisible.

Roadmap wording therefore reads `Scenes → Media → Timeline`, not
`Scenes → Timeline`.

---

## 8. Scoping & anti-enumeration

Scenes inherit the Project aggregate's rules (PROJECT_AGGREGATE §2/§3):

* **Owner + tenant scoped through the project.** A caller may only touch
  scenes of a project they own within their tenant. The project gate is
  the α5a `get_owned(project_id, tenant_id, owner_user_id)`.
* **Uniform `404`** for: unknown project, project owned by another
  user/tenant, soft-deleted project, unknown scene id, scene belonging to
  a different project, or soft-deleted scene. No `403`, no existence leak.
* **No cross-project scene access** — a `scene_id` that exists but sits
  under a different project's storyboard is a `404`, decided by joining
  scene → storyboard → project and re-checking ownership.

---

## 9. Structured-log posture

Scene events log **ids and field names only**, never content values
(`title`/`narration`/`subtitle` text is never logged) — same GDPR-minimal
posture as α4/α5a/α5b. Ordering events log `previous_position` /
`new_position` (computed), not raw `scene_number`.

---

## 10. Open evolution (explicitly out of α5c)

| Later | What it adds | Where it lands |
|---|---|---|
| α5d | Scene child configs (Voice/Camera/Animation/Music), Prompts | maps onto the deferred columns / `prompts` table |
| α5d/α6 | Scene `status` enum (draft/ready/generating/…) | **new column — migration** |
| α6 | Media assets generated from scenes | `media_assets` |
| α6b | Timeline / tracks / clips | `timelines` → `tracks` → `clips` |
| later | Multi-storyboard (regeneration), explicit `/storyboards` API | `is_default` or "active storyboard" pointer |
| later | Insert-at-position on create; bulk reorder; fractional keys | reorder ergonomics |
| Versions | Snapshot of storyboard + ordered scenes + content | `project_versions` |

---

## 11. Change log

| Date | Change |
|---|---|
| 2026-07-11 | Initial authoring ahead of Phase 3 α5c (Scene CRUD). Records D1–D4 outcomes: implicit default storyboard, slim domain over the fat table, `scene_number` gap ordering, timeline downstream of media. Surfaces the `script→narration` / `prompt→prompts` / `status→(absent)` reconciliation against the baseline. |
| 2026-07-11 | Added §3.1 — Scene `id` is a durable identity, stable across future Version snapshot/restore (pre-flight Q8/D16). |
