# Phase 3 Slice α6.3 — Timeline / Tracks / Clips — Pre-flight

> Status: **DRAFT — AWAITING SIGN-OFF.** Load-bearing questions in §7 (Q1
> concurrency/aggregate shape, Q2 slice decomposition, Q6 clip-overlap policy,
> Q13 OCC-token granularity) need a decision before any branch is cut. Nothing
> is implemented yet.
>
> Mirrors the α5/α6.1/α6.2 discipline: ground in the physical schema → lock
> decisions → sign-off → branch → implement → CI → merge → tag. Read-only
> planning artefact per `docs/engineering/RUNBOOK_WAVE.md` §1.
>
> **Predecessors.**
> α5c (`v0.4.7`) Scenes CRUD+reorder · α5d.1–3 (`v0.4.8`–`v0.4.10`) Version
> capture/read/restore/diff/branch + **Aggregate OCC Rule** · α6.1 (`v0.4.11`)
> **Prompt aggregate — generation-input CRUD** + **ADR-0036** · α6.2 (`v0.4.12`)
> **Media Asset aggregate — generation-output CRUD** + **ADR-0037**.
>
> **Companion design docs.**
> * `docs/decisions/ADR-0035-project-version-snapshots.md` — the version ledger.
>   **Explicitly excludes `timeline/clips` from the snapshot boundary** ("It
>   excludes derived / not-yet-API-managed artifacts: prompts, media assets,
>   render jobs, timeline/clips, tags, folder placement") and **defers**
>   per-version `timelines.project_version_id` binding to α7+. Both facts anchor
>   Q1.
> * `docs/decisions/ADR-0036` / `ADR-0037` — the two established concurrency
>   postures (editorial OCC-in-snapshot vs generation no-OCC). α6.3 introduces a
>   **third**: a self-contained aggregate with its own OCC token, outside the
>   project snapshot.
> * `docs/domain/PROJECT_AGGREGATE.md` §6/§8 — parent; the Aggregate OCC Rule
>   (ADR-0035 D9) that α6.3 re-applies with the **timeline** as the aggregate
>   root.
> * `docs/domain/SCENE_AGGREGATE.md` — the closest structural sibling: a
>   version-fenced child-bearing aggregate (scene reorder / OCC wire format is
>   the pattern to mirror for tracks/clips).
>
> **Baseline versioning.** `main` is at `0.4.12` (tag
> `v0.4.12-phase3-alpha6.2`). First α6.3 commit bumps `app/main.py` →
> `"0.4.13-phase3-alpha6.3-dev"` (or `-alpha6.3a-dev` if Q2 splits the slice).

---

## Section 1 — Scope

### 1.1 One-line thesis

α6.3 introduces the **Timeline aggregate** — the *composition layer* that places
registered media (α6.2) onto ordered **tracks** as time-ranged **clips**
(`Scene → Media → Clip → Timeline`). Unlike prompts/media (no OCC) and unlike
scenes (in the project snapshot), the timeline is **its own OCC-guarded
aggregate**: `timelines` alone carries `VersionMixin` + a `version_bump` trigger,
is **1:1 with a project** (unique `project_id` where `deleted_at IS NULL`), and
is **excluded** from `project_versions` snapshots/restore/diff (ADR-0035). Tracks
and clips are its children (no own `version`). α6.3 ships **zero migrations**
(all four tables + indexes + triggers exist in baseline `0001`).

### 1.2 What's in *(exact surface pending Q2/Q4)*

1. **Timeline** — the 1:1 project timeline: create/provision, read, patch
   (`aspect_ratio`, `frame_rate`, `background_color`, `duration_seconds`),
   version-fenced. Soft-deletable? (Q3 — likely no separate delete; it dies with
   the project.)
2. **Tracks** — CRUD + `z_index` ordering (unique per timeline), `kind`
   (`track_kind` enum: video/audio/subtitle/effect), `locked`/`muted`/`name`.
3. **Clips** — CRUD + time placement (`start_seconds`/`end_seconds`,
   `source_*`), `media_asset_id` link, `volume`, `effects`, `locked`.
   Transition links deferred (Q8).
4. **Domain** entities (`Timeline`, `Track`, `Clip`); `Transition` is a lookup
   (out of scope, Q8).
5. **Repositories** + use cases + DTOs + routers (prefix per Q4); DI wiring
   (`.timeline` / `.tracks` / `.clips` or a single `.timeline` repo — Q).
6. **Docs**: `API_CONTRACT.md` (fill the `/timeline` stub), `CHANGELOG`,
   `ROADMAP`, `PROJECT_AGGREGATE.md`, **`TIMELINE_AGGREGATE.md`** (new),
   **ADR-0038** (new — timeline as a self-contained OCC aggregate).

### 1.3 Non-goals (explicit, will NOT ship in α6.3)

* **Rendering / export** — `render_jobs` / `export_jobs` are α6.4. α6.3 only
  composes; it does not render.
* **Timeline in the project snapshot** — the version ledger stays
  {project + scenes} (ADR-0035). No capture/restore/diff of the timeline.
* **Per-version timeline binding** — `timelines.project_version_id` write path
  is deferred to α7+ (ADR-0035). α6.3 leaves it `NULL` (or Q).
* **Transitions authoring** — `transitions` is an unseeded lookup table with no
  API; α6.3 does not create transitions and (Q8) defers clip↔transition links.
* **Real-time collaboration** — `/ws/timeline/{project_id}` (reserved for CRDT)
  is untouched.
* **Overlap-prevention EXCLUSION constraint** — schema defers the gist
  `numrange` exclusion (schema.md §14). α6.3 does not add it (migration); overlap
  policy is an app-level decision (Q6).
* **Render-oriented derived fields** — auto-deriving `timeline.duration_seconds`
  from clip extents is a Q11 (likely deferred to render prep).
* **Migrations** — none. If the slice appears to need one, stop and re-scope.

### 1.4 Anti-scope-creep envelope

* *"Render the timeline to a video."* — No; α6.4.
* *"Snapshot the timeline into a project version."* — No; ADR-0035 excludes it.
* *"Add the no-overlap gist constraint."* — No; that is a migration (Q6 resolves
  overlap at the app layer or defers it).
* *"Author transitions / effects libraries."* — No; `transitions` has no API and
  `effects` is an opaque JSONB array in α6.3.
* *"Bind the timeline to a specific project version."* — No; deferred to α7+.

---

## Section 2 — Foundational facts (grounded in the physical schema)

Read straight off `0001_baseline.py` (lines 741–825, 1544–1594) +
`models/timeline.py` + `enums.py` + `schema.md` §14. **Not** decisions — the
constraints α6.3 must honour.

### F1 — `timelines` carries its own OCC (`VersionMixin` + `version_bump`).
`Timeline(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, VersionMixin)`.
`timelines` **is** in `_VERSION_BUMP_TABLES` (baseline L1564-1572) → an UPDATE
auto-increments `version`. Columns: `project_id` (`ON DELETE CASCADE`, NOT NULL),
`project_version_id` (`ON DELETE SET NULL`, nullable), `duration_seconds`
(`numeric(10,3)`, default 0), `aspect_ratio` (text, NOT NULL, **no default**),
`frame_rate` (int, default 30, CHECK 1–240), `background_color` (text, default
`'#000000'`), `version` (default 1), timestamps, `deleted_at`.
**UNIQUE (`project_id`) WHERE `deleted_at IS NULL`** → at most one live timeline
per project (1:1).

### F2 — `tracks` and `clips` have **no** `version` (only `touch_updated_at`).
Both are `UUIDPrimaryKeyMixin + TimestampMixin + SoftDeleteMixin` — **no
`VersionMixin`**, **absent** from `_VERSION_BUMP_TABLES`. → They carry **no
per-row OCC token**. Their concurrency guard, if any, must be the **parent
timeline's** `version` (Q1/Q13 — the Aggregate OCC Rule re-applied with the
timeline as root).

### F3 — Ownership is **derived through the project** (like scenes/prompts).
No `tenant_id`/`owner_user_id` on `timelines`/`tracks`/`clips`. Ownership chains
`timeline.project_id → projects.(tenant_id, owner_user_id)`; tracks/clips reach
it via `timeline_id → track_id`. → The visibility gate is the **project gate**
first (own the live project → else 404), then the child gate (timeline/track/clip
live under it → else 404). Same two-level anti-enumeration as α5c/α6.1.

### F4 — `tracks`: unique `z_index` per timeline; `track_kind` enum.
`UNIQUE (timeline_id, z_index) WHERE deleted_at IS NULL`; `track_kind ∈ {video,
audio, subtitle, effect}`; index `(timeline_id, kind)`. → Stacking order is a
unique integer per live track (Q5 — reorder semantics mirror α5c scene `move`,
or client-assigned `z_index` with 409 on collision).

### F5 — `clips`: time CHECKs, no overlap constraint, SET NULL links.
`track_id` (`ON DELETE CASCADE`, NOT NULL); `media_asset_id` (`ON DELETE SET
NULL`, **nullable**); `start_seconds` (CHECK ≥ 0), `end_seconds` (CHECK >
start_seconds), `source_start_seconds`/`source_end_seconds` (default 0),
`transition_in_id`/`transition_out_id` (`ON DELETE SET NULL`, nullable), `effects`
(jsonb `[]`), `volume` (`numeric(4,2)`, default 1.00, CHECK 0–4), `locked`.
Indexes `(track_id, start_seconds)`, `(media_asset_id)`. **No overlap
constraint** — schema.md §14 defers the gist `numrange` exclusion. → end>start
and start≥0 are DB-enforced; **overlap is physically allowed** (Q6).

### F6 — `transitions` is an **unseeded lookup table** (no API, no ownership).
`transitions(id, name, kind, duration_seconds, params)` — no `tenant_id`, no
soft-delete, no version, and **no baseline seed rows**. Clips reference it via
`transition_in_id`/`out_id` (`SET NULL`). → With no rows and no authoring API, a
non-null transition link is unsatisfiable in α6.3 (Q8 — defer transition links).

### F7 — Link durability: SET NULL fires only on hard delete.
`clips.media_asset_id` / `transition_*_id` are `ON DELETE SET NULL`; media is
only ever *soft-deleted* (α6.2), so a clip's `media_asset_id` **survives** a
media soft-delete (same F5/F6 pattern as α6.1/α6.2). `timeline.project_id` and
`track.timeline_id` / `clip.track_id` are `ON DELETE CASCADE` — but projects are
only soft-deleted, so cascade never fires via the API.

### F8 — `timelines.project_version_id` (SET NULL, nullable) is provenance.
schema.md §14 + ADR-0035: it anticipated per-version timelines; render jobs reach
the version via `timeline_id → timelines.project_version_id`. Its **write path is
deferred to α7+** (ADR-0035). → α6.3 leaves it `NULL` (Q3 sub-question).

### F9 — `aspect_ratio` mismatch (project enum vs timeline free-text).
`projects.aspect_ratio ∈ {horizontal, vertical, square}` (enum-like CHECK);
`timelines.aspect_ratio` is **free text** (NOT NULL, no default) — schema.md
examples imply `'16:9'`-style. → Timeline creation must **source `aspect_ratio`**
(from the body, or a project→timeline mapping) since there is no default (Q3).

---

## Section 3 — Implementation decisions (α6.3-specific, proposed)

> Follow from §2 + the α5c/α6.1/α6.2 precedent. Load-bearing choices escalate to
> **Q1–Q15** in §7; the rest are mechanical mirrors.

### D1 — New `timeline` bounded context
Add `domain/timeline` (`Timeline`, `Track`, `Clip`), `use_cases/timeline`,
`schemas/timeline.py`, `routers/timeline.py`, repositories. Routing per Q4.

### D2 — Project visibility gate first, then child gate (two-level, like α5c)
Own the live project → else uniform 404. Then timeline/track/clip must be live
under it → else the same 404 (anti-enumeration). Link failures (bad
`media_asset_id`) are `422`, not `404` (α6.1/α6.2 pattern).

### D3 — Timeline is a self-contained OCC aggregate (Q1)
`timelines.version` is the OCC token for **{timeline + tracks + clips}**. A
track/clip create/update/delete **bumps `timelines.version`** (Aggregate OCC Rule
re-applied with the timeline as root — the `version_bump` trigger fires on the
timeline row when the use case touches it). Timeline mutations are **version-
fenced** (body `version` → `412` on mismatch, mirroring α5c scenes). The timeline
does **NOT** bump `projects.version` and is **NOT** captured in
`project_versions` snapshots (ADR-0035).

### D4 — Slim domain over the physical rows
`Timeline = {id, project_id, project_version_id, duration_seconds, aspect_ratio,
frame_rate, background_color, version, created_at, updated_at}`; `Track = {id,
timeline_id, kind, z_index, locked, muted, name, created_at, updated_at}`;
`Clip = {id, track_id, media_asset_id, start_seconds, end_seconds,
source_start_seconds, source_end_seconds, transition_in_id, transition_out_id,
effects, volume, locked, created_at, updated_at}`. Frozen dataclasses.

### D5 — DELETE = owner-scoped soft delete, idempotent-by-404
Tracks and clips soft-delete (`204`), idempotent-by-404. The timeline itself
likely has **no** DELETE in α6.3 (it is 1:1 and dies with the project) — Q3.
Deletes bump `timelines.version` (D3).

### D6 — Reads side-effect-free, ordered
Timeline read returns the timeline + its tracks (ordered by `z_index`) + each
track's clips (ordered by `start_seconds`) — the natural composition tree —
**or** flat sub-resource lists (Q4/Q12). Soft-deleted children excluded.

### D7 — Body validation maps to HTTP
DB CHECKs (frame_rate 1–240, start≥0, end>start, volume 0–4, unique z_index) are
mirrored in DTO validation → `422`; a unique `z_index` collision → `409` (F4); a
foreign/unknown `media_asset_id` → `422` (Q7). Stale `version` → `412`.

> **Concurrency/aggregate shape (Q1/Q13), slice decomposition (Q2), routing
> (Q4), z-index reorder (Q5), overlap policy (Q6), media-link validation (Q7),
> transition links (Q8), and DTO shape (Q12) are decided in §7.**

---

## Section 4 — Acceptance criteria (behavioural, provisional on §7)

**Timeline.** T1 provision/create (1:1; second create → `409` or idempotent
`200` — Q3); T2 read (own project) → `200` composition tree; T3 read unowned →
`404`; T4 patch fenced fields → `200`, `version` advances; T5 patch stale
`version` → `412`; T6 patch bad `frame_rate`/`aspect_ratio` → `422`.

**Tracks.** K1 create (kind + z_index) → `201`, bumps timeline `version`; K2
create duplicate `z_index` → `409`; K3 list ordered by `z_index`; K4 patch
(`name`/`locked`/`muted`/`z_index`) fenced → `200`; K5 reorder z_index (Q5); K6
delete → `204`, idempotent-by-404, bumps timeline `version`; K7 unowned → `404`.

**Clips.** C1 create (start/end, optional `media_asset_id`) → `201`, bumps
timeline `version`; C2 create end≤start / start<0 → `422`; C3 create foreign
`media_asset_id` → `422`; C4 overlap (Q6) → `201` (allow) or `409/422` (guard);
C5 patch (time/volume/effects/`media_asset_id`) fenced → `200`; C6 delete →
`204`, idempotent-by-404; C7 `media_asset_id` survives media soft-delete (F7);
C8 unowned → `404`.

**Engineering (E1–E6):** CI gate green; **no new migration**; no new
`noqa`/`type: ignore`; unit coverage ≥ 80%; `import-linter` layering kept; schema
validator + ERD unchanged.

---

## Section 5 — Test matrix (provisional)

### 5.1 Unit — use cases (fakes)
Timeline create/read/patch (fenced, 412, 422); Track create/list/patch/delete
(z_index unique→409, reorder, bump-timeline-version); Clip create/list/patch/
delete (time validation, media-link foreign→422, overlap policy Q6, bump-
timeline-version); project + timeline scoping threaded on all; version-fence
mismatch → 412.

### 5.2 Repository integration (real DB, SAVEPOINT rollback)
Timeline 1:1 unique (second insert → conflict); `version_bump` fires on timeline
UPDATE; track z_index unique partial (soft-deleted frees the slot); clip time
CHECKs enforced; **F7 link durability**: clip `media_asset_id` survives media
*soft-delete*; ordering (tracks by z_index, clips by start_seconds); soft-delete
exclusion; **the load-bearing aggregate-bump test**: a track/clip mutation bumps
`timelines.version`.

### 5.3 HTTP integration — `test_timeline.py`
Register→project→provision timeline→tracks→clips end-to-end; T/K/C matrix
(201/200/204/404/422/409/412/401, two-level 404, version fence, ordering,
idempotent-by-404, media-link validation).

---

## Section 6 — Structured-log catalogue (α6.3 additions, provisional)

| Event | Level | Fields |
|---|---|---|
| `timeline.provisioned` | INFO | `timeline_id`, `project_id`, `aspect_ratio`, `frame_rate`, `owner_user_id`, `ip`, `request_id` |
| `timeline.updated` | INFO | `timeline_id`, `project_id`, `changed_fields`, `version`, `ip`, `request_id` |
| `track.created` / `track.updated` / `track.deleted` | INFO | `track_id`, `timeline_id`, `kind`, `z_index`, `ip`, `request_id` |
| `clip.created` / `clip.updated` / `clip.deleted` | INFO | `clip_id`, `track_id`, `media_asset_id`, `start_seconds`, `end_seconds`, `ip`, `request_id` |
| `timeline.mutation_rejected` | WARN | `reason` (`stale_version` / `not_visible` / `z_index_conflict` / `foreign_media` / `overlap`), `timeline_id`, `ip`, `request_id` |

* `effects` / `params` **values** never logged (field names only).

---

## Section 7 — Decisions & Open Questions (SIGN-OFF NEEDED)

### Q1 — Concurrency & aggregate shape ★★ load-bearing
`timelines` has `VersionMixin` + `version_bump` (F1); `tracks`/`clips` do not
(F2); ADR-0035 **excludes** the timeline from project snapshots and defers
per-version binding.

| Option | Model | Verdict |
|---|---|---|
| **A — Timeline is its own OCC aggregate** ★ | `timelines.version` fences {timeline+tracks+clips}; child mutations bump it; NOT in project snapshot; NO `projects.version` bump | **Recommended** |
| B — No-OCC composition artefact (like media) | ignore `version`; last-writer-wins | Rejected — fights the deliberate `version_bump` trigger on `timelines` (the schema signal that separates it from prompts/media) |
| C — Part of the project editorial aggregate | bump `projects.version`; enter snapshots | Rejected — ADR-0035 explicitly excludes `timeline/clips`; huge slice |

**Recommendation: A.** It honours the one table in this group that was given a
version column, introduces a clean *third* posture (self-contained OCC aggregate,
outside the editorial ledger), and keeps `projects.version` / snapshots
untouched. This is the biggest decision — everything else follows from it.

### Q2 — Slice decomposition ★★ load-bearing
Timeline + tracks + clips is a large surface (3 sub-resources, OCC, ordering,
link validation). Options:

| Option | Slices | Verdict |
|---|---|---|
| **A — Split** ★ | **α6.3a** = Timeline + Tracks (provision, patch, track CRUD + z_index) → `v0.4.13`; **α6.3b** = Clips (+ media link, time, overlap) → `v0.4.14` | **Recommended** |
| B — One slice | all of it in `v0.4.13` | Viable but a big PR; harder to review/keep green |

**Recommendation: A (split).** Each α6.x slice so far has been one cohesive,
reviewable PR; clips carry the most nuance (time model, overlap, media link) and
deserve their own slice. α6.3a establishes the aggregate + OCC + tracks; α6.3b
adds clips on top. (If you prefer one slice, say so and I'll scope §8 as one PR.)

### Q3 — Timeline provisioning & lifecycle
* **Create:** the 1:1 timeline needs `aspect_ratio` (no default, F9). Options:
  (a) **explicit** `POST /projects/{id}/timeline` (body `aspect_ratio?`,
  `frame_rate?`, `background_color?`; `aspect_ratio` defaults from a
  project→ratio map, e.g. `horizontal→'16:9'`, `vertical→'9:16'`,
  `square→'1:1'`); second create → `409` (or idempotent `200`). (b) **lazy
  auto-provision** on first `GET` (like the α5c default storyboard).
  **Recommendation: (a) explicit create with a derived `aspect_ratio` default**,
  `409` on the second create (1:1 is a hard invariant).
* **Delete:** no timeline DELETE in α6.3 — it is 1:1 and dies with the project
  (soft-deleted projects hide it). **Recommendation: no timeline delete.**
* **`project_version_id`:** left `NULL` in α6.3 (F8, ADR-0035 deferral).

### Q4 — Routing shape
**Recommendation:** singleton timeline (no id in path, 1:1), nested children:
```
POST/GET/PATCH   /projects/{id}/timeline
POST/GET         /projects/{id}/timeline/tracks
GET/PATCH/DELETE /projects/{id}/timeline/tracks/{track_id}
POST/GET         /projects/{id}/timeline/tracks/{track_id}/clips
GET/PATCH/DELETE /projects/{id}/timeline/tracks/{track_id}/clips/{clip_id}
```
Reconciles the existing `/projects/{id}/timeline` stub (API_CONTRACT L64).
Alternative flat clip addressing (`/timeline/clips/{clip_id}`) rejected — the
track parent is the natural scope.

### Q5 — Track z_index / reorder
`UNIQUE (timeline_id, z_index)` partial (F4). **Recommendation:** client assigns
`z_index` on create/patch; a live collision → `409`. Defer a dedicated
`move`/reorder endpoint (α5c-style) unless you want it now — simple integer
assignment with 409 is enough for α6.3a. (Open: allow gaps? Yes — z_index is a
sparse stacking key, not a dense sequence.)

### Q6 — Clip overlap policy ★ load-bearing (scope)
Schema **defers** the gist exclusion constraint (F5). Options:
(a) **allow overlaps** — no app check (matches the schema's "deferred"; overlaps
are a legitimate editing intermediate state; simplest); (b) **app-level guard** —
reject overlapping live clips on the same track → `409`/`422` (more work, and
racy without the DB constraint). **Recommendation: (a) allow overlaps in α6.3**;
revisit when/if the exclusion constraint is added (a future migration). Flag in
`TIMELINE_AGGREGATE.md` as a known deferral.

### Q7 — `media_asset_id` link validation
Nullable. **Recommendation:** when present, must be a **live media asset owned by
the caller** (reuse `media.get_owned(media_id, tenant, owner)`) → else `422`
(consistent with α6.1/α6.2 link validation; the caller owns both the project and
the media). A clip with `media_asset_id = NULL` is a legal placeholder/gap.

### Q8 — Transition links (`transition_in_id` / `transition_out_id`)
`transitions` is unseeded with no API (F6). **Recommendation: defer** — do NOT
accept transition links in α6.3 (omit from the create/patch DTO; `extra="forbid"`
→ `422`). Revisit when a transitions catalogue/API exists. `effects` stays an
opaque JSONB array (accepted as-is, no schema in α6.3).

### Q9 — Clip time semantics
DB enforces `start ≥ 0`, `end > start` (F5). **Recommendation:** mirror in the
DTO (`422`); accept `source_start_seconds`/`source_end_seconds` (default 0); do
**NOT** cross-validate against `media_assets.duration_seconds` in α6.3 (media may
be an image with `NULL` duration; render-time concern).

### Q10 — Track-kind ↔ clip/media compatibility
**Recommendation:** no cross-kind enforcement in α6.3 (a video track accepting an
audio clip is not blocked). Structural only; compatibility is a render-validation
concern. Flag as a deferral.

### Q11 — `timeline.duration_seconds`
**Recommendation:** client-set / default 0 in α6.3; do **NOT** auto-derive from
clip extents yet (derivation belongs to render prep, α6.4). Revisit.

### Q12 — DTO shapes
**Recommendation:** `TimelinePublic` = all timeline columns **incl. `version`**
(it is the OCC token, unlike prompts/media); `TrackPublic` = track columns (no
`version` — the fence is the timeline's); `ClipPublic` = clip columns (no
`version`). Timeline read may embed `tracks: [ {..., clips: [...] } ]` (Q4/D6) or
expose flat sub-resource lists. **Recommendation: embed the composition tree on
`GET /timeline`, plus flat list endpoints for tracks/clips.**

### Q13 — OCC-token granularity ★ load-bearing (pairs with Q1)
Since tracks/clips have no `version` (F2), **the OCC token for every timeline-
aggregate mutation is `timelines.version`** (mirrors α5c, where the scene fence
is carried in the body and the aggregate bumps). **Recommendation:** track/clip
create/update/delete **carry the timeline's expected `version`** in the body and
**bump it** on success; a stale value → `412`. Wire format = body `version`
field (mirror `scenes.py`), not an `If-Match` header (consistency with existing
endpoints). *Sub-question:* is a `version` required on child **create** (POST)?
Recommendation: **optional on POST** (a create cannot be "stale" in a harmful
way) but **required on PATCH/DELETE**. Confirm.

### Q14 — Companion docs: `TIMELINE_AGGREGATE.md` + ADR-0038
**Recommendation:** (a) `docs/domain/TIMELINE_AGGREGATE.md` (identity, 1:1,
self-contained OCC, the third concurrency posture, exclusion from project
snapshots, overlap deferral); (b) **new ADR-0038 — "Timeline as a self-contained
OCC aggregate"** recording Q1/Q13 and citing ADR-0035 (snapshot exclusion),
ADR-0036/0037 (the other two postures). Does not mutate prior ADRs.

### Q15 — One migration? **Recommendation: none.** All four tables + indexes +
`version_bump`/`touch_updated_at` triggers exist in baseline `0001`. If the slice
appears to need a migration (e.g. the overlap exclusion constraint), stop and
re-scope.

---

## Section 8 — File inventory (provisional, assumes Q2=split → α6.3a shown)

### 8.1 New files (α6.3a — Timeline + Tracks)
| Path | LOC est. | Purpose |
|---|---:|---|
| `backend/app/domain/timeline/__init__.py` | ~3 | package |
| `backend/app/domain/timeline/timeline.py` | ~60 | frozen `Timeline` (with `version`) |
| `backend/app/domain/timeline/track.py` | ~45 | frozen `Track` |
| `backend/app/infrastructure/repositories/timeline_repository.py` | ~230 | timeline + track persistence (provision, get, patch [fenced], track CRUD, z_index conflict → 409, aggregate version bump) |
| `backend/app/application/use_cases/timeline/*` | ~320 | `ProvisionTimeline`, `GetTimeline`, `UpdateTimeline`, `CreateTrack`, `ListTracks`, `UpdateTrack`, `DeleteTrack` |
| `backend/app/api/v1/schemas/timeline.py` | ~140 | `TimelineProvisionRequest`, `TimelineUpdateRequest`, `TimelinePublic`, `Track*` DTOs |
| `backend/app/api/v1/routers/timeline.py` | ~180 | routes (Q4) |
| `backend/tests/unit/.../timeline/test_*.py` | ~420 | unit matrix (§5.1) |
| `backend/tests/integration/infrastructure/repositories/test_timeline_repository.py` | ~260 | repo matrix (§5.2) |
| `backend/tests/integration/api/test_timeline.py` | ~360 | HTTP matrix (§5.3) |
| `docs/domain/TIMELINE_AGGREGATE.md` | ~150 | companion (Q14) |
| `docs/decisions/ADR-0038-timeline-occ-aggregate.md` | ~120 | Q1/Q13 (Q14) |

### 8.2 New files (α6.3b — Clips) *(if Q2=split)*
`domain/timeline/clip.py`; clip use cases; clip repo methods; clip DTOs; clip
routes; clip tests. `v0.4.14`.

### 8.3 Modified files
| Path | Change |
|---|---|
| `backend/app/main.py` | version bump; mount timeline router |
| `backend/app/application/interfaces/repositories.py` | add `ITimelineRepository` |
| `backend/app/application/interfaces/unit_of_work.py` | add `.timeline` |
| `backend/app/infrastructure/uow/sqlalchemy_unit_of_work.py` | instantiate `TimelineRepository` |
| `backend/app/core/container.py` | use-case factories |
| `backend/app/api/v1/deps.py` | `*TimelineDep` aliases |
| `backend/tests/integration/conftest.py` | `.timeline` on `_TestUnitOfWork` |
| `backend/tests/unit/.../auth/_fakes.py` | `FakeTimelineRepository` (+ `FakeUnitOfWork.timeline`) |
| `API_CONTRACT.md` | fill the `/timeline` stub (§3.2.x) |
| `CHANGELOG.md` / `ROADMAP.md` | α6.3 entries |
| `docs/domain/PROJECT_AGGREGATE.md` | §6/§8: timeline as self-contained OCC aggregate, outside the snapshot |

### 8.4 Deliberately NOT touched
No migration; `transitions` (no API, Q8); `render_jobs`/`export_jobs` (α6.4);
version-ledger code (timeline out of snapshot); `/ws/timeline` (CRDT).

> **UoW note (α5c/α6.1/α6.2 lesson).** The real `UnitOfWork`, the integration
> `_TestUnitOfWork`, and `FakeUnitOfWork` must all gain `.timeline` or every
> timeline use-case test fails at attribute access.

---

## Section 9 — Reviewer sign-off

**SIGNED OFF (2026-07-13).** All recommendations accepted as drafted.

* **Q1 ✅ A** — Timeline is its own OCC-guarded aggregate (`timelines.version`
  fences {timeline + tracks + clips}; excluded from project snapshots; no
  `projects.version` bump). Three clean, non-overlapping concurrency postures:
  Projects+Scenes (aggregate OCC, in ledger) · Prompts+Media (last-writer-wins,
  excluded) · Timeline (aggregate OCC, excluded).
* **Q2 ✅ Split** — **α6.3a** = Timeline + Tracks (`v0.4.13`); **α6.3b** = Clips
  (`v0.4.14`).
* **Q6 ✅ Allow overlaps** — validation limited to media reference + non-negative
  start + positive duration; editorial rules deferred.
* **Q13 ✅ `timeline.version`** as the single aggregate OCC token; required on
  PATCH/DELETE, optional on child POST.
* **Q3–Q5, Q7–Q12, Q14, Q15 ✅** — accepted exactly as drafted (explicit
  `POST /projects/{id}/timeline`, no lazy create, no timeline DELETE; nested
  routes; client `z_index` with `409`; validate `media_asset_id` → `422`; defer
  transitions; new **ADR-0038**; **zero migrations**).

---

## Section 10 — Implementation order (once approved; α6.3a first if Q2=split)

1. Cut `phase3/alpha6.3a-timeline` off fresh `main`; bump `app/main.py`.
2. `Timeline` + `Track` domain entities.
3. `ITimelineRepository` + wire `.timeline` on the UoW (+ `_TestUnitOfWork`, +
   `FakeUnitOfWork`).
4. `TimelineRepository` (provision, get, fenced patch, track CRUD, z_index→409,
   aggregate version bump) + `FakeTimelineRepository`.
5. Use cases + unit tests (§5.1); `pytest -m unit` + mypy green.
6. DTOs + container factories + deps + `routers/timeline.py`; mount it.
7. Repo integration (§5.2, incl. aggregate-bump + F7 durability) + HTTP (§5.3).
8. Docs: API_CONTRACT stub filled, CHANGELOG, ROADMAP, PROJECT_AGGREGATE §6/§8,
   TIMELINE_AGGREGATE.md, ADR-0038.
9. Local CI gate green (no migration).
10. Commit, push, PR, merge, tag `v0.4.13-phase3-alpha6.3a`; then α6.3b (clips).

---

## Section 11 — Post-α6.3 roadmap (dependency order)

* **α6.4 — Render / export jobs** — orchestrate the composed timeline;
  `render_jobs.timeline_id → timelines.project_version_id` reaches the version;
  outputs point back at `media_assets` (`output_media_asset_id`).
* **CR-8 — Asset Library** — reusable media over the timeline.
* **α7+ — Generation + per-version timelines** — write `project_version_id`;
  possibly the clip-overlap exclusion constraint + transitions catalogue.
