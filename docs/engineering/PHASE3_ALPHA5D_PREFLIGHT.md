# Phase 3 α5d — Project Versions (pre-flight)

> Status: **SIGNED OFF — α5d.1 IN PROGRESS.** Q1–Q10 resolved (Q3 → UUID
> addressing; Q7 canonical serialization confirmed). Branch
> `phase3/alpha5d-project-versions` cut; implementation follows §7. Full
> semantics recorded in `docs/decisions/ADR-0035-project-version-snapshots.md`.
> Mirrors the α5a/α5b/α5c discipline: ground in the physical schema → lock
> decisions → sign-off → branch → implement → CI → merge → tag.

Target tag on completion: **`v0.4.8-phase3-alpha5d`**
Working version during the slice: **`0.4.8-phase3-alpha5d-dev`**

---

## 1. Goal

Give a Project an **immutable version history**: the ability to capture the
project's authoring state (root fields + ordered scenes) as a point-in-time
**snapshot**, list that history, and read any single snapshot back. This is
the product "version history" feature — the ledger that a future **restore**
(α5d.2) and **branch** operation build on.

Scenes were sequenced *before* versions deliberately: a snapshot is only
meaningful once there is real child content to capture. That content now
exists (α5c).

---

## 2. Grounding — what the physical schema already decides (LOCKED)

These are **not** open questions. The `project_versions` table (see
`app/infrastructure/db/models/projects.py`, `schema.md` §9) commits us:

| ID | Locked decision | Evidence |
|----|-----------------|----------|
| **DS1** | **Representation = immutable JSONB snapshot.** One `snapshot JSONB NOT NULL` column holds the whole capture. No normalized snapshot tables. | `ProjectVersion.snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)` |
| **DS2** | **Append-only, DB-enforced immutability.** `project_versions` ∈ `EXPECTED_IMMUTABLE`; carries `tg_project_versions_bud_reject_mutation` (BEFORE UPDATE/DELETE). Uses `CreatedAtOnlyMixin` — no `updated_at`, no `deleted_at`, no OCC `version`. | `validate_schema.py::EXPECTED_IMMUTABLE`, `mixins.CreatedAtOnlyMixin` |
| **DS3** | **Monotonic per-project numbering.** `version_number INTEGER NOT NULL`, unique `(project_id, version_number)`. | `uq_project_versions_project_id_version_number` |
| **DS4** | **Lineage chain.** `parent_version_id` self-FK, `ON DELETE RESTRICT`, `CHECK id <> parent_version_id`. Supports restore/branch lineage. | model `__table_args__` |
| **DS5** | **Authorship + reason.** `created_by_user_id` (RESTRICT), `reason version_reason_enum ∈ {manual_save, autosave, restore, branch, generated}`. | model + `enums.version_reason_enum` |
| **DS6** | **Current pointer.** `projects.current_version_id → project_versions ON DELETE SET NULL` (circular FK via `use_alter`). New projects leave it `NULL` (α5a). | `Project.current_version_id` |
| **DS7** | **Two version mechanisms stay distinct.** Row-OCC `version` (concurrency guard) vs snapshot ledger (user-facing history). Already documented in `PROJECT_AGGREGATE.md` §6. | §6 |
| **DS8** | **`storyboards` and `timelines` carry a nullable `project_version_id`.** The original schema anticipated per-version storyboards/timelines. See Q-tension in §6 (R2). | `timeline.py`, `scenes.py::Storyboard` |

**Consequence of DS2 (important):** a restore can never modify or delete an
existing version. Restore = *append a new version* (`reason=restore`,
`parent_version_id` = the version being restored) + *rewrite the live child
rows* + *repoint `projects.current_version_id`*. History is monotonic and
permanent. (Restore is **α5d.2**, out of scope for the thin cut — see Q1.)

---

## 3. Snapshot boundary (proposed — Q2)

What a snapshot **captures** (the "authoring intent" of the project):

- **Project root fields**: `name`, `description`, `aspect_ratio`,
  `duration_seconds`, `language`, `style`, `settings`, plus the root
  `version` at capture time (for provenance).
- **The default storyboard** identity (`id`, `generated_by`) — the implicit
  storyboard α5c auto-creates.
- **All live scenes** (`deleted_at IS NULL`), **ordered by `scene_number`
  ascending**, each captured as its **full physical row** — every fat column
  (`title`, `duration_seconds`, `narration`, `subtitle`, `emotion`,
  `camera_angle`, `camera_motion`, `lens`, `lighting`, `weather`, `location`,
  `animation`, `transition_in`, `music_mood`, `extra`) **plus `id` and
  `scene_number`**.

Why full-row (not the α5c slim subset): the snapshot must be **restore-ready**
even though restore is deferred. Capturing every column now (cheap in JSONB)
means α5d.2 can round-trip faithfully without a format migration, and future
subsystems that write the fat fields are already covered.

What a snapshot **excludes** (for now — deferred, generated/derived artifacts
that are not yet in the managed API surface):

- Prompts, media assets, render jobs, timeline/clips, tags, folder placement.

These are downstream/generated outputs; freezing them into the snapshot
boundary before we manage them via API would lock a contract we can't yet
honor. The boundary is explicitly **"project + default storyboard + ordered
scenes."** (Confirm in Q2.)

**Ordering is part of the snapshot (advisor point #6):** because scenes are
captured in `scene_number` order *and* each row's `scene_number` is stored,
a restore reproduces both the *content* and the *canonical ordering*, not
merely the set of scenes.

**Scene identity is preserved (α5c Q8, re-affirmed here):** the snapshot
stores each scene's real `id`. A future restore reuses those ids rather than
minting new ones, so comments / analytics / references that point at a scene
survive a restore.

### Proposed snapshot JSON (`schema_version` gates future evolution)

```json
{
  "schema_version": 1,
  "project": {
    "id": "…uuid…",
    "name": "My Video",
    "description": null,
    "aspect_ratio": "horizontal",
    "duration_seconds": "12.500",
    "language": "en",
    "style": null,
    "settings": {},
    "version": 7
  },
  "storyboard": { "id": "…uuid…", "generated_by": "system" },
  "scenes": [
    {
      "id": "…uuid…",
      "scene_number": 1000,
      "title": "Opening",
      "duration_seconds": "3.000",
      "narration": null,
      "subtitle": null,
      "emotion": null,
      "camera_angle": null,
      "camera_motion": null,
      "lens": null,
      "lighting": null,
      "weather": null,
      "location": null,
      "animation": null,
      "transition_in": null,
      "music_mood": null,
      "extra": {}
    }
  ]
}
```

Serialization is **canonical** (Q7): sorted keys, ISO-8601 `Z` timestamps,
`Numeric` → decimal **string** (never float) so snapshots are diffable and
hash-stable.

---

## 4. Proposed API surface (α5d.1 thin cut)

Nested under the project, same two-level visibility gate as scenes
(project owned+live → version belongs to project; otherwise `404`).

| Method | Path | Behavior |
|--------|------|----------|
| `POST` | `/api/v1/projects/{project_id}/versions` | Capture a snapshot of the current project + scenes. `reason=manual_save` (server-set). Assigns next `version_number`, sets `projects.current_version_id`, returns the new version's metadata. `201`. |
| `GET` | `/api/v1/projects/{project_id}/versions` | List version **metadata**, newest first (no snapshot bodies). `200`. |
| `GET` | `/api/v1/projects/{project_id}/versions/{version_id}` | Read a single version **with** its full snapshot, addressed by its UUID `id`. `200` / `404`. |

**Deferred to α5d.2** (see Q1): `POST …/versions/{version_id}/restore`, branch.

DTOs:
- `ProjectVersionCreateRequest` — thin; optional `note` folded into
  `diff_summary` (or empty body). `extra="forbid"`.
- `ProjectVersionPublic` — metadata: `id` (UUID), `version_number`, `reason`,
  `created_by_user_id`, `created_at`, `parent_version_id`, `is_current`
  (derived: `== projects.current_version_id`). No snapshot.
- `ProjectVersionDetail` — `ProjectVersionPublic` + `snapshot`.

The resource is **addressed by its UUID `id` in the path** (Q3 — keeps the
whole API UUID-addressed, consistent with projects/scenes). `version_number`
is exposed in the body as the user-facing label, not the routing key.

---

## 5. Open questions — need sign-off (Q1–Q10)

| # | Question | Recommendation |
|---|----------|----------------|
| **Q1** | **Thin-cut scope:** ship snapshot **create + list + get** now and defer **restore/branch** to α5d.2? | **Yes, split.** Snapshot-capture-and-view is a complete, independently-valuable vertical ("capture & view history"). Restore is a live-mutating, identity-preserving, OCC-interacting operation that earns its own slice + test surface. This is not premature splitting (α5c Q7 concern) — the two capabilities are genuinely distinct, and α5a set the precedent (create/list/get first, mutations later). |
| **Q2** | **Snapshot boundary** = project root + default storyboard + full-row ordered scenes; exclude prompts/media/render/timeline/tags/folder? | **Accept** the boundary in §3. Full-row scene capture for restore-readiness; derived artifacts excluded until they're API-managed. |
| **Q3** | **Address versions by `version_number` (int) or `id` (UUID)** in the URL? | **`id` (UUID)** — RATIFIED. Keeps the API invariant that every resource is UUID-addressed (projects, scenes, versions), so routing, authorization, and repository code stay uniform. `version_number` is exposed in the body as the user-facing label only. |
| **Q4** | **List payload:** metadata-only, or full snapshots? | **Metadata-only.** Snapshots can be large; a list of full snapshots doesn't scale. Full snapshot only on GET-one. |
| **Q5** | **Which `reason` values does α5d.1 accept from the API?** | **Only `manual_save`, server-set.** No client choice. `autosave` is background (later), `restore`/`branch` are α5d.2, `generated` is the generation pipeline (α7+). |
| **Q6** | **`version_number` assignment + concurrency, and does create bump the project row?** | Assign `MAX(version_number)+1` **under the project row lock** (reuse α5c `_lock_project`) to serialize concurrent captures and avoid unique-violation races. Create **sets `current_version_id = new.id`**, which bumps the project row `version` via trigger — consistent with §6 ("a restore… bumps the row version"). The newest manual save becomes current. |
| **Q7** | **Canonical snapshot serialization?** | **Yes** — sorted keys, ISO-8601 `Z`, `Numeric`→string. Diffable + hash-stable; makes α5d.2 diff_summary and any future integrity hashing trivial. |
| **Q8** | **Scene identity stable across restore** (re-affirm α5c Q8)? | **Yes.** Snapshot stores scene `id`; restore reuses ids. Documented now so α5d.1 snapshots are already restore-ready. |
| **Q9** | **Snapshot of a project with zero scenes** allowed? | **Yes** — `scenes: []` is a valid capture. |
| **Q10** | **New ADR-0035 for snapshot/restore semantics**, or extend `schema.md` §9? | **New `ADR-0035`** (versioning: immutable ledger, restore-by-new-version, identity preservation). 0034 is the latest; this introduces genuinely new semantics worth a dedicated record. |

---

## 6. Risks / tensions

- **R1 — CASCADE vs reject_mutation on hard project delete.**
  `ProjectVersion.project_id → projects ON DELETE CASCADE`, but the
  `reject_mutation` trigger blocks DELETE on `project_versions`. A *hard*
  project delete would deadlock these. **Mitigation:** projects are only
  ever **soft-deleted** (α5b) — hard delete is an ops/retention concern, never
  an API action — so CASCADE never fires. **Record this explicitly in
  ADR-0035** so the constraint is not "rediscovered" later; no code needed.

- **R2 — Storyboard/timeline `project_version_id` vs α5c implicit storyboard.**
  The schema anticipated per-version storyboards. α5c made the storyboard
  implicit (one default per project). **α5d.1 does not create per-version
  storyboards**; it captures the *current* default storyboard's identity into
  the snapshot. Per-version storyboard/timeline binding stays deferred. This
  keeps α5c's model intact and is revisited only if/when generation (α7)
  needs version-scoped storyboards.

- **R3 — Snapshot size.** Large scene counts → large JSONB. Acceptable now
  (JSONB handles it); revisit compression/externalization only if a real
  project blows past sane limits.

- **R4 — Serialization determinism.** `duration_seconds` is `Numeric`;
  naïve float serialization drifts. Q7's decimal-string rule prevents it.

- **R5 — Fat fields captured but not API-managed.** The snapshot includes
  scene columns α5c doesn't expose. That's intentional (faithful capture),
  but α5d.2 restore must handle round-tripping fields the API can't set.
  Noted for the restore slice.

---

## 7. Implementation order (α5d.1 — mirrors α5a/α5b/α5c)

1. **Version bump** → `0.4.8-phase3-alpha5d-dev` (`app/main.py`).
2. **Domain**: `ProjectVersion` frozen dataclass (slim: `id`, `project_id`,
   `version_number`, `parent_version_id`, `created_by_user_id`, `reason`,
   `created_at`, `snapshot: dict`, `diff_summary: dict | None`) in
   `app/domain/versions/`.
3. **Interface**: `IProjectVersionRepository` (`create_snapshot`,
   `list_by_project`, `get_owned` — by version UUID) + wire `.versions` onto `IUnitOfWork`,
   `SqlAlchemyUnitOfWork`, integration `_TestUnitOfWork`, and
   `FakeUnitOfWork`.
4. **Repository**: `SqlAlchemyProjectVersionRepository` — project-row-locked
   `version_number` assignment, live project+scenes read, canonical snapshot
   assembly, immutable insert, `current_version_id` repoint; two-level
   visibility gate on reads.
5. **`FakeProjectVersionRepository`** modelling the same observable contract.
6. **Use cases**: `CreateProjectVersion`, `ListProjectVersions`,
   `GetProjectVersion`.
7. **DTOs**: `ProjectVersionCreateRequest`, `ProjectVersionPublic`,
   `ProjectVersionDetail`.
8. **Wiring**: container factories, `deps` aliases, `versions` router,
   mount under `/api/v1`.
9. **Unit tests** (fakes): create assigns next number + sets current;
   snapshot boundary correct; ordering preserved; scene ids captured; unowned
   project → 404; empty-scenes snapshot; list metadata-only; get returns
   snapshot; version-not-found → 404.
10. **Integration tests**: repo (numbering under concurrency, snapshot fidelity
    incl. ordering + fat fields + decimal strings, immutability — UPDATE/DELETE
    rejected, two-level gate) + HTTP (create/list/get happy + error paths).
11. **Docs**: `API_CONTRACT.md` §3.3, `CHANGELOG.md`, `ROADMAP.md`,
    `PROJECT_AGGREGATE.md` §6 refinement, `SCENE_AGGREGATE.md` cross-ref,
    new `ADR-0035`.

No new migration is required — `project_versions`, its indexes, the
immutability trigger, and `projects.current_version_id` all already exist in
the baseline. (Confirm during implementation; if a supporting index is
missing for the list scan, add it in a new numbered migration.)

---

## 8. Definition of done (α5d.1)

- CI gate 10/10 green; integration suite green.
- Oracle-style fidelity: a snapshot round-trips project + ordered scenes with
  full fat fields and decimal-string numerics.
- Immutability proven by test (UPDATE/DELETE on a version row is rejected).
- `current_version_id` correctly repointed on create; project row `version`
  bumps once per capture.
- Two clean commits if a formatting delta appears; otherwise one `feat`.
- Merge → tag `v0.4.8-phase3-alpha5d`.
