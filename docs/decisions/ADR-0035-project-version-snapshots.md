# ADR-0035 — Project Version Snapshots (Immutable Content Ledger, Restore-by-New-Version, Identity Preservation)

**Status:** Proposed (documents patterns shipped in Phase 3 α5d.1 — Project Versions capture + read — and α5d.2 — restore + diff + the Aggregate OCC Rule). Flips to Accepted on merge of this ADR PR.
**Refines / documents:** `docs/domain/PROJECT_AGGREGATE.md` §6 (the two versioning mechanisms + the Aggregate OCC Rule), `schema.md` §9 (`project_versions`), `API_CONTRACT.md` §3.3, and the α5d pre-flights (`docs/engineering/PHASE3_ALPHA5D_PREFLIGHT.md`, `docs/engineering/PHASE3_ALPHA5D2_PREFLIGHT.md`). Builds on ADR-0034 (authenticated endpoint pattern), the α5a Project aggregate, α5b soft-delete + OCC PATCH, and the α5c Scene aggregate.
**Wave:** Phase 3, content-versioning slice (α5d.1 read path: capture + list + get; α5d.2: restore + diff + Aggregate OCC Rule; α5d.3: branch = fork-to-new-project + `branched_from` provenance, D12; autosave deferred to α5d.4+).

---

## Context

A Project accumulates authoring state — root fields plus an ordered list of
scenes (α5c). Users need a **version history**: capture the project's state
at a point in time, browse that history, and read any past state back. This
is a different concern from the row-level optimistic-concurrency counter
(`projects.version`) introduced in α5b, and the two are routinely confused.

The physical schema already committed several decisions before α5d began (see
the α5d pre-flight §2), so this ADR records the *semantics* layered on top of
that schema rather than re-litigating the table shape:

- `project_versions` carries a single `snapshot JSONB NOT NULL` column, is in
  `EXPECTED_IMMUTABLE`, uses `CreatedAtOnlyMixin`, and is guarded by a
  `reject_mutation` BEFORE-UPDATE/DELETE trigger.
- `version_number INTEGER` with `UNIQUE (project_id, version_number)`.
- `parent_version_id` self-FK (`ON DELETE RESTRICT`, `CHECK id <> parent`).
- `created_by_user_id` + `reason version_reason_enum ∈ {manual_save, autosave,
  restore, branch, generated}`.
- `projects.current_version_id → project_versions ON DELETE SET NULL`.

Without an ADR, a future contributor sees an append-only table, a `reason`
enum with a `restore` value, and a `current_version_id` pointer, but not *why*
restore must not mutate history, *why* the snapshot captures full scene rows
while the API exposes a slim scene, or *why* versions are UUID-addressed. This
ADR promotes those from "implemented convention" to "recorded decision."

---

## Decision

### D1 — A version is an immutable content snapshot, distinct from row-OCC

`projects.version` and the `project_versions` ledger are **two unrelated
mechanisms** and must stay that way:

- **`projects.version`** (row OCC) — a concurrency guard. It bumps on every
  persisted mutation of the live project row (α5b PATCH fence, and the
  `current_version_id` repoint in D4). It answers "did the row change under
  me?" It is **not** a user-facing history.
- **`project_versions`** (content ledger) — user-facing, append-only history.
  Each row is a frozen, self-describing JSON snapshot of the project's
  authoring state. It answers "what did this project look like at save N?"

Capturing a version is not a "mutation" of the project's content; it is an
*append* to the ledger plus a pointer advance. The row-OCC bump that results
from the pointer advance (D4) is incidental bookkeeping, not the versioning
mechanism.

### D2 — Immutability is DB-enforced; restore appends, never mutates

`project_versions` is append-only, enforced by the `reject_mutation` trigger
(no UPDATE, no DELETE at the DB layer — proven by an integration test that
asserts a direct `UPDATE` raises). The consequence, recorded here so it is not
rediscovered later:

> **A restore can never modify or delete an existing version.** Restore (α5d.2)
> = *append a new version* (`reason=restore`, `parent_version_id` = the version
> being restored) + *rewrite the live child rows from that snapshot* + *repoint
> `projects.current_version_id`*. History is monotonic and permanent; "going
> back" moves forward in the ledger.

### D3 — Snapshot boundary: project + default storyboard + full-row ordered scenes

A snapshot captures the project's **authoring intent**:

- **Project root fields** (`name`, `description`, `aspect_ratio`,
  `duration_seconds`, `language`, `style`, `settings`) plus the root `version`
  at capture time (provenance).
- **The default storyboard identity** (the implicit storyboard α5c
  auto-creates), or `null` if none exists yet.
- **All live scenes** (`deleted_at IS NULL`), **ordered by `scene_number`
  ascending**, each captured as its **full physical row** — every "fat" column
  plus `id` and `scene_number`.

It **excludes** derived / not-yet-API-managed artifacts: prompts, media assets,
render jobs, timeline/clips, tags, folder placement. Freezing those into the
snapshot boundary before they are API-managed would lock a contract we cannot
yet honor.

**Full-row (not the slim α5c view) is deliberate:** the snapshot must be
*restore-ready* even though restore is deferred. Capturing every column now
(cheap in JSONB) lets α5d.2 round-trip faithfully without a format migration,
and covers future subsystems that write the fat fields. Ordering is part of
the snapshot: because scenes are captured in `scene_number` order *and* each
row's `scene_number` is stored, a restore reproduces both the *content* and the
*canonical ordering*, not merely the set of scenes.

### D4 — Capture serializes on the project row and advances the current pointer

A capture (`POST …/versions`) runs under the α5c project-row lock
(`SELECT … FOR UPDATE` on `projects`), which serializes concurrent captures so
the monotonic `version_number = MAX + 1` assignment cannot race into a unique
violation. Within the same transaction it:

1. Assigns the next `version_number` (1, 2, 3 …).
2. Links `parent_version_id` to the project's **previous** `current_version_id`
   (a linear lineage chain; branch forks to a *new* project rather than
   creating a non-linear head, so each project's chain stays linear — D12).
3. Inserts the immutable snapshot with `reason = manual_save`.
4. Repoints `projects.current_version_id = new.id`, which bumps
   `projects.version` by exactly one (row-OCC trigger). The newest manual save
   becomes current.

`reason` is **server-set to `manual_save`** in α5d.1; the API accepts no client
`reason`. `restore` is α5d.2, `branch` is α5d.3 (D12), `autosave` is background
(α5d.4+), `generated` is the generation pipeline (α7+).

### D5 — Scene identity is preserved across capture (and future restore)

The snapshot stores each scene's real `id` (re-affirming α5c Q8). A future
restore **reuses those ids** rather than minting new ones, so comments,
analytics, and cross-references that point at a scene survive a restore. α5d.1
snapshots are therefore already restore-ready on the identity axis.

### D6 — Canonical, hash-stable serialization

The snapshot JSON is canonical: a leading `schema_version` integer,
sorted-key project/scene objects, and — critically — **`Numeric` values
serialized as decimal strings, never floats** (`duration_seconds`
`"5.000"`, not `5.0`). This keeps snapshots diffable and hash-stable, which
makes the α5d.2 `diff_summary` and any future integrity hashing trivial and
prevents float-drift from making two "equal" snapshots differ.

### D7 — Versions are UUID-addressed; `version_number` is a label

Version resources are addressed by their UUID `id` in the URL
(`/projects/{id}/versions/{version_id}`), keeping the whole API UUID-addressed
(projects, scenes, versions) so routing, authorization, and repository code
stay uniform. `version_number` is exposed in the body as the user-facing label,
not the routing key.

### D8 — Two-level ownership gate; anti-enumeration 404s

Every version endpoint is authenticated (`CurrentUserDep`, ADR-0034 D1) and
runs the **project ownership gate first** (`projects.get_owned` → uniform
`404` if the project is missing / soft-deleted / not the caller's), then — for
single-version GET — checks the version belongs to that project (→ `404`). A
version that exists under another project is indistinguishable from
"never existed." This mirrors the α5c scene gate and the α2/α3
anti-enumeration stance.

### D9 — `projects.version` is the aggregate-wide OCC token (Aggregate OCC Rule)

Restore needs a fence that answers *"has anything in this project changed since
the user opened the restore screen?"* — including a **scene edit**, not just a
root-column PATCH. In α5c, scene mutations bumped only `scenes.version`, so a
project-level fence could miss a concurrent scene edit. α5d.2 closes that gap by
promoting the root counter to an aggregate-wide token:

> **`projects.version` is the optimistic-concurrency token for the entire
> Project aggregate. Any mutation of any aggregate child that changes externally
> observable project state MUST increment `projects.version`.**

Concretely, the α5c scene `create` / `update` / `move` / `delete` paths now also
bump `projects.version` (via `IProjectRepository.touch_version`), **guarded so a
no-op edit does not bump** (a same-value PATCH, a move that doesn't change
order). Restore fences on this token: the value the caller last observed is
invalidated by *any* observable aggregate change. This is the contract every
future child aggregate (Timeline, Assets, Tags, Branch) inherits — a small,
cheap cross-cutting change made now rather than after those aggregates exist.

### D10 — Restore is a single-transaction, scene-reconciling, exactly-one-bump append

Restore (`POST …/versions/{id}/restore`) runs entirely under the α5c project-row
lock in **one transaction**, `404`-before-`412` (ownership + version-belongs-to-
project gates precede the fence; stale aggregate token → `412` with **zero
writes**). On a passing fence it:

1. Loads the source snapshot and asserts `aspect_ratio` **invariance** — it is
   immutable (never rewritten); a divergence is corruption, surfaced not hidden.
2. Rehomes under the **live** default storyboard (`ensure_default_storyboard`);
   the snapshot's `storyboard.id` is provenance only.
3. **Reconciles scenes keyed on `id`** — blanket soft-deletes every live scene
   first (emptying the `WHERE deleted_at IS NULL` partial unique index on
   `(storyboard_id, scene_number)`), then upserts each snapshot scene with its
   captured `scene_number` verbatim. A snapshot id whose physical row was
   soft-deleted is **revived in place** (clear `deleted_at`, rewrite columns — an
   INSERT would collide on the PK); an id with no physical row is **inserted**
   with the snapshot's UUID; rows absent from the snapshot are left soft-deleted.
   Because the index is empty during the rewrite, permuted orderings (a move
   between capture and restore) cannot collide.
4. Captures a trailing `reason=restore` version (`parent_version_id` = the
   **source** version, per D2), rewrites the mutable root, advances
   `current_version_id`, and hand-sets `version = version + 1` — a **single**
   `projects` UPDATE that produces **exactly one** aggregate bump (D9). The
   guarded trigger no-ops because the statement already changed `version`.

All-or-nothing: an injected mid-restore failure rolls the whole transaction back
to zero writes (proven by an integration test). Restore returns `200` (it
mutates the live project and returns its new current version — the version row
is a side effect), whereas capture returns `201`.

### D11 — Diff is computed on demand, coarse, and never persisted

`GET …/versions/{id}/diff?against={base}` computes a change summary **on the fly**
from the two stored snapshots — nothing is written (the `diff_summary` column
stays reserved for a future materialized/branch use). Both versions are gated to
the caller's owned project (uniform `404` on either side); `against` is required.
The α5d.2 shape is deliberately **coarse**: `project_changed` (business columns
differ), and scene `added` / `removed` / `modified` counts keyed by scene `id`
(present-in-target-not-base, present-in-base-not-target, present-in-both-with-
different-content). Field-level diffs are deferred — the canonical serialization
(D6) keeps a richer diff cheap to add later.

### D12 — Branch = fork a snapshot into a new independent project (α5d.3)

`POST …/versions/{version_id}/branch` **forks** a historical snapshot into a
**new, independently-editable project** owned by the caller (α5d.3 pre-flight Q1
Option A). This is the only migration-free reading of "branch" that is genuinely
distinct from restore (D10): restore rewinds *this* project onto an old
snapshot; branch leaves the source **untouched** and creates a *fresh* aggregate
seeded from the chosen version's content. Branch does not mutate the source, so
— unlike restore — it has **no OCC fence** and does **not** bump the source
`projects.version` (D9 does not apply; there is no source write). The new
project is materialized from the snapshot: root fields copied (including the
otherwise-immutable `aspect_ratio` — a fork legitimately seeds it; `name` comes
from the request body), scenes re-materialized with **freshly-minted** ids (a
new project is a new identity space — the opposite of restore's id-preservation
in D10, because the source scenes still live in the source project), and a
`reason=branch` `v1` captured via the canonical builder (D6). That `v1` has
`parent_version_id = NULL` (a fresh root — lineage stays self-contained per
project, never a cross-project `parent`) and embeds a structured
**`branched_from`** provenance block inside its snapshot:

```json
"branched_from": { "project_id": "…", "version_id": "…", "version_number": 2 }
```

The same block is echoed in the response `meta.branched_from`. The new project's
current pointer is then advanced to `v1`, so its `version` follows the normal
"created + first capture" arc → 2. The whole fork (project + storyboard + scenes
+ `v1` + pointer advance) runs in **one transaction**; a duplicate live project
name for the caller → `409` (raised before any child write, no debris). The
`branched_from` breadcrumb is a **one-way historical record**, not a live
coupling — after the fork the two projects evolve fully independently.

---

## Alternatives Considered

1. **Normalized snapshot tables (snapshot the scenes into a
   `scene_versions` table).** *Rejected* — the schema already commits to a
   single JSONB `snapshot` column, and a denormalized blob is exactly what a
   point-in-time capture wants: one row, one read, no join fan-out, trivially
   diffable. Normalization would re-introduce referential coupling to the very
   live tables the snapshot is meant to be independent of.

2. **Mutable "latest" version updated in place.** *Rejected* — defeats the
   purpose of a history and is impossible anyway (the `reject_mutation` trigger
   blocks UPDATE). Each save is a new immutable row.

3. **Restore by mutating/deleting later versions ("truncate history to the
   restored point").** *Rejected* — destroys audit history and races with the
   immutability trigger. Restore appends a new `reason=restore` version (D2).

4. **Slim snapshot (only the α5c-exposed scene fields).** *Rejected* — a
   snapshot that cannot faithfully rebuild the row is not restore-ready.
   Capturing full rows now avoids a snapshot-format migration when α5d.2 and
   later fat-field subsystems land. JSONB makes the extra columns nearly free.

5. **Float serialization of `Numeric` durations.** *Rejected* — float drift
   makes snapshots non-deterministic and breaks hashing/diffing. Decimal
   strings are lossless and stable (D6).

6. **Address versions by `version_number` (int) in the URL.** *Rejected* —
   would make versions the only non-UUID-addressed resource, forcing
   per-resource routing/authorization special-casing. `version_number` remains
   the human label in the body (D7).

7. **New scene ids on restore.** *Rejected* — would orphan every external
   reference to a scene (comments, analytics) on each restore. Ids are
   preserved (D5).

8. **Return full snapshots in the LIST response.** *Rejected* — snapshots can
   be large; a history list of full blobs does not scale. LIST is
   metadata-only; the full snapshot is fetched on demand by single-version GET.

---

## Consequences

- **Positive — clean separation of concerns.** "Concurrency guard" vs "content
  history" is now explicit; a contributor is far less likely to conflate
  `projects.version` with the ledger or to try to "simplify" one into the
  other.
- **Positive — durable, auditable history.** DB-enforced immutability means the
  ledger cannot be silently rewritten; the append-only + monotonic-numbering +
  lineage-chain shape gives a faithful, ordered record.
- **Positive — restore-ready today.** Full-row capture + preserved scene ids +
  canonical serialization mean α5d.2 restore is a pure consumer of α5d.1
  snapshots — no format migration.
- **Contract — captures advance `current_version_id` and bump `version`.**
  Clients that hold a project representation should expect its `version` to
  advance by one after a capture, and `current_version_id` to point at the new
  version. `is_current` on the version DTO is derived from this pointer.
- **Constraint — hard project delete would deadlock the ledger.**
  `project_versions.project_id → projects ON DELETE CASCADE` conflicts with the
  `reject_mutation` trigger (CASCADE would attempt a DELETE the trigger blocks).
  **Mitigation:** projects are only ever *soft-deleted* (α5b) via the API; hard
  delete is an ops/retention concern, never an API action, so CASCADE never
  fires. Recorded here so the constraint is not "rediscovered" as a bug.
- **Deferred — per-version storyboards/timelines.** The schema's nullable
  `storyboards.project_version_id` / `timelines.project_version_id` anticipated
  version-scoped children. α5d.1 keeps α5c's implicit single default storyboard
  and captures its *identity* into the snapshot; per-version binding is revisited
  only if/when generation (α7) needs it.
- **Resolved (α5d.2) — restore round-trips fat fields.** The snapshot includes
  scene columns the α5c API does not expose; α5d.2 restore writes them back
  faithfully (D10), covered by a full fat-column round-trip integration test
  (capture → mutate → restore → snapshot equality modulo `project.version`).

---

## Pattern Reference (Examples)

- **Capture (canonical):** `POST /api/v1/projects/{id}/versions` —
  `app/api/v1/routers/versions.py`,
  `app/application/use_cases/versions/create_version.py`,
  `app/infrastructure/repositories/project_version_repository.py`
  (`create_snapshot`, `_build_snapshot`).
- **List (metadata-only):** `GET /api/v1/projects/{id}/versions` —
  `list_versions.py` + `ProjectVersionSummary` read model.
- **Get (full snapshot):** `GET /api/v1/projects/{id}/versions/{version_id}` —
  `get_version.py`.
- **Restore (append + reconcile, α5d.2):**
  `POST /api/v1/projects/{id}/versions/{version_id}/restore` —
  `restore_version.py`, `project_version_repository.py::restore`
  (`_reconcile_scenes`, `_ensure_default_storyboard`).
- **Diff (on-demand, α5d.2):**
  `GET /api/v1/projects/{id}/versions/{version_id}/diff?against={base}` —
  `diff_versions.py` (pure function over two snapshots).
- **Aggregate OCC Rule (α5d.2):** `IProjectRepository.touch_version` wired into
  the four `app/application/use_cases/scenes/*` mutation use cases.
- **Domain:** `app/domain/versions/project_version.py`
  (`ProjectVersion`, `ProjectVersionSummary`).
- **Immutability trigger:** `tg_project_versions_bud_reject_mutation`
  (`schema.md` §9; `validate_schema.py::EXPECTED_IMMUTABLE`).

New content-versioned aggregates copy these shapes rather than reinventing
them.

---

## Future Extensions

- **α5d.2 — restore + diff (shipped).** Restore-by-new-version (D2/D10) with the
  Aggregate OCC Rule fence (D9), and an on-demand coarse diff between two
  snapshots (D11, enabled by canonical serialization, D6).
- **α5d.3 — branch (shipped).** Branch = **fork a snapshot into a new
  independent project** (D12), not an in-project non-linear head. Lineage stays a
  self-contained per-project chain; the cross-project link is a one-way
  `branched_from` provenance breadcrumb in the new project's `v1` snapshot.
- **In-project multi-head branching (deferred).** A second live head *inside* one
  project (branch labels, switching, merge semantics) would need a new table /
  migration and earns its own slice if ever required.
- **Autosave.** Background `reason=autosave` captures with a retention/pruning
  policy so autosaves do not swamp the manual-save history.
- **Snapshot compression / externalization.** Only if a real project's JSONB
  snapshot blows past sane inline limits (R3 in the pre-flight).
- **Integrity hashing.** A content hash over the canonical snapshot for
  tamper-evidence / dedupe, made trivial by D6.
- **`snapshot.schema_version` migrations.** The leading `schema_version` gates
  future content-shape evolution; a reader upgrades old snapshots forward.
