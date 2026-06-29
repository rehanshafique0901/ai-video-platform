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
    "code": "PROJECT_NOT_FOUND",
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
| Scenes | `/projects/{id}/scenes`, `/scenes/{id}` |
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

### 3.3 Project Versions (CR-6)

```
GET   /projects/{id}/versions                        list
GET   /projects/{id}/versions/{version_id}
POST  /projects/{id}/versions                        body: { reason: "manual_save", note?: string }
POST  /projects/{id}/versions/{version_id}/restore   → creates new version pointing at the restored snapshot
GET   /projects/{id}/versions/{version_id}/diff?against={other_version_id}
```

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
