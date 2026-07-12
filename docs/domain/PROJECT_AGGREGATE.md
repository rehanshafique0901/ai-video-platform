# Project Aggregate — Domain Design

> **Purpose.** The canonical reference for the `Project` aggregate root:
> what it is, who owns it, what lives inside its boundary, how it is
> scoped, and the rules every future slice touching Projects must honour.
> This is a **domain design document**, not an ADR — it describes the
> shape of a bounded-context aggregate rather than recording a single
> decision. It is the Projects-side counterpart to
> `docs/api/AUTH_ENDPOINTS.md` for the identity surface.
>
> **Status.** Living document. First authored ahead of Phase 3 α5a
> (Project creation + read). Sections marked *(α5b+)* describe behaviour
> that is designed here but not yet implemented — they set the contract
> so later slices slot in without re-litigating the model.
>
> **Grounding.** Schema: `docs/database/schema.md` §6–§9;
> ERD: `docs/database/ERD.md` (Cluster 6);
> ORM: `backend/app/infrastructure/db/models/projects.py`;
> API surface: `API_CONTRACT.md` §3.2 / §3.3.

---

## 1. What is a Project?

A **Project** is the top-level container for one piece of video work on
the platform — the aggregate root that every downstream artefact
(scenes, prompts, media assets, render jobs, timeline, exports, versions)
ultimately belongs to. It is the unit a user creates first, opens in the
editor, lists on their dashboard, duplicates, archives, and deletes.

In domain terms it is the **consistency boundary** for a single video's
authoring state: its name, format (`aspect_ratio`), language, style, and
free-form `settings`, plus the identity/versioning bookkeeping
(`owner_user_id`, `tenant_id`, `version`, timestamps). Everything a user
would think of as "this video project's settings" lives on the root row;
everything that is a *collection of things inside the project* (scenes,
assets, versions) is a child entity referenced by FK, not embedded state.

**One-line definition:** *A Project is the owner-scoped, tenant-bound
aggregate root that owns a single video's authoring state and is the
parent of all scene / prompt / asset / render / version children.*

---

## 2. Ownership

A Project has **two identity anchors**, both non-null FKs on the row:

| Field | Meaning | `ON DELETE` |
|---|---|---|
| `tenant_id` | The tenant (billing/isolation boundary) the project belongs to. | `RESTRICT` |
| `owner_user_id` | The user who created and owns the project. | `RESTRICT` |

**v1 reality.** Self-service signup (α2a) auto-creates exactly one tenant
per user, so today `tenant_id` ↔ `owner_user_id` is effectively 1:1. The
model nonetheless carries both because multi-user tenants (invitations,
seats) are on the roadmap, and retrofitting a tenant column onto a
live aggregate is far more painful than carrying it from day one.

**Scoping rule (authoritative).** Every read and write of a Project is
scoped by **both** `tenant_id` **and** `owner_user_id` derived from the
authenticated caller (`CurrentUserDep`) — never from client input. A
caller can only see and mutate projects that are (a) in their tenant and
(b) owned by them. When shared-tenant collaboration lands, the
*owner* filter is the one that relaxes (guarded by a role/permission
check); the *tenant* filter never does.

**Anti-enumeration.** A Project that exists but is outside the caller's
scope is reported as **`404 NOT_FOUND`**, never `403`. A client must not
be able to distinguish "no such project" from "someone else's project" —
the same posture α3 established for the auth surface.

### 2.1 Identity & addressing (no slug)

A Project is addressed **by UUID** — `API_CONTRACT.md` §1 mandates *"IDs:
UUIDv4. URLs use the ID as-is (no slugs)."* The `projects` table has **no
`slug` column**, and α5a introduces none. The human label is `name`,
which is unique per `(tenant_id, owner_user_id)` among live rows but is
**not** used for addressing.

**Documented future slug policy** (recorded to avoid a later migration
surprise — not implemented until/unless a pretty-URL feature is scoped):

- **Derivation:** `slugify(name)` — e.g. `"My First Video"` →
  `my-first-video`.
- **Uniqueness:** per `(tenant_id, owner_user_id)`, mirroring the `name`
  constraint — **not** global, **not** tenant-wide.
- **Mutability:** regenerated when `name` changes (mutable). No
  historical-slug redirect table in v1 — the UUID stays the canonical
  address, so a changed slug never breaks a stored link.
- **Migration shape:** additive nullable `slug` column + a partial-unique
  index matching the `name` index predicate (`WHERE deleted_at IS NULL`).
  Its own slice.

---

## 3. Aggregate boundary — inside vs outside

### Inside the boundary (root-row state)

These are attributes of the Project itself, mutated through Project
endpoints, and covered by the root's `version` fence:

- `name` — human label. Unique per `(tenant_id, owner_user_id)` among
  live rows (`WHERE deleted_at IS NULL`).
- `description` — optional free text.
- `aspect_ratio` — `horizontal` | `vertical` | `square` (DB `CHECK`).
- `language` — BCP-ish short code, default `en`.
- `style` — optional free text style hint.
- `settings` — JSONB bag for project-level editor/render preferences.
- `folder_id` — optional organisational parent *(assignment is α5b+)*.
- `duration_seconds` — derived from content; **not** user-set at create.

### Outside the boundary (child aggregates / references)

These are **not** embedded in the Project row and are **not** mutated
through Project CRUD — each is its own aggregate reached by its own
endpoints:

- **Storyboards** (`storyboards.project_id`, `ON DELETE CASCADE`) — a
  generated shot list. A project owns **one auto-created default
  storyboard** today and may own several once regeneration lands. Carries
  generation provenance (`generated_by`, `generated_at`).
- **Scenes** (`scenes.storyboard_id`, `ON DELETE CASCADE`) — the ordered
  shot list *inside a storyboard*, **not** a direct child of the project
  row. The α5c public API presents scenes under the project
  (`/projects/{id}/scenes`) by resolving them through the project's
  default storyboard; the storyboard stays implicit until multi-storyboard
  regeneration is a feature. (Schema fact corrected 2026-07-11: earlier
  revisions of this doc wrongly listed `scenes.project_id` — the ORM has
  always keyed Scene to `storyboard_id`.)
- **Prompts** (`prompts.*`) — generation inputs.
- **Media assets** (`media_assets.project_id`) — rendered/uploaded media.
- **Render jobs** (`render_jobs.project_id`) — render lifecycle.
- **Project versions** (`project_versions.project_id`) — the immutable
  snapshot ledger (see §6). `projects.current_version_id` is a *pointer*
  into this ledger, managed by the versioning slice — not part of the
  create/read surface.
- **Tags** (`project_tags` join) — labelling *(α5b+)*.

**Rule of thumb:** if it is a *collection of things the project contains*,
it is a child aggregate with its own endpoints. If it is *a property of
the project as a whole*, it lives on the root row.

---

## 4. Lifecycle

```
        create                (α5b) archive?              (α5b) DELETE
  ∅ ───────────▶  active  ───────────────▶  archived  ───────────────▶  soft-deleted
                    │                                                     (deleted_at set)
                    └───────────────────────── DELETE ───────────────────┘
```

- **active** — the normal state. `deleted_at IS NULL`. All reads return it.
- **archived** *(α5b+, optional)* — a product-level "hidden from the main
  list but not destroyed" state. If introduced, it is a status flag, not
  a separate table. Not modelled in α5a.
- **soft-deleted** — `deleted_at` is set. The row is retained (for
  restore / audit / cascade integrity) but is invisible to every scoped
  query. Deletion is **soft** by default; hard deletion is an ops/retention
  concern, never an API action.

α5a implements only `∅ → active` (create) and reading active rows. The
`archived` and `soft-deleted` transitions are designed here but shipped
in α5b (`DELETE /projects/{id}`).

---

## 5. Mutability rules

| Field | Set at create | Mutable later | Notes |
|---|---|---|---|
| `id` | server-generated | never | UUIDv4, assigned by the app. |
| `tenant_id` | from caller | never | Immutable identity anchor. |
| `owner_user_id` | from caller | never (v1) | Ownership transfer is a future, deliberate feature. |
| `name` | ✅ required | ✅ *(α5b)* | Unique per owner among live rows. |
| `description` | optional | ✅ *(α5b)* | |
| `aspect_ratio` | ✅ required | ✅ *(α5b)* | Constrained set. |
| `language` | optional (`en`) | ✅ *(α5b)* | |
| `style` | optional | ✅ *(α5b)* | |
| `settings` | optional (`{}`) | ✅ *(α5b)* | JSONB merge/replace policy decided in α5b. |
| `folder_id` | ✗ *(α5b)* | ✅ *(α5b)* | Move-to-folder is its own operation. |
| `duration_seconds` | ✗ | system-set | Derived from timeline/render, never client-set. |
| `current_version_id` | ✗ | system-set | Managed by the versioning slice. |
| `version` | server `1` | system-only | OCC fence; increments only on a real persisted change (see §6). |
| `created_at` / `updated_at` | server | system-only | `updated_at` bumps on real change only. |
| `deleted_at` | `NULL` | system-only *(α5b)* | Set by soft-delete; never client-set directly. |

**Client-controlled at create (α5a):** `name`, `aspect_ratio`, and
optionally `description`, `language`, `style`, `settings`. Everything
else is server-derived or deferred.

---

## 6. Versioning rules

The Project participates in **two distinct versioning mechanisms** that
must not be confused:

1. **Optimistic-concurrency `version` (root row).** An integer on the
   `projects` row, starting at `1`, used exactly as α4/ADR-0034 defined
   for users: the client round-trips the last-observed `version` on a
   mutation, the repository does a compare-and-swap, and a stale value
   yields `412 VERSION_CONFLICT`. It increments **only when a persisted
   field actually changes** — never on read, never on a same-value PATCH.
   `ProjectPublic` exposes it so α5b's `PATCH` has its fence. This is the
   only versioning surface α5a touches.

2. **`project_versions` snapshot ledger (child aggregate, CR-6).** An
   immutable, append-only history of full project snapshots
   (`project_versions` table, `reason` enum, `snapshot` JSONB), pointed at
   by `projects.current_version_id`. This is the *product* "version
   history / restore" feature — a separate slice with its own endpoints
   (`API_CONTRACT.md` §3.3). **α5a did not create, read, or reference it**;
   new projects leave `current_version_id` NULL until first capture.
   **Shipped in α5d.1 (capture + list + get):** a capture serializes on the
   project row, assigns a monotonic `version_number`, links a
   `parent_version_id` lineage chain, stores a canonical JSONB snapshot
   (project root + default storyboard + full-row ordered scenes, `Numeric`
   as decimal strings, scene ids preserved), and advances
   `current_version_id` (which bumps the row `version` by one). The ledger
   is append-only, DB-enforced by a `reject_mutation` trigger. Full
   semantics — restore-by-new-version, identity preservation, the hard-delete
   constraint — live in **ADR-0035**. Restore / branch / diff are α5d.2.

**Do not conflate them:** the row `version` is a concurrency guard; the
`project_versions` ledger is a user-facing history. A capture (and, later, a
restore) appends to the ledger and, via the `current_version_id` repoint,
bumps the row `version` as incidental bookkeeping — the append, not the bump,
is the versioning act (ADR-0035 D1).

---

## 7. Invariants (must always hold)

1. `tenant_id` and `owner_user_id` are non-null and immutable.
2. A live project's `name` is unique within `(tenant_id, owner_user_id)`
   (partial unique index, `WHERE deleted_at IS NULL`). Two soft-deleted
   projects may share a name with a live one.
3. `aspect_ratio ∈ {horizontal, vertical, square}` (DB `CHECK`).
4. Every scoped query filters `deleted_at IS NULL`.
5. Reads/writes are scoped by caller `tenant_id` **and** `owner_user_id`;
   out-of-scope access is `404`, not `403`.
6. `version` increments monotonically and only on a real persisted change.
7. The Project is created through the application layer (a use case), which
   generates the `id` and initial `version`; the client never supplies them.

---

## 8. Future relationships (designed, not yet built)

```
Project (root)
 ├── Storyboards       storyboards.project_id           (α5c; one default auto-created)
 │     └── Scenes      scenes.storyboard_id             (α5c — ordered by scene_number)
 │            └── Prompts   prompts.* (scene-scoped)    (later)
 ├── Media Assets      media_assets.project_id          (α6) ← generated *from* scenes
 ├── Timeline          timelines.project_id (1:1)       (α6b) → Tracks → Clips → media_assets
 ├── Render Jobs       render_jobs.project_id           (α7)
 ├── Versions          project_versions.project_id      (α5d.1 capture+read; restore/branch α5d.2)
 ├── Tags              project_tags (join)              (later)
 └── Folder (parent)   folders.id  ← projects.folder_id (later move-to-folder)
```

> **Media pipeline direction (baseline schema fact).** The `Timeline`
> does **not** own scenes. Scenes generate **Media Assets**
> (`media_assets`), assets are placed as **Clips** on **Tracks** inside the
> `Timeline` (`clips.media_asset_id`, `ON DELETE SET NULL`). The chain is
> `Scene → Media Asset → Clip → Timeline`, so scene ordering
> (`scene_number`) lives with the storyboard/scene, never with the
> timeline. This keeps rendering downstream of, and decoupled from, the
> editorial shot list.

Each child arrives in its own slice and reuses the same ownership +
tenant-scoping + anti-enumeration rules defined here. When a child slice
lands, it references this document rather than re-deriving the scoping
model.

---

## 9. Change log

| Date | Change |
|---|---|
| 2026-07-11 | Initial authoring ahead of Phase 3 α5a (create + read). Sections 4/5/6 mark α5b+ behaviour as designed-not-shipped. |
| 2026-07-11 | Added §2.1 (identity & addressing — no slug; documented future slug policy) per α5a reviewer sign-off (pre-flight D15). |
| 2026-07-11 | **Corrected child-aggregate drift ahead of α5c (Scenes):** the boundary now reads *Project owns Storyboards, Storyboard owns Scenes* (`scenes.storyboard_id`, not the previously-listed `scenes.project_id`). §8 diagram redrawn with the `Project → Storyboard → Scene` hierarchy and the `Scene → Media Asset → Clip → Timeline` media-pipeline direction. See `docs/domain/SCENE_AGGREGATE.md` and `docs/engineering/PHASE3_ALPHA5C_PREFLIGHT.md` (D1/D4). |
| 2026-07-12 | **§6 updated for α5d.1 (Project Versions capture + read):** the snapshot ledger is now shipped (capture / list / get). Documented the monotonic-numbering + lineage + canonical-snapshot + current-pointer-advance semantics and cross-referenced **ADR-0035**. §8 diagram: `Versions` marked α5d.1 (restore/branch α5d.2); `Prompts` re-marked *later* (α5d is versions, not prompts). |
