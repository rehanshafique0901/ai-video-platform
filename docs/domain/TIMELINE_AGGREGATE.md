# Timeline Aggregate

> **Convention.** This is a domain design document (companion to
> `docs/domain/PROJECT_AGGREGATE.md`, `docs/domain/SCENE_AGGREGATE.md`,
> `docs/domain/PROMPT_AGGREGATE.md`, and `docs/domain/MEDIA_AGGREGATE.md`). It
> defines the **Timeline** aggregate — its identity, boundary, **derived**
> (project-scoped) ownership, and the defining stance: a **self-contained
> optimistic-concurrency aggregate** whose single OCC token is
> **`timelines.version`**, yet which is **excluded** from the project version
> ledger. It is the design authority for Phase 3 **α6.3** (α6.3a: timeline root +
> tracks; α6.3b: clips). Read it alongside the α6.3 pre-flight
> (`docs/engineering/PHASE3_ALPHA6_3_PREFLIGHT.md`) and **ADR-0038**.
>
> **Grounding.** Every schema claim is checked against the live ORM
> (`backend/app/infrastructure/db/models/timeline.py`) and the baseline migration
> (`backend/alembic/versions/0001_baseline.py`), not an idealised model. Two
> baseline facts are the whole thesis: `timelines` carries `VersionMixin` **and is
> in `_VERSION_BUMP_TABLES`** (it has a `version` + the guarded bump trigger),
> while `tracks` (and `clips`) carry **neither**. So the timeline root is the only
> versioned member of the tree — its token fences the whole aggregate.

---

## 1. Purpose & position in the model

A **Timeline** is the **composition layer**: it places registered media (α6.2)
onto ordered **tracks** as time-ranged **clips**, turning a project's generation
outputs into an arrangement that a renderer can consume. It is **1:1 with a
project** (one live timeline per project).

Position in the hierarchy (baseline schema fact):

```
Project (α4/α5)
  └── Timeline  (timelines.project_id, 1:1 — uq_timelines_project_id where deleted_at IS NULL)   ← α6.3a
         └── Track  (tracks.timeline_id — no version of its own)                                  ← α6.3a
                └── Clip  (clips.track_id → media_asset_id)                                        ← α6.3b

Prompt (α6.1) ──drives──▶ Media Asset (α6.2) ──placed as──▶ Clip ──on Track──▶ Timeline (α6.3)
```

The critical separation this document establishes:

> **Scenes are editorial state. Prompts are generation inputs. Media assets are
> generation outputs. The Timeline is the composition of those outputs — its own
> optimistic-concurrency aggregate, excluded from the editorial version ledger.**

This is a **third** concurrency posture (ADR-0038), distinct from both the
editorial aggregate (projects + scenes — aggregate OCC, *in* the ledger) and the
generation artefacts (prompts + media — last-writer-wins, no OCC, excluded).

---

## 2. Aggregate boundary

### 2.1 The aggregate root — `Timeline` (the only versioned member)

The domain `Timeline` is a slim, frozen view of the physical row:

- `id` — durable UUID, server-minted.
- `project_id` — the parent link (1:1; `ON DELETE CASCADE`, but timelines are only
  soft-deleted via the API so the cascade never fires). Ownership is **derived**
  through the project — the table carries **no** `tenant_id` / `owner_user_id`.
- `project_version_id` — optional **provenance** link (which project version the
  timeline was composed against). **Write path deferred to α7+** (ADR-0035); α6.3
  leaves it `None` and surfaces it read-only.
- `aspect_ratio` — free text (e.g. `'16:9'`), `NOT NULL`. Defaults from the
  project orientation on provision when omitted. **Mutable.**
- `frame_rate` — `1–240` (CHECK `frame_rate_range`), server-default `30`.
  **Mutable.**
- `background_color` — hex text, server-default `'#000000'`. **Mutable.**
- `duration_seconds` — `Numeric(10,3)` (domain `float`), server-default `0`.
  **Mutable.**
- `version` — **the single OCC token for the whole aggregate** (§4). Server-owned.
- `created_at` / `updated_at` — timestamps; `updated_at` is trigger-owned.

### 2.2 The child — `Track` (no version of its own)

The domain `Track` is a slim, frozen view of the `tracks` row:

- `id` — durable UUID, server-minted.
- `timeline_id` — the parent link (`ON DELETE CASCADE`).
- `kind` — one of the `track_kind` enum values (`video, audio, subtitle, effect`).
  **Mutable.**
- `z_index` — the stacking order, a **sparse integer unique per live timeline**
  (`uq_tracks_timeline_id_z_index`, partial). **Client-assigned**; a collision is
  a `409` (§3). Gaps are legal (a stacking key, not a dense sequence). **Mutable.**
- `locked` / `muted` — track flags, server-default `false`. **Mutable.**
- `name` — free text, `NOT NULL`. **Mutable.**
- `created_at` / `updated_at` — timestamps; `updated_at` is trigger-owned.

### 2.2b The child — `Clip` (α6.3b, no version of its own)

The domain `Clip` is a slim, frozen view of the `clips` row — the third tier of
the aggregate, a **child of a `Track`** (which is itself a child of the timeline):

- `id` — durable UUID, server-minted.
- `track_id` — the parent link (`ON DELETE CASCADE`). **Immutable** in α6.3b — a
  cross-track move is modelled as delete + recreate (pre-flight Q4).
- `media_asset_id` — optional link to a **live media asset the caller owns**;
  validated on create and on any PATCH that sets it (else `422`). An explicit
  `null` on PATCH **unlinks**. **Mutable.**
- `start_seconds` / `end_seconds` — the clip's placement on the track timeline;
  `end_seconds > start_seconds` (DB CHECK + DTO). Overlaps between clips are
  **allowed** (pre-flight Q6). **Mutable.**
- `source_start_seconds` / `source_end_seconds` — the trim window into the source
  asset; `source_end_seconds ≥ source_start_seconds` (default `0`). **Mutable.**
- `volume` — playback gain `0–4`, server-default `1`. **Mutable.**
- `locked` — clip flag, server-default `false`. **Mutable.**
- `transition_in_id` / `transition_out_id` / `effects` — **read-only** in α6.3b
  (write paths deferred to α6.4); surfaced as-persisted (`effects` defaults `[]`).
- `created_at` / `updated_at` — timestamps; `updated_at` is trigger-owned.

Clips are ordered by `start_seconds` ASC (`id` ASC tiebreak) and embedded into
`TrackPublic.clips[]` in composition reads (`GET …/timeline`, `GET …/tracks`).

### 2.3 The defining fact: only the root carries `version`

`Timeline` is `UUIDPrimaryKeyMixin + TimestampMixin + SoftDeleteMixin +
VersionMixin` and **is** in `_VERSION_BUMP_TABLES` (guarded
`tg_timelines_biu_version_bump`). `Track` (and α6.3b `Clip`) are built **without**
`VersionMixin` and are **absent** from `_VERSION_BUMP_TABLES`. So there is exactly
one version in the tree — the root's — and it is the aggregate's OCC token (§4).

---

## 3. Ownership, scoping & anti-enumeration

Ownership is **derived through the project** (the timeline/track tables have no
owner columns). Every endpoint is authenticated (`CurrentUserDep`) and runs a
**two-level gate** (contrast the single-level media gate):

- **Project gate** — `IProjectRepository.get_owned(project_id, tenant, owner)`;
  `None` → uniform `404 NOT_FOUND` (missing / soft-deleted / not the caller's).
- **Timeline gate** — `TimelineRepository.get_by_project(project_id)`; `None` →
  `404` (the timeline is created **explicitly**, so an un-provisioned project has
  no timeline). Track access adds a **track gate** (`get_track`) → `404`.

Because tracks are scoped by `timeline_id` and the timeline is 1:1 with the
already-owned project, no further tenant/owner check is needed at the track layer.

`z_index` uniqueness is enforced by a partial-unique index; a collision — on
create or on a `z_index`-changing update — is a **`409 CONFLICT`** (the server
does **not** silently reorder). A second `POST /timeline` is a **`409`** from
`uq_timelines_project_id`.

---

## 4. Concurrency: a self-contained OCC aggregate (ADR-0038)

`timelines.version` is the **single OCC token for the whole tree** (root + tracks
+ clips). This is a third posture, distinct from projects-and-scenes (aggregate
OCC *in* the ledger) and prompts-and-media (no OCC):

- **Root update** (`PATCH …/timeline`) is a **version-fenced CAS** on the
  timeline's own columns — the same 404-before-412 control flow as `UpdateScene` /
  `UpdateProject`. The repository hand-sets `version + 1` over the guarded bump
  trigger (net **+1**), exactly like `ProjectRepository.update_owned`. Stale → zero
  rows → `None` → `412`. A same-value patch is a `200` no-op (no write, no bump).
- **Child mutations** (track create/update/delete; α6.3b clips) have **no fence on
  the child row** (no child `version`). The use case pairs each real child write
  with an **aggregate roll-up** — `bump_version` — in the same transaction:
  - **create** (`POST …/tracks`): `version` is **optional** (a create cannot be
    harmfully stale). Omitted → bump **unconditionally**; supplied → fence (stale →
    `412`).
  - **update / delete**: `version` is **required** — a `PATCH` body field, or a
    `DELETE …?version=<n>` query parameter. Fenced roll-up; stale → `412`;
    rollback undoes the child write.

The child (track) wire carries **no `version`** — the aggregate token travels in
the response `meta.timeline_version`, which the client carries into the next
fenced write.

**A timeline/track/clip mutation never bumps `projects.version`** (§5).

---

## 5. Exclusion from the project version ledger (ADR-0038 adopts ADR-0035)

The `project_versions` snapshot boundary is **{project root + default storyboard +
ordered scenes}** (ADR-0035) — the timeline is **excluded**: not captured, not
restored, not diffed, and a timeline edit does **not** bump `projects.version`.

The governing principle (ADR-0038):

> **The Timeline is a self-contained optimistic-concurrency aggregate.
> `timelines.version` is the single OCC token for the whole tree; a
> timeline/track/clip mutation fences on and bumps it, but is a composition change
> — not versioned editorial content — so it neither bumps `projects.version` nor
> appears in any project-version snapshot / restore / diff.**

Consequences:

- Restoring a project to an old version does **not** resurrect or delete timeline
  composition; `projects.version` is unaffected by any timeline edit.
- `project_version_id` records *forward provenance* (which project version the
  timeline was composed against) — read-only in α6.3, write path α7+.

---

## 6. Lifecycle

```
      provision                     PATCH (fenced)            DELETE track (fenced, ?version=)
  ∅ ────────────▶  timeline live ──────────────────▶ …      ─────────────────────────▶ track soft-deleted
   (version = 1,       │  + tracks (create/update/                                       (z_index freed;
    tracks = [])       │    delete, each bumps                                             timelines.version++)
                       └──  timelines.version)        second POST /timeline → 409
```

- **provision** — `POST …/timeline`; explicit, non-lazy (Q3). `version = 1`, no
  tracks. `aspect_ratio` defaults from project orientation when omitted. Second
  provision → `409`. `201`.
- **root PATCH** — version-fenced, narrow (aspect_ratio / frame_rate /
  background_color / duration_seconds). `412` on stale; `200` no-op on same-value.
- **track create** — `POST …/tracks`; `z_index` client-assigned (collision →
  `409`); optional `version` fence; bumps the aggregate token. `201` +
  `meta.timeline_version`.
- **track PATCH** — required `version`; z_index collision → `409`; bumps the token;
  `200` no-op on same-value. 404-before-412.
- **track DELETE** — required `?version=`; soft-deletes (frees z_index), bumps the
  token; **idempotent-by-404** (repeat delete → `404`, not `412`). `204`.

`GET …/timeline` (root + ordered tracks) and `GET …/timeline/tracks` (tracks,
`z_index` ASC) are side-effect-free, soft-delete-excluded, and surface
`timeline_version`.

---

## 7. Structured-log posture

Timeline lifecycle events are logged with identifiers + field names only:

- `timeline.provisioned` (INFO) — `timeline_id`, `project_id`, `aspect_ratio`,
  `frame_rate`, `owner_user_id`, `ip`.
- `timeline.updated` / `timeline.update_rejected` — `timeline_id`, `project_id`,
  `changed_fields` / `reason` (`version_mismatch` / `same_value_noop`),
  `previous_version`, `new_version`, `ip`.
- `track.created` (INFO) — `track_id`, `timeline_id`, `project_id`, `kind`,
  `z_index`, `new_version`, `ip`.
- `track.updated` / `track.update_rejected` — `track_id`, `changed_fields` /
  `reason`, `previous_version`, `new_version`, `ip`.
- `track.deleted` / `track.delete_rejected` — `track_id`, `previous_version`,
  `new_version` / `reason`, `ip`.

---

## 8. Open evolution (explicitly out of α6.3)

- **Clips (α6.3b) — shipped.** `clips.media_asset_id` places a registered asset on
  a track; clip create/update/delete fences on / bumps the same
  `timelines.version`. Validation covers a valid media link (`422`), non-negative
  start, `end > start`, `source_end ≥ source_start`. **Overlaps allowed** (Q6);
  `track_id` immutable (Q4); `effects` / transitions read-only (write → α6.4).
- **Editorial rules.** Snapping / ripple / trim / split / magnetic timeline /
  grouping — later phases; not enforced at the schema/API level in α6.3.
- **Transitions.** `clips.transition_in_id` / `transition_out_id` exist in
  baseline; the `transitions` aggregate is deferred to its own slice (not
  half-built into clips).
- **`project_version_id` write path.** Forward-provenance link, read-only `None`
  in α6.3; populated α7+.
- **Render / export.** α6.4 consumes the composed timeline to produce deliverables.

---

## 9. Change log

| Date | Change |
|---|---|
| 2026-07-13 | Initial authoring for Phase 3 α6.3a (Timeline + Tracks). Establishes the composition-layer identity, derived (project-scoped) ownership + two-level visibility gate, explicit non-lazy provision (second → 409), client-assigned `z_index` uniqueness (→ 409), the self-contained OCC aggregate model (`timelines.version` as the single token; root fenced CAS; child roll-up via `bump_version`; child-POST version optional, child-PATCH/DELETE required; 404-before-412), exclusion from the project version ledger (no `projects.version` bump, no snapshot), and the lifecycle. Adopts ADR-0038 (which adopts ADR-0035). Clips are α6.3b. |
| 2026-07-14 | Phase 3 α6.3b (Clips). Adds the third aggregate tier — `Clip` (child of a track, no `version`), fenced by the same `timelines.version`. Nested clip CRUD under `…/tracks/{track_id}/clips`; `media_asset_id` ownership validation (`422`); `end > start` + `source_end ≥ source_start` (incl. merged-range check on PATCH); ordered by `start_seconds` (`id` tiebreak); overlaps allowed; `track_id` immutable (Q4); `effects`/transitions read-only (write → α6.4); `duration_seconds` stays client-controlled (Q5). Clips embedded in `TrackPublic.clips[]` on composition reads. Idempotent-by-404 soft delete; 404-before-412. |
