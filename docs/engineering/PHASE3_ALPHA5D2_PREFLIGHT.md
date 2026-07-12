# Phase 3 α5d.2 — Project Version Restore + Diff (pre-flight)

> Status: **SIGNED OFF — α5d.2 ready to implement.** Q1 → **Option A**
> (`projects.version` becomes the aggregate-wide OCC token — see the Aggregate
> OCC Rule in §4); Q2–Q11 accepted as drafted. Branch
> `phase3/alpha5d2-restore-diff`; implementation follows §12. Mirrors the
> α5a–α5d.1 discipline: ground in the physical schema + ADR-0035 → lock
> decisions → sign-off → branch → implement → CI → merge → tag.

Target tag on completion: **`v0.4.9-phase3-alpha5d2`**
Working version during the slice: **`0.4.9-phase3-alpha5d2-dev`**

---

## 1. Goal

Turn the **read-only** version ledger (α5d.1: capture / list / get) into a
usable version *workflow* by adding the two mutating-but-safe operations:

- **Restore** — make a chosen historical snapshot the project's live content
  again, **without** rewriting history (append-only, per ADR-0035 D2).
- **Diff** — compare two versions and report what changed, computed **on
  demand** (never persisted).

**Branch is explicitly deferred to α5d.3** (see §10). Restore alone is already
a transactional mutation across three aggregates (project + storyboard +
scenes); branching adds lineage/multiple-heads/merge semantics that deserve
their own slice and PR. Combining all three would produce an unreviewable
diff.

---

## 2. Grounding — what is already LOCKED (schema + ADR-0035 + α5c/α5d.1)

Not open questions. These are fixed by the code already merged:

| ID | Locked fact | Evidence |
|----|-------------|----------|
| **G1** | **History is immutable.** `project_versions` ∈ `EXPECTED_IMMUTABLE`; `reject_mutation` trigger blocks UPDATE/DELETE. Restore may **never** modify an existing version. | ADR-0035 D2; `validate_schema.py` |
| **G2** | **Restore = append a new version.** A restore appends `Version(reason=restore)`, rewrites live child rows, and repoints `current_version_id`. History grows monotonically. | ADR-0035 D2 |
| **G3** | **`reason` enum already has `restore`.** No enum/schema migration needed for restore. `version_reason_enum ∈ {manual_save, autosave, restore, branch, generated}`. | `enums.version_reason_enum` |
| **G4** | **Snapshot is restore-ready.** It stores full fat scene rows (`title`, `duration_seconds`, `narration`, `subtitle`, `emotion`, `camera_angle`, `camera_motion`, `lens`, `lighting`, `weather`, `location`, `animation`, `transition_in`, `music_mood`, `extra`) + `id` + `scene_number`, decimal-string durations, and the project's business columns. | `_build_snapshot` |
| **G5** | **Scenes are version-fenced + soft-deletable.** `SceneRepository.update_owned` (CAS, hand-set `+1` over the guarded trigger), `soft_delete_owned` (no fence). Scoping is through the project-row lock + storyboard sub-select. | `scene_repository.py` |
| **G6** | **Project mutation is version-fenced CAS.** `ProjectRepository.update_owned` (`WHERE version = :expected`, hand-set `+1`). `aspect_ratio` is **immutable** (α5b Q3 — not in the mutable set). | `project_repository.py`; α5b |
| **G7** | **Capture serializes on the project-row lock.** `ProjectVersionRepository.create_snapshot` already assigns numbering + lineage + snapshot + `current_version_id` advance under `SELECT … FOR UPDATE` on `projects`. Restore reuses this exact machinery for its trailing capture. | `project_version_repository.py` |
| **G8** | **Two-level ownership gate.** Every version endpoint runs `projects.get_owned` → 404, then version-belongs-to-project → 404 (anti-enumeration). Restore/diff inherit it. | ADR-0035 D8; α5d.1 |

---

## 3. Restore algorithm (LOCKED shape — one transaction)

Per the sign-off, restore is a single transaction; any exception rolls back
**everything** (no partial restore). Steps, in order:

```
POST /api/v1/projects/{project_id}/versions/{version_id}/restore
body: { "version": <project row version the client last observed> }

 1. project ownership gate          projects.get_owned → 404 if missing/not owner
 2. lock project                    SELECT … FOR UPDATE on projects (serializes writers)
 3. OCC fence on project.version    stale/absent → 412 (no writes) — see §4
 4. load source snapshot            versions.get_owned(project_id, version_id) → 404 if not this project's
 5. ensure default storyboard       reuse SceneRepository.ensure_default_storyboard (get-or-create)
 6. rewrite live project root       mutable fields ← snapshot.project (aspect_ratio invariant — §6/Q2)
 7. reconcile scenes (upsert by id) live scenes ← snapshot.scenes  (§5: revive / update / insert; soft-delete extras)
 8. capture the restore version     create_snapshot(reason=restore, parent = source version id — Q7)
                                     → advances current_version_id → new head
 9. commit                          one transaction; success only if all of 3–8 held
```

The trailing capture (step 8) re-runs `_build_snapshot` over the *now-restored*
live state, so the new `reason=restore` version's snapshot is produced by the
single canonical builder and naturally records the freshly-bumped
`project.version` (Q6). Its content equals the source snapshot modulo the
`project.version` field.

**Post-condition example** (user's diagram):

```
before:  1   2   3            current → 3
restore version 1
after:   1   2   3   4        current → 4   (reason=restore, parent_version_id = 1)
```

History `1,2,3` is untouched; `4` is a new immutable head.

---

## 4. OCC / version fence + the Aggregate OCC Rule (own section)

**DECIDED (Q1 = Option A).** `projects.version` becomes the optimistic-
concurrency token for the **entire Project aggregate**, not just the project
row's own columns. This makes the restore fence semantically honest and sets
the contract every future child aggregate (Timeline, Assets, Tags, Branch)
inherits.

> ### Aggregate OCC Rule (new project-wide invariant)
>
> **`projects.version` is the optimistic-concurrency token for the entire
> Project aggregate. Any mutation of any aggregate child that changes
> externally observable project state MUST increment `projects.version`.**
>
> Consequences:
> - Scene create / update / move / delete each bump `projects.version` (α5d.2
>   extends the α5c paths to do this — see §12 step 2).
> - Restore bumps it (via the trailing capture's `current_version_id` repoint,
>   which already triggers a row bump — §3 step 8).
> - Future Timeline / Asset / Tag mutations that belong to the aggregate bump
>   it too.
> - Reads never bump it; a same-value/no-op mutation never bumps it (the
>   project-wide "version moves only on a real persisted change" invariant from
>   ADR-0034 D2 still holds).
>
> This is recorded in `PROJECT_AGGREGATE.md` §6 during implementation so a
> future contributor is not surprised that a Scene update also advances the
> Project row.

Restore mutates live state, so it is **version-fenced exactly like PATCH**
(α4/α5b CAS discipline):

- The request body carries `{ "version": N }` — the project-aggregate
  `version` the client last observed (from a prior `ProjectPublic`).
- Because of the Aggregate OCC Rule, **any** change to the project since the
  client read it — a rename, a scene edit, a scene move/delete, another
  capture/restore — has advanced `projects.version`. So a stale fence returns
  **`412 VERSION_CONFLICT`** with **no writes**, and the contract reads
  cleanly: *"restore only if nothing in this project aggregate changed since I
  last read it."* No silent overwrite of unseen work.
- The project-row lock (step 2) additionally serializes writers so nothing can
  interleave once the fence passes; lock + aggregate fence together give a
  serialized, all-or-nothing restore.
- **404-before-412** (α5b pattern): the project ownership gate (step 1) and
  the version-belongs-to-project check (step 4) run *before* the fence
  matters, so a caller can never learn a project/version exists via a `412`.

---

## 5. Scene reconciliation — identity, missing, added, revived (LOCKED)

Restore makes the **live** scene set equal the **snapshot** scene set, keyed on
scene `id`, preserving UUIDs (G4; ADR-0035 D5). Given snapshot ids `S` and
live-or-soft-deleted physical rows:

| Case | Live state | Snapshot | Action |
|------|-----------|----------|--------|
| **Modified** | live row `id` exists | `id` ∈ `S` | **UPDATE** all fat columns + `scene_number` from the snapshot; clear `deleted_at` if set |
| **Removed** | live row `id` exists | `id` ∉ `S` | **soft-delete** (`deleted_at = now()`) — never physical delete |
| **Added / revived** | no live row, but a **soft-deleted** physical row with `id` exists | `id` ∈ `S` | **UPDATE** that row: clear `deleted_at`, rewrite all columns (revive in place — an INSERT would collide on PK) |
| **Added (truly new to this DB)** | no physical row with `id` | `id` ∈ `S` | **INSERT** with the snapshot's `id` (reuse the UUID) |

**Key subtlety (Q3):** because scene ids are preserved *and* deletes are soft,
a scene present in the snapshot but currently soft-deleted must be **revived by
upsert on the existing physical row**, not inserted (the PK still exists). So
reconciliation is an **upsert keyed on `id` across all physical rows (live +
soft-deleted)**, not just live ones. This is the trickiest part of the slice.

**Ordering to avoid `(storyboard_id, scene_number)` unique collisions (Q4):**
soft-delete the "removed" set first (frees their `scene_number` slots under the
`WHERE deleted_at IS NULL` partial-unique index), then upsert/insert the
snapshot scenes with their **captured `scene_number` verbatim** (which were
unique at capture time). This reproduces the exact canonical ordering, not just
the set.

**Storyboard (Q5):** restore rehomes scenes under the project's live default
storyboard (`ensure_default_storyboard`). The snapshot's `storyboard.id` is
provenance, not a restore target — we do not recreate/rename storyboards
(keeps α5c's implicit-single-storyboard model). An empty snapshot
(`scenes: []`) soft-deletes all live scenes → empty project.

---

## 6. Fat-field round-trip (LOCKED, with one invariant to confirm)

The snapshot holds cinematography columns the α5c API cannot set (`emotion`,
`camera_angle`, …, `extra`). Restore **must write them back faithfully** even
though no API surface produced them (ADR-0035 "deferred" note). The restore
repository writes the full fat column set from `snapshot.scenes[*]`, converting
decimal-string durations back to `Numeric`.

**Project root (Q2):** restore rewrites the **mutable** root fields (`name`,
`description`, `duration_seconds`, `language`, `style`, `settings`) from
`snapshot.project`. `aspect_ratio` is **immutable** (G6), so a snapshot's
`aspect_ratio` always equals the live value and restore treats it as an
invariant: **assert equal, do not write** (a mismatch would indicate corruption
and should raise, not silently mutate an immutable column).

---

## 7. Diff — on demand, never persisted (LOCKED)

```
GET /api/v1/projects/{project_id}/versions/{version_id}/diff?against={other_version_id}
```

- Both versions must belong to the caller's owned project (two-level gate on
  each; otherwise `404`). `against` is a required query param; a malformed UUID
  → `422`.
- Computed live from the two stored snapshots — **no `diff_summary` column is
  written** (the α5d.1 `diff_summary` stays `null`; we do not backfill it).
- **Direction (Q8):** `target = {version_id}`, `base = {against}`. Changes are
  reported as base → target.
- Response shape (coarse for α5d.2):

```json
{
  "base_version_number": 2,
  "target_version_number": 4,
  "project_changed": true,
  "scene_changes": { "added": 2, "removed": 1, "modified": 4 }
}
```

- `added` = scene ids in target not in base; `removed` = in base not in target;
  `modified` = ids in both whose captured columns differ; `project_changed` =
  any project business column differs. Field-level diffs are deferred (α5d.3+).

---

## 8. Proposed API surface (α5d.2)

| Method | Path | Body / Query | Behavior |
|--------|------|--------------|----------|
| `POST` | `/projects/{project_id}/versions/{version_id}/restore` | `{ "version": N }` (`extra="forbid"`, required) | Restore that snapshot into live state; append a `reason=restore` version; advance `current_version_id`. `200` with the new `ProjectVersionDetail`. `412` on stale fence; `404` project/version gate; `422` bad body. |
| `GET` | `/projects/{project_id}/versions/{version_id}/diff` | `?against={uuid}` (required) | Coarse change summary base(`against`) → target(`version_id`). `200`; `404` gate on either version; `422` bad/missing `against`. |

Both authenticated (`CurrentUserDep`), both UUID-addressed (ADR-0035 D7).

**Restore returns `200` (not `201`)** — it mutates an existing resource
(the project) and returns the project's new current version; the created
version row is a side effect, and the client's primary interest is "what is my
project now?" (Q10 — confirm 200 vs 201).

DTOs:
- `ProjectVersionRestoreRequest` — `{ version: int }`, `extra="forbid"`.
- `ProjectVersionDiff` — the §7 shape.
- Reuse `ProjectVersionDetail` for the restore response.

---

## 9. Rollback Strategy (NEW — required on every mutating pre-flight)

Per the nine-slice maturity checkpoint, every mutating slice from here on
documents its failure/atomicity story explicitly.

- **What guarantees atomicity?** The entire restore (steps 2–8 of §3) runs in
  **one database transaction** opened by the Unit of Work. The commit at step 9
  is the only durability point.
- **What happens if restore fails halfway?** Any exception (a scene upsert
  error, a constraint violation, a lost fence, an infra blip) propagates out of
  the use case *before* `uow.commit()`, so the `async with uow:` context exits
  via the error path and the transaction is **rolled back in full**. No writes
  land.
- **Can partially-restored scenes exist?** **No.** Because soft-deletes,
  upserts, inserts, the project rewrite, and the trailing capture are all in
  the same transaction, a failure reverts every one of them. There is no
  intermediate committed state.
- **What if `current_version_id` advances but scene updates fail?** Impossible
  to observe: the `current_version_id` repoint (inside step 8's
  `create_snapshot`) and the scene writes (step 7) share the transaction — they
  commit together or not at all.
- **Concurrency:** the project-row lock (step 2) is held for the whole
  transaction, so no other writer can interleave; the OCC fence (step 3)
  rejects a caller working from stale state before any write. Lock + fence +
  single transaction = serialized, all-or-nothing restore.
- **Idempotency:** restore is **not** idempotent by design — each call appends a
  new version. Re-issuing a restore after a `412` is safe (it made no writes);
  re-issuing after success creates another `reason=restore` head, which is the
  intended semantics.

Net: **one transaction; any exception rolls back everything.**

---

## 10. Open questions — need sign-off (Q1–Q11)

| # | Question | Recommendation |
|---|----------|----------------|
| **Q1** | **What does the OCC fence check?** Scene edits bump `scenes.version`, not `projects.version`, so a project fence alone might not "see" a concurrent scene edit made after the user opened the restore screen. | **DECIDED → Option A.** `projects.version` is the aggregate-wide OCC token (see §4 Aggregate OCC Rule). α5d.2 extends the α5c scene create/update/move/delete paths to also bump `projects.version`, and restore fences on it. Restore's contract becomes "restore only if nothing in the aggregate changed since I read it" — no silent overwrite. Small, cheap cross-cutting change now vs. after Timeline/Assets/Branch exist. |
| **Q2** | **Does restore rewrite `aspect_ratio`?** | **No — assert-equal invariant.** `aspect_ratio` is immutable (G6); snapshot value always equals live. Restore rewrites only the mutable root set and raises if `aspect_ratio` differs (corruption guard). |
| **Q3** | **Revive soft-deleted scenes by upsert-on-id (not insert)?** | **Yes.** Reconciliation is an upsert keyed on `id` across all physical rows (live + soft-deleted); a snapshot scene whose row was soft-deleted is revived in place (clear `deleted_at`, rewrite columns). Insert only for ids with no physical row at all. |
| **Q4** | **Write snapshot `scene_number` verbatim, soft-delete "removed" first?** | **Yes.** Reproduces canonical ordering; soft-deleting the removed set first frees `scene_number` slots under the partial-unique index, so re-applying captured numbers cannot collide. |
| **Q5** | **Rehome restored scenes under the live default storyboard (ignore snapshot storyboard id)?** | **Yes.** `ensure_default_storyboard`; snapshot `storyboard.id` is provenance only. Keeps α5c's implicit single-storyboard model. |
| **Q6** | **Does the `reason=restore` version re-capture live state, or copy the source snapshot?** | **Re-capture** via `create_snapshot` after the rewrite. One canonical builder; naturally records the new `project.version`. Content equals source snapshot modulo `project.version`. |
| **Q7** | **`parent_version_id` of the restore version = source version, or previous current?** | **Source version** (the one being restored), per ADR-0035 D2 and the sign-off diagram (`4` parents `1`). This makes lineage a DAG — fine; α5d.3 branch generalizes it. |
| **Q8** | **Diff direction + shape?** | `target={version_id}`, `base={against}`; coarse `{project_changed, scene_changes:{added,removed,modified}}` + both version numbers. Field-level deferred. |
| **Q9** | **Diff auth/scope?** | Two-level gate on **both** versions (each must belong to the caller's owned project); `against` required; malformed → `422`. |
| **Q10** | **Restore returns `200` or `201`?** | **`200`** — it mutates the existing project and returns its new current version; the version row is a side effect. (Capture stays `201`.) |
| **Q11** | **Any migration?** | **None.** `reason=restore` already in the enum (G3); diff is computed. No schema change. (Confirm during implementation; Q1's project-version-bump-on-scene-mutation is a code change, not a migration.) |

---

## 11. Risks / tensions

- **R1 — Resolved by Q1 (Option A).** Scene mutations now bump
  `projects.version`, so the restore fence is a true aggregate token and cannot
  silently overwrite an unseen scene edit. The residual risk is *coverage*: the
  Aggregate OCC Rule must be applied to **every** current scene path
  (create/update/move/delete) — a missed path re-opens the gap. Enforced by
  regression tests (§12 step 2) asserting each scene mutation advances
  `projects.version`.
- **R2 — Upsert-on-id complexity (Q3).** Reviving soft-deleted rows means the
  reconciliation query set includes soft-deleted physical rows — a different
  scope than every other α5c read (which excludes them). Must be covered by a
  dedicated integration test (restore after a scene was deleted).
- **R3 — Fat-field fidelity.** Restore writes columns no API sets; a bug here
  is invisible to α5c tests. Integration test must assert a full fat-column
  round-trip (capture → mutate → restore → re-read snapshot equality).
- **R4 — Diff cost.** Diff loads two full snapshots and compares in Python.
  Fine for bounded scene counts; revisit if snapshots grow large.
- **R5 — Restore of the current version (no-op-ish).** Restoring the current
  head still appends a new `reason=restore` version (not a no-op) — confirm
  that's acceptable (it is, per "restore is not idempotent", §9). No special
  casing.
- **R6 — `duration_seconds` decimal round-trip.** String→`Numeric` on restore
  must preserve scale (`"3.000"`). Test the exact-string round-trip.

---

## 12. Implementation order (α5d.2 — mirrors prior slices)

1. **Version bump** → `0.4.9-phase3-alpha5d2-dev` (`app/main.py`).
2. **Aggregate OCC Rule (Q1 / Option A)** — scene create/update/move/delete
   additionally bump `projects.version` (the aggregate token). Prefer a single
   shared helper on `SceneRepository` (or a `projects.touch_version(project_id)`
   CAS-free bump under the already-held project-row lock) so all four paths use
   one code path. Add regression tests asserting **each** scene mutation
   advances `projects.version` by exactly one, and that a same-value/no-op
   scene PATCH does **not** bump it. Update `PROJECT_AGGREGATE.md` §6 with the
   Aggregate OCC Rule.
3. **Repository**: `ProjectVersionRepository.restore(...)` (project-locked,
   fenced, reconcile-by-upsert, fat-field write, trailing capture) +
   `diff(project_id, target_id, base_id)`; extend the interface,
   `FakeProjectVersionRepository`, and integration `_TestUnitOfWork`.
4. **Use cases**: `RestoreProjectVersion` (ownership gate → repo restore →
   commit; maps `None` → 412), `DiffProjectVersions` (gate both → compute).
5. **DTOs**: `ProjectVersionRestoreRequest`, `ProjectVersionDiff`; reuse
   `ProjectVersionDetail` for the restore response.
6. **Wiring**: container factories, `deps` aliases, router endpoints on the
   existing versions router.
7. **Unit tests** (fakes): restore appends `reason=restore` + advances current;
   parent = source; scene reconcile (modified/removed/revived/new); empty
   snapshot clears scenes; stale fence → 412; unowned/unknown → 404; diff
   added/removed/modified/project_changed; cross-project → 404.
8. **Integration tests**: repo (full fat round-trip incl. decimal strings,
   revive-soft-deleted, ordering, one-transaction rollback on injected
   failure, immutability of history unchanged) + HTTP (restore happy/412/404/
   422; diff happy/404/422).
9. **Docs**: `API_CONTRACT.md` §3.3 (restore + diff), `CHANGELOG.md`,
   `ROADMAP.md`, `PROJECT_AGGREGATE.md` §6, `ADR-0035` (flip restore/diff from
   "deferred" to shipped; record the Q1 decision), this pre-flight → SIGNED OFF.

---

## 13. Definition of done (α5d.2)

- CI gate 10/10 green; integration suite green.
- Restore is provably one transaction: an injected mid-restore failure leaves
  **zero** writes (history, scenes, project, current pointer all unchanged).
- Full fat-field + decimal round-trip proven (capture → mutate → restore →
  snapshot equality modulo `project.version`).
- Revive-soft-deleted-scene path covered by test (Q3).
- History immutability re-verified: restore never UPDATEs/DELETEs an existing
  version row.
- Stale-fence restore → `412` with no writes.
- Diff coarse summary correct for added/removed/modified/project_changed.
- Merge → tag `v0.4.9-phase3-alpha5d2`.

---

## 14. Roadmap after α5d.2 (confirmed sequence)

```
✓ α5d.1  Capture / List / Read
  α5d.2  Restore + Diff          ← this pre-flight
  α5d.3  Branch                  (lineage / multiple heads / merge)
  α6     Timeline                (eventually part of the snapshot boundary)
  α7     Render Jobs
```

Timeline stays *after* the full versioning workflow: once restore semantics
are settled, they define the contract for how future aggregates (Timeline,
etc.) participate in version history.
