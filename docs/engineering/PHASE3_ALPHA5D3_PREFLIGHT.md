# Phase 3 α5d.3 — Project Version **Branch** — Pre-flight

> Status: **SIGNED OFF — α5d.3 READY.** Q1 = **Option A** (fork to a new
> independent project); Q2–Q11 accepted as drafted. Provenance breadcrumb shape
> fixed to a structured `branched_from` object (Q3/Q7). Branch
> `phase3/alpha5d3-branch`; implementation follows §9.
>
> Mirrors the α5a/α5b/α5c/α5d.1/α5d.2 discipline: ground in the physical schema
> → lock decisions → sign-off → branch → implement → CI → merge → tag.

---

## 1. Goal

Complete the versioning story: let a user **branch** from any historical
project version into an **independent, separately-editable copy** — the "fork
this save into a new project" operation. Restore (α5d.2) rewinds *this* project
onto an old snapshot; branch instead spins up a *new* project seeded from a
snapshot, leaving the source project untouched.

This closes CR-6's version-operation surface (capture ✅, list/get ✅, restore
✅, diff ✅, **branch** ← here); autosave remains a later background concern.

---

## 2. Physical schema grounding (what already exists — NO migration)

| # | Fact | Source |
|---|------|--------|
| DS1 | `project_versions` is **immutable / append-only** (`reject_mutation` trigger; `CreatedAtOnlyMixin`). | `models/projects.py`, schema.md §9 |
| DS2 | `version_number INTEGER`, **unique per project** (`uq_project_versions_project_id_version_number`). | model `__table_args__` |
| DS3 | `parent_version_id` self-FK, `ON DELETE RESTRICT`, `CHECK id <> parent_version_id`. **No `project_id` equality constraint on the parent** — a parent *could* live in another project (relevant to Q3). | model |
| DS4 | `reason version_reason_enum ∈ {manual_save, autosave, restore, **branch**, generated}` — `branch` already exists. | `enums.py`, schema.md §2 |
| DS5 | `projects.current_version_id` — a **single** head pointer per project (`ON DELETE SET NULL`, `use_alter`). There is **no** branch-name / multi-head / head-label column. | model, schema.md §6/§9 |
| DS6 | Aggregate OCC Rule (α5d.2): `projects.version` is the aggregate-wide OCC token; scene mutations + restore bump it. | ADR-0035 D9 |

**Consequence that drives Q1:** with exactly one `current_version_id` per
project and per-project-unique `version_number`, the schema **cannot** represent
two concurrent live heads *inside one project* without a new table/column (a
migration). So true "git-style in-project branches" are out of scope for a
no-migration slice. The migration-free, product-meaningful reading of "branch"
is **fork-to-a-new-project** (Q1 Option A below).

---

## 3. The branch algorithm (assuming Q1 = Option A — fork to new project)

`POST /api/v1/projects/{project_id}/versions/{version_id}/branch` — one
transaction:

1. **Source project gate** — `projects.get_owned(project_id, tenant, owner)` →
   `None` → `404`.
2. **Source version gate** — `versions.get_owned(project_id, version_id)` →
   `None` → `404` (404-before-anything, anti-enumeration; same as restore).
3. **Resolve the new name** — from the request body (Q4). Uniqueness is enforced
   by the existing partial-unique index; a collision → `409 CONFLICT` (reuse
   `ProjectRepository.add`'s existing behaviour).
4. **Create the new project row** — copy the **mutable root** fields from the
   source *snapshot* (name←requested, description, aspect_ratio, duration_seconds,
   language, style, settings), owned by the caller, `current_version_id=NULL`,
   `version` server-default `1`.
5. **Materialize scenes** — ensure the new project's default storyboard, then
   insert each snapshot scene under it, **ordered by `scene_number`**, writing
   the full fat-column set. Scene `id`s are **freshly minted** (Q5: a new
   project is a new identity space; cross-project id reuse would violate the
   single-owner semantics of a scene id and confuse analytics/comments that
   assume one scene → one project).
6. **Seed the ledger** — capture a `reason=branch` version **v1** of the new
   project (canonical builder, same as capture/restore), recording provenance as
   a structured `branched_from` block embedded in the v1 snapshot (Q3):
   ```json
   "branched_from": {
     "project_id":     "<source project uuid>",
     "version_id":     "<source version uuid>",
     "version_number": 2
   }
   ```
   `parent_version_id` stays **NULL** (fresh root); the breadcrumb is the only
   cross-project link. Advance the new project's `current_version_id` → it (one
   guarded bump on the *new* row).
7. **Commit** — all-or-nothing. Return `201` with the **new project's**
   `ProjectPublic` (Q6), plus provenance in the body/headers (Q7).

The **source project is never touched** — no OCC fence on the source is needed
(we don't mutate it), which is the clean part: branch is a *read* of the source
snapshot + a *create* of a new aggregate.

---

## 4. Concurrency / OCC

- **No source fence.** Branch does not mutate the source project, so no
  `projects.version` fence on the source (unlike restore). The source snapshot
  is immutable (DS1), so it cannot change under us.
- **New project needs no fence** — it's an insert (α5a precedent: inserts are
  unfenced).
- The new project's default-storyboard creation + scene inserts + v1 capture all
  happen under the **new** project's row lock (reuse α5c `_lock_project` on the
  freshly-created row) to keep the `MAX(version_number)+1` assignment race-free —
  though for a brand-new project there is no contender, the lock keeps one code
  path with capture/restore.

---

## 5. Endpoint shape (Q2)

Proposed (mirrors restore's nesting):

```
POST /projects/{project_id}/versions/{version_id}/branch
  body:  { name }                    (new project name; other root fields copied from snapshot)
  → 201  { data: ProjectPublic (the NEW project), meta }
  → 409  { error: { code: CONFLICT } }         (duplicate live name for this owner)
  → 404  { error: { code: NOT_FOUND } }        (source project/version missing / not yours)
  → 422  { error: { code: VALIDATION_FAILED } } (bad body)
  → 401  (via CurrentUserDep)
```

`API_CONTRACT.md` §3.3 currently lists a `…/branch` path only implicitly ("branch
α5d.3"); this slice fills it in as shipped.

---

## 6. Rollback strategy

- The whole branch is **one transaction** (new project row + storyboard + scenes
  + v1 capture + pointer advance). Any failure → full rollback, **zero** rows
  created (no orphan project, no orphan scenes, no orphan version). An
  integration test injects a mid-branch failure and asserts nothing persists.
- Name-collision (`409`) is raised **before** any child rows are written (the
  project `add` is step 4, first write) so a rejected branch leaves no debris.
- Reversibility: a branched project is an ordinary project — the user
  soft-deletes it via the existing α5b `DELETE` if unwanted. No special teardown.

---

## 7. Risks

- **R1 — Cross-project lineage (Q3).** If the new v1's `parent_version_id` points
  at the source version (in the *other* project), lineage becomes cross-project.
  The FK allows it (DS3), but it complicates "list this project's history" (a
  parent that isn't in this project). **Decision:** new v1 has
  `parent_version_id = NULL` (a fresh root) and records a structured
  `branched_from` block (`project_id` + `version_id` + `version_number`) inside
  the snapshot as provenance. Keeps each project's ledger self-contained while
  fully answering "where did this project come from?".
- **R2 — Fat-field + ordering fidelity.** Same class as restore R3: the fork must
  reproduce fat columns, decimal-string durations, and `scene_number` ordering.
  Covered by a full round-trip integration test (source snapshot ≡ new project's
  v1 snapshot, modulo ids/project_id/version).
- **R3 — Name uniqueness UX.** Duplicate name → `409`. We do **not** auto-suffix
  ("Copy of X (2)") server-side in α5d.3 (keeps the contract simple; client picks
  the name). Flagged as a possible later enhancement.
- **R4 — Quota / fan-out.** Branch multiplies projects/scenes. No per-user project
  quota exists yet, so no new gate here; noted for the future billing slice.

---

## 8. Open questions (need sign-off)

| # | Question | Recommendation |
|---|----------|----------------|
| **Q1** | **What *is* a branch, given one `current_version_id` + per-project-unique `version_number` + no-migration?** (A) fork the source version into a **new independent project**; (B) a second live **head inside the same project** (needs a `project_branches` table/label → **migration**, larger slice); (C) alias for restore-with-`reason=branch` (collapses into restore — semantically empty). | **Option A — fork to a new project.** The only migration-free reading that is genuinely useful and distinct from restore. B is a real feature but earns its own migration-bearing slice later; C adds nothing over restore. |
| **Q2** | Endpoint = `POST …/versions/{version_id}/branch`, returns the **new** project as `ProjectPublic`, `201`? | **Yes** (see §5). |
| **Q3** | New v1 lineage: `parent_version_id` → source version (cross-project DAG) **or** `NULL` + provenance breadcrumb in snapshot? | **DECIDED: NULL + structured `branched_from`** (`{project_id, version_id, version_number}`) embedded in the v1 snapshot. Keeps each ledger self-contained (R1). |
| **Q4** | Branch request body — just `{ name }` (required), other root fields copied from the snapshot? | **Yes.** `name` required, `extra="forbid"`. Everything else inherited from the source snapshot (incl. immutable `aspect_ratio`). |
| **Q5** | Scene `id`s in the new project — freshly minted or preserved from the snapshot? | **Freshly minted.** New project = new identity space; a scene id must map to exactly one project. |
| **Q6** | Response body — the new `ProjectPublic`, or the new project's v1 `ProjectVersionDetail`? | **`ProjectPublic`** (the actionable resource is the new project; its `id` is what the client navigates to). Provenance via a `source_version_id` field or `Location` header (Q7). |
| **Q7** | Surface provenance how? | Echo the `branched_from` block in the branch **response meta** and persist it in the new v1 snapshot; optional `Location: /projects/{new_id}`. |
| **Q8** | Does branching bump the **source** project's `projects.version`? | **No** — the source is not mutated (§4). Only the new project gets its (single, incidental) bump. |
| **Q9** | Reason value + `reason=branch` scope. | New project's **v1** carries `reason=branch`; all subsequent captures on it are `manual_save`. |
| **Q10** | Migration? | **None.** `reason=branch` already in the enum; provenance rides in the JSONB snapshot; no new table/column (Q1=A). |
| **Q11** | Version bump string. | `0.4.10-phase3-alpha5d3-dev`. |

---

## 9. Implementation order (§12-style, pending sign-off)

1. **Version bump** → `0.4.10-phase3-alpha5d3-dev` (`main.py`).
2. **Repository** — `IProjectVersionRepository.branch(...)` (or a
   `ProjectRepository.create_from_snapshot` + version capture) returning the new
   project + its v1; reuse restore's scene-materialization helpers
   (`_scene_write_values`, `_ensure_default_storyboard`, canonical `_build_snapshot`).
   Extend the fake + integration `_TestUnitOfWork`.
3. **Use case** — `BranchProjectVersion` (source project + source version gates →
   repo branch → commit; `ConflictError` on name → `409`).
4. **DTO** — `ProjectVersionBranchRequest` (`{ name }`, `extra="forbid"`); reuse
   `ProjectPublic` for the response; provenance in meta.
5. **Wiring** — container factory, `deps` alias, router endpoint on the versions
   router.
6. **Unit tests** — branch happy (new project seeded from snapshot, scenes
   materialized with new ids in order, v1 `reason=branch`, provenance recorded,
   source untouched), name-collision `409`, unowned/unknown `404`, bad body `422`.
7. **Integration tests** — repo (full fat round-trip source≡new-v1 modulo
   ids/project; one-transaction rollback on injected failure; source ledger &
   scenes unchanged) + HTTP (branch happy/409/404/422; then GET the new project +
   its scenes + its v1 to prove it's a first-class project).
8. **Docs** — API_CONTRACT §3.3 (branch shipped), CHANGELOG, ROADMAP,
   PROJECT_AGGREGATE §6/§8 (branch complete; lineage/provenance model), ADR-0035
   (D12 branch = fork-to-new-project; alternatives B/C recorded).
9. **black + CI gate 10/10 + full integration**; fix failures.

---

## 10. Non-goals (explicit)

- In-project multi-head branches (Q1 Option B) — deferred to a migration-bearing
  slice if ever needed.
- Merge / rebase between branches — not in the product model.
- Autosave / retention — separate later concern.
- Server-side name auto-suffixing (R3).
