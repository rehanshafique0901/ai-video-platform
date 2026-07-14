# Phase 3 Slice α6.3b — Clips — Pre-flight

> Status: **DRAFT — AWAITING SIGN-OFF.** The load-bearing α6.3 decisions
> (aggregate shape, OCC-token granularity, overlap policy, slice split) were
> signed off in `PHASE3_ALPHA6_3_PREFLIGHT.md` (2026-07-13) and shipped in α6.3a
> (`v0.4.13`). This doc resolves the **clip-specific** open questions (§4) that
> α6.3a deferred. Nothing is implemented yet.
>
> Mirrors the α5/α6.1/α6.2/α6.3a discipline: ground in the physical schema →
> lock decisions → sign-off → branch → implement → CI → merge → tag. Read-only
> planning artefact per `docs/engineering/RUNBOOK_WAVE.md` §1.
>
> **Predecessor.** α6.3a (`v0.4.13`, `main` @ `b8b884e`) — Timeline aggregate
> root + tracks; self-contained OCC via `timelines.version`; ADR-0038.
>
> **Baseline versioning.** `main` is at `0.4.13` (tag `v0.4.13-phase3-alpha6.3a`).
> First α6.3b commit bumps `backend/app/main.py` → `"0.4.14-phase3-alpha6.3b-dev"`.
> **Zero migrations** — the `clips` table + indexes + CHECKs already exist in
> baseline `0001` (`docs/database/schema.md` §14).

---

## Section 1 — Scope

### 1.1 One-line thesis

α6.3b completes the composition tree `Timeline → Track → **Clip**`: it places a
registered media asset (α6.2) onto a track (α6.3a) as a **time-ranged clip**.
Clips are **children of the timeline aggregate** — they carry no `version` of
their own; every clip create/update/delete is fenced on and bumps the parent
`timelines.version` (ADR-0038 / Q13), never `projects.version`, and is never
captured in a `project_versions` snapshot (ADR-0035).

### 1.2 What's in

1. **Clip CRUD** nested under a track:
   `POST/GET /projects/{id}/timeline/tracks/{track_id}/clips`,
   `GET/PATCH/DELETE …/clips/{clip_id}`.
2. **Time placement** — `start_seconds` / `end_seconds` (required),
   `source_start_seconds` / `source_end_seconds` (the trim window into the source
   media; optional, default 0).
3. **`media_asset_id`** link — nullable; when present, validated as an owned,
   live media asset → `422` (mirrors the α6.2 `_links` pattern).
4. **`volume`** (0–4), **`locked`** — mutable clip attributes.
5. **Composition tree** — `GET …/timeline` and `GET …/tracks` embed each track's
   ordered clips; a flat `GET …/tracks/{track_id}/clips` is also provided.
6. **Domain** `Clip` entity; **repository** extension on `ITimelineRepository`;
   **use cases** (create/list/get/update/delete clip); **DTOs**; **router**
   extension; DI wiring; unit + integration tests; docs.

### 1.3 What's out (deferred)

- **Transition links** (`transition_in_id` / `transition_out_id`) — deferred to
  α6.4 (Transitions). Server leaves them `None`; surfaced read-only (like
  `timelines.project_version_id`). No write path in α6.3b (Q8 inherited).
- **`effects`** JSONB write path — see §4 Q1 (recommend defer, read-only `[]`).
- **Overlap enforcement** — clips may overlap in time on the same track (Q6
  inherited: allowed, no exclusion constraint, no app-level check).
- **Cross-track move** via PATCH (changing `track_id`) — see §4 Q4 (recommend
  defer).
- **`timelines.duration_seconds` auto-derivation** from clip extents — stays
  client-set (α6.3a behaviour); auto-roll-up deferred to α7+ (§3 D6).
- **Zero migrations.**

---

## Section 2 — Grounded facts (the physical `clips` table)

From `backend/app/infrastructure/db/models/timeline.py` (baseline `0001`,
`schema.md` §14):

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | `gen_random_uuid()` |
| `track_id` | UUID FK → `tracks.id` | `ON DELETE CASCADE` (tracks only soft-delete via API, so never fires) |
| `media_asset_id` | UUID FK → `media_assets.id`, **nullable** | `ON DELETE SET NULL` — app-level validation needed (α6.2 `_links` pattern) |
| `start_seconds` | `Numeric(10,3)` NOT NULL | CHECK `start_seconds >= 0` (`start_nonnegative`) |
| `end_seconds` | `Numeric(10,3)` NOT NULL | CHECK `end_seconds > start_seconds` (`end_after_start`) |
| `source_start_seconds` | `Numeric(10,3)` NOT NULL, default 0 | trim-in into source |
| `source_end_seconds` | `Numeric(10,3)` NOT NULL, default 0 | trim-out into source |
| `transition_in_id` / `transition_out_id` | UUID FK → `transitions.id`, nullable | `ON DELETE SET NULL` — **deferred** |
| `effects` | JSONB NOT NULL, default `'[]'` | freeform list — **deferred** write path |
| `volume` | `Numeric(4,2)` NOT NULL, default `1.00` | CHECK `volume BETWEEN 0 AND 4` (`volume_range`) |
| `locked` | Boolean NOT NULL, default `false` | |
| timestamps + `deleted_at` | | `TimestampMixin` + `SoftDeleteMixin` |
| **no `version`** | | child of the timeline aggregate (ADR-0038) |

Indexes: `ix_clips_track_id_start_seconds` (`track_id`, `start_seconds`) — the
natural ordering key; `ix_clips_media_asset_id`. **No unique constraint** on
`(track_id, start_seconds)` → overlaps/duplicate starts are physically allowed
(Q6). **No `z_index`/order column** on clips — ordering is by `start_seconds`.

Key consequences:
- The DB already enforces `start >= 0` and `end > start`. The DTO enforces the
  same (→ `422` before the write) so the client gets a clean validation error,
  not a `500` from a CHECK violation.
- `Numeric` → psycopg returns `Decimal`; the domain/DTO model these as `float`
  (same `Decimal → float` handling as `timelines.duration_seconds` in α6.3a).

---

## Section 3 — Decisions (inherited + clip-specific), recommended

- **D1 — Aggregate membership / OCC (inherited Q1/Q13).** Clip is a timeline
  child. `version` is **optional** on clip `POST` (a create cannot be harmfully
  stale → unconditional `bump_version`), **required** on `PATCH`/`DELETE` (fenced
  CAS → `412` on stale). Each successful mutation bumps `timelines.version` once,
  in the same transaction. Never touches `projects.version`.
- **D2 — Routing.** Nested under the track:
  `/projects/{project_id}/timeline/tracks/{track_id}/clips[/{clip_id}]`. The
  timeline is 1:1 with the project (α6.3a), so the track_id disambiguates the
  parent. Extends the existing `timeline.py` router (no new router module).
- **D3 — Visibility gate / 404-before-412.** Four-level uniform `404`: project
  (owned) → timeline (exists) → track (in timeline, live) → clip (in track,
  live). All missing/not-yours/soft-deleted cases are indistinguishable `404`
  (α5a D5 lineage). The fence (`412`) is consulted **after** clip visibility, so
  a repeat DELETE is `404` (idempotent-by-404), not `412`.
- **D4 — `media_asset_id` validation.** Nullable. When present on create or
  changed on update, validate via `IMediaRepository.get_owned(media_id,
  tenant_id, owner_user_id)`; `None` → `ValidationFailedError` (`422`), never
  `404` (the *body* is bad, the route target is fine — α6.2 `_links` semantics).
  Reuse the pattern in a small `timeline/_links.py` helper.
- **D5 — Time model.** `start_seconds` / `end_seconds` required on create;
  DTO enforces `start >= 0`, `end > start` (→ `422`). `source_start_seconds` /
  `source_end_seconds` optional, default 0, `>= 0`. Clip ranges are **free-form**
  — not required to fit within `timelines.duration_seconds` or the track, and may
  overlap other clips (Q6). No enforcement in α6.3b.
- **D6 — `timelines.duration_seconds` stays client-set.** A clip write does
  **not** auto-extend the timeline duration. Auto-roll-up (max clip `end_seconds`)
  is deferred to α7+ so α6.3b keeps the α6.3a duration semantics intact.
- **D7 — Ordering.** `GET …/clips` returns clips ordered by `start_seconds ASC,
  id ASC` (a *total* order — `id` tiebreaks equal starts so pagination/listing is
  deterministic, matching the α5c/α6.1 total-order discipline).
- **D8 — Composition tree.** `GET …/timeline` and `GET …/tracks` embed each
  track's ordered clips (`TrackPublic.clips: list[ClipPublic]`); the flat
  `GET …/tracks/{track_id}/clips` returns the same clip list with the aggregate
  token in `meta.timeline_version`. `ClipPublic` has **no `version`** (shares the
  timeline's token, surfaced in `meta`).
- **D9 — Transition links / effects surfaced read-only.** `transition_in_id`,
  `transition_out_id`, `effects` appear in `ClipPublic` (read-only) but are not
  writable in α6.3b (§1.3).

---

## Section 4 — Open questions for sign-off

**Q1 — `effects` write path.** The column is a freeform JSONB list with no
schema yet. **Recommend: defer** — surface `effects` read-only (always `[]` in
α6.3b), add the write path when an effect schema is defined (likely alongside
transitions in α6.4). Keeps the slice tight and avoids shipping an unvalidated
opaque-blob endpoint. *(Alternative: accept an opaque `list` passthrough now.)*

**Q2 — `source_start_seconds` / `source_end_seconds` in the write DTO.**
**Recommend: include** as optional mutable fields (default 0, `>= 0`). They are
core clip semantics (the trim window into the source media) and are simple
numerics with a clear meaning — cheap to support now. The DB has no
`source_end >= source_start` CHECK; **recommend a soft DTO validation** (if both
supplied, `source_end_seconds >= source_start_seconds`) → `422`. *(Alternative:
defer both to α7 trimming UI.)*

**Q3 — DELETE fence mechanism.** **Recommend: mirror tracks exactly** — required
`?version=<n>` query parameter, fenced on `timelines.version`, `412` on stale,
`404`-before-`412`. Consistency with α6.3a `DELETE …/tracks/{id}` is the whole
point of the shared token.

**Q4 — Cross-track move via PATCH (`track_id` change).** **Recommend: defer** —
`track_id` is immutable in α6.3b; moving a clip = delete + recreate. Allowing a
move would require validating the destination track (same timeline, live) inside
the update path; not worth the surface now. *(Alternative: allow `track_id`
change with destination validation → `422`/`404`.)*

**Q5 — Timeline `duration_seconds` auto-derive (confirm D6).** **Recommend:
confirm client-set only** for α6.3b (no auto-roll-up). Flagging explicitly since
it's the most likely "shouldn't the timeline grow when I add a clip?" question —
the answer is "yes, but that's an α7 concern; α6.3b keeps duration a manual root
field."

---

## Section 5 — Planned surface (pending §4)

```
POST   /api/v1/projects/{id}/timeline/tracks/{track_id}/clips
  body:  { media_asset_id?, start_seconds, end_seconds,
           source_start_seconds?, source_end_seconds?, volume?, locked?, version? }
  → 201  { data: ClipPublic, meta: { timeline_version } }
  → 404 (project/timeline/track missing) · 412 (stale, if version sent)
  · 422 (bad time range / bad media_asset_id) · 401

GET    /api/v1/projects/{id}/timeline/tracks/{track_id}/clips
  → 200  { data: [ClipPublic … start_seconds ASC], meta: { timeline_version } } · 404 · 401

GET    /api/v1/projects/{id}/timeline/tracks/{track_id}/clips/{clip_id}
  → 200  { data: ClipPublic, meta } · 404 · 401

PATCH  /api/v1/projects/{id}/timeline/tracks/{track_id}/clips/{clip_id}
  body:  { version, media_asset_id?, start_seconds?, end_seconds?,
           source_start_seconds?, source_end_seconds?, volume?, locked? }
  → 200  { data: ClipPublic, meta: { timeline_version } }
  → 404 · 412 (stale) · 422 · 401

DELETE /api/v1/projects/{id}/timeline/tracks/{track_id}/clips/{clip_id}?version=<n>
  → 204 · 404 (missing/already-deleted) · 412 (stale) · 401
```

Implementation order (mirrors α6.3a): domain `Clip` → `ITimelineRepository`
clip methods + `TimelineRepository` impl + fakes/UoW wiring → `timeline/_links.py`
+ clip use cases → DTOs + container factories + deps aliases → router extension +
embed clips in `TimelineResult`/`TimelinePublic`/`TrackPublic` → unit tests →
integration tests → docs (ADR-0038 addendum / `TIMELINE_AGGREGATE.md`,
`API_CONTRACT.md`, `CHANGELOG.md`, `ROADMAP.md`) → CI gate → merge → tag
`v0.4.14-phase3-alpha6.3b`.

---

## Section 6 — Reviewer sign-off

**SIGNED OFF (2026-07-14).** All five §4 questions accepted as drafted:

- **Q1 — Effects:** ✅ Defer write support to α6.4. Surface read-only (`[]` or
  existing persisted values). No unvalidated JSON-blob endpoint in α6.3b.
- **Q2 — Source trim fields:** ✅ Include `source_start_seconds` /
  `source_end_seconds` as optional mutable fields. Validation: `source_start >= 0`,
  `source_end >= source_start` → `422`.
- **Q3 — DELETE OCC fence:** ✅ Mirror tracks exactly — required
  `?version=<timeline_version>`, 404-before-412, `412` on stale, successful
  delete bumps `timelines.version`.
- **Q4 — Cross-track move:** ✅ Defer. `track_id` immutable in α6.3b; a move is
  DELETE old + CREATE new.
- **Q5 — Timeline duration:** ✅ `duration_seconds` stays client-controlled; no
  auto-growth from clips (auto-derivation belongs to α7+).

Proceed: branch `phase3/alpha6.3b-clips`, bump `app/main.py` →
`0.4.14-phase3-alpha6.3b-dev`, implement in the §5 order.
