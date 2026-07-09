# Authentication & Identity Endpoints

**Status:** ships with Phase 3 α3.3 (v0.4.3-phase3-alpha3-dev)
**Base URL:** `/api/v1`
**Envelope contract:** [API_CONTRACT §1.1](../engineering/API_CONTRACT.md)

This document is the canonical guide to every authenticated surface
the backend currently exposes. It covers the four `/auth/*` endpoints
(register, login, refresh, logout) and the first authenticated
business endpoint, `GET /users/me`. It also describes the
`CurrentUserDep` pattern that every future authenticated endpoint must
follow.

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
    "created_at": "2026-07-09T22:15:04.512Z"
  },
  "meta": { "request_id": "..." }
}
```

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

## 9. Future Authenticated Endpoints

Not yet shipped — reserved surface for upcoming slices:

* `PATCH /api/v1/users/me` — update `display_name`, `email` (with
  re-verification flow).
* `GET /api/v1/users/{id}` — cross-user reads (requires RBAC first).
* `GET /api/v1/projects`, `POST /api/v1/projects` — first business
  domain endpoints (Phase 3 α4+).

Every one of these will use `CurrentUserDep` and follow the envelope
contract described above.
