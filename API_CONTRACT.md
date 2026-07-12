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
| Prompts | `/projects/{id}/prompts`, `/prompts/{id}` |
| AI — Script | `/ai/script` |
| AI — Storyboard | `/ai/storyboard` |
| AI — Images | `/ai/images` |
| AI — Videos | `/ai/videos` |
| AI — Voice | `/ai/voice` |
| AI — Subtitles | `/ai/subtitles` |
| Timeline | `/projects/{id}/timeline` |
| Render | `/projects/{id}/render` |
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

### 3.3 Project Versions (CR-6)

```
GET   /projects/{id}/versions                        list
GET   /projects/{id}/versions/{version_id}
POST  /projects/{id}/versions                        capture a snapshot (α5d.1)
POST  /projects/{id}/versions/{version_id}/restore   → creates new version pointing at the restored snapshot
GET   /projects/{id}/versions/{version_id}/diff?against={other_version_id}
```

> **Shipped in Phase 3 α5d.1 (capture + browse) and α5d.2 (restore + diff).**
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
>
> **Deferred to α5d.3+:** `…/versions/{version_id}/branch`, autosave. See
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
