# Authentication & Identity Endpoints

**Status:** ships with Phase 3 α4 (v0.4.4-phase3-alpha4-dev)
**Base URL:** `/api/v1`
**Envelope contract:** [API_CONTRACT §1.1](../engineering/API_CONTRACT.md)

This document is the canonical guide to every authenticated surface
the backend currently exposes. It covers the four `/auth/*` endpoints
(register, login, refresh, logout) and the `/users/me` endpoints —
`GET /users/me` (the first authenticated read, α3.3) and
`PATCH /users/me` (the first authenticated mutation, α4). It also
describes the `CurrentUserDep` pattern every authenticated endpoint
follows (§8) and the **canonical authenticated mutation flow** every
write endpoint follows (§9).

---

## 1. Authentication Overview

The backend uses **JWT bearer tokens** with a **rotating refresh
family** design (see [`AUTH_TOKEN_LIFECYCLE.md`](../engineering/AUTH_TOKEN_LIFECYCLE.md)
for the full lifecycle).

* Access tokens are short-lived (15 min default). They carry `sid`
  (session id) and `fam` (family id) claims alongside `sub` (user id).
* Refresh tokens are long-lived (30 days default) and single-use.
  Using a refresh token rotates it — the previous string is
  invalidated.
* Sessions are server-side rows. Revocation (logout) or reuse
  detection sets `revoked_at`, and every subsequent authenticated
  request checks liveness before serving.

All error responses follow the standard error envelope
(`{ "error": { "code": ..., "message": ... }, "meta": { ... } }`).
The 401 message is deliberately generic on every failure path — see
§7 "Anti-enumeration" below.

---

## 2. `POST /api/v1/auth/register`

Create a new tenant + owner user and mint the first token pair.

**Request body**

```json
{
  "email": "user@example.com",
  "password": "correct horse battery staple",
  "name": "Display Name"
}
```

* `email` — normalised to lowercase before insertion (`CITEXT` column).
* `password` — 8–128 characters. Hashed with Argon2id server-side.
* `name` — 1–200 characters. Not required to be unique.

**Response** — 201 Created

```json
{
  "data": {
    "user": { /* UserPublic */ },
    "access_token": "eyJ...",
    "refresh_token": "eyJ...",
    "token_type": "Bearer"
  },
  "meta": { "request_id": "..." }
}
```

**Errors**

| Status | `error.code` | When |
|--------|--------------|------|
| 409 | `CONFLICT` | Email already registered |
| 422 | `VALIDATION_FAILED` | Missing/short password, malformed email |

---

## 3. `POST /api/v1/auth/login`

Verify credentials and issue a **new session family** (parallel
devices get independent families).

**Request body**

```json
{ "email": "user@example.com", "password": "..." }
```

**Response** — 200 OK — same payload shape as `/register`.

**Errors**

| Status | `error.code` | When |
|--------|--------------|------|
| 401 | `UNAUTHENTICATED` | Wrong password **or** unknown email — same message (anti-enumeration) |

---

## 4. `POST /api/v1/auth/refresh`

Rotate a refresh token: the old refresh becomes invalid, a new
access+refresh pair is issued. The session family is **preserved**;
the individual session id (`sid`) rotates.

**Request body**

```json
{ "refresh_token": "eyJ..." }
```

**Response** — 200 OK — same payload shape as `/register`.

**Errors**

| Status | `error.code` | When |
|--------|--------------|------|
| 401 | `UNAUTHENTICATED` | Invalid signature, expired, wrong `kind`, or **reuse detected** (see below) |

**Reuse detection:** replaying an already-rotated refresh token
revokes the **entire family**. Every session that shares the family
id is marked revoked; a stolen refresh cannot outlive detection.

---

## 5. `POST /api/v1/auth/logout`

Revoke the caller's current session. Idempotent.

**Request headers**

```
Authorization: Bearer <access_token>
```

**Response** — 204 No Content, empty body.

**Errors**

| Status | `error.code` | When |
|--------|--------------|------|
| 401 | `UNAUTHENTICATED` | Missing/malformed header, non-access token, invalid signature |

Note: logout accepts **expired** access tokens (deliberate exception,
pre-flight §2.D5). Every other authenticated endpoint rejects them.

---

## 6. Authentication Flow (visual)

```
┌──────────────────────┐
│ POST /auth/register  │ ──► access_token (15m) + refresh_token (30d)
│  or /auth/login      │
└──────────────────────┘
                          │
                          │  Authorization: Bearer <access_token>
                          ▼
        ┌───────────────────────────────────┐
        │ GET /api/v1/users/me              │
        │ (any future authenticated route)  │
        └───────────────────────────────────┘
                          │
                          │  access token expires (401)
                          ▼
                ┌────────────────────┐
                │ POST /auth/refresh │ ──► rotated access + refresh
                └────────────────────┘
                          │
                          │  user logs out
                          ▼
                ┌────────────────────┐
                │ POST /auth/logout  │ ──► session revoked (204)
                └────────────────────┘
```

---

## 7. `GET /api/v1/users/me`

Return the authenticated caller's own public profile. **First
authenticated business endpoint** — establishes the pattern every
future authenticated endpoint follows.

**Request headers**

```
Authorization: Bearer <access_token>
```

**Response** — 200 OK

```json
{
  "data": {
    "id": "9f8a...",
    "tenant_id": "1b2c...",
    "email": "user@example.com",
    "display_name": "Display Name",
    "email_verified_at": null,
    "created_at": "2026-07-09T22:15:04.512Z",
    "updated_at": "2026-07-09T22:15:04.512Z",
    "version": 1
  },
  "meta": { "request_id": "..." }
}
```

The `UserPublic` projection carries `version` and `updated_at` as of
α4. `version` is the optimistic-concurrency fence a client sends back
with `PATCH /users/me` (§7.1); `updated_at` supports "last modified"
UX. Both are additive — they appear in every response returning a
`UserPublic` (register, login, refresh, `GET /me`, `PATCH /me`).
`password_hash` and `last_login_at` remain internal and never appear
in the projection.

**Errors** — 401 `UNAUTHENTICATED` on every failure path, with an
identical generic message. The server-side structured log
(`auth.request.rejected`) records the specific reason:

| Server-side `reason` | Trigger |
|---|---|
| `missing_header` | No `Authorization` header |
| `malformed_header` | Wrong scheme, or empty token after `Bearer ` |
| `verify_failed` | Bad signature / wrong `kind` / expired |
| `sid_missing_session` | Valid JWT, but `sid` points to no session |
| `session_revoked` | Session row was revoked (logout, reuse-detection sweep) |
| `session_expired` | Session TTL exceeded |
| `sid_user_gone` | User row deleted between token issuance and request |

Signature-failure and forged-`sid` branches additionally carry
`security_event: true` — SIEM/alerting hook.

---

## 7.1 `PATCH /api/v1/users/me`

Update the authenticated caller's own profile. **First authenticated
mutation endpoint** — the reference implementation of the canonical
mutation flow (§9). In α4 the only mutable field is `display_name`.

**Request headers**

```
Authorization: Bearer <access_token>
```

**Request body**

```json
{ "display_name": "New Name", "version": 3 }
```

* `display_name` — **required**, 1–200 chars, whitespace-stripped.
  Explicit `null` is rejected (422); "clearing" the name is not a
  supported operation in α4.
* `version` — **required**, integer ≥ 1. The client's last-observed
  `version` (from any prior `UserPublic`). Used as an optimistic-
  concurrency fence: the update only applies if the DB row still has
  this version. There is no "server uses latest" fallback — omitting
  `version` is a 422.
* Any other key (`email`, `password`, `id`, `tenant_id`, `created_at`,
  …) is rejected with 422 (`extra="forbid"`).

**Response** — 200 OK, the updated `UserPublic` in the standard
envelope (never `204` — the client needs the new `version` without a
follow-up `GET`):

```json
{
  "data": {
    "id": "9f8a...",
    "tenant_id": "1b2c...",
    "email": "user@example.com",
    "display_name": "New Name",
    "email_verified_at": null,
    "created_at": "2026-07-09T22:15:04.512Z",
    "updated_at": "2026-07-10T16:40:11.882Z",
    "version": 4
  },
  "meta": { "request_id": "..." }
}
```

**Same-value no-op.** A `PATCH` whose `display_name` equals the current
value returns 200 with the current representation, and `version` is
**not** incremented (nor is `updated_at` bumped). A client that saves
an unchanged form observes no version churn. The distinction is
recorded server-side only (`user.profile.update_rejected`,
`reason=same_value_noop`); the wire response is indistinguishable from
a real change of the same shape.

**Version-increment invariant.** `version` increments **only when a
persisted field actually changes**. It never moves on authentication,
reads, identical PATCHes, or failed mutations. Future write endpoints
inherit this invariant.

**Errors**

| Status | `error.code` | When |
|--------|--------------|------|
| 401 | `UNAUTHENTICATED` | Any α3 auth-rejection branch (same generic message + `reason` log as §7) |
| 412 | `VERSION_CONFLICT` | Stale `version`, **or** the user was soft-deleted mid-request — indistinguishable by design (anti-enumeration). Message: `"Resource has been modified."` No mutation occurs. |
| 422 | `VALIDATION_FAILED` | Missing/empty/`null`/over-long `display_name`, missing/`< 1` `version`, or any non-whitelisted key |

The 412 body:

```json
{
  "error": { "code": "VERSION_CONFLICT", "message": "Resource has been modified." },
  "meta": { "request_id": "..." }
}
```

**Structured-log catalogue (α4)**

| Event | Level | Fields |
|---|---|---|
| `user.profile.updated` | INFO | `user_id`, `changed_fields` (field names only — never values), `previous_version`, `new_version`, `ip` |
| `user.profile.update_rejected` | WARN (`version_mismatch`) / INFO (`same_value_noop`) | `user_id`, `reason`, `expected_version`, `current_version` (when known), `ip` |

Per-value audit (who set what to which string) is deferred to a future
DB audit-log table — application logs carry field **names** only.

---

## 8. The `CurrentUserDep` Pattern

Every future authenticated endpoint **MUST** use this pattern and
**MUST NOT** parse JWTs, call `ITokenIssuer`, or touch a repository
directly for authentication.

**Minimal example:**

```python
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.api.v1.deps import CurrentUserDep

router = APIRouter(prefix="/widgets", tags=["widgets"])


@router.get("/mine")
async def list_my_widgets(
    request: Request,
    current_user: CurrentUserDep,
) -> JSONResponse:
    # current_user is a domain User; DI already validated the request.
    ...
```

`CurrentUserDep` resolves to `Annotated[User, Depends(get_current_user)]`.
By the time the handler runs, the token has been verified, the
session has been checked for liveness, and the user has been
hydrated. No further authentication work is warranted.

### α3 Exit Criteria (enforced going forward)

* Every authenticated endpoint uses `CurrentUserDep`.
* No endpoint imports `ITokenIssuer` / `IJWTService`.
* No endpoint imports `ISessionRepository` for authentication.
* `GET /users/me` is the canonical reference implementation.

Violations should be caught in code review; long-term, an
`import-linter` contract may be added to enforce this at CI time.

---

## 9. Canonical Authenticated Mutation Flow

`PATCH /users/me` (§7.1) is the reference implementation. **Every
future authenticated write endpoint follows the same six steps** — copy
this shape rather than inventing a new one:

```
1. Authenticate        →  CurrentUserDep (§8). No JWT parsing in the
                          handler; the domain User is already hydrated.
2. Validate the body   →  A Pydantic request DTO with
                          model_config = ConfigDict(extra="forbid").
                          Field constraints (lengths, ge=1, non-null)
                          produce the 422 surface before the handler
                          body runs.
3. Check concurrency   →  A required `version` field carries the
                          client's last-observed version. The fence is
                          enforced at the repository via SQL compare-
                          and-swap (WHERE id = ? AND version = ?),
                          never by a read-then-write in Python (TOCTOU).
4. Mutate in a use case → Business logic lives in
                          app/application/use_cases/<domain>/, not in
                          the router and not in a dependency. The use
                          case emits the structured audit log.
5. Persist via CAS      → A targeted repository method
                          (update_<thing>) does the versioned UPDATE …
                          RETURNING. Zero rows updated → the use case
                          raises VersionConflictError → 412
                          VERSION_CONFLICT. A generic update-by-entity
                          is deliberately NOT offered — targeted methods
                          keep "what can this endpoint change?" explicit.
6. Return the resource  → 200 OK with the updated representation in the
                          API_CONTRACT §1.1 envelope, including the new
                          `version`. Never 204 — the client needs the
                          fence for its next write.
```

**Invariants this flow guarantees**

* **Version increments only on a real persisted change** (§7.1) —
  never on auth, reads, identical PATCHes, or failed mutations.
* **Anti-enumeration on the failure branch** — "stale version" and
  "resource gone / soft-deleted" collapse to the same 412 so a caller
  cannot probe resource existence through the mutation surface.
* **No PII in logs** — audit events carry changed field *names*, never
  the submitted values.

Mapping to the code (α4):

| Step | Artifact |
|---|---|
| 1 | `app/api/v1/deps.py::CurrentUserDep` |
| 2 | `app/api/v1/schemas/users.py::UpdateUserProfileRequest` |
| 3–4 | `app/application/use_cases/users/update_profile.py::UpdateUserProfile` |
| 5 | `app/infrastructure/repositories/user_repository.py::UserRepository.update_profile` |
| 6 | `app/api/v1/routers/users.py::update_me` + `_to_public` |

---

## 10. Future Authenticated Endpoints

Not yet shipped — reserved surface for upcoming slices:

* `POST /api/v1/auth/change-password` — current-password re-auth; its
  own endpoint, not folded into `PATCH /users/me` (security boundary).
* Email-change flow — out-of-band verification round-trip (send token
  to old + new address); a slice of its own.
* `GET /api/v1/users/{id}` — cross-user reads (requires RBAC first).
* `GET /api/v1/projects`, `POST /api/v1/projects` — first business
  domain endpoints.

Every one of these will use `CurrentUserDep`; every write among them
will follow the canonical mutation flow (§9).
