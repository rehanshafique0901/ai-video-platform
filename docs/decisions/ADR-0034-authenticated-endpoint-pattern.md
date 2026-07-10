# ADR-0034 — Authenticated Endpoint Pattern (`CurrentUserDep` + Optimistic-Concurrency Mutation)

**Status:** Proposed (documents patterns already shipped in Phase 3 α3 — PR #13/#14, merge `02185cf` — and α4 — PR #15, merge `1462307`; flips to Accepted on merge of this ADR PR).
**Refines / documents:** `docs/api/AUTH_ENDPOINTS.md` §8 (The `CurrentUserDep` Pattern) and §9 (Canonical Authenticated Mutation Flow); `CHANGELOG.md` α3 + α4 entries. Builds on the auth foundation from α2a (register/login), α2b (refresh/logout), and α1 (DI container + JWT + Unit of Work).
**Wave:** Phase 3, authenticated-surface slices (α3 read path, α4 write path).

---

## Context

Phase 3 introduced authenticated HTTP endpoints on top of the α1–α2b auth
foundation. Two cross-cutting decisions were made *in code* during α3 and
α4 but were never recorded in the decisions log, so the rationale is
currently discoverable only by reading the endpoint docs and the
implementation:

1. **How does an endpoint learn "who is the caller?"** Every authenticated
   route needs the bearer access token verified, the session checked for
   liveness, and the `User` hydrated — and it must fail *identically* for
   every rejection reason (anti-enumeration). Left to each endpoint, this
   would be copy-pasted JWT parsing and repository access scattered across
   routers, drifting over time and leaking the auth mechanism into the
   API layer (an architecture-boundary violation).

2. **How does an authenticated *mutation* stay safe under concurrency?**
   A signed-in user (or two tabs, or a retried request) updating their own
   record can race. Last-writer-wins silently discards a concurrent
   change; a read-modify-write in application code has a TOCTOU window.
   The `users.version` column and its `bump_version` / `touch_updated_at`
   triggers existed from α1, but there was no recorded decision on *how*
   endpoints must use them, nor why a client-supplied `version` is
   **required** on a write.

Without an ADR, a contributor six months from now sees `version` as a
required field on `PATCH /users/me` and a `CurrentUserDep` alias
everywhere, but not *why* — and is liable to "simplify" by dropping the
version fence or hand-rolling a second authentication path. This ADR
promotes both patterns from "implemented convention" to "recorded
decision."

---

## Decision

Adopt two coupled, project-wide patterns for every authenticated endpoint.

### D1 — Authentication seam: `CurrentUserDep`

Authentication is resolved by exactly **one** FastAPI dependency,
`get_current_user` (`app/api/v1/deps.py`), exposed as the type alias
`CurrentUserDep = Annotated[User, Depends(get_current_user)]`.

- The dependency verifies the bearer access token (`allow_expired=False`),
  performs a sid-driven session-liveness check, and does a
  soft-delete-aware user lookup, returning a hydrated domain `User`.
- Every failure mode returns an **anti-enumeration 401** with a single
  generic client message; the specific reason is emitted only in the
  server-side structured log (`auth.request.rejected`, with a
  `security_event` flag on tamper-flavoured reasons).
- **Endpoints MUST NOT** parse JWTs, call `ITokenIssuer`, or touch
  `ISessionRepository` for authentication. Consuming `CurrentUserDep` is
  the only sanctioned authentication path. `GET /users/me` is the
  canonical read reference.

### D2 — Authenticated mutation: optimistic-concurrency version fence + CAS

Every authenticated write follows the canonical flow (documented in
`AUTH_ENDPOINTS.md` §9):

> `CurrentUserDep` → DTO validation (`extra="forbid"`) →
> optimistic-concurrency check → domain mutation (use case) → versioned
> repository compare-and-swap → updated representation in the
> API_CONTRACT §1.1 envelope.

- The client **must** supply its last-observed `version`; there is no
  "server uses latest" fallback (omitting it is a 422). This is the
  optimistic-concurrency fence.
- The repository performs the update as a single SQL compare-and-swap
  (`UPDATE … WHERE id = ? AND version = ? AND deleted_at IS NULL …
  RETURNING`), so a stale fence is detected **atomically at the DB layer**
  — no TOCTOU window.
- A stale `version` **or** a soft-deleted row both return `None` from the
  repository, surfacing as `412 VERSION_CONFLICT` with an identical
  message (`"Resource has been modified."`) — the two collapse by design
  (anti-enumeration).
- A same-value write short-circuits *before* any `UPDATE`: no row write,
  no `version` bump, no `updated_at` bump. It returns `200` with the
  current representation.
- **Version-increment invariant (project-wide):** `version` moves **only
  when a persisted field actually changes** — never on authentication,
  reads, identical PATCHes, or failed mutations.
- Successful mutations return `200` with the updated representation
  (never `204`) so the client obtains the new `version` without a
  follow-up `GET`. Audit logs record changed field **names** only, never
  submitted values.

`PATCH /users/me` is the canonical mutation reference.

---

## Alternatives Considered

1. **Per-endpoint inline authentication (parse the JWT in each handler).**
   *Rejected* — copy-pasted verification drifts, leaks the auth mechanism
   into the API layer (violating the `app.api` → not-`app.infrastructure`
   import boundary), and makes anti-enumeration uniformity impossible to
   guarantee. A single dependency is the only place the failure shape is
   authored once.

2. **Middleware-based authentication (populate `request.state.user`).**
   *Rejected* for the primary seam — middleware runs for every route
   (including public ones) and pushes per-route opt-in/opt-out into
   configuration rather than the handler signature. A FastAPI dependency
   is explicit at the call site, is trivially testable in isolation, and
   composes with future `require_role(...)` dependencies. (Middleware
   remains appropriate for cross-cutting concerns like request-id and
   structured-log binding, which is where the project already uses it.)

3. **Last-writer-wins on mutations (no version fence).** *Rejected* —
   silently discards concurrent edits; a user with two tabs open loses
   data with no signal. Unacceptable for user-owned records and sets a
   dangerous precedent for the higher-value aggregates (projects, render
   jobs, timelines) that later slices will mutate.

4. **Server-side read-modify-write (fetch row, mutate in Python, save).**
   *Rejected* — has a TOCTOU window between the read and the write that a
   concurrent request can slip through. The DB-level CAS
   (`WHERE … AND version = ?`) closes the window atomically; the database
   is the single arbiter of "did the row change under me?"

5. **Server-latest version (client omits `version`; server reads current
   and bumps).** *Rejected* — defeats the entire point of optimistic
   concurrency: it makes every write unconditionally succeed, so a client
   editing a stale representation clobbers a newer one. Requiring the
   client's observed `version` is what makes the conflict detectable.

6. **ETag / `If-Match` HTTP-header concurrency instead of a body field.**
   A valid REST idiom (weak/strong ETags + `412 Precondition Failed`).
   *Deferred, not rejected on merit* — the body-field `version` is
   simpler for the current JSON-envelope API, keeps the fence visible in
   the same DTO that `extra="forbid"` already guards, and maps cleanly to
   the integer `users.version` column. Revisit if/when a broad HTTP-cache
   story lands; the `412` status was deliberately chosen to remain
   compatible with an ETag migration.

7. **Distinguish `412` (stale version) from `404` (soft-deleted).**
   *Rejected* — returning `404` for a soft-deleted user leaks whether an
   account exists/was closed, an enumeration vector. Collapsing both into
   `412 VERSION_CONFLICT` with an identical message keeps the surface
   information-free, consistent with the anti-enumeration stance of the
   α2/α3 auth work.

---

## Consequences

- **Positive — one authentication seam.** Authentication logic lives in a
  single, unit-tested dependency. Every authenticated endpoint is a
  one-liner (`current_user: CurrentUserDep`); the auth mechanism never
  leaks into routers, preserving the `app.api` architecture boundary.
- **Positive — race-free user-owned writes.** Optimistic concurrency with
  a DB-level CAS makes concurrent-edit conflicts explicit (`412`) instead
  of silent data loss, with no TOCTOU window.
- **Positive — recorded rationale.** The "why `version` is required" and
  "why there is only one auth path" questions now have a durable answer,
  reducing the risk of a future contributor removing the fence or adding a
  second authentication path.
- **Contract — clients must round-trip `version`.** Any client of an
  authenticated mutation must read `version` from a prior `UserPublic` and
  send it back. This is why `UserPublic` carries `version` on every
  response (register/login/refresh/GET /me/PATCH /me). Documented in
  `AUTH_ENDPOINTS.md` §7–§9.
- **Contract — mutations return the representation, not `204`.** Clients
  never need a follow-up `GET` to learn the new `version`.
- **Enforcement is review-time today.** The "no endpoint imports
  `ITokenIssuer` / `ISessionRepository` for auth" and "every authenticated
  endpoint uses `CurrentUserDep`" rules are currently enforced in code
  review. A future `import-linter` contract can promote these to CI-time
  guarantees (see Future Extensions).
- **Operational.** The CAS is a single indexed `UPDATE … WHERE id = ? AND
  version = ?`; negligible cost. The same-value short-circuit avoids
  needless `updated_at`/`version` churn on no-op saves.

---

## Pattern Reference (Examples)

- **Read (canonical):** `GET /api/v1/users/me` — `app/api/v1/routers/users.py`.
- **Write (canonical):** `PATCH /api/v1/users/me` — `app/api/v1/routers/users.py`,
  `app/application/use_cases/users/update_profile.py`,
  `IUserRepository.update_profile` in `app/application/interfaces/repositories.py`,
  and the CAS impl in `app/infrastructure/repositories/user_repository.py`.
- **Auth seam:** `get_current_user` / `CurrentUserDep` — `app/api/v1/deps.py`.
- **Conflict error:** `VersionConflictError` (`code="VERSION_CONFLICT"`, HTTP 412) — `app/core/errors.py`.
- **Prose docs:** `docs/api/AUTH_ENDPOINTS.md` §7.1, §8, §9.

New authenticated endpoints copy these shapes rather than reinventing
them.

---

## Future Extensions

- **`require_role(...)` dependency** layered on top of `CurrentUserDep` for
  authorization (roles/scopes) — lands with the roles/RBAC slice.
- **Tenant-scoping dependency** enforcing "this request may only see
  resources in the caller's tenant" — first exercised by the
  project/content endpoints.
- **`import-linter` contract** promoting the "endpoints don't import
  `ITokenIssuer` / `ISessionRepository` for auth" rule from review-time to
  CI-time enforcement.
- **Generalised version fence** — the α4 CAS pattern is written to be the
  template for future versioned-aggregate writes (projects, render jobs,
  timelines). Those aggregates reuse D2 verbatim.
- **ETag / `If-Match`** — reconsider the header-based concurrency idiom if
  a broad HTTP-cache story lands (the `412` status keeps this migration
  open).
