# API_CONTRACT

> **Designed before implementation.** This file is the binding source of truth for the public API surface. Backend code is generated to match this contract; the contract is not generated from code. Any change here requires an ADR.

---

## 1. Conventions

- **Base path:** `/api/v1`
- **Content-Type:** `application/json; charset=utf-8`
- **Auth:** `Authorization: Bearer <access_jwt>` unless explicitly marked **public**.
- **IDs:** UUIDv4. URLs use the ID as-is (no slugs).
- **Timestamps:** RFC 3339 / ISO 8601 in UTC; field names always end in `_at`.
- **Money:** `{ "amount_cents": 120, "currency": "USD" }`.
- **Pagination:** cursor-based; query params `?cursor=…&limit=…` (max 100). Response includes `next_cursor`.
- **Idempotency:** unsafe methods accept an `Idempotency-Key` header; the server caches the response for 24 h.
- **Versioning:** path-based (`/v1`, `/v2`); breaking changes require a new major version.

### 1.1 Envelope

Success:
```
{
  "data": <object or array>,
  "meta": { "request_id": "…", "next_cursor": "…" }
}
```

Error:
```
{
  "error": {
    "code": "NOT_FOUND",
    "message": "Human-readable explanation.",
    "details": { "project_id": "…" },
    "request_id": "…"
  }
}
```

### 1.2 Error Codes (initial set)

`UNAUTHENTICATED`, `FORBIDDEN`, `NOT_FOUND`, `VALIDATION_FAILED`, `CONFLICT`, `RATE_LIMITED`, `PROVIDER_ERROR`, `MODEL_DEPRECATED`, `MODEL_RETIRED`, `FEATURE_DISABLED`, `INSUFFICIENT_CREDITS`, `WORKFLOW_NOT_RESUMABLE`, `INTERNAL_ERROR`.

---

## 2. Resource Map

| Group | Routes (prefix `/api/v1`) |
|---|---|
| Auth | `/auth/*` |
| Users | `/users/me`, `/users/{id}` (admin) |
| Projects | `/projects`, `/projects/{id}` |
| Project Versions (CR-6) | `/projects/{id}/versions`, `/projects/{id}/versions/{version_id}` |
| Scenes | `/projects/{id}/scenes`, `/projects/{id}/scenes/{scene_id}`, `/projects/{id}/scenes/{scene_id}/move` |
| Prompts (α6.1) | `/projects/{id}/prompts`, `/projects/{id}/prompts/{prompt_id}` |
| Media (α6.2) | `/media`, `/media/{media_id}` (top-level, owner-scoped) |
| AI — Script | `/ai/script` |
| AI — Storyboard | `/ai/storyboard` |
| AI — Images | `/ai/images` |
| AI — Videos | `/ai/videos` |
| AI — Voice | `/ai/voice` |
| AI — Subtitles | `/ai/subtitles` |
| Timeline (α6.3a/b) | `/projects/{id}/timeline`, `/projects/{id}/timeline/tracks`, `/projects/{id}/timeline/tracks/{track_id}`, `…/tracks/{track_id}/clips`, `…/tracks/{track_id}/clips/{clip_id}` |
| Render Jobs (α7.1) | `/projects/{id}/render-jobs`, `/projects/{id}/render-jobs/{render_job_id}`, `…/render-jobs/{render_job_id}/cancel` |
| Workflow Runs (α7.2) | `/projects/{id}/workflow-runs`, `/projects/{id}/workflow-runs/{workflow_run_id}`, `…/workflow-runs/{workflow_run_id}/advance`, `…/workflow-runs/{workflow_run_id}/cancel` |
| Export | `/projects/{id}/export` |
| Asset Library (CR-8) | `/library/assets`, `/library/assets/{id}` |
| Workflows (CR-7) | `/workflows`, `/workflows/{id}`, `/workflows/{id}/pause|resume|cancel` |
| Pipelines (CR-2) | `/pipelines` (read-only catalogue) |
| AI Models (CR-11) | `/ai-models`, `/ai-models/{id}`, `/ai-models/defaults` |
| Usage & Cost (CR-12) | `/usage`, `/usage/{id}`, `/usage/summary`, `/projects/{id}/usage` |
| Credits | `/credits`, `/credits/transactions`, `/credits/purchase` |
| Billing | `/billing/subscription`, `/billing/invoices`, `/billing/portal` |
| Templates | `/templates`, `/templates/{id}` |
| Analytics | `/analytics/events`, `/analytics/dashboard` |
| Notifications | `/notifications`, `/notifications/{id}/read` |
| Feature Flags (CR-9) | `/feature-flags`, `/feature-flags/evaluate`, admin: `/admin/feature-flags/{key}` |
| Plugins (CR-1) | `/plugins` (read), admin: `/admin/plugins` |
| Admin: Queues (CR-13) | `/admin/queues`, `/admin/queues/{queue}/dlq` |
| Admin: AI Models | `/admin/ai-models/refresh`, `/admin/ai-models/{id}` |
| Webhooks | `/webhooks/stripe`, `/webhooks/providers/{name}` |
| WebSockets | `/ws/progress/{run_id}`, `/ws/workflows/{run_id}`, `/ws/timeline/{project_id}` |
| Health | `/healthz`, `/readyz` (public) |

---

## 3. Detailed Endpoint Sketches

### 3.1 Authentication

```
POST /auth/register
  body: { email, password, name }
  → 201 { user, access_token, refresh_token }

POST /auth/login
  body: { email, password }
  → 200 { user, access_token, refresh_token }

POST /auth/refresh
  body: { refresh_token }
  → 200 { access_token, refresh_token }       # rotates both

POST /auth/logout
  → 204

POST /auth/email/verify         body: { token } → 204
POST /auth/email/resend         → 204
POST /auth/password/forgot      body: { email } → 204
POST /auth/password/reset       body: { token, new_password } → 204

GET  /auth/oauth/google/start   → 302 (PKCE)
GET  /auth/oauth/google/callback → 302 to frontend with auth cookie
```

### 3.2 Projects

```
POST   /projects                       create     → ProjectCreated event
GET    /projects                       list (?folder_id, ?tag, ?query)
GET    /projects/{id}
PATCH  /projects/{id}                  partial update → autosave version
POST   /projects/{id}/duplicate
DELETE /projects/{id}                  soft delete
POST   /projects/{id}/autosave         explicit autosave snapshot
```

> **Shipped in Phase 3 α5a (create + read):** `POST /projects` (201,
> `version=1`), `GET /projects` (owner-scoped, newest-first
> `created_at DESC, id DESC`, cursor pagination via `?limit=` (1–100,
> default 20) + opaque `?cursor=`), and `GET /projects/{id}`. All three
> are authenticated (`CurrentUserDep`) and owner-and-tenant scoped:
> ownership/tenancy are taken from the caller, never the request body,
> and a project owned by another user (or in another tenant) is
> indistinguishable from a missing one — `GET /projects/{id}` returns a
> uniform `404 NOT_FOUND`. A duplicate live `name` for the same owner is
> `409 CONFLICT`.
>
> **Shipped in Phase 3 α5b (update + soft-delete):** `PATCH /projects/{id}`
> and `DELETE /projects/{id}`.
>
> * **`PATCH /projects/{id}`** — partial, version-fenced update. Body:
>   a required `version` (the client's last-observed value — the
>   optimistic-concurrency fence) plus any subset of the mutable fields
>   `name` / `description` / `language` / `style` / `settings`. Tri-state
>   semantics: an **absent** field is left unchanged; an explicit **`null`**
>   clears a nullable field (`description` / `style`); a **value** sets it.
>   `settings` is whole-object **replace** (not deep-merge). `aspect_ratio`,
>   `folder_id`, and all identity/server fields are **immutable** here
>   (`extra="forbid"` → `422`); an empty patch (only `version`) is `422`.
>   Returns `200` with the updated `ProjectPublic`; on a real change
>   `version` increments by exactly 1 and `updated_at` advances, while a
>   same-value patch is a `200` no-op (version unchanged). **404-before-412:**
>   a project that is missing, soft-deleted, or owned by another
>   user/tenant is a uniform `404 NOT_FOUND` (visibility is decided before
>   the version fence, so existence never leaks via a `412`); a stale
>   `version` on a visible project is `412 VERSION_CONFLICT`; a rename that
>   collides with another live project of the same owner is `409 CONFLICT`.
> * **`DELETE /projects/{id}`** — owner-scoped **soft** delete (sets
>   `deleted_at`); returns `204 No Content`. No version fence. Idempotent
>   **by-404**: the first delete succeeds, and every subsequent `DELETE` —
>   plus any `GET`/`PATCH` — on that id returns `404` (as does deleting
>   another user's/tenant's or an unknown project). Soft-deleting frees the
>   project `name` for re-use (the uniqueness index excludes deleted rows).
>
> `duplicate` / `autosave`, restore/un-delete, move-to-folder, and the
> `?folder_id` / `?tag` / `?query` list filters remain deferred to later
> slices. `ProjectPublic` omits `current_version_id` and `duration_seconds`
> (managed by later slices). See `docs/domain/PROJECT_AGGREGATE.md`.

#### 3.2.1 Scenes (Phase 3 α5c)

```
POST   /projects/{project_id}/scenes                 create (append)
GET    /projects/{project_id}/scenes                 list (ordered, un-paginated)
GET    /projects/{project_id}/scenes/{scene_id}
PATCH  /projects/{project_id}/scenes/{scene_id}      partial, version-fenced content update
POST   /projects/{project_id}/scenes/{scene_id}/move version-fenced reorder
DELETE /projects/{project_id}/scenes/{scene_id}      soft delete
```

> **Shipped in Phase 3 α5c (Scene CRUD + reorder).** Scenes are nested
> under a project; the physical `Project → Storyboard → Scene` hierarchy is
> hidden — a single **implicit default storyboard** is auto-created on the
> first scene, so `storyboard_id` never appears on the wire. All six
> endpoints are authenticated (`CurrentUserDep`) and run a **two-level
> visibility gate**: the caller must own the live project (else uniform
> `404 NOT_FOUND`), then the scene must live under it (else the same `404`).
> `ScenePublic` exposes a dense 1-based `position` (computed server-side)
> and **omits** both `storyboard_id` and the raw sparse ordering key
> `scene_number` (internal detail).
>
> * **`POST …/scenes`** — body `{ title, duration_seconds, narration?,
>   subtitle? }`; ordering/identity are server-owned (`extra="forbid"` →
>   `422`; `duration_seconds > 0`). Always **appends** at the end (`201`,
>   `version=1`, `position` = last). Repositioning is `…/move`, never create.
> * **`GET …/scenes`** — returns the project's live scenes ordered by
>   `position` ascending. **Not paginated** (a project's scene set is a
>   bounded editorial list). Read-only: a project with no scenes yet returns
>   `[]` and creates no storyboard.
> * **`GET …/scenes/{scene_id}`** — one scene (`200`), or the uniform `404`.
> * **`PATCH …/scenes/{scene_id}`** — partial, version-fenced, **content
>   only** (`title` / `duration_seconds` / `narration` / `subtitle`). Body:
>   a required `version` fence plus any subset of those fields. Tri-state:
>   absent = unchanged; explicit `null` clears a nullable field
>   (`narration` / `subtitle`); a value sets it (`title` /
>   `duration_seconds` are non-nullable → explicit `null` is `422`). An
>   empty patch (only `version`) is `422`; `position` is **not** accepted
>   here (`extra="forbid"` → `422`). On a real change `version` increments
>   by exactly 1; a same-value patch is a `200` no-op. **404-before-412**:
>   a stale `version` on a visible scene is `412 VERSION_CONFLICT`.
> * **`POST …/scenes/{scene_id}/move`** — a dedicated, version-fenced
>   reorder. Body `{ version, position }` (1-based). `position` is clamped
>   into `[1, N]`; a move to the current slot is a `200` no-op. `412` on a
>   stale `version` (including a concurrent content PATCH). Ordering uses a
>   sparse gap key with a transparent full rebalance when a gap is exhausted.
> * **`DELETE …/scenes/{scene_id}`** — owner-scoped **soft** delete (`204`),
>   no version fence, idempotent **by-404** (a second delete — and any
>   `GET`/`PATCH`/`move` after delete — is `404`; deleting another user's
>   scene or an unknown id is the same `404`).
>
> Scene identity (`id`) is a **durable UUID, stable across future Version
> restores** (a snapshot captures scene content keyed by the existing `id`;
> restore re-materialises under the same `id`, never minting new ones).
> Richer per-scene aggregates (prompt, voice, camera, assets) are deferred.
> See `docs/domain/SCENE_AGGREGATE.md`.

#### 3.2.2 Prompts (Phase 3 α6.1)

```
POST   /projects/{project_id}/prompts                create
GET    /projects/{project_id}/prompts                list (newest-first, filters: ?kind= ?scene_id=)
GET    /projects/{project_id}/prompts/{prompt_id}
PATCH  /projects/{project_id}/prompts/{prompt_id}    partial content update (no version fence)
DELETE /projects/{project_id}/prompts/{prompt_id}    soft delete
```

> **Shipped in Phase 3 α6.1 (Prompt CRUD).** A prompt is a **generation
> input** — authored text (`kind` + `text_content`) that later drives media
> generation — owned by a project and *optionally* linked to a scene. Prompts
> are nested under a project and run the same **two-level visibility gate** as
> scenes: the caller must own the live project (else uniform `404 NOT_FOUND`),
> then the prompt must live under it (else the same `404`).
>
> **Prompts are NOT versioned editorial content (ADR-0036).** They have **no
> `version` column**, do **not** participate in optimistic concurrency, and are
> **excluded** from `project_versions` snapshots / restore / diff. A `PATCH` is
> therefore **last-writer-wins** — there is no `version` on the wire and no
> `412`. Mutating a prompt does **not** bump `projects.version`. This keeps the
> versioned aggregate = {project root + scenes}; generation inputs (prompts,
> and later media/timeline) have their own lifecycle.
>
> * **`POST …/prompts`** — body `{ kind, text_content, scene_id?, model_id?,
>   extra? }`. `kind` is required and validated against the `prompt_kind` enum
>   (`image, video, animation, negative, camera, motion, lighting, style` —
>   modality kinds, **not** chat roles); `text_content` is required
>   (`1 ≤ len ≤ 10000`, whitespace-stripped). `scene_id` is optional — when
>   present it must reference a **live scene in the same project** (else `422
>   VALIDATION_FAILED`, not `404` — the route project is fine, the *body* is
>   invalid). `model_id` is optional — when present it must reference a live
>   `ai_models` row that is not `retired` (else `422`). `extra` is a free-form
>   JSON object (default `{}`). Identity + provenance are server-owned
>   (`generated_by_agent` stays server-`NULL`; `extra="forbid"` → any
>   non-declared key such as `id` / `generated_by_agent` is `422`). Returns
>   `201` + `PromptPublic`.
> * **`GET …/prompts`** — the project's live prompts, ordered **newest-first**
>   (`created_at` desc, `id` desc). Optional `?kind=<enum>` and
>   `?scene_id=<uuid>` filters narrow the result (combined = AND); a bad enum /
>   non-UUID is `422`. Not paginated. Empty → `200 []`.
> * **`GET …/prompts/{prompt_id}`** — one prompt (`200`), or the uniform `404`
>   (unknown / another project / soft-deleted).
> * **`PATCH …/prompts/{prompt_id}`** — partial, **content-only**, **no version
>   fence**. Body = any subset of `{ text_content, kind, model_id, extra }`.
>   Tri-state: absent = unchanged; explicit `null` clears the nullable
>   `model_id` (a re-validated non-null `model_id` must be linkable → else
>   `422`); `text_content` / `kind` are non-nullable (explicit `null` → `422`).
>   `scene_id` is **immutable** (no re-parenting in α6.1) and **not** accepted
>   (`extra="forbid"` → `422`). An empty patch → `422`. A same-value patch is a
>   `200` no-op. Returns `200` + `PromptPublic`.
> * **`DELETE …/prompts/{prompt_id}`** — owner-scoped **soft** delete (`204`),
>   no version fence, idempotent **by-404** (a second delete — and any
>   `GET`/`PATCH` after delete — is `404`; deleting another user's prompt or an
>   unknown id is the same `404`).
>
> `PromptPublic` = `{ id, project_id, scene_id, kind, text_content, model_id,
> extra, created_at, updated_at }` — **no `version`**; `generated_by_agent` and
> `deleted_at` are server-internal and omitted. A prompt's `scene_id` link
> **survives** a scene *soft-delete* and a version *restore* (the FK
> `ON DELETE SET NULL` fires only on a hard scene delete, which the API never
> performs). See `docs/domain/PROMPT_AGGREGATE.md` and
> `docs/decisions/ADR-0036-prompts-generation-inputs.md`.

#### 3.2.3 Media assets (Phase 3 α6.2)

```
POST   /media                register a media asset (metadata only)
GET    /media                list (newest-first, filters: ?kind= ?source= ?project_id= ?scene_id=)
GET    /media/{media_id}
PATCH  /media/{media_id}     narrow partial update (no version fence)
DELETE /media/{media_id}     soft delete
```

> **Shipped in Phase 3 α6.2 (Media CRUD).** A media asset is a **generation
> output** — a registered pointer to a concrete stored object (image / video /
> narration / subtitle / music / sound_effect / thumbnail). Unlike prompts and
> scenes, media is an **owner-level** artefact: the `media_assets` row carries
> its **own `tenant_id` + `owner_user_id`** and only a **nullable `project_id`**,
> so the endpoints are **top-level and owner-scoped** (not project-nested). The
> visibility gate is a single direct row match — a missing / soft-deleted /
> other-owner asset is a uniform `404 NOT_FOUND` (anti-enumeration).
>
> **Media is NOT versioned editorial content (ADR-0037, adopts ADR-0036).** It
> has **no `version` column**, does **not** participate in optimistic
> concurrency, and is **excluded** from `project_versions` snapshots / restore /
> diff. A `PATCH` is **last-writer-wins** — no `version` on the wire, no `412` —
> and mutating a media asset does **not** bump `projects.version`.
>
> **Register-by-metadata (α6.2 scope).** `POST /media` registers an object the
> client **already holds**: the API makes **no** provider call, **no** byte
> upload, **no** presigned URL, and does **not** verify the checksum. Object
> storage and AI generation are later slices.
>
> * **`POST /media`** — body `{ kind, source, storage_backend, storage_bucket,
>   storage_key, mime_type, size_bytes, checksum_sha256, project_id?, scene_id?,
>   prompt_id?, model_id?, provider?, width?, height?, duration_seconds?,
>   source_metadata? }`. `kind` (`media_kind`) and `storage_backend`
>   (`local, s3, r2, azure_blob, gcs`) are validated enums; `source` accepts only
>   `uploaded` / `stock` (`generated` → `422`, deferred to α8). `checksum_sha256`
>   is a **64-char hex** string (→ 32 bytes). `size_bytes ≥ 0`. Optional links
>   are validated for the caller — a foreign/unknown `project_id`, a `scene_id` /
>   `prompt_id` without (or outside) that project, or an unknown/retired
>   `model_id` → `422 VALIDATION_FAILED` (not `404`). Identity + ownership are
>   server-owned (`extra="forbid"` → any non-declared key such as `id` /
>   `owner_user_id` / `tenant_id` is `422`). Duplicate `(storage_backend,
>   storage_bucket, storage_key)` → **`409 CONFLICT`**. Returns `201` +
>   `MediaPublic`.
> * **`GET /media`** — the caller's live assets, ordered **newest-first**
>   (`created_at` desc, `id` desc). Optional `?kind=<enum>`, `?source=<str>`,
>   `?project_id=<uuid>`, `?scene_id=<uuid>` filters (combined = AND); a bad enum
>   / non-UUID is `422`. Not paginated. Empty → `200 []`.
> * **`GET /media/{media_id}`** — one asset (`200`), or the uniform `404`
>   (unknown / another owner / soft-deleted).
> * **`PATCH /media/{media_id}`** — **narrow**, partial, **no version fence**.
>   Body = any subset of the **mutable** set `{ project_id, scene_id, prompt_id,
>   model_id, provider, source_metadata }`. Tri-state: absent = unchanged;
>   explicit `null` clears a nullable link (re-validated when non-null → else
>   `422`); `source_metadata` is non-nullable (explicit `null` → `422`). The
>   physical-object fields (`storage_*`, `checksum_sha256`, `mime_type`,
>   `size_bytes`, `width`, `height`, `duration_seconds`, `kind`, `source`) are
>   **immutable** — presence in the patch → `422` (`extra="forbid"`). Empty patch
>   → `422`; a same-value patch is a `200` no-op. Returns `200` + `MediaPublic`.
> * **`DELETE /media/{media_id}`** — owner-scoped **soft** delete (`204`), no
>   version fence, idempotent **by-404** (a second delete — and any `GET`/`PATCH`
>   after delete — is `404`; deleting another owner's asset or an unknown id is
>   the same `404`).
>
> `MediaPublic` = `{ id, kind, source, project_id, scene_id, prompt_id, model_id,
> provider, storage_backend, storage_bucket, storage_key, mime_type, size_bytes,
> width, height, duration_seconds, checksum_sha256 (hex), source_metadata,
> created_at, updated_at }` — **no `version`**; `owner_user_id` / `tenant_id` /
> `deleted_at` are server-internal and omitted. A media asset's `project_id` /
> `scene_id` / `prompt_id` links **survive** a parent *soft-delete* and a version
> *restore* (the FK `ON DELETE SET NULL` fires only on a hard parent delete,
> which the API never performs). See `docs/domain/MEDIA_AGGREGATE.md` and
> `docs/decisions/ADR-0037-media-generation-outputs.md`.

#### 3.2.4 Timeline, tracks & clips (Phase 3 α6.3a/b)

```
POST   /projects/{project_id}/timeline                    provision the single timeline (explicit)
GET    /projects/{project_id}/timeline                    timeline root + ordered tracks
PATCH  /projects/{project_id}/timeline                    version-fenced root update
POST   /projects/{project_id}/timeline/tracks             append a track (version optional)
GET    /projects/{project_id}/timeline/tracks             list tracks (z_index ASC)
PATCH  /projects/{project_id}/timeline/tracks/{track_id}  version-fenced track update
DELETE /projects/{project_id}/timeline/tracks/{track_id}?version=<n>   version-fenced soft delete
POST   /projects/{project_id}/timeline/tracks/{track_id}/clips              append a clip (version optional)
GET    /projects/{project_id}/timeline/tracks/{track_id}/clips              list clips (start_seconds ASC)
GET    /projects/{project_id}/timeline/tracks/{track_id}/clips/{clip_id}    one clip
PATCH  /projects/{project_id}/timeline/tracks/{track_id}/clips/{clip_id}    version-fenced clip update
DELETE /projects/{project_id}/timeline/tracks/{track_id}/clips/{clip_id}?version=<n>   version-fenced soft delete
```

> **Shipped in Phase 3 α6.3a (Timeline + Tracks).** The Timeline is the
> **composition layer** — it places registered media (α6.2) onto ordered **tracks**
> as time-ranged clips (`Scene → Media → Clip → Timeline`; clips are α6.3b). It is
> **1:1 with a project** and **project-nested**; ownership is derived through the
> project, so every access runs a two-level gate (project ownership → timeline
> resolution, both `404`).
>
> **The Timeline is a self-contained OCC aggregate (ADR-0038, adopts ADR-0035).**
> The `timelines` root carries a `version` (its children — tracks, clips — do
> not), so **`timelines.version` is the single OCC token for the whole tree**: a
> timeline/track/clip mutation fences on and bumps it. A timeline edit is a
> composition change — it does **NOT** bump `projects.version` and is **excluded**
> from `project_versions` snapshots / restore / diff. The **track wire carries no
> `version`** — the aggregate token travels in the response `meta.timeline_version`,
> which the client carries into the next fenced write.
>
> * **`POST …/timeline`** — **explicit, non-lazy** creation (one per project). Body
>   `{ aspect_ratio?, frame_rate?, background_color? }`; `aspect_ratio` defaults
>   from the project orientation (`horizontal→16:9`, `vertical→9:16`,
>   `square→1:1`) when omitted; `frame_rate` `1–240`; `background_color` hex
>   (`#rrggbb`). A second provision → **`409 CONFLICT`**. Returns `201` +
>   `TimelinePublic` (`version = 1`, `tracks = []`). Missing/foreign project →
>   `404`.
> * **`GET …/timeline`** — the timeline root plus its live tracks ordered by
>   `z_index` ASC. Un-provisioned timeline → `404`.
> * **`PATCH …/timeline`** — version-fenced. Body `{ version, aspect_ratio?,
>   frame_rate?, background_color?, duration_seconds? }`; `version` (the aggregate
>   token) required plus ≥ 1 mutable field (empty patch → `422`). A real change
>   advances `version` by **+1**; a stale `version` → **`412`**; a same-value patch
>   is a `200` no-op. No `projects.version` bump.
> * **`POST …/timeline/tracks`** — body `{ kind, z_index, name, locked?, muted?,
>   version? }`. `kind` a `track_kind` enum (`video, audio, subtitle, effect`);
>   `z_index ≥ 0`, **client-assigned** and unique per live timeline (collision →
>   **`409`** — the server does not silently reorder). `version` is **optional** (a
>   child create cannot be harmfully stale): omitted → bump the token
>   unconditionally; supplied → fence (stale → `412`). Returns `201` +
>   `TrackPublic` with `meta.timeline_version`.
> * **`GET …/timeline/tracks`** — the live tracks (`z_index` ASC);
>   `meta.timeline_version`.
> * **`PATCH …/timeline/tracks/{track_id}`** — body `{ version, kind?, z_index?,
>   name?, locked?, muted? }`; `version` (the **timeline's**) **required** plus ≥ 1
>   mutable field (empty → `422`). z_index collision → `409`; stale `version` →
>   `412`; same-value → `200` no-op. **404-before-412** (a missing track is `404`
>   even with a stale token). Returns `200` + `TrackPublic` with
>   `meta.timeline_version`.
> * **`DELETE …/timeline/tracks/{track_id}?version=<n>`** — the expected timeline
>   `version` is a **required** query parameter. Soft-deletes (frees the `z_index`),
>   bumps the token; `204`. **Idempotent-by-404**: a repeat delete — or deleting an
>   unknown / another owner's track — is `404` (not `412`), decided before the fence.
>
> **Clips (α6.3b).** A **clip** is a time-bounded placement of a media asset on a
> track — the third tier of the aggregate, and (like tracks) a **child with no
> `version`**, fenced by the same `timelines.version`. `track_id` is **immutable**
> (a cross-track move is a delete + recreate); clip **overlaps are allowed**; the
> timeline `duration_seconds` is **not** auto-grown from clips.
>
> * **`POST …/tracks/{track_id}/clips`** — body `{ media_asset_id?, start_seconds,
>   end_seconds, source_start_seconds?, source_end_seconds?, volume?, locked?,
>   version? }`. `end_seconds > start_seconds` and `source_end_seconds ≥
>   source_start_seconds` (else `422`); `volume` `0–4`. `media_asset_id`, when set,
>   must reference a **live media asset you own** (else `422`). `version` **optional**
>   (child create can't be stale): omitted → bump unconditionally; supplied → fence
>   (stale → `412`). `201` + `ClipPublic`, token in `meta.timeline_version`.
>   Unknown project / timeline / track → `404`.
> * **`GET …/tracks/{track_id}/clips`** — the track's live clips ordered by
>   `start_seconds` ASC (`id` tiebreak); `meta.timeline_version`.
> * **`GET …/tracks/{track_id}/clips/{clip_id}`** — one clip. Four-level gate
>   (project → timeline → track → clip); any miss / cross-track → `404`.
> * **`PATCH …/tracks/{track_id}/clips/{clip_id}`** — body `{ version,
>   media_asset_id?, start_seconds?, end_seconds?, source_start_seconds?,
>   source_end_seconds?, volume?, locked? }`; `version` (the **timeline's**)
>   required plus ≥ 1 mutable field (empty → `422`). `media_asset_id` re-validated
>   when present (explicit `null` unlinks); the **merged** range is validated
>   against stored values (else `422`). Bumps the token; stale → `412`; same-value
>   → `200` no-op. **404-before-412**.
> * **`DELETE …/tracks/{track_id}/clips/{clip_id}?version=<n>`** — required
>   `?version=`; soft-deletes, bumps the token; `204`. **Idempotent-by-404**.
>
> `TimelinePublic` = `{ id, project_id, project_version_id (read-only, α7+),
> aspect_ratio, frame_rate, background_color, duration_seconds, version,
> created_at, updated_at, tracks: [TrackPublic, …] }`. `TrackPublic` = `{ id,
> timeline_id, kind, z_index, name, locked, muted, created_at, updated_at, clips:
> [ClipPublic, …] }` — **no `version`** (the aggregate token is
> `meta.timeline_version`); `clips` is populated in composition reads
> (`GET …/timeline`, `GET …/tracks`) and `[]` on mutation responses. `ClipPublic` =
> `{ id, track_id, media_asset_id, start_seconds, end_seconds, source_start_seconds,
> source_end_seconds, volume, locked, transition_in_id, transition_out_id (read-only,
> α6.4+), effects (read-only, α6.4+), created_at, updated_at }` — **no `version`**.
> See `docs/domain/TIMELINE_AGGREGATE.md` and
> `docs/decisions/ADR-0038-timeline-self-contained-occ-aggregate.md`.

#### 3.2.5 Render jobs (Phase 3 α7.1)

```
POST   /projects/{project_id}/render-jobs                            enqueue a render (idempotent)
GET    /projects/{project_id}/render-jobs                            list jobs (newest first; ?status= filter)
GET    /projects/{project_id}/render-jobs/{render_job_id}            one job
POST   /projects/{project_id}/render-jobs/{render_job_id}/cancel     version-fenced cancel
```

> **Shipped in Phase 3 α7.1 (`RenderJob` aggregate — CRUD + cancel; NO worker).**
> A **RenderJob** is the request to render a project's timeline and the record of
> that request's lifecycle — the first **orchestration** aggregate (contrast the
> α5–α6 domain-model aggregates). It owns **only orchestration metadata**; it does
> **not** own rendered/exported files, workflow state, or timeline edits (it
> references them by FK and coordinates via events — ADR-0039, pre-flight D3.10).
> It is **project-nested**; ownership is derived through the project, so every
> access runs a **two-level gate** (project ownership → render-job resolution, both
> `404` — anti-enumeration).
>
> **Self-versioned OCC (ADR-0039, adopts ADR-0037).** `render_jobs.version` is a
> **real column** and the job's **own** OCC token (unlike the timeline's borrowed
> token) — a cancel fences on / bumps it and never touches `projects.version` or
> `timelines.version`. The job has **no `deleted_at`**: it is an audit record, so
> there is **no `DELETE` verb** — "removal" is the `cancel` status transition, and a
> canceled job stays `GET`-able.
>
> **Status machine (α7.1 subset — no worker).** `queued` on create; `cancel` moves
> `queued`/`running` → `canceled`. The `running`/`succeeded`/`failed` transitions
> (and `output_media_asset_id`, `started_at`, `finished_at`, `error`, and `progress`
> beyond `'0.00'`) are **worker-owned (α8.x)**.
>
> * **`POST …/render-jobs`** — enqueue. Body `{ pipeline?, pipeline_version?,
>   queue?, priority?, idempotency_key? }`; defaults `pipeline='ffmpeg'`,
>   `pipeline_version='0.0.0'`, `queue='normal'`, `priority=0` (clamped `0–1000`).
>   The **timeline is resolved server-side** (1:1 with the project) — a project with
>   **no timeline → `422`** (visible but not fulfillable, so not `404`). `version`
>   `1`, `status='queued'`, `progress='0.00'`. Returns `201` + `RenderJobPublic` and
>   emits `RenderJobCreated` to the `event_outbox`. **Idempotency (Q4):** a repeat
>   with the **same `idempotency_key`** for the project returns the **existing** job
>   with **`200`** (no duplicate, no second event). Missing/foreign project → `404`.
> * **`GET …/render-jobs`** — the project's jobs, **newest first** (`created_at`
>   DESC, `id` DESC tiebreak). Optional **`?status=`** filters by one
>   `render_status` value (bad enum → `422`). Missing/foreign project → `404`.
> * **`GET …/render-jobs/{render_job_id}`** — one job. Two-level gate; unknown job,
>   or a job under another owner's project → `404`.
> * **`POST …/render-jobs/{render_job_id}/cancel`** — body `{ version }` (the job's
>   own token, **required**). A **version-fenced CAS** with a race-safe terminal
>   guard, decided **404 → classify → 412**:
>   * missing/foreign project or job → `404`;
>   * already `canceled` → **`200`** idempotent no-op (no event, no bump);
>   * `succeeded`/`failed` → **`409`** (completed work is not cancelable);
>   * cancelable but stale `version` → **`412`**;
>   * success → **`200`** + `RenderJobPublic` (`status='canceled'`, `version` +1) and
>     emits `RenderJobCanceled`.
>
> `RenderJobPublic` = `{ id, project_id, timeline_id, workflow_run_id (null in α7.1),
> pipeline, pipeline_version, queue, priority, status, progress, started_at (null),
> finished_at (null), error (null), output_media_asset_id (null), idempotency_key,
> version, created_at, updated_at }`. The events carry orchestration fields only
> (`render_job_id, project_id, timeline_id, pipeline, pipeline_version, queue,
> priority, status, version`; `event_version="1.0"`) — α7.1 only **produces** outbox
> rows (no dispatcher; the relay is α7.3). See `docs/domain/RENDER_JOB_AGGREGATE.md`
> and `docs/decisions/ADR-0039-render-job-orchestration-aggregate.md`.

#### 3.2.6 Workflow runs (Phase 3 α7.2)

```
POST   /projects/{project_id}/workflow-runs                              queue a run (idempotent)
GET    /projects/{project_id}/workflow-runs                              list runs (newest first; ?status= filter)
GET    /projects/{project_id}/workflow-runs/{workflow_run_id}            one run (+ steps + latest checkpoint)
POST   /projects/{project_id}/workflow-runs/{workflow_run_id}/advance    run the deterministic runner
POST   /projects/{project_id}/workflow-runs/{workflow_run_id}/cancel     status-guarded cancel
```

> **Shipped in Phase 3 α7.2 (`WorkflowRun` aggregate + the synchronous deterministic
> runner — NO worker, NO providers).** A **WorkflowRun** is the record of one workflow
> execution and the orchestration graph beneath it — the **second** orchestration
> aggregate (after `RenderJob`) and the first that **sequences** work: it owns an
> ordered graph of `WorkflowStep` children and **append-only** `WorkflowCheckpoint`
> children (ADR-0040, pre-flight D3.10). It is **project-nested**; ownership is derived
> through the project, so every access runs a **two-level gate** (project ownership →
> run resolution, both `404` — anti-enumeration). It never mutates `projects.version`
> / `RenderJob` / `MediaAsset` / `Timeline` — it coordinates via events.
>
> **Status-guarded CAS, not versioned OCC (ADR-0040 D2 — divergence from ADR-0039).**
> `workflow_runs` / `workflow_steps` carry **no `version` column** (not in
> `_VERSION_BUMP_TABLES`), so every lifecycle transition is a **status-predicated
> compare-and-swap** (`UPDATE … WHERE status IN (<allowed_from>)`); metadata is
> last-writer-wins. There is **no `?version=` on any endpoint, no `412`, and no
> `DELETE`** — the wire carries no OCC token, and "removal" is the `cancel` status
> transition (a canceled run stays `GET`-able).
>
> **Steps are pure, deterministic, side-effect-free (D3.11).** A step handler is a
> **pure function** `(StepContext) -> StepResult` that *returns a command/result
> describing* what should happen — it never calls providers. The **runner** (the
> imperative shell) interprets it: persists the step output, appends the checkpoint,
> emits events, and handles retries. α7.2 ships four provider-free registry workflows
> at `key@1.0.0`: `noop-chain`, `retry-succeed`, `terminal-fail`, `retry-exhaust`.
>
> **Status machine (α7.2 — synchronous runner).** `queued` on create (steps seeded
> `pending`); `advance` moves `queued → running`, runs every step, and settles the run
> `→ succeeded` / `→ failed` within one call; `cancel` moves `queued`/`running`/`paused`
> `→ canceled`. `paused` is **not** produced by the synchronous runner (pause/resume is
> α8.x).
>
> * **`POST …/workflow-runs`** — queue. Body `{ workflow_key, workflow_version,
>   input_snapshot?, idempotency_key? }` (`extra="forbid"`; `input_snapshot` defaults
>   `{}`). `workflow_key@workflow_version` is resolved against the **in-code registry
>   before any DB work** — an unknown pair → **`422`** (the project IS visible, so not
>   `404`). Seeds ordered `pending` steps from the definition. Returns `201` +
>   `WorkflowRunPublic` (`status='queued'`) and emits `WorkflowRunCreated`.
>   **Idempotency (Q7):** a repeat with the **same `idempotency_key`** for the project
>   returns the **existing** run with **`200`** (no duplicate, no second event).
>   Missing/foreign project → `404`.
> * **`GET …/workflow-runs`** — the project's runs, **newest first** (`created_at`
>   DESC, `id` DESC tiebreak), as summaries. Optional **`?status=`** filters by one
>   `workflow_status` value (bad enum → `422`). Missing/foreign project → `404`.
> * **`GET …/workflow-runs/{workflow_run_id}`** — one run with its ordered `steps` and
>   `latest_checkpoint`. Two-level gate; unknown run, or a run under another owner's
>   project → `404`.
> * **`POST …/workflow-runs/{workflow_run_id}/advance`** — **no body**. Runs the
>   deterministic runner to a terminal state (resume-safe: already-`succeeded`/`skipped`
>   steps are skipped, threading their checkpoint forward). `404` (project/run not
>   visible); **`409`** if already terminal; otherwise **`200`** + `WorkflowRunPublic`
>   (`succeeded` or `failed` — the outcome is in the body). Emits `WorkflowRunStarted`
>   (first `queued → running`), one `WorkflowStepCompleted` per step, and a terminal
>   `WorkflowRunSucceeded` / `WorkflowRunFailed`.
> * **`POST …/workflow-runs/{workflow_run_id}/cancel`** — **no body**. A status-guarded
>   CAS decided **404 → classify**:
>   * missing/foreign project or run → `404`;
>   * already `canceled` → **`200`** idempotent no-op (no event);
>   * `succeeded`/`failed` → **`409`** (completed work is not cancelable);
>   * `queued`/`running`/`paused` → **`200`** + `WorkflowRunPublic` (`status='canceled'`)
>     and emits `WorkflowRunCanceled`.
>
> `WorkflowRunSummary` (list) = `{ id, project_id, workflow_key, workflow_version,
> status, started_at, finished_at, triggered_by_user_id, idempotency_key,
> output_summary, error, created_at, updated_at }`. `WorkflowRunPublic` (detail) adds
> `{ input_snapshot, steps[], latest_checkpoint }`, where each step is `{ id,
> step_index, step_name, status, started_at, finished_at, retries, output, error }`
> and a checkpoint is `{ id, step_index, state, created_at }`. There is **no `version`
> field**. The six events carry orchestration fields only (`workflow_run_id,
> project_id, workflow_key, workflow_version, status`, plus step coordinates on the
> step event; `event_version="1.0"`) — α7.2 only **produces** outbox rows (no
> dispatcher; the relay is α7.3). See `docs/domain/WORKFLOW_RUN_AGGREGATE.md` and
> `docs/decisions/ADR-0040-workflow-run-orchestration-aggregate.md`.

### 3.3 Project Versions (CR-6)

```
GET   /projects/{id}/versions                        list
GET   /projects/{id}/versions/{version_id}
POST  /projects/{id}/versions                        capture a snapshot (α5d.1)
POST  /projects/{id}/versions/{version_id}/restore   → creates new version pointing at the restored snapshot
GET   /projects/{id}/versions/{version_id}/diff?against={other_version_id}
POST  /projects/{id}/versions/{version_id}/branch    → forks the snapshot into a NEW independent project (α5d.3)
```

> **Shipped in Phase 3 α5d.1 (capture + browse), α5d.2 (restore + diff), and
> α5d.3 (branch).**
> A project version is an
> **immutable content snapshot** of the project plus its ordered scenes —
> distinct from the row-OCC `version` counter that guards live mutations. The
> `project_versions` ledger is append-only (a DB `reject_mutation` trigger
> rejects UPDATE/DELETE). All three endpoints are authenticated
> (`CurrentUserDep`) and run the project ownership gate first (missing / not
> the caller's → uniform `404 NOT_FOUND`). Versions are **addressed by their
> UUID `id`** in the path (keeping the whole API UUID-addressed);
> `version_number` is the user-facing label carried in the body, not the
> routing key.
>
> * **`POST …/versions`** — captures a snapshot under a project-row lock:
>   assigns the next monotonic `version_number` (1, 2, 3 …), links
>   `parent_version_id` to the project's previous current version (a linear
>   lineage chain), advances `projects.current_version_id` (which bumps
>   `projects.version` by exactly 1), and stores the denormalized snapshot.
>   `reason` is server-set to `manual_save` (the body carries **no** fields in
>   α5d.1; `extra="forbid"` → `422`). Returns `201` with the full
>   `ProjectVersionDetail` (metadata + `snapshot`). An empty project (no
>   scenes) is valid — `snapshot.scenes` is `[]`.
> * **`GET …/versions`** — lists the project's version history as
>   **metadata only** (`ProjectVersionPublic`: `id`, `version_number`,
>   `reason`, `parent_version_id`, `created_by_user_id`, `created_at`), newest
>   first by `version_number`. No snapshot bodies (fetch a single version for
>   those). Not paginated in α5d.1.
> * **`GET …/versions/{version_id}`** — one version WITH its immutable
>   `snapshot` (`ProjectVersionDetail`), or the uniform `404` (missing, or the
>   version belongs to another project). The `snapshot` is a self-describing
>   JSON blob carrying `schema_version` + the project's business columns + the
>   default storyboard + the ordered scenes (full "fat" columns, scene `id`
>   preserved for restore round-tripping; `Numeric` durations as
>   lossless strings).
> * **`POST …/versions/{version_id}/restore`** — makes a historical snapshot the
>   project's **live** content again **without rewriting history** (ADR-0035 D2):
>   it appends a new `reason=restore` version parented on the source and repoints
>   `projects.current_version_id` to it. The body carries the **aggregate OCC
>   token** `{ version }` — the `projects.version` the caller last observed.
>   Per the **Aggregate OCC Rule**, `projects.version` guards the whole aggregate,
>   so any scene mutation between the caller's read and the restore invalidates a
>   stale token. Two-level `404` gate (project, then version) runs **before** the
>   fence (anti-enumeration); a stale token → `412 VERSION_CONFLICT` with **zero
>   writes**; a missing `version` or an extra field → `422`. The whole restore
>   (scene reconcile keyed by `id` — surviving scenes kept, removed scenes
>   soft-deleted, added scenes dropped; root rewrite; trailing capture) runs in
>   **one transaction** and produces **exactly one** `projects.version` bump.
>   Returns `200` with the new head as `ProjectVersionDetail`.
> * **`GET …/versions/{version_id}/diff?against={base_version_id}`** — a **coarse**
>   change summary between the `against` base and the `{version_id}` target,
>   computed **on demand** from the two stored snapshots (nothing persisted).
>   Both versions are gated to the caller's owned project (uniform `404` on
>   either side); `against` is required (missing/malformed → `422`). Returns
>   `200` with `ProjectVersionDiff` — `base_version_number`,
>   `target_version_number`, `project_changed` (business columns differ), and
>   `scene_changes` (`added` / `removed` / `modified` counts keyed by scene `id`).
> * **`POST …/versions/{version_id}/branch`** — **forks** a historical snapshot
>   into a **new, independently-editable project** (α5d.3 — "fork to a new
>   project"; ADR-0035 D12). Unlike restore (which rewinds *this* project),
>   branch leaves the source **untouched** and creates a fresh aggregate owned
>   by the caller, seeded from the chosen version's snapshot (root fields +
>   scenes materialized with **freshly-minted** ids). The body carries just
>   `{ name }` (the new project's name; every other root field — including the
>   immutable `aspect_ratio` — is inherited from the snapshot; `extra="forbid"`
>   → `422`). The new project's `v1` is `reason=branch` (`parent_version_id`
>   NULL) and its snapshot embeds a structured **`branched_from`** provenance
>   block (`{ project_id, version_id, version_number }` of the source), also
>   echoed in the response `meta.branched_from`. There is **no OCC fence** (the
>   source is not mutated) and **no source `projects.version` bump**. Two-level
>   `404` gate (source project, then version) runs first (anti-enumeration); a
>   duplicate live project name for the caller → `409 CONFLICT`. The whole fork
>   (new project + storyboard + scenes + `v1` + pointer advance) runs in **one
>   transaction**. Returns `201` with the **new project** as `ProjectPublic`
>   (its `version` is the branched project's OCC handle).
>
> **Deferred to α5d.4+:** autosave, field-level diff detail. See
> `docs/domain/PROJECT_AGGREGATE.md` §6 and
> `docs/decisions/ADR-0035-project-version-snapshots.md`.

### 3.4 Workflows (CR-7) — replaces the old "/generate" endpoint

```
POST  /workflows
  body: { project_id, pipeline_id, inputs }   # inputs validated against PipelineRegistry.inputs_schema
  → 202 { run_id, status: "queued" }
  emits: workflow.started

GET   /workflows/{id}
  → { id, project_id, pipeline_id, status, steps[…], started_at, ended_at, error? }

POST  /workflows/{id}/pause                  → 202
POST  /workflows/{id}/resume                 → 202
POST  /workflows/{id}/cancel                 → 202

WS    /ws/workflows/{id}                     server-sent events: every state transition
```

### 3.5 Pipelines (CR-2) — read-only

```
GET   /pipelines           → [ { id, name, description, inputs_schema, required_caps } ]
```

The frontend uses this to render the picker; pipelines are not hardcoded client-side.

### 3.6 AI Models (CR-11)

```
GET   /ai-models                      ?kind=video&capability=TEXT_TO_VIDEO&status=available
GET   /ai-models/{id}
GET   /ai-models/defaults             → resolved chain for the caller (per-kind)
POST  /admin/ai-models/refresh        admin only — re-runs discovery
PATCH /admin/ai-models/{id}           admin only — override status/tags/default
```

### 3.7 Plugins (CR-1)

```
GET   /plugins                        ?kind=image
       → [ { name, version, kind, capabilities, enabled, health } ]
PATCH /admin/plugins/{name}           admin only — enable/disable
```

### 3.8 Asset Library (CR-8)

```
GET    /library/assets                ?kind=image&q=…&tag=…
GET    /library/assets/{id}
PATCH  /library/assets/{id}           body: { tags?, name? }
DELETE /library/assets/{id}           soft delete
POST   /library/assets/{id}/reuse     body: { project_id, scene_id? }
POST   /library/assets/search/similar body: { vector or asset_id }
```

### 3.9 Usage & Cost (CR-12)

```
GET  /usage                  ?from=…&to=…&group_by=model|provider|user
GET  /usage/{id}
GET  /usage/summary          → totals + breakdown
GET  /projects/{id}/usage    drill-down per project
```

### 3.10 Credits

```
GET   /credits                         → { balance, currency }
GET   /credits/transactions            paginated ledger view (read-only; ledger is append-only)
POST  /credits/purchase                body: { pack_id } → Stripe checkout URL
```

### 3.11 Feature Flags (CR-9)

```
GET   /feature-flags                          flags visible to current caller
GET   /feature-flags/evaluate?key=…           single-flag eval against caller context
PUT   /admin/feature-flags/{key}              admin only — set rules
GET   /admin/feature-flags                    admin only — list all
```

### 3.12 Admin: Queues (CR-13)

```
GET   /admin/queues                            → [ { name, depth, oldest_age_s, workers } ]
GET   /admin/queues/{name}/dlq                 list dead-letter items
POST  /admin/queues/{name}/dlq/{id}/requeue    move back to source queue
```

### 3.13 Webhooks

```
POST /webhooks/stripe                  HMAC-signed by Stripe
POST /webhooks/providers/{name}        HMAC-signed by the provider (Veo, Runway, …)
```

All webhook handlers are **idempotent** keyed by `(provider, request_id)`.

---

## 4. WebSocket Surfaces

| Endpoint | Direction | Payload |
|---|---|---|
| `/ws/progress/{run_id}` | server → client | `{ event: "progress", step, percent, message }` |
| `/ws/workflows/{run_id}` | server → client | every event in topic `workflow.*` for that run |
| `/ws/timeline/{project_id}` | bidirectional | reserved for future CRDT collaboration |

All WS endpoints require a valid access token (passed as `?token=…` or via subprotocol).

---

## 5. Rate Limits (initial)

| Tier | Per-user per minute | Generation calls per minute |
|---|---|---|
| Free | 60 | 5 |
| Pro | 300 | 30 |
| Business | 1200 | 120 |
| Enterprise | custom | custom |

Headers returned: `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset`.

---

## 6. Pagination Example

Request:
```
GET /projects?limit=20&cursor=eyJpZCI6Ii4uLiJ9
```
Response:
```
{
  "data": [ … 20 projects … ],
  "meta": { "request_id": "…", "next_cursor": "eyJpZCI6Ii4uLiJ9" }
}
```

When `next_cursor` is absent, the page is the last one.

---

## 7. OpenAPI Generation Policy

- FastAPI generates `/openapi.json`; this file is exported to `docs/api/openapi.yaml` on every release.
- A schema diff between `docs/api/openapi.yaml` and the runtime spec runs in CI; non-additive diffs fail the build unless a new `/vN` path exists.
- Client SDKs (TypeScript, Python) are generated from the exported spec.

---

## 8. Stability Tiers

| Tier | Path examples | Guarantees |
|---|---|---|
| Stable | `/v1/projects`, `/v1/workflows` | No breaking changes within v1 |
| Beta | `/v1/ai-models`, `/v1/library/assets/search/similar` | May change; `Sunset` header given 30 days in advance |
| Experimental | `/v1/admin/*` | May change without notice; admin-only |

---

## 9. Open Questions (to resolve before Phase 4)

1. Whether to enforce per-tenant data residency (GCS in EU vs S3 in US) via API hints or implicit routing.
2. Whether `Idempotency-Key` is required (vs optional) for all write endpoints.
3. Whether usage records should be exposed as a streaming feed (NDJSON) for enterprise customers.

These will become ADRs once decided.
