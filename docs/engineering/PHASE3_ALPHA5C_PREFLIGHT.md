# Phase 3 Slice α5c — Read-Only Pre-Flight (Scenes)

> **Convention.** Read-only planning artefact per
> `docs/engineering/RUNBOOK_WAVE.md` §1. Records scope, non-goals, design
> decisions, file inventory, acceptance criteria, test matrix, and CI
> impact for **Slice α5c** before any branch is cut or any code is
> written. Approve as-is (or push back on §7) before implementation begins.
>
> **Status.** ✅ **Approved 2026-07-11.** D1–D4 resolved (§2). Q1–Q6
> accepted as recommended; **Q7 adjusted** — α5c ships as **one cohesive
> slice** (split only if implementation actually forces it, not by
> default); **Q8 added** — Scene IDs are **stable across future Version
> restores** (identity is snapshotted, not re-minted). Branch cut
> authorised: `phase3/alpha5c-scenes` (see §9).
>
> **Predecessors.**
> α1 (`v0.4.0-phase3-alpha1`, PR #8) — DI container + JWT + UoW,
> α2a/α2b (`v0.4.1`/`v0.4.2`) — register/login, refresh/logout,
> α3 (`v0.4.3`) — `CurrentUserDep` + `GET /users/me`,
> α4 (`v0.4.4`) — `PATCH /users/me` (canonical mutation, ADR-0034 D2),
> α5a (`v0.4.5-phase3-alpha5a`, PR #18) — `POST/GET /projects` (create + read),
> hygiene (PR #19) — centralized `client_ip` + success `envelope` (M1/M2),
> α5b (`v0.4.6-phase3-alpha5b`) — `PATCH`/`DELETE /projects/{id}`
> (update + soft-delete, **404-before-412** pattern, M3 index `0008`).
>
> **Companion design docs.**
> * `docs/domain/SCENE_AGGREGATE.md` — the Scene aggregate (identity,
>   boundary, ordering, lifecycle, fat-table/slim-domain split). **Read it
>   first.**
> * `docs/domain/PROJECT_AGGREGATE.md` — parent aggregate; §8 was corrected
>   (2026-07-11) to `Project owns Storyboards, Storyboard owns Scenes`.
>
> **Reference implementations to mirror.**
> * 404-before-412 path-addressed mutation → α5b `UpdateProject` +
>   `ProjectRepository.update_owned` / `get_owned`.
> * Owner+tenant scoping / 404-anti-enumeration → α5a `GetProject` /
>   `ProjectRepository.get_owned` (D5).
> * CAS-on-`version`, trigger-owned bump → α4 `UserRepository.update_profile`.
> * Soft-delete idempotent-by-404 → α5b `DeleteProject` /
>   `soft_delete_owned`.
>
> **Baseline versioning.** `app/main.py` currently reads
> `"0.4.6-phase3-alpha5b-dev"` → the tag dropped `-dev` on merge, so `main`
> is at `0.4.6`. The first α5c commit bumps to `"0.4.7-phase3-alpha5c-dev"`;
> the release tag `v0.4.7-phase3-alpha5c` drops `-dev` on merge.

---

## Section 1 — Scope

### 1.1 One-line thesis

α5c introduces the **Scene aggregate** — the first real *project content* —
as a full owner-scoped CRUD surface under a project
(`/projects/{id}/scenes`), backed by the existing `storyboards`/`scenes`
tables with an **implicit default storyboard** and **gap-based ordering**
on the existing `scene_number` column. It reuses every pattern established
through α5b (CurrentUserDep, owner+tenant scoping, 404-anti-enumeration,
CAS-on-`version`, 404-before-412, soft-delete-idempotent-by-404) and adds
**one new pattern**: a **two-level visibility gate** (project → scene) plus
a **project-row-lock ordering serialisation** for `scene_number`
assignment. **α5c ships zero migrations** (the tables and every needed
index already exist in baseline `0001`).

### 1.2 What's in

1. **`POST /api/v1/projects/{project_id}/scenes`** — create (append) a
   scene in the project's default storyboard (lazily creating that
   storyboard, race-safe). `201` + `ScenePublic`.
2. **`GET /api/v1/projects/{project_id}/scenes`** — list the default
   storyboard's live scenes, ordered, with computed `position`. `200` +
   `{ data: [ScenePublic], meta }`. Side-effect-free (does **not** create a
   storyboard).
3. **`GET /api/v1/projects/{project_id}/scenes/{scene_id}`** — single
   scene. `200` / `404`.
4. **`PATCH /api/v1/projects/{project_id}/scenes/{scene_id}`** — partial,
   version-fenced content update (`title` / `duration_seconds` /
   `narration` / `subtitle`). `200` / `404` / `412` / `422`.
5. **`POST /api/v1/projects/{project_id}/scenes/{scene_id}/move`** —
   reorder to a target `position`, version-fenced; server computes the new
   `scene_number` (gap midpoint, rebalance if exhausted). `200` +
   `ScenePublic`. *(shape locked by Q1)*
6. **`DELETE /api/v1/projects/{project_id}/scenes/{scene_id}`** —
   owner-scoped soft delete, unconditional, `204`, idempotent-by-404.
7. **Domain:** `app/domain/scenes/scene.py` — frozen `Scene` entity (slim
   surface, `dataclasses.replace` mutators).
8. **`ISceneRepository`** + `SqlAlchemySceneRepository`:
   `ensure_default_storyboard` (FOR UPDATE on the project row), `add`
   (append number), `list_owned`, `get_owned`, `update_owned` (CAS),
   `reorder_owned` (CAS + gap/rebalance), `soft_delete_owned`.
9. **Use cases** (`app/application/use_cases/scenes/`): `CreateScene`,
   `ListScenes`, `GetScene`, `UpdateScene`, `MoveScene`, `DeleteScene`.
10. **DTOs** (`app/api/v1/schemas/scenes.py`): `SceneCreateRequest`,
    `SceneUpdateRequest` (tri-state + `version`, `extra="forbid"`),
    `SceneMoveRequest` (`position` + `version`), `ScenePublic`.
11. **Router** `app/api/v1/routers/scenes.py`, prefix
    `/projects/{project_id}/scenes`; mounted alongside the projects router.
12. **DI**: container factories + `deps.py` aliases for all six use cases.
13. **Fakes**: `FakeSceneRepository` for unit tests.
14. **Docs**: `API_CONTRACT.md` scenes section; `CHANGELOG.md` α5c entry;
    version bump; `ROADMAP.md` row.

### 1.3 Non-goals (explicit, will NOT ship in α5c)

* **Storyboards API** — no `/storyboards` endpoints; the default
  storyboard stays implicit. No multi-storyboard, no regeneration.
* **Scene child configs** — no Voice/Camera/Animation/Music write surface;
  the fat columns (`emotion`, `camera_*`, `lens`, `lighting`, `weather`,
  `location`, `animation`, `transition_in`, `music_mood`, `extra`) are
  **not** written or read (α5d).
* **Prompts** — the `prompts` table / `scene_id` link is α5d, untouched.
* **Scene `status`** — no status column exists; α5c does **not** add one
  (that is a migration). Lifecycle is `live → soft-deleted`.
* **Media / Timeline / Assets / Renders / AI generation** — all downstream
  (α6+).
* **Project Versions / snapshots** — later; scenes make them meaningful,
  but α5c does not snapshot.
* **Restore / un-delete / hard delete / bulk ops.**
* **Insert-at-position on create** — create appends; repositioning is via
  `move` (Q3).
* **Cursor pagination for scenes** — a storyboard's scene set is bounded;
  α5c returns the full ordered list (Q2).
* **Migrations** — none. If the slice appears to need one, stop and
  re-scope.

### 1.4 Anti-scope-creep envelope

If any of the following show up in review, push back:

* *"Expose the camera/lighting fields while we're here."* — No; that is the
  α5d child-config slice with its own DTOs and mapping story.
* *"Add a `status` column so scenes can be draft/ready."* — No; that is a
  migration and a lifecycle-design decision, deferred.
* *"Let create take a `position` / insert in the middle."* — No; append +
  `move` covers it without a second insertion path.
* *"Add `/storyboards` endpoints now."* — No; the default storyboard is
  intentionally implicit until regeneration is a feature.
* *"Snapshot scenes into a project version here."* — No; Versions is its
  own slice after scenes exist.
* *"Paginate scenes with a cursor like projects."* — No unless a real
  scale need appears; bounded child list ships as a full array.

---

## Section 2 — Foundational Decisions (D1–D4, reviewer-resolved)

These four are **settled** (reviewer sign-off, recorded 2026-07-11) and are
the reason α5c looks the way it does.

### D1: `Project → Storyboard → Scene`, storyboard implicit
Keep the DB hierarchy exactly as the baseline has it. Auto-create **one
default storyboard per project** (`generated_by='system'`), resolved as the
earliest live storyboard `(created_at, id)`; expose only
`/projects/{id}/scenes`. Multi-storyboard later needs **zero** API
breakage. `PROJECT_AGGREGATE.md` §8 corrected to *Project owns
Storyboards, Storyboard owns Scenes* (was wrongly `scenes.project_id`).

### D2: Slim domain over the fat table, no migration
Do not migrate the `scenes` table and do not expose every column. Domain
Scene = `{id, storyboard_id(internal), scene_number(internal), title,
duration_seconds, narration, subtitle, version, timestamps, deleted_at}`.
The cinematography/audio columns stay physical, remain `NULL`, and become
the α5d child-config storage with no API change. **Reconciliation with the
approval sketch:** `script → narration`, `prompt → the prompts child table
(α5d)`, `status → absent (deferred, needs a migration)`. See
`SCENE_AGGREGATE.md` §4.

### D3: `scene_number` as a sparse gap ordering key, no `position` column
Reuse `scene_number` (already `NOT NULL`, already partial-unique per live
storyboard). Number `1000, 2000, 3000, …`; midpoint on insert; rebalance
when a gap is exhausted. The API returns a computed 1-based `position`;
raw keys are never surfaced. No new column, no migration, no duplicated
truth. See `SCENE_AGGREGATE.md` §5.

### D4: Timeline downstream of media, not the owner of ordering
`Scene → Media Asset → Clip → Timeline`. Scene ordering lives with the
storyboard/scene; the timeline consumes clips referencing assets. Roadmap
reworded `Scenes → Media → Timeline`. Scene soft-delete cannot break a
timeline (clips reference assets, not scenes). See `SCENE_AGGREGATE.md` §7.

---

## Section 3 — Implementation Decisions (α5c-specific)

### D5: New `scenes` bounded context, mounted under projects
α5c adds `domain/scenes`, `use_cases/scenes`, `schemas/scenes.py`,
`routers/scenes.py`, and `SqlAlchemySceneRepository`. The router prefix is
`/projects/{project_id}/scenes` (nested), mirroring the resource
hierarchy. **Rationale:** scenes are a distinct aggregate with their own
lifecycle; nesting the *route* under projects (not the *code* inside the
projects package) keeps both the URL hierarchy and the package boundaries
clean.

### D6: Two-level visibility gate (NEW sub-pattern)
Every scene endpoint resolves visibility in two steps, both `404` on
failure (anti-enumeration, extends α5b's D3):
1. **Project gate** — `ProjectRepository.get_owned(project_id, tenant,
   owner)`; `None` → `404`. (Re-uses α5a; no new project code.)
2. **Scene gate** — the scene must be live **and** belong to the resolved
   project's default storyboard; else `404`.
Only after both does a mutation apply its version fence (`412`).
**Rationale:** a `scene_id` can exist under another user's project; leaking
that via `412`/`403` would break anti-enumeration. Visibility precedes
concurrency, one level deeper than α5b.

### D7: CAS-on-`version`, hand-set `+1` over the *guarded* trigger
`PATCH` and `move` use `UPDATE … WHERE id=? AND version=:expected` and
**hand-set** `version = SceneRow.version + 1, updated_at = func.now()` —
**exactly** mirroring `ProjectRepository.update_owned`. The baseline
`bump_version()` trigger (`tg_scenes_biu_version_bump`) is **guarded**:
```
IF NEW.version = OLD.version THEN NEW.version = OLD.version + 1; END IF;
```
Hand-setting `+1` makes `NEW.version != OLD.version`, so the trigger
**no-ops** and the net increment is **exactly +1** (not +2).
`tg_scenes_biu_touch_updated_at` sets `updated_at = now()`
unconditionally; the CAS also sets it, and both resolve to `now()`, so it
is harmless and keeps the CAS shape identical to α4/α5b. The R6 test is the
load-bearing guard on this (§5.2).

### D8: Default-storyboard get-or-create is project-row-locked
`ensure_default_storyboard(project_id)` runs `SELECT … FROM projects WHERE
id=? … FOR UPDATE` (the project is already fetched for the ownership gate;
the lock upgrade is cheap) before the get-or-create insert. **Rationale:**
`storyboards` has no `is_default` column and no per-project uniqueness, so
two concurrent first-scene creates could otherwise create two storyboards
and split scenes. The row lock serialises them; exactly one default
storyboard results — with no migration. **Read is exempt:** `GET
/scenes` never creates a storyboard (side-effect-free; empty project →
`200 []`).

### D9: Ordering mutations are project-row-locked; content PATCH is not
`create` (append: `max(scene_number)+1000`) and `move`/rebalance take the
same project row lock (D8) so `scene_number` assignment is race-free and
the partial-unique index can never be violated by a concurrent writer. A
**content-only** `PATCH` (title/duration/narration/subtitle) takes **no**
ordering lock — it is a pure version-fenced CAS. `DELETE` takes no lock.
**Rationale:** localise the lock to the exact operations that compute an
ordering key; keep the hot content-edit path lock-free.

### D10: `SceneCreateRequest` surface
`extra="forbid"`. Fields:

| Field | Type | Rule |
|---|---|---|
| `title` | `str` | **required**; strip; `1 ≤ len ≤ 200` |
| `duration_seconds` | `float` | **required**; `0 < d < 100000` (fits `numeric(8,3)`); rounded to 3 dp |
| `narration` | `str \| None` | optional; default `null` |
| `subtitle` | `str \| None` | optional; default `null` |

`scene_number`, `position`, `storyboard_id`, `id`, `version` are **not**
accepted (server-assigned / `extra="forbid"` → `422`). Create always
appends.

### D11: `SceneUpdateRequest` surface (tri-state + version)
`extra="forbid"`, required `version` (`ge=1`). Mutable content fields with
α5b tri-state semantics:

| Field | Type | Semantics |
|---|---|---|
| `version` | `int` (`ge=1`) | **required** OCC fence |
| `title` | `str` | present → set (strip, `1..200`); absent → unchanged; `null` → `422` (non-nullable) |
| `duration_seconds` | `float` | present → set (`0 < d < 100000`); absent → unchanged; `null` → `422` |
| `narration` | `str \| None` | present+value → set; present+`null` → clear; absent → unchanged |
| `subtitle` | `str \| None` | present+value → set; present+`null` → clear; absent → unchanged |

Tri-state via `model_fields_set`. Empty patch (only `version`) → `422`
(mirrors α5b Q4). Ordering is **not** mutable here — use `move`.

### D12: `SceneMoveRequest` = `{ position: int(ge=1), version: int(ge=1) }`
`move` repositions a scene to a 1-based target `position` among the
storyboard's live scenes, version-fenced on the moved scene. The server
translates `position` → a new `scene_number` (midpoint of the neighbours
at the target slot; rebalance if no integer room). A `position` beyond the
live count clamps to "append to end". `position` equal to the scene's
current slot is a `200` no-op (no write, version unchanged).
*(shape locked by Q1.)*

### D13: DELETE = unconditional soft delete, `204`, idempotent-by-404
Exactly α5b D6/D7/D8, one level deeper: `soft_delete_owned` sets
`deleted_at` on the caller's own live scene (project-gated); `False` →
`404`. First delete `204`; repeat delete and any `GET`/`PATCH`/`move`
after → `404`. Soft-delete frees the `scene_number`; remaining computed
positions close up (no renumber).

### D14: Reorder/rebalance may bump other scenes' `version` (accepted)
A rebalance updates several `scene_number`s; the version-bump trigger fires
on each, so those scenes' `version` advances. This is accepted and
documented: a concurrent editor holding a stale scene `version` after a
rebalance gets a `412` ("reorder happened, re-read"). Rebalance is rare
(only when a gap is exhausted). No attempt to suppress the trigger.

### D15: One cohesive slice (Q7 — reviewer-adjusted)
**Decision:** α5c ships as **one slice / one PR** — the full Scene CRUD
vertical (create/list/get/patch/move/delete). **Rationale:** every prior
alpha slice has been a *complete vertical capability* (α5a = project
create+read, α5b = project update+delete); splitting Scene CRUD across
α5c.1/α5c.2 would ship two partial aggregates and duplicate the
branch/tag/pre-flight/review overhead. The codebase is comfortable with
cohesive ~2,000–2,600-line green PRs (α5b was ~2,600 insertions).
**Escape hatch (only if forced):** if implementation *naturally* grows past
what is comfortably reviewable, split at the read/create ↔ mutate
boundary — **α5c.1** (`Scene` domain + repo `ensure_default_storyboard` /
`add` / `list_owned` / `get_owned` + `CreateScene`/`ListScenes`/`GetScene`
+ `SceneCreateRequest`/`ScenePublic` + `POST`/`GET` + tests); **α5c.2**
(`update_owned`/`reorder_owned`/`soft_delete_owned` +
`UpdateScene`/`MoveScene`/`DeleteScene` + `SceneUpdateRequest`/
`SceneMoveRequest` + `PATCH`/`move`/`DELETE` + tests). Default is **not**
to split.

### D16: Scene identity is stable across Version restores (Q8 — NEW)
**Decision:** a Scene's `id` (UUID) is a **stable, durable identity** that
must survive future Project Version snapshot/restore. When Versions land
(post-α5c), a snapshot captures scene **content keyed by the existing
scene `id`**, and a restore re-materialises that content **under the same
`id`** rather than minting new scene identities. **Rationale:** stable
scene identity is load-bearing for nearly every future feature — comments,
analytics, AI regeneration, diffing between versions, and collaboration all
key off a durable scene id. Re-minting ids on restore would silently break
all cross-version references. α5c changes nothing operationally (there is
no restore yet), but it **documents the contract now** (see
`SCENE_AGGREGATE.md` §3) so the α6/Versions slice inherits it as a
requirement, not a late discovery. **Implication for α5c:** never treat a
scene `id` as ephemeral or derived; it is server-minted once
(`gen_random_uuid()`) and never regenerated for the life of the scene.

---

## Section 4 — Acceptance Criteria

### 4.1 Behavioural (A1–A22)

**A1. Create happy.** `POST …/scenes` (owned project, valid body) → `201`
+ `ScenePublic`; `position == last+1`; a default storyboard now exists.
**A2. Create is append + gap.** Two creates → `scene_number` 1000 then
2000 (internal); `position` 1 then 2.
**A3. Create envelope.** `{ data: ScenePublic, meta.request_id }`.
**A4. Create auto-creates default storyboard once.** First create makes a
`generated_by='system'` storyboard; a second create reuses it (still one
storyboard for the project).
**A5. Create validation.** Missing `title`/`duration_seconds`,
`duration ≤ 0`, `duration ≥ 100000`, forbidden field (`scene_number`,
`position`, `storyboard_id`, `version`) → `422`.
**A6. Create on not-owned/missing project → 404** (project gate,
anti-enumeration).
**A7. List happy.** `GET …/scenes` → `200` ordered by position `1..N`,
live scenes only.
**A8. List is side-effect-free.** `GET` on a project with no storyboard →
`200 []`, and **no** storyboard row is created.
**A9. List excludes soft-deleted** and closes positions (delete #2 of 3 →
remaining are positions 1,2).
**A10. List on not-owned/missing project → 404.**
**A11. Get single happy / 404.** Own live scene → `200`; unknown / other
project's / soft-deleted scene → `404`.
**A12. PATCH happy (content).** Correct `version`, changed field → `200`;
`version` +**exactly 1**; `updated_at` advances.
**A13. PATCH partial (absent unchanged).** Sending only `title` leaves
`duration`/`narration`/`subtitle` untouched.
**A14. PATCH explicit-null clears nullable.** `narration:null` clears;
`title:null` / `duration_seconds:null` → `422`.
**A15. PATCH same-value no-op.** → `200`, `version` unchanged, no write.
**A16. PATCH stale version → 412.**
**A17. PATCH not-owned/missing/soft-deleted (project or scene) → 404**
(never `412`/`403`; 404-before-412, two-level).
**A18. PATCH empty patch / forbidden field / missing version → 422.**
**A19. move happy.** `position` change → `200`; `position` reflects new
slot; other scenes' displayed positions update; moved scene `version` +1.
**A20. move no-op / clamp.** Same-slot `position` → `200` no-op (version
unchanged); `position` past the end → append (last slot).
**A21. move stale version → 412; move not-visible → 404; bad body → 422.**
**A22. DELETE happy / idempotent-by-404 / frees ordering.** First → `204`;
second → `404`; `GET`/`PATCH`/`move` after → `404`; deleting middle scene
closes the remaining positions; not-owned/unknown → `404`; no auth →
`401`; non-UUID path → `422`.

### 4.2 Engineering (E1–E6)

**E1.** CI gate 10/10 green. **No new migration** → stages 5/6/7 unchanged
from α5b's chain (0001–0008).
**E2.** No new `noqa` / `type: ignore` / coverage override.
**E3.** Unit-only coverage ≥ 80% total.
**E4.** `import-linter` 5/5 kept — no new layering edges (scenes mirror the
projects layering: api → application → domain; infrastructure implements
interfaces).
**E5.** Schema validator (stage 8) green with **no** ORM change (no new
`Index`/column/table) → **no** hardcoded count bump; ERD (stage 9)
unchanged.
**E6.** No ERD drift — α5c touches only code + existing tables.

---

## Section 5 — Test Matrix

### 5.1 Unit — use cases (fakes, no DB)

| # | Case | Assertion |
|---|---|---|
| U1 | `CreateScene` happy | Appends; `position=last+1`; storyboard ensured; `commit()` once; `scene.created` logged |
| U2 | `CreateScene` first scene | `ensure_default_storyboard` called; number 1000; position 1 |
| U3 | `CreateScene` project not owned | project gate `None` → `NotFoundError` (404); no write |
| U4 | `ListScenes` ordered | Returns live scenes by position; soft-deleted excluded |
| U5 | `ListScenes` empty (no storyboard) | `[]`; **no** storyboard created (read-only) |
| U6 | `ListScenes` project not owned | `NotFoundError` (404) |
| U7 | `GetScene` happy / not-visible | entity / `NotFoundError` (both project- and scene-gate paths) |
| U8 | `UpdateScene` real change | `version+1`, changed field, commit once, `scene.updated` |
| U9 | `UpdateScene` same-value no-op | unchanged entity, version same, no write |
| U10 | `UpdateScene` scene not visible | `NotFoundError` (404), no CAS |
| U11 | `UpdateScene` stale version | `VersionConflictError` (412), no CAS |
| U12 | `UpdateScene` CAS race | `update_owned` None → `VersionConflictError` |
| U13 | `UpdateScene` explicit-null clears | `narration=None` clears; version bumps |
| U14 | `MoveScene` reorder | new position; moved scene `version+1`; `scene.reordered` |
| U15 | `MoveScene` same-slot no-op | version unchanged, no write |
| U16 | `MoveScene` clamp past end | appended to last slot |
| U17 | `MoveScene` not visible / stale | `NotFoundError` / `VersionConflictError` |
| U18 | `DeleteScene` happy | `soft_delete_owned` True → commit, `scene.deleted` |
| U19 | `DeleteScene` not visible / idempotent | False → `NotFoundError` (404) |
| U20 | scoping (all use cases) | tenant/owner/project args threaded from the caller |

### 5.2 Repository integration (real DB, SAVEPOINT rollback)

| # | Case | Assertion |
|---|---|---|
| R1 | `ensure_default_storyboard` creates once | One `generated_by='system'` row; second call returns the same id |
| R2 | `ensure_default_storyboard` picks earliest | With two storyboards, returns the `(created_at,id)`-earliest |
| R3 | `add` append numbering | Numbers 1000, 2000, 3000; partial-unique holds |
| R4 | `list_owned` order + soft-delete exclusion | Ordered by `scene_number`; deleted excluded |
| R5 | `get_owned` cross-project isolation | A scene under project B is invisible to project A's owner |
| R6 | `update_owned` real change | Field updated; `version` **+1 exactly** (trigger, **not** double-bumped — D7); `updated_at` advanced |
| R7 | `update_owned` version-stale / wrong scene | None; row untouched |
| R8 | `reorder_owned` midpoint | Between 1000 & 2000 → 1500; single-row update |
| R9 | `reorder_owned` rebalance | Exhausted gap → renumber to 1000,2000,…; all live scenes contiguous |
| R10 | `soft_delete_owned` happy / frees number | `deleted_at` set; `get_owned` None; number reusable |
| R11 | `soft_delete_owned` wrong owner / already-deleted | False, no change |

> **R-note (D7 trigger).** R6 is load-bearing (same as α5b R8): assert
> post-PATCH `version == expected + 1` **exactly**. The guarded
> `bump_version()` no-ops when `NEW.version != OLD.version`, so the
> hand-set `version = SceneRow.version + 1` (mirroring α5b) nets to +1, not
> +2. If R6 ever sees +2, the guard was removed/changed — do **not** just
> drop the `+1`; reconcile against `ProjectRepository.update_owned`, which
> is the reference.

### 5.3 HTTP integration — `test_scenes.py`

Register→token→create-project→call, mirroring α5a/α5b. Cover A1–A22
end-to-end (create/list/get/patch/move/delete; 201/200/204/404/409?/412/422/401;
two-level 404; tri-state PATCH; idempotent-by-404; ordering close-up).

---

## Section 6 — Structured-Log Catalogue (α5c additions)

| Event | Level | Fields |
|---|---|---|
| `scene.created` | INFO | `scene_id`, `project_id`, `storyboard_id`, `owner_user_id`, `tenant_id`, `position`, `ip`, `request_id` |
| `scene.updated` | INFO | `scene_id`, `project_id`, `owner_user_id`, `changed_fields`, `previous_version`, `new_version`, `ip`, `request_id` |
| `scene.update_rejected` | WARN | `reason` (`version_mismatch`), `scene_id`, `expected_version`, `ip`, `request_id` |
| `scene.reordered` | INFO | `scene_id`, `project_id`, `previous_position`, `new_position`, `rebalanced` (bool), `ip`, `request_id` |
| `scene.deleted` | INFO | `scene_id`, `project_id`, `owner_user_id`, `ip`, `request_id` |
| `scene.delete_rejected` | WARN | `reason` (`not_visible`), `scene_id`, `ip`, `request_id` |
| `storyboard.default_created` | INFO | `storyboard_id`, `project_id`, `generated_by='system'`, `request_id` |

* **No content values** in logs — `title`/`narration`/`subtitle` text is
  never logged (field **names** only). `position` is the computed value,
  not raw `scene_number`.

---

## Section 7 — Decisions & Open Questions

### 7A. Resolved (reviewer sign-off, 2026-07-11)
* ✅ **D1** — Project → Storyboard → Scene; default storyboard implicit.
* ✅ **D2** — slim domain over the fat table; `script→narration`,
  `prompt→α5d`, `status→deferred`; no migration.
* ✅ **D3** — reuse `scene_number` as a gap key; computed `position`.
* ✅ **D4** — Timeline downstream of media; scene ordering is editorial.

### 7B. Resolved (reviewer sign-off, 2026-07-11)

* ✅ **Q1 — Reorder API shape:** dedicated `POST …/scenes/{id}/move` with
  `{ position, version }` (D12). Reordering is a domain action, not a
  content PATCH; it earns its own endpoint and keeps PATCH simple.
* ✅ **Q2 — Scene list shape:** full ordered array, no cursor. A project's
  scene set is not large enough to need pagination yet; simpler API and
  simpler OCC semantics.
* ✅ **Q3 — Create semantics:** append-only; repositioning belongs to
  `move`. Keeps creation deterministic (D10).
* ✅ **Q4 — `move` version fence:** yes (D12). Reordering is a structural
  mutation and must participate in OCC.
* ✅ **Q5 — Rebalance version bump:** accept the trigger bumping touched
  scenes' `version` (D14). The trigger owns versioning — never
  hand-increment in application code (α5b discipline).
* ✅ **Q6 — `ScenePublic` fields:** omit `storyboard_id` and raw
  `scene_number` (D-exposed set in §4.1 / `SCENE_AGGREGATE.md` §4.1).
  Storyboard is an implementation detail today; clients must not depend on
  hidden internals.
* 🔄 **Q7 — One slice vs split (adjusted):** **one cohesive α5c slice**
  (D15). Do **not** pre-split; each alpha has been a complete vertical, and
  a ~2,000–2,600-line green PR is within the codebase's comfort. Split only
  if implementation actually forces it.
* ➕ **Q8 — Scene identity across Version restores (added):** Scene `id` is
  **stable/durable** and must survive future snapshot→restore (identity is
  snapshotted, not re-minted) (D16). Documented now so α6/Versions inherits
  it as a requirement.

---

## Section 8 — File Inventory

### 8.1 New files (α5c.1 unless noted α5c.2)

| Path | LOC est. | Slice | Purpose |
|---|---:|---|---|
| `backend/app/domain/scenes/__init__.py` | ~3 | .1 | package |
| `backend/app/domain/scenes/scene.py` | ~70 | .1 | frozen `Scene` entity + `replace`-based mutators |
| `backend/app/infrastructure/repositories/scene_repository.py` | ~200 | .1/.2 | `SqlAlchemySceneRepository` (all 7 methods; .2 adds update/reorder/soft-delete) |
| `backend/app/application/use_cases/scenes/__init__.py` | ~6 | .1 | exports |
| `backend/app/application/use_cases/scenes/create_scene.py` | ~70 | .1 | `CreateScene` |
| `backend/app/application/use_cases/scenes/list_scenes.py` | ~45 | .1 | `ListScenes` |
| `backend/app/application/use_cases/scenes/get_scene.py` | ~40 | .1 | `GetScene` |
| `backend/app/application/use_cases/scenes/update_scene.py` | ~90 | .2 | `UpdateScene` (fetch-then-fence) |
| `backend/app/application/use_cases/scenes/move_scene.py` | ~80 | .2 | `MoveScene` (gap/rebalance) |
| `backend/app/application/use_cases/scenes/delete_scene.py` | ~55 | .2 | `DeleteScene` |
| `backend/app/api/v1/schemas/scenes.py` | ~110 | .1/.2 | `ScenePublic`+`SceneCreateRequest` (.1); `SceneUpdateRequest`+`SceneMoveRequest` (.2) |
| `backend/app/api/v1/routers/scenes.py` | ~130 | .1/.2 | nested router; POST/GET (.1); PATCH/move/DELETE (.2) |
| `backend/tests/unit/application/use_cases/scenes/test_*.py` | ~350 | .1/.2 | U1–U20 |
| `backend/tests/integration/infrastructure/repositories/test_scene_repository.py` | ~220 | .1/.2 | R1–R11 |
| `backend/tests/integration/api/test_scenes.py` | ~420 | .1/.2 | A1–A22 |

### 8.2 Modified files

| Path | Change | LOC |
|---|---|---:|
| `backend/app/main.py` | version bump → `0.4.7-phase3-alpha5c-dev` | +1 |
| `backend/app/application/interfaces/repositories.py` | add `ISceneRepository` (7 methods + docstrings) | +55 |
| `backend/app/core/container.py` | 6 use-case factories + `SceneRepository` wiring on the UoW | +40 |
| `backend/app/api/v1/deps.py` | 6 `*SceneDep` aliases | +18 |
| `backend/app/api/v1/routers/__init__.py` *(or app include)* | mount scenes router | +2 |
| `backend/app/infrastructure/db/unit_of_work.py` *(and test `_TestUnitOfWork`)* | expose `.scenes` repository | +8 |
| `backend/tests/unit/application/use_cases/auth/_fakes.py` | `FakeSceneRepository` (+ `FakeUnitOfWork.scenes`) | +90 |
| `API_CONTRACT.md` | new "Scenes" subsection | +40 |
| `CHANGELOG.md` | `[Unreleased]` α5c entry | +35 |
| `ROADMAP.md` | Phase 3 row α5c status | +1 |

> **UoW note (load-bearing).** Both the real `UnitOfWork` and the
> integration `_TestUnitOfWork` must gain a `.scenes` repository, and
> `FakeUnitOfWork` a `FakeSceneRepository`, or every scene use-case test
> fails at attribute access (same lesson as α5a/α5b adding `.projects`).

### 8.3 Deliberately NOT touched

* No migration file. No `scenes.py` / `mixins.py` ORM change (tables +
  indexes already exist).
* `app/api/v1/helpers.py` (M1/M2 helpers consumed as-is).
* `routers/{auth,users,health,projects}.py` untouched (scenes get their own
  router).
* `app/application/pagination.py` (α5c list is a bounded array, Q2).
* `app/core/errors.py` (`NotFoundError`/`ConflictError`/`VersionConflictError`
  all exist).

---

## Section 9 — Reviewer Sign-Off

**Reviewer verdict — 2026-07-11: ✅ Approved.** D1–D4 resolved (§2).
Q1–Q6 accepted as recommended; **Q7 adjusted** — α5c ships as **one
cohesive slice** (D15; split only if implementation forces it); **Q8
added** — Scene IDs are **stable across future Version restores** (D16).
No other reviewer-added decisions.

Branch cut authorised: **`phase3/alpha5c-scenes`** (single slice). The
D15 escape-hatch split is available only if the diff grows beyond
comfortably reviewable.

---

## Section 10 — α5c Exit Criteria

α5c is complete when:

1. `POST`/`GET`(list)/`GET`(one)/`PATCH`/`move`/`DELETE` on
   `/projects/{id}/scenes[/{scene_id}]` are live, tested, and documented in
   `API_CONTRACT.md`.
2. The default-storyboard behaviour (auto-create once, race-safe, read
   never mutates) is proven by A4/A8 + R1/R2.
3. Gap ordering (append, midpoint, rebalance) + computed `position` is
   proven by A2/A19/A20 + R3/R8/R9.
4. Two-level 404-before-412 (project gate → scene gate → version fence) is
   proven by A17/A21 + R5/R7.
5. Soft-delete idempotent-by-404 + ordering close-up proven by A9/A22 +
   R10.
6. CI gate 10/10 green; `import-linter` 5/5; **no migration**; no ERD/schema
   drift (E5/E6).
7. The Scene aggregate exists — the trigger to move to **α5d (scene
   content: prompts + child configs + generation)** per §11.

---

## Section 11 — Post-α5c Roadmap

Per the reviewer's revised sequencing (domain-first, snapshots-last):

* **α5d — Scene content & generation seams:** Prompts (`prompts.scene_id`),
  the Voice/Camera/Animation/Music child configs (mapped onto the fat
  columns first), and possibly a `status` column (its first migration).
* **α6 — Media assets under a project** (`/projects/{id}/assets`) — scenes
  begin generating assets.
* **α6b — Timeline / tracks / clips** — assets placed on the timeline
  (`Scene → Media → Timeline`).
* **α7 — Render jobs.**
* **Versions — Project snapshots** (storyboard + ordered scenes + content)
  — now meaningful because there is real content to snapshot.
* **α8+ — AI provider integration, end-to-end generation.**

**Deferred-from-α5c backlog:** storyboards API + multi-storyboard;
scene `status`; child-config write surface; prompts; insert-at-position on
create; scene list pagination; bulk reorder / fractional ordering keys;
restore/un-delete.

---

## Section 12 — Implementation Order (once approved)

1. Cut `phase3/alpha5c-scenes` (or `…-create-read` if split) off fresh
   `main`; bump `app/main.py` → `0.4.7-phase3-alpha5c-dev`.
2. Add `Scene` domain entity (`domain/scenes/scene.py`).
3. Add `ISceneRepository` to `interfaces/repositories.py`; wire `.scenes`
   onto the UoW (+ `_TestUnitOfWork`, + `FakeUnitOfWork`).
4. Implement `SqlAlchemySceneRepository.ensure_default_storyboard`
   (FOR UPDATE), `add` (append), `list_owned`, `get_owned`.
5. Implement `FakeSceneRepository` (same semantics) for unit tests.
6. `CreateScene` / `ListScenes` / `GetScene` + unit tests (U1–U7, U20).
7. `pytest -m unit` + mypy green before HTTP.
8. `ScenePublic` + `SceneCreateRequest`; container factories + deps;
   `routers/scenes.py` `POST`/`GET`; mount it.
9. Repo integration R1–R5; HTTP A1–A11.
10. Local CI gate 10/10. **→ α5c.1 PR (if split).**
11. Implement `update_owned` (CAS, D7 no double-bump), `reorder_owned`
    (midpoint/rebalance, D9 lock), `soft_delete_owned`; extend the fake.
12. `UpdateScene` / `MoveScene` / `DeleteScene` + unit tests (U8–U19).
13. `SceneUpdateRequest` + `SceneMoveRequest`; `PATCH`/`move`/`DELETE`
    handlers + deps/factories.
14. Repo integration R6–R11 (esp. **R6 trigger single-bump**); HTTP
    A12–A22.
15. Update `API_CONTRACT.md`, `CHANGELOG.md`, `ROADMAP.md`.
16. Local CI gate 10/10 (stages 5/6/7 unchanged — no migration).
17. Commit, push, PR. **→ α5c.2 PR (if split).**
18. Post-merge: tag `v0.4.7-phase3-alpha5c`; **pivot to α5d (scene
    content)** per §11.
