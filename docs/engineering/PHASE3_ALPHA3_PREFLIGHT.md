# Phase 3 Slice α3 — Read-Only Pre-Flight

> **Convention.** This document is a read-only planning artefact per
> `docs/engineering/RUNBOOK_WAVE.md` §1. It records scope, non-goals,
> design decisions, file inventory, acceptance criteria, test matrix,
> and CI impact for **Slice α3** before any branch is cut or any code
> is written. Approve as-is (or push back) before implementation
> begins.
>
> **Status.** ✅ Approved 2026-07-10 with four refinements applied
> in-doc (Reviewer sign-off recorded in §9). Ready for branch cut
> once §10 Exit Criteria are accepted alongside the rest.
>
> **Predecessors.** α1 (`v0.4.0-phase3-alpha1`, PR #8), α2a
> (`v0.4.1-phase3-alpha2a`, PR #9, merge `281471d`), α2b
> (`v0.4.2-phase3-alpha2b`, PR #10, merge `0d5af9c`). Retrospective:
> `docs/engineering/PHASE3_AUTH_RETROSPECTIVE.md`.
>
> **Companion runbook update (docs-adjacent to this slice).**
> `RUNBOOK_WAVE.md` §7.5 codifying **"no file-sync-hosted repositories"**
> — the OneDrive → `C:\dev\ai-video-platform` migration on
> 2026-07-09 makes this a permanent workflow rule; fold it into the
> first α3 docs-adjacent commit. Recorded here so it does not slip.
>
> **Deferred until end of α3.** The missing `v0.4.0-phase3-alpha1`
> annotated tag (α1 shipped without one) — revisit at α3 close, not
> now. Left in the retrospective's "debts carried" list.

---

## Section 1 — Scope

### 1.1 One-line thesis

α3 delivers the **authenticated-request seam**: a `get_current_user`
FastAPI dependency that resolves a bearer access token into a live
`User` domain entity, plus the first HTTP endpoint that uses it —
`GET /api/v1/users/me` — as end-to-end proof that the seam is
correctly wired through the container, exception handlers, and
middleware stack.

### 1.2 What's in

1. **`get_current_user` dependency** (`app/api/v1/deps.py`) resolving
   `Authorization: Bearer <access>` → `User`. Strict verification
   (`allow_expired=False`), sid-driven session-liveness check,
   soft-delete-aware user lookup. Emits anti-enumeration 401 with a
   single generic message for every failure mode; server-side
   structured log carries the specific reason.
2. **`CurrentUserDep` type alias** for consumer readability.
3. **`GET /api/v1/users/me`** endpoint returning `UserPublic`
   (already defined in `app/api/v1/schemas/auth.py` — re-exported
   from a new `app/api/v1/schemas/users.py` module to avoid the
   users router importing an auth-named schema module).
4. **`app/api/v1/routers/users.py`** — new router registered under
   `/api/v1` prefix in `app.main`.
5. **Structured-log event catalogue** for the new dep:
   `auth.request.authenticated` (info, happy path);
   `auth.request.rejected` (warn, `reason=` field, `security_event`
   flag on the tamper-flavoured reasons).
6. **`RUNBOOK_WAVE.md` §7.5** — "no file-sync-hosted repositories"
   (folded into the same PR; see §3.6 below).
7. **`AUTH_TOKEN_LIFECYCLE.md` §3.5** appendix documenting the
   authenticated-request path (`bearer → verify_access →
   session-liveness → user-liveness → User`), consistent with the
   §3.1–§3.4 diagrams already in that document.
8. **`ROADMAP.md`** Phase-3 row update: α3 status line.
9. **`CHANGELOG.md`** `[Unreleased]` block: Added / Changed /
   Validated / Not modified / Scope discipline, following the α2b
   template.

### 1.3 Non-goals (explicit, will NOT ship)

* **Authorization / roles.** `get_current_user` returns `User` but
  does NOT check role claims or scopes. Any endpoint added in α3+
  that needs role gating uses a separate `require_role(...)`
  dependency landing in a later slice (candidate α4).
* **Rate limiting.** Deferred to its own slice. `/users/me` will be
  callable at the same rate as `/auth/refresh`; the anti-enumeration
  constant-time budget from α2b still applies at the token-verify
  layer.
* **Tenant scoping middleware.** `User.tenant_id` is present on the
  returned entity but no dependency yet enforces "this request may
  only see resources in the caller's tenant". First cross-tenant
  test surface lands with the projects endpoints (α4+).
* **Email-verify / password-reset endpoints** listed in
  API_CONTRACT §3.1 (`/auth/email/verify`, etc.). Out of scope —
  those are an α4-or-later slice.
* **OAuth / social login.** Out of scope (α5 territory).
* **`GET /users/{id}` (admin)** endpoint mentioned in API_CONTRACT
  §2. Requires the admin-role dependency; deferred with the roles
  slice.
* **New Alembic migration.** α3 is application-layer only. Zero
  DDL. Stages 5–9 of the CI gate remain no-op deltas.
* **New repository or use case.** α3 reuses `IUserRepository.get_by_id`
  and `ISessionRepository.get_by_hash` — wait, we do not have hash
  at the access-token side. See §2.D3 below: sid lookup is a new
  read method on `ISessionRepository` (`get_by_id`). This is the
  ONE port-surface addition α3 introduces; kept deliberately
  minimal.

---

## Section 2 — Design Decisions

Numbered so review can push back on any single decision without
re-opening the whole document.

### D1. `get_current_user` lives in `deps.py`, not in a use case

**Decision.** The dependency implementation is a plain async function
in `app/api/v1/deps.py`. No `AuthenticateRequest` use-case class.

**Reasoning.**
* It is a pure read composition of three ports
  (`ITokenIssuer.verify_access`, `ISessionRepository.get_by_id`,
  `IUserRepository.get_by_id`). No orchestration state, no
  transactional writes, no branching business rules beyond fail-closed
  checks.
* α2a's `_bearer_access_token` is already a dep-layer function that
  does header parsing; extending the same pattern one layer deeper
  (verify + lookup) keeps the API surface consistent.
* Import-linter contracts still hold: `deps.py` may import from
  `application.interfaces` and `core.container`; it does not touch
  `infrastructure` directly.
* If a later slice needs `AuthenticateRequest` as a testable unit
  (e.g., for a WebSocket sub-protocol authenticator that shares
  the same rules), the promotion is a trivial refactor at that
  time — YAGNI applies now.

**Alternatives considered.**
* *AuthenticateRequest use case* — adds a class + fake + unit-test
  file with zero behavioural benefit for α3. Rejected on
  minimality grounds. Recorded here so the pattern is re-visited
  the day a second consumer appears.

### D2. `allow_expired=False` is the default; logout stays the only exception

**Decision.** `get_current_user` calls
`token_issuer.verify_access(token)` with `allow_expired` at its
default (`False`). Expired access tokens receive 401.

**Reasoning.** Anything short of "strict expiry" on authenticated
business endpoints would defeat the whole point of short-lived
access tokens. `LogoutSession` is the deliberate exception
documented in `AUTH_TOKEN_LIFECYCLE.md` §Logout and does not
generalise.

### D3. Session-liveness check uses **`ISessionRepository.get_by_id`** (new method)

**Decision.** Extend the port with one new read method:

```python
async def get_by_id(self, session_id: UUID) -> Session | None: ...
```

Contract: returns the row (revoked or not) matching `session_id`,
else `None`. Callers inspect `revoked_at` and `expires_at`
themselves — mirrors the α2b `get_by_hash` contract shape.

**Reasoning.**
* The access-token flow has the `sid` claim in-hand but never
  computes `sha256(refresh_jwt)` — using `get_by_hash` here would
  be architecturally wrong (it is a refresh-side method by name
  and by column).
* `get_by_id` is the smallest possible port addition that gives
  `get_current_user` its liveness signal.
* Fake implementation is one line
  (`return self._rows.get(session_id)`), integration coverage is
  cheap (one repository integration test).

**Alternatives considered.**
* *Rename `get_by_hash` to `get_by_hash_or_id`* — schema-mixing;
  rejected.
* *Do without session-liveness at α3* — would mean a logged-out
  user with a still-in-window access token retains access to
  `/users/me` until token expiry. Would silently negate the α2b
  logout guarantee. Rejected.

**Liveness rule.** A session is treated as LIVE iff
`row.revoked_at IS NULL` AND `row.expires_at > clock.now()`.
Both branches produce the same generic 401 client-side; server
log carries `reason=session_revoked` vs `reason=session_expired`
(the latter is possible if the access token is unexpired but the
refresh window has closed — narrow edge, still fail-closed).

### D4. Anti-enumeration — one message, differentiated logs

**Decision.** Every 401 raised by `get_current_user` uses the exact
same client-facing envelope:

```json
{"error": {"code": "UNAUTHENTICATED",
           "message": "not authenticated",
           "details": {}, "request_id": "..."}}
```

Server-side `auth.request.rejected` log carries the specific
`reason`: `missing_header` | `malformed_header` | `verify_failed` |
`sid_missing_session` | `session_revoked` | `session_expired` |
`sid_user_gone`. `security_event=True` on `sid_missing_session`
and `verify_failed` only when the underlying JWT decode says
`invalid signature` (not on `expired`) — same discipline as α2b.

**Reasoning.** Matches the α2a/α2b anti-enumeration posture verbatim.
Every prior auth-layer failure surface uses the same pattern; α3
does not deviate.

### D5. `UserPublic` moves to `app/api/v1/schemas/users.py`

**Decision.** Create `schemas/users.py`; re-export
`UserPublic` from there. `schemas/auth.py` continues to import it
from the new location (no duplication) so the auth response DTOs
are unchanged on the wire.

**Reasoning.** The users router should not import from the auth
schemas module by name — it would read as an implicit
architectural claim ("users depend on auth for schema"). Moving
the shared DTO into its natural home makes the router's imports
self-explanatory and pre-empts a rename during α4 when
`/users/{id}` lands.

**Wire impact.** None. The DTO fields are identical; only its
Python module path changes. Integration tests continue to
`json()["data"]["user"]` unchanged.

### D6. `/users/me` response envelope

**Decision.** Success response uses the standard API_CONTRACT §1.1
envelope:

```json
{"data": {"user": <UserPublic>},
 "meta": {"request_id": "..."}}
```

**Reasoning.** Consistent with `/auth/register` and `/auth/login`.
Rejects a bare-`UserPublic` variant so response shapes stay
uniform for future `/users/{id}` and admin surfaces.

### D7. Container additions

**Decision.** No new container singleton. The dependency injects
one already-composed `ITokenIssuer` singleton (shared with auth
use cases) plus a fresh `IUnitOfWork` per request (reads only —
no `commit`). Sessions and users are read through the UoW's
existing repository accessors.

Rationale: identical to how `RefreshSession` already reads the
session row today. Zero change to `container.py` beyond a small
factory `get_current_user_dep_deps()` **only if** we prefer to
keep the dep signature parameter-free — see §2.D8.

### D8. Dep signature and testability

**Decision.** `get_current_user` receives its dependencies through
FastAPI `Depends(...)` sub-dependencies:

```python
async def get_current_user(
    token: BearerAccessTokenDep,
    token_issuer: TokenIssuerDep,
    uow: UoWDep,
) -> User: ...
```

with `TokenIssuerDep = Annotated[ITokenIssuer, Depends(container.get_token_issuer)]`
added to `deps.py`. Enables integration tests to override at the
FastAPI dep layer (parity with α2a/α2b tests). No fakes needed at
the unit-test layer — the dep is tested via an isolated
`AsyncClient` in integration.

**Reasoning.** Mirrors `RegisterUserDep` / `LoginUserDep` shape.
Any future consumer (e.g., WebSocket authenticator) can call the
underlying primitives directly.

### D9. `verify_access` still emits `security_event` correctly

**Confirmation, not a change.** α2b's `verify_access` raises
`UnauthorizedError` on signature / kind / expiry / claim-shape
failures. `get_current_user` translates them all into the generic
401 and logs `auth.request.rejected reason=verify_failed`. No
change to `AuthTokenIssuer` or `JWTService` is needed.

### D10. Test ordering and internal checkpoint plan

**Decision.** Slice fits under the ~20-file / ~1000-net-LOC
checkpoint threshold from RUNBOOK §7.1, so no PR-level split is
needed. However, for implementation-time discipline (RUNBOOK §7.2
bottom-up), the work still divides into **three internal
checkpoints inside a single PR**, each with its own local CI-green
stamp before the next begins. This matches the "working, testable
state after each checkpoint" pattern the RUNBOOK codifies from
α2b.1 / α2b.2.

**Checkpoint α3.1 — Contracts (interfaces + type aliases).**
* Extend `ISessionRepository` with `get_by_id` (interface only).
* Add `TokenIssuerDep` + `CurrentUserDep` **type aliases** (no
  implementation of `get_current_user` yet — imports the port
  types only).
* Extend `FakeSessionRepository.get_by_id` (one-liner) so the
  existing auth unit suite still compiles.
* **Exit gate:** `mypy` + `import-linter` + full unit suite green
  locally. No HTTP change yet, no new integration tests.

**Checkpoint α3.2 — Infrastructure + dep + repo integration.**
* Implement `SessionRepository.get_by_id` against the live DB.
* Implement `get_current_user` in `deps.py` (all seven 401
  reasons wired, structured logs emitted, anti-enumeration
  generic message).
* Add repository integration tests R1–R3 (§5.1).
* **Exit gate:** local CI gate 10/10 green including the three
  new repository integration tests. HTTP surface still unchanged.

**Checkpoint α3.3 — HTTP + docs + acceptance.**
* Create `schemas/users.py` with `UserPublic` (re-exported by
  `schemas/auth.py`).
* Create `routers/users.py` with `GET /users/me`.
* Wire the router in `app.main` under `/api/v1`.
* Add integration tests H1–H14 (§5.2).
* Add `AUTH_TOKEN_LIFECYCLE.md` §3.5, `RUNBOOK_WAVE.md` §7.5,
  `CHANGELOG.md`, `ROADMAP.md` updates.
* **Exit gate:** local CI gate 10/10 green; acceptance criteria
  §4 A1–A13 + B1–B4 + C1–C3 + D1–D4 + E1–E3 all tick; anti-scope-creep
  envelope (§8) verified against the diff by grep.

Test ordering within each checkpoint follows RUNBOOK §7.2:
repository integration tests before use-case unit tests before
HTTP integration tests. The dep is exercised by integration only
(no unit tests by default; §D1).

If the file inventory grows past ~20 files during implementation
or if any checkpoint exit gate fails on the first attempt, pause
and re-scope before pushing.

---

## Section 3 — File Inventory

### 3.1 New files

| Path | Purpose |
| --- | --- |
| `backend/app/api/v1/schemas/users.py` | `UserPublic` (moved from `schemas/auth.py`); public projection reused by `/users/me` and future user endpoints. |
| `backend/app/api/v1/routers/users.py` | `GET /users/me` handler; declared with `CurrentUserDep`. |
| `backend/tests/unit/api/test_deps_get_current_user.py` | Optional unit-level dep tests using `TestClient` + dependency overrides — CREATE ONLY IF integration coverage leaves branches uncovered. Recorded as a fallback, not a default. |
| `backend/tests/integration/api/test_users_me.py` | Integration matrix for `/users/me` (see §5). |
| `backend/tests/integration/infrastructure/repositories/test_session_repository.py` | (extend existing file) new `get_by_id` cases. |

### 3.2 Extended files

| Path | Change |
| --- | --- |
| `backend/app/api/v1/deps.py` | Add `TokenIssuerDep`, `get_current_user` function, `CurrentUserDep` alias. |
| `backend/app/api/v1/schemas/auth.py` | Re-export `UserPublic` from `schemas.users` (single-line import + `__all__` entry). |
| `backend/app/api/v1/routers/__init__.py` | Register `users` router. |
| `backend/app/main.py` | Wire the `users` router under `/api/v1`. |
| `backend/app/application/interfaces/repositories.py` | Add `ISessionRepository.get_by_id`. |
| `backend/app/infrastructure/repositories/session_repository.py` | Implement `get_by_id`. |
| `backend/tests/unit/application/use_cases/auth/_fakes.py` | `FakeSessionRepository.get_by_id` (one-liner). |
| `backend/app/application/use_cases/auth/` | **No change.** Existing use cases are untouched. |
| `docs/engineering/AUTH_TOKEN_LIFECYCLE.md` | Add §3.5 authenticated-request path + §6 log-event rows. |
| `docs/engineering/RUNBOOK_WAVE.md` | Add §7.5 (no file-sync-hosted repos). |
| `ROADMAP.md` | Phase-3 row status line: `α3 (users/me) IN PROGRESS → ✅` on merge. |
| `CHANGELOG.md` | `[Unreleased]` block under Phase 3 → α3. |

### 3.3 Estimated size

* New source LOC (app): **~120** (dep + router + schema module +
  repo method + interface method).
* New test LOC: **~180** (repo integration + HTTP integration).
* New docs LOC: **~100** (runbook §7.5 + token-lifecycle §3.5 +
  changelog + roadmap).
* **Files touched: ~13.** Comfortably under the RUNBOOK §7.1
  checkpoint threshold; single-checkpoint plan stands.

### 3.4 Files explicitly NOT touched

`app/core/container.py`, `app/application/use_cases/auth/*`,
`app/domain/*`, `app/infrastructure/security/*`, `app/infrastructure/db/*`
(no migration), `alembic/*`, `pyproject.toml`, `.github/workflows/*`.
Any diff against these paths during implementation is a scope
violation and must be justified inline or removed.

### 3.5 Import-linter delta

Zero. No new cross-layer edges. The dep function stays inside the
API layer; the interface addition stays inside the application
layer; the concrete repository update stays inside infrastructure.
All five existing contracts remain KEPT.

### 3.6 The `RUNBOOK_WAVE.md` §7.5 addition (spelled out for the PR)

Title: **§7.5 — Repositories MUST NOT reside inside synchronized
folders (OneDrive, Dropbox, Google Drive, iCloud Drive).**

Framing: this is **engineering policy**, not guidance. Any PR that
introduces a new clone inside a synchronized folder is out of
policy and must be rejected on those grounds alone, regardless of
whether the sync agent has caused visible corruption yet on that
particular clone.

Body (draft):

> Hosting the repository inside a file-sync provider's watch
> directory (`C:\Users\<user>\OneDrive\...`,
> `~/Dropbox/...`, `~/Google Drive/...`, `~/Library/Mobile Documents/...`)
> is a known way to corrupt `.git` internals — the sync agent
> touches ref files and pack contents git expects nothing else to
> write to. Symptoms observed on this repository across α2a → α2b
> (before the 2026-07-09 migration to `C:\dev\ai-video-platform`):
>
> * `git branch -d` prints `Deletion of directory failed. Should I try again?`
>   prompts because the sync agent holds the empty parent open.
> * Sporadic file-locked errors on `.pytest_cache` cleanup during
>   the CI gate.
> * Random UTF-8 BOM re-introduction on message files after
>   `Remove-Item` (empirically observed once during α2b).
>
> **Policy.** New clones **MUST** live under `C:\dev\<repo>`
> (Windows), `~/dev/<repo>` (macOS/Linux), or an equivalent
> non-synced path. `git clone` into a sync-agent directory is
> **prohibited**, even temporarily. A clone discovered inside a
> synchronized folder must be migrated out at the next natural
> pause (procedure below) — not "eventually", and not "if problems
> appear".
>
> **Migration procedure** (executed 2026-07-09; recorded here as
> the reference):
>
> 1. Confirm working tree clean on the source clone.
> 2. `git clone <origin> C:\dev\<repo>` (or copy `.git/` intact if
>    the source is authoritative and preserves reflog).
> 3. Verify `git status` / `git log -5` / `git tag -l "v0.4.*"`
>    match byte-for-byte between source and destination.
> 4. Run the CI gate on the destination — must pass 10/10 stages
>    before deleting the source clone.
> 5. Delete the OneDrive-hosted clone. Update any IDE workspace
>    to point at the new path (`move_agent_to_root` for Cursor).
>
> Applies to Wave-style and Slice-style work equally. Retro-adopted
> as of α3 (2026-07-09).

---

## Section 4 — Acceptance Criteria

Written so a reviewer can tick each one before green-lighting the
merge. Every criterion is enforceable via test, log, or grep.

### 4.1 Endpoint behavior (`GET /users/me`)

| # | Criterion |
| --- | --- |
| A1 | Happy path: `Authorization: Bearer <valid access>` → 200 + envelope containing `UserPublic` matching the caller's row. |
| A2 | Missing `Authorization` header → 401 with `code = UNAUTHENTICATED`, message `not authenticated`, empty `details`. |
| A3 | Malformed header (`Bearer`, wrong scheme, no whitespace) → 401 (same message). |
| A4 | Invalid-signature JWT → 401 (same message); log `auth.request.rejected reason=verify_failed security_event=True`. |
| A5 | Expired access token → 401 (same message); log `reason=verify_failed`, `security_event` **not** set. |
| A6 | Token whose `sid` no longer matches any `sessions` row → 401; log `reason=sid_missing_session`, `security_event=True`. |
| A7 | Token whose `sid` references a revoked session → 401; log `reason=session_revoked`. |
| A8 | Token whose `sid` references an expired session row → 401; log `reason=session_expired`. |
| A9 | Token whose `sub` references a soft-deleted user → 401; log `reason=sid_user_gone`. |
| A10 | Response envelope always carries `request_id` from the middleware. |
| A11 | On success: log `auth.request.authenticated user_id=… session_id=…`. |
| A12 | **Mid-flight revocation** (session deleted / revoked between the token being minted and this request arriving): 401 with `reason=session_revoked` — distinct from replay-of-already-known-revoked because the client had no way to know. Verifies the liveness check runs on **every** request, not just first-time-through cache logic. |
| A13 | **JWT-first short-circuit.** An invalid-signature bearer produces 401 **without any repository call**. Enforced by an integration test that installs a spy repository whose `get_by_id` / `get_by_hash` raise `AssertionError` if invoked, and confirms the endpoint still returns 401. Establishes the invariant that JWT validity is a strict pre-condition for DB access — a required property for the eventual rate-limiting slice. |

### 4.2 Dep contract

| # | Criterion |
| --- | --- |
| B1 | `get_current_user` returns a `User` domain entity (frozen dataclass), never a Pydantic model. |
| B2 | The returned `User.password_hash` is **not exposed** to the route response — enforced structurally by `UserPublic` (already excludes `password_hash`). |
| B3 | Import-linter reports 5/5 contracts KEPT after the change. |
| B4 | `deps.py` imports NOTHING from `app.infrastructure` (verified by grep + import-linter). |

### 4.3 Repository

| # | Criterion |
| --- | --- |
| C1 | `ISessionRepository.get_by_id(session_id)` returns the row (revoked or not) or `None`. |
| C2 | Concrete adapter matches contract against the live DB (integration test). |
| C3 | `FakeSessionRepository.get_by_id` matches the same contract. |

### 4.4 Docs

| # | Criterion |
| --- | --- |
| D1 | `RUNBOOK_WAVE.md` now has §7.5 (grep: `## 7.5` present). |
| D2 | `AUTH_TOKEN_LIFECYCLE.md` §3.5 diagram present and links to the log-event catalogue in §6. |
| D3 | `CHANGELOG.md [Unreleased]` block populated with Added / Changed / Validated / Not modified / Scope discipline. |
| D4 | `ROADMAP.md` Phase-3 row shows α3 status. |

### 4.5 CI gate

| # | Criterion |
| --- | --- |
| E1 | Local CI gate `python scripts/ci_gate.py` → **PASSED** 10/10. |
| E2 | Remote CI check green on the PR. |
| E3 | Coverage does not drop more than 1 percentage point from v0.4.2's 81.05% (§ RUNBOOK §5.4 "expected drift on HTTP-layer growth"). |

---

## Section 5 — Test Matrix

Grouped by layer. Each row is one test function.

### 5.1 Repository integration (`test_session_repository.py` extensions)

| # | Scenario | Assertion |
| --- | --- | --- |
| R1 | `get_by_id` on an existing live row | returns row with `revoked_at is None` |
| R2 | `get_by_id` on a revoked row | returns row with `revoked_at != None` (does NOT filter revoked out) |
| R3 | `get_by_id` on an unknown UUID | returns `None` |

### 5.2 HTTP integration (`test_users_me.py`)

| # | Scenario | Expected |
| --- | --- | --- |
| H1 | Register → use returned access token → `GET /users/me` | 200, `data.user.email` matches |
| H2 | No `Authorization` header | 401, `UNAUTHENTICATED`, `not authenticated` |
| H3 | `Authorization: Basic xyz` | 401 (scheme mismatch) |
| H4 | `Authorization: Bearer` (empty) | 401 (malformed) |
| H5 | Bearer garbage-JWT | 401 |
| H6 | Bearer JWT with tampered signature | 401 + `security_event=True` log |
| H7 | Bearer expired-but-signed JWT | 401, no `security_event` |
| H8 | Login → logout → replay access | 401 (`reason=session_revoked`) |
| H9 | Login → refresh (rotates sid) → replay OLD access | 401 (`reason=session_revoked`) |
| H10 | Register → soft-delete user directly in DB → replay access | 401 (`reason=sid_user_gone`) |
| H11 | Register → in-DB update `sessions.expires_at = past` → replay access | 401 (`reason=session_expired`) |
| H12 | Password field never appears in the response | grep-style assertion on the JSON body |
| H13 | **Mid-flight revocation.** Register → capture access token → directly `UPDATE sessions SET revoked_at = now()` for the caller's sid (bypassing the logout endpoint) → `GET /users/me` with the still-in-window access token | 401, `reason=session_revoked`. Distinct from H8 in that the client never called `/logout`; the revocation is server-side (admin action / SIEM response). Proves liveness re-checks on every request. |
| H14 | **JWT-first short-circuit.** Install FastAPI dependency overrides that swap `IUserRepository` + `ISessionRepository` for spies whose every method raises `AssertionError("repository must not be reached")`. Send `Authorization: Bearer abc.def.tampered` | 401 returned; **neither spy method invoked**. Proves signature failure short-circuits before any DB access. |

### 5.3 Structured-log spot-checks

Wired via `capsys` + ANSI-strip (α2b pattern) inside H1, H6, H8,
H10. Not standalone test functions.

### 5.4 Unit-layer coverage

None planned by default. If integration coverage of `deps.py` or
`session_repository.get_by_id` drops below existing thresholds
(96%+ for application-layer files; branch coverage on the router
function), add targeted unit tests as a fallback — recorded in
§3.1 as an optional file.

---

## Section 6 — CI Impact

### 6.1 Stage-by-stage

| Stage | Impact |
| --- | --- |
| 1 lint | zero |
| 2 format | zero |
| 3 mypy + import-linter | zero — no new cross-layer edges; new module type-checked as any other |
| 4 unit + coverage | +3 unit tests (repo `_fakes.py` change); ~+3 s runtime |
| 5 alembic up | no migration → no-op |
| 6 alembic down | no-op |
| 7 alembic up (idempotency) | no-op |
| 8 schema validator | no-op |
| 9 ERD compare | no-op |
| 10 coverage report | HTTP-integration statements grow; unit-only coverage may drift down slightly. Acceptance criterion E3 keeps the drift bounded. |

### 6.2 Runtime budget

Total CI gate expected to remain **≤ 2 minutes** end-to-end (α2b
baseline). New tests are cheap: `/users/me` handler is a single
lookup + a fresh AsyncClient issuing a small request.

### 6.3 Post-migration workflow verification

First slice after the OneDrive → `C:\dev\ai-video-platform`
migration. §7.5 (once merged) should mean:

* `git branch -d phase3/alpha3-*` prints no `Deletion of directory failed` prompts.
* `.pytest_cache` cleanup between test runs is clean.
* No BOM re-introduction on message files.

If any of these regressions reappear on α3, `RUNBOOK §7.5` is
already wrong on a detail and the retro captures the correction.

---

## Section 7 — Open Questions (resolved 2026-07-10)

Q1. **Session-liveness read — `get_by_id` vs `get_live_by_id`?**
**Resolved: keep `get_by_id` returning revoked rows too.** Mirrors
α2b `get_by_hash` "caller decides" contract; keeps log-reason
granularity (`session_revoked` vs `session_expired`) in the dep;
avoids pushing business rules into the repository. Prefer the
simplest dependency chain; do not add abstractions until α4
genuinely needs them.

Q2. **Move `UserPublic` to `schemas/users.py` in α3, or defer?**
**Resolved: move now.** Response is the public projection only —
`UserPublic` — and never leaks `password_hash`, `version`,
`deleted_at`, or any tenant-internal field. `schemas/users.py`
becomes the canonical home; `schemas/auth.py` re-exports for its
existing consumers.

Q3. **Tenant middleware / tenant-scoped dependencies in α3?**
**Resolved: no.** `CurrentUserDep` is enough for α3. Tenant
context arrives naturally when project endpoints land (α4+); any
attempt to introduce tenant middleware now is scope creep and
must be rejected.

Q4. **Role / authorization checks in α3?** **Resolved: no.**
Authentication first, authorization later — consistent with the
existing roadmap. `get_current_user` returns a `User`; no
`require_role(...)` dep, no scope claims, no admin surfaces in
this slice.

Q5. **Cut a `v0.4.3-phase3-alpha3` tag on merge?** **Resolved:
defer.** Leave the tagging decision until α3 ships. Revisit
alongside the missing `v0.4.0-phase3-alpha1` gap at end of α3
(Backlog Item 2 from the retrospective) so both are decided
holistically rather than mid-slice.

**Additional resolutions from review that were not in the
original Q-list:**

Q6. **`WWW-Authenticate: Bearer` header on 401 responses (RFC 6750)?**
**Resolved: not in α3.** Out of the anti-scope-creep envelope
below; retro-fit to α2a/α2b + wire in α3 lands in its own
one-liner hardening slice.

Q7. **Log caller IP + user-agent on `auth.request.authenticated` /
`auth.request.rejected`?** **Resolved: yes, ~2 lines each.**
Parity with the register/login/refresh audit trail already
established in α2a/α2b. Does not expand scope.

---

## Section 8 — Anti-scope-creep envelope

The single largest risk on an "authentication seam" slice is
letting it silently absorb every neighbouring auth concern. The
following are **explicitly deferred** and any attempt to include
them in α3 must be rejected in review:

* auth middleware (per-request `request.state.user` mutation)
* permissions / roles / scopes
* rate limiting
* tenant-scoping middleware
* audit-logging service (beyond the two structured-log events
  in §2.D4)
* IP-header hardening beyond the α2a `X-Forwarded-For` first-hop
  pattern already in use
* CSRF handling
* OAuth pre-hooks / OIDC discovery endpoints
* password-reset / email-verify endpoints (API_CONTRACT §3.1)
* `GET /users/{id}` admin endpoint
* `WWW-Authenticate: Bearer` header retro-fit (see Q6)

If any of the above is judged genuinely needed while implementing
α3, the response is to **pause and open a new pre-flight** for
that item, not to fold it into this slice.

---

## Section 9 — Reviewer sign-off (2026-07-10)

* **Design decisions D1–D10:** all approved as written.
* **§3.6 RUNBOOK_WAVE.md §7.5 draft:** approved with strengthened
  policy wording (title changed from "Repository must not…" to
  "Repositories MUST NOT reside inside synchronized folders…" and
  body explicitly framed as policy, not guidance).
* **Open questions Q1–Q5 + Q6–Q7:** resolved inline above (§7).
* **Additional refinements folded in:**
  * A12 — mid-flight revocation acceptance criterion.
  * A13 — JWT-first short-circuit acceptance criterion.
  * H13 + H14 — corresponding integration tests.
  * §8 anti-scope-creep envelope (this slice explicitly rejects
    the classic "auth cleanup" scope-drift pattern).
  * §10 α3 Exit Criteria (below) — codifies the seam's
    architectural invariants so they outlive this slice.
* **Approved to cut branch:** `phase3/alpha3-current-user`.
  Implementation proceeds top-to-bottom against §3's file
  inventory in a single checkpoint per §2.D10, closing with the
  standard RUNBOOK §3–§4 verification-and-release sequence.

---

## Section 10 — α3 Exit Criteria (architectural invariants)

α3 is complete when the following are true and remain true in
every subsequent slice. These become the seam's permanent
contract; any α4+ code that violates them is a regression.

1. **Every authenticated endpoint uses `CurrentUserDep`.** Grep
   invariant: `rg "current_user: CurrentUserDep" app/api/` covers
   every non-public route in the router set.
2. **No endpoint (or use case, or middleware) manually parses a
   JWT.** Grep-negatives to enforce in review:
   * `rg "jwt\\.decode" app/api/ app/application/` → must be empty
   * `rg "Authorization" app/api/v1/routers/` → must be empty
     (header access is exclusively via the `_bearer_access_token`
     dep in `deps.py`).
3. **No endpoint accesses `JWTService` or `ITokenIssuer` directly
   from a router.** Router-layer JWT access is a scope violation.
   Grep-negative: `rg "JWTService|ITokenIssuer" app/api/v1/routers/`
   must be empty.
4. **`GET /users/me` is the canonical worked example.** Any
   contributor adding a new authenticated endpoint copies its
   shape:

   ```python
   @router.get("/some-resource")
   async def get_thing(
       current_user: CurrentUserDep,
       # ...other deps...
   ) -> JSONResponse:
       ...
   ```

   Nothing more is required for authentication. If a future
   endpoint's author feels tempted to "just re-verify the token
   here for safety," that is a review-blocker signal — the seam
   has been designed exactly so this is never necessary.
5. **New authenticated endpoints require only `CurrentUserDep`.**
   No copy-pasted token parsing, no re-imports of `deps._bearer_access_token`,
   no reach-through into `container.get_token_issuer()` at the
   route layer. If a new endpoint needs anything beyond
   `CurrentUserDep` to know who is calling, that is the signal to
   open a new pre-flight for whatever cross-cutting concern is
   actually being introduced (roles, tenancy, rate limiting) —
   not to widen the seam.
6. **`get_current_user` remains fail-closed.** Every code path in
   the dep that reaches a non-happy branch raises
   `UnauthorizedError` with the anti-enumeration generic message.
   A regression that leaks a specific error string is a
   security-review-blocker signal.

These invariants outlive the slice and are enforceable via grep +
import-linter contracts. If any becomes hard to preserve in a
later slice, the correct response is a new ADR proposing an
explicit seam change — not silent drift.
