# ADR-0038 — The Timeline Is a Self-Contained OCC Aggregate (`timelines.version` fences the whole tree)

**Status:** Proposed (documents the pattern shipped in Phase 3 α6.3a — Timeline +
Tracks). Flips to Accepted on merge of this ADR PR.
**Introduces a third concurrency posture.** It does **not** mutate ADR-0035
(project-version snapshots), ADR-0036 (prompts — generation inputs), or ADR-0037
(media — generation outputs). It records that the Timeline is neither *aggregate
OCC in the project ledger* (projects + scenes) nor *last-writer-wins, no OCC*
(prompts + media), but a **distinct** posture: **its own** optimistic-concurrency
aggregate, fenced by **`timelines.version`**, yet **excluded** from the project
version ledger.
**Refines / documents:** `docs/domain/TIMELINE_AGGREGATE.md`,
`docs/domain/PROJECT_AGGREGATE.md` §6/§8 (aggregate boundary + snapshot
exclusion), `API_CONTRACT.md` §3.2.4 (new Timeline resource), and the α6.3
pre-flight (`docs/engineering/PHASE3_ALPHA6_3_PREFLIGHT.md`, Q1/Q2/Q3/Q4/Q5/Q13).
Builds on **ADR-0035** (snapshots), **ADR-0036** (prompts), **ADR-0037** (media),
**ADR-0034** (authenticated endpoint pattern).
**Wave:** Phase 3, generation-pipeline slice α6.3a (Timeline aggregate root +
tracks). α6.3b adds clips under the same posture.

---

## Context

α6.3 introduces the **Timeline aggregate** — the *composition layer* that places
registered media (α6.2) onto ordered **tracks** as time-ranged **clips**
(`Scene → Media → Clip → Timeline`). The slice ships **zero migrations** (the
`timelines`, `tracks`, `clips`, `transitions` tables, all indexes, the partial
uniques, and the `frame_rate` CHECK all exist in baseline `0001`).

Two prior ADRs bracket the concurrency design space, and the Timeline fits
**neither** cleanly:

- **ADR-0035** — the versioned **Project aggregate** is `{project root + default
  storyboard + ordered scenes}`. A scene mutation fences on, and bumps,
  `projects.version` (the **Aggregate OCC Rule**), and the whole aggregate is
  captured in `project_versions` snapshots / restore / diff.
- **ADR-0036 / ADR-0037** — **prompts** (generation inputs) and **media**
  (generation outputs) have **no `version` column**, take **no OCC** (PATCH is
  last-writer-wins, no `412`), never bump `projects.version`, and are **excluded**
  from snapshots.

The physical schema signals a third posture for the Timeline (pre-flight §2):

- `timelines` carries `VersionMixin` and **is** in `_VERSION_BUMP_TABLES`
  (baseline `0001`) — it has a `version` column and the guarded
  `tg_timelines_biu_version_bump` trigger. It is **1:1 with a project** (partial
  unique `uq_timelines_project_id` where `deleted_at IS NULL`).
- `tracks` (and `clips`) carry **no** `VersionMixin` and are **absent** from
  `_VERSION_BUMP_TABLES` — no per-row version, no bump trigger. Ownership is
  **derived through the project** (the tables have no `tenant_id` /
  `owner_user_id`).
- ADR-0035 **explicitly excludes** the timeline from `project_versions` snapshots.

So the timeline *has* an OCC token, but that token is deliberately **not**
`projects.version`, and the timeline is deliberately **not** in the editorial
ledger. Without an ADR, a future contributor sees a versioned table that is 1:1
with a project, tracks/clips with no version of their own, and a snapshot builder
that ignores all three — and cannot tell whether that is a **decision** or an
**oversight** to be "fixed" (e.g. by folding the timeline into `projects.version`,
or by giving tracks their own `version`). This ADR promotes the implemented
convention to a recorded decision.

---

## Decision

### D1 — The Timeline is its own OCC aggregate (α6.3 Q1 = Option A)

The **aggregate root** is the `Timeline` (1:1 with a project). It alone carries a
`version`; its children (`Track`, and — α6.3b — `Clip`) do **not**. The governing
principle:

> **The Timeline is a self-contained optimistic-concurrency aggregate.
> `timelines.version` is the single OCC token for the whole tree (timeline root +
> tracks + clips): every fenced timeline/track/clip mutation compares against it
> and, on a real change, advances it by exactly one. Children have no version of
> their own — the timeline's is theirs.**

This mirrors how `projects.version` protects `{Project + Scenes}` — but scoped to
`{Timeline + Tracks + Clips}`, without inventing nested (per-track / per-clip)
concurrency.

### D2 — `timelines.version` is the OCC token for every aggregate mutation (Q13)

- **Root update** (`PATCH …/timeline`) is a version-fenced CAS on the timeline's
  own columns — the same 404-before-412 control flow as `UpdateScene` /
  `UpdateProject`. The repository hand-sets `version + 1` over the guarded
  `tg_timelines_biu_version_bump` trigger (net **+1**), exactly like
  `ProjectRepository.update_owned`.
- **Child mutations** (`Track` create / update / delete; α6.3b clips) do **not**
  carry a fence on the child row (there is no child `version`). The use case pairs
  each real child write with an **aggregate roll-up** — `bump_version` — in the
  same transaction, advancing `timelines.version`. A fenced roll-up that loses a
  race to a concurrent aggregate mutation returns zero rows → `None` → `412`;
  rollback undoes the child write.

### D3 — Child-`POST` version is optional; `PATCH` / `DELETE` version is required (Q13)

A **child create cannot be harmfully stale** (it adds a new row; it does not
overwrite a concurrent edit), so `version` is **optional** on `POST …/tracks`:
when omitted, the aggregate token is bumped **unconditionally**; when supplied, it
is a fence (stale → `412`). A **child update / delete** *can* clobber, so `version`
is **required** — on `PATCH …/tracks/{id}` (body) and `DELETE …/tracks/{id}`
(a required `?version=` query parameter). The client carries the
`meta.timeline_version` returned by the previous write into the next fenced write.

### D4 — The Timeline is excluded from `projects.version` and from snapshots (adopts ADR-0035)

A timeline/track/clip mutation is a **composition change, not versioned editorial
content**. It therefore:

- does **NOT** bump `projects.version` (the Aggregate OCC Rule of ADR-0035 D9
  stops at the editorial aggregate — it does not extend to the timeline), and
- is **not** captured in `project_versions` snapshots, **not** restored, **not**
  diffed. Restoring a project neither resurrects nor deletes timeline composition.

`project_version_id` on `timelines` is an optional **provenance** link (which
project version the timeline was composed against); its **write path is deferred
to α7+** — α6.3 leaves it `None` and surfaces it read-only.

### D5 — Explicit, non-lazy creation; one live timeline per project (Q3)

`POST /projects/{id}/timeline` **explicitly** creates the single timeline
(`version = 1`, no tracks). There is no lazy "create-on-first-GET". A second
`POST` is a **`409 CONFLICT`**, surfaced from the `uq_timelines_project_id`
partial-unique index (the repository maps the `IntegrityError` to `ConflictError`;
the index is the race-safe backstop). `aspect_ratio` defaults from the project's
`aspect_ratio` orientation enum (`horizontal→16:9`, `vertical→9:16`,
`square→1:1`) when the body omits it; the client may override with an explicit
ratio string.

### D6 — Project-nested routing (Q4)

Ownership is derived through the project, so the surface is **project-nested** —
the ownership model stays obvious and every access runs a two-level gate (project
ownership → timeline resolution, both `404`):

```
POST   /api/v1/projects/{project_id}/timeline
GET    /api/v1/projects/{project_id}/timeline
PATCH  /api/v1/projects/{project_id}/timeline
POST   /api/v1/projects/{project_id}/timeline/tracks
GET    /api/v1/projects/{project_id}/timeline/tracks
PATCH  /api/v1/projects/{project_id}/timeline/tracks/{track_id}
DELETE /api/v1/projects/{project_id}/timeline/tracks/{track_id}?version=<n>
```

### D7 — Client-assigned `z_index`, unique per live timeline → `409` (Q5)

`z_index` is the track stacking order, a **sparse integer** the **client assigns**
(gaps are legal — it is a stacking key, not a dense sequence). It is **unique per
live timeline** (`uq_tracks_timeline_id_z_index` partial-unique index). A
collision — on create or on a `z_index`-changing update — is a **`409 CONFLICT`**:
the server does **not** silently reorder. Soft-deleting a track frees its slot.

### D8 — Track soft-delete is idempotent-by-404; 404-before-412 (Q13)

Deleting a track soft-deletes it (frees the `z_index`) and advances
`timelines.version`. Control flow is **404-before-412**: a missing / already-deleted
track is a uniform `404` *before* the fence is consulted, so a **repeat delete is
`404`, not `412`**. Only a **live** track with a **stale** token yields `412`.

### D9 — Overlapping clips are allowed in α6.3 (Q6, α6.3b)

Editorial constraints are **not** enforced this early: clip validation covers only
a valid `media_asset_id` link (`422` if foreign/dead), non-negative start, and
positive duration. Overlaps are legitimate during editing (they later become
crossfades / dissolves / trims). App-level overlap policy and any DB exclusion
constraint are deferred.

---

## Alternatives Considered

1. **Fold the timeline into the Project aggregate — fence on `projects.version`,
   capture in snapshots (Q1 Option B).** *Rejected.* The schema gives `timelines`
   its **own** `VersionMixin` + bump trigger and ADR-0035 already excludes it from
   snapshots; folding it in would contradict both, make restore resurrect/delete
   composition, and couple high-churn timeline edits to the editorial ledger.

2. **Last-writer-wins, no OCC (treat the timeline like media/prompts).**
   *Rejected.* Unlike a media pointer, a timeline is **co-edited, high-contention**
   state (drag tracks, reorder, retime) where a lost update is a real hazard — and
   the baseline deliberately gave `timelines` a `version` the others lack.

3. **Per-track / per-clip `version` (nested concurrency).** *Rejected.* Tracks and
   clips have **no** `version` in baseline and are absent from the bump-trigger
   set. A single aggregate token is simpler for clients (one number to carry) and
   matches the `{Project + Scenes}` precedent.

4. **Lazy timeline creation on first access.** *Rejected* (Q3): explicit creation
   is easier to reason about (a project provably has 0 or 1 timelines), and the
   `409`-on-second is a clean, race-safe contract.

5. **Silently reorder on `z_index` collision.** *Rejected* (Q5): silent
   reordering hides client intent; a `409` lets the client resolve it.

6. **Amend ADR-0035/0036/0037 instead of a new ADR (Q14 alt).** *Rejected.* Each
   record stays focused and immutable; ADR-0038 *introduces* the third posture and
   references the others rather than editing them.

---

## Consequences

- **Positive — every aggregate now has exactly one concurrency model.** Projects
  + Scenes → aggregate OCC (in the ledger); Prompts + Media → last-writer-wins (no
  OCC, excluded); Timeline + Tracks + Clips → its own OCC (excluded). No mixture
  inside any aggregate — a strong signal the boundaries are right.
- **Contract — the track wire carries no `version`.** `TrackPublic` has **no**
  `version` field; the aggregate token travels in the response
  `meta.timeline_version`. Clients carry that token into the next fenced
  timeline/track write. Timeline root `PATCH` uses `version` in the body; track
  `DELETE` uses a required `?version=` query parameter.
- **Contract — restore does not touch the timeline.** A project restore neither
  captures nor rewrites the timeline; `projects.version` is unaffected by any
  timeline edit.
- **Positive — small, migration-free slice.** α6.3a is timeline root + tracks +
  ownership gate + OCC + `z_index` uniqueness — clips (α6.3b) reuse the same
  posture with no new concurrency machinery.
- **Precedent — clips inherit this.** α6.3b `clips.media_asset_id` places a
  registered asset on a track; a clip mutation fences on / bumps the same
  `timelines.version`.

---

## Pattern Reference (Examples)

- **Domain:** `app/domain/timeline/timeline.py` (frozen `Timeline`, **with**
  `version`), `app/domain/timeline/track.py` (frozen `Track`, **no** `version`).
- **Repository:** `app/infrastructure/repositories/timeline_repository.py`
  (`TimelineRepository`: `add` [unique→`ConflictError`], `get_by_project`,
  `update_owned` [version-fenced CAS, net +1], `bump_version` [fenced vs
  unconditional aggregate roll-up], `add_track` / `list_tracks` / `get_track` /
  `update_track` [z_index→`ConflictError`] / `soft_delete_track`).
- **Use cases:** `app/application/use_cases/timeline/*` — `ProvisionTimeline`,
  `GetTimeline`, `UpdateTimeline` (fenced), `CreateTrack` (optional fence + bump),
  `ListTracks`, `UpdateTrack` (required fence + bump, 404-before-412), `DeleteTrack`
  (required fence + bump, idempotent-by-404). None call
  `IProjectRepository.touch_version`.
- **DTOs / router:** `app/api/v1/schemas/timeline.py`,
  `app/api/v1/routers/timeline.py` (project-nested; `meta.timeline_version`;
  track wire has no `version`).

New composition-layer aggregates copy these shapes rather than reinventing them.

---

## Future Extensions

- **α6.3b — Clips** — `clips.media_asset_id` (`Scene → Media → Clip → Timeline`);
  clip create/update/delete fences on / bumps `timelines.version`; overlaps
  allowed (D9).
- **α6.4 — Render / export jobs** — consume the composed timeline; produce
  deliverables that point back at `media_assets`.
- **α7+ — `project_version_id` write path** — record which project version a
  timeline was composed against (provenance), currently read-only `None`.
- **Later — editorial constraints & transitions** — snapping / ripple / trim /
  split / magnetic timeline; `transitions` gets its own aggregate rather than
  being half-built into clips.
