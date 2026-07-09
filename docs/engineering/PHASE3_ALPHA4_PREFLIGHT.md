# Phase 3 Slice α4 — Read-Only Pre-Flight

> **Convention.** This document is a read-only planning artefact per
> `docs/engineering/RUNBOOK_WAVE.md` §1. It records scope, non-goals,
> design decisions, file inventory, acceptance criteria, test matrix,
> and CI impact for **Slice α4** before any branch is cut or any code
> is written. Approve as-is (or push back) before implementation begins.
>
> **Status.** ✅ Approved 2026-07-10 with seven in-doc refinements
> applied (reviewer sign-off in §9). Branch cut authorised.
>
> **Predecessors.**
> α1 (`v0.4.0-phase3-alpha1`, PR #8),
> α2a (`v0.4.1-phase3-alpha2a`, PR #9),
> α2b (`v0.4.2-phase3-alpha2b`, PR #10),
> α3 (`v0.4.3-phase3-alpha3`, PR #14, merge `02185cf`).
>
> **Baseline versioning.** `app/main.py` currently reads
> `"0.4.3-phase3-alpha3-dev"`. The first α4 commit bumps it to
> `"0.4.4-phase3-alpha4-dev"`; the release tag `v0.4.4-phase3-alpha4`
> drops the `-dev` suffix on merge.

---

## Section 1 — Scope

### 1.1 One-line thesis

α4 delivers the **first authenticated mutation** — `PATCH /api/v1/users/me`
allowing a signed-in user to update their own `display_name` — and
establishes the write-path counterpart to α3's read-path
`CurrentUserDep` pattern. **`PATCH /users/me` becomes the canonical
authenticated mutation endpoint.** The endpoint exercises the full
write stack (request DTO → use case → repository mutation →
optimistic-concurrency check → response DTO) so that every future
authenticated mutation follows the same shape.

**Architectural invariant established by α4** (reviewer refinement R4):
*All future authenticated write endpoints follow the pattern introduced
by `PATCH /users/me` — `CurrentUserDep` → DTO validation → optimistic-
concurrency check → domain mutation → updated representation returned
in the API_CONTRACT §1.1 envelope.*

### 1.2 What's in

1. **`UpdateUserProfile` use case** (`app/application/use_cases/users/update_profile.py`)
   — orchestrates the display-name update, checks the version fence,
   emits an audit log event.
2. **`IUserRepository.update_profile`** interface method + concrete
   SQLAlchemy implementation. Uses a compare-and-swap on the `version`
   column so optimistic-concurrency violations are detected atomically
   at the SQL layer.
3. **`UpdateUserProfileRequest` DTO** in `app/api/v1/schemas/users.py`
   — partial-update body with a single optional field (`display_name`)
   in α4. Rejects unknown / non-whitelisted keys.
4. **`PATCH /api/v1/users/me`** handler in `app/api/v1/routers/users.py`.
   Uses `CurrentUserDep` (proving the α3 seam works for writes too) +
   the new `UpdateUserProfileDep`.
5. **Structured-log event catalogue** for the mutation:
   `user.profile.updated` (info, happy path — carries `user_id`,
   `changed_fields`, `new_version`);
   `user.profile.update_rejected` (warn, `reason=` field for
   `version_mismatch` / `no_changes` / `same_value_noop`).
6. **`AUTH_ENDPOINTS.md` §7.1**: new sub-section documenting
   `PATCH /users/me` alongside the existing `GET /users/me`.
6a. **`AUTH_ENDPOINTS.md` §8 — Canonical authenticated mutation flow**
    (reviewer refinement R7): a short, standalone architectural
    reference that documents the six-step mutation lifecycle every
    future authenticated write endpoint follows. This lifts the R4
    invariant from an inline sentence in α4's thesis into a
    first-class, reusable template future slices copy from:

    1. Authenticate via `CurrentUserDep`.
    2. Validate request DTO (Pydantic, `extra="forbid"`).
    3. Check optimistic concurrency (`version` fence).
    4. Apply domain mutation (in a use case, not the router).
    5. Persist via a targeted repository CAS.
    6. Return the updated representation in the API_CONTRACT §1.1
       envelope with `200 OK`.
7. **`CHANGELOG.md`** `[Unreleased]` block following the α2b / α3
   template.
8. **Version bump** in `app/main.py` (see §Baseline versioning above).

### 1.3 Non-goals (explicit, will NOT ship)

* **Email changes.** `PATCH /users/me` will NOT accept `email` in α4.
  Changing email requires an out-of-band verification flow (send
  token to old + new address, re-verify) that is a slice of its own.
* **Password changes.** Explicitly a separate endpoint
  (`POST /auth/change-password`) with current-password re-authentication.
  Sharing a code path with generic profile updates would be a security
  anti-pattern.
* **Soft delete / account closure.** `DELETE /users/me` deferred; needs
  its own design conversation (revoke all sessions? preserve for legal
  hold? tombstone vs hard-delete?).
* **Admin edits.** `PATCH /users/{id}` is admin surface (RBAC required)
  and is deferred until roles land.
* **Avatar / media fields.** No media_id column changes; those belong
  in a future media slice.
* **New DB migrations.** α4 uses the existing `users` schema (`version`
  column is already present from α1).
* **Rate limiting.** Same posture as α3.
* **Profile completeness features.** No avatar / profile picture,
  timezone, locale, bio, phone number, or any adjacent field. α4
  updates **exactly one field — `display_name`**. Profile-completeness
  work is its own product decision and belongs in a later slice; the
  goal of α4 is to establish the write architecture, not to grow the
  profile surface.

### 1.4 Anti-scope-creep envelope

If any of the following show up in review, push back:

* "While we're in `UserRepository`, let's also add `list_users` /
  `search_users`." — No. `list_users` needs pagination, filtering,
  tenant scoping — its own slice.
* "Let's introduce a generic `Patch<T>` DTO base class." — No.
  Premature abstraction; add it after two or three real patch endpoints
  exist and share duplicated shape.
* "Let's use `PUT` instead of `PATCH`." — No. `PUT` implies full-resource
  replacement, which forces the client to send every field including
  fields it doesn't own (`created_at`, `id`, `tenant_id`). `PATCH` with
  a whitelist is the correct semantic for user-driven profile edits.
* "Let's make `version` optional (server uses current DB version if
  omitted)." — See Q1 in §7; this is the one live design question and
  the answer must be locked before implementation.

---

## Section 2 — Design Decisions

### D1: Where does `update_profile` live?

**Decision:** New `app/application/use_cases/users/` package. Not
under `auth/`.

**Rationale:** Profile updates are a user-management concern, not an
authentication concern. Placing them under `auth/` would blur the
package boundary and make the next users-domain endpoints
(`GET /users/{id}`, `PATCH /users/{id}`, etc.) hunt for their home.
The empty `users/` package created in α4 becomes the natural landing
zone for those.

### D2: Repository method — targeted vs generic

**Decision:** Add a **targeted** `IUserRepository.update_profile(user_id,
expected_version, display_name)` method rather than a generic
`update(user: User)` that accepts a full domain entity.

**Rationale:**

* The domain entity has many fields we deliberately don't want an
  endpoint mutating (`password_hash`, `tenant_id`, `email`, timestamps).
  A generic update method makes the "what can this endpoint change?"
  question implicit and easy to get wrong.
* Targeted methods are trivially testable: the fake repository's
  implementation is 3 lines, the SQL is `UPDATE ... WHERE id = ? AND
  version = ?`, no orchestration inside the port.
* Follows the α2a/α2b precedent: `update_last_login`, `revoke`,
  `assign_role_by_code` are all targeted mutations, not generic
  update-by-entity.

### D3: Optimistic concurrency — where does the version check live?

**Decision:** The version check lives in the **repository**, using a
SQL compare-and-swap:

```sql
UPDATE users
   SET display_name = :new_name,
       version      = version + 1,
       updated_at   = NOW()
 WHERE id           = :user_id
   AND version      = :expected_version
   AND deleted_at IS NULL
RETURNING version
```

The repository returns the new `version` on success; if `RETURNING`
yields zero rows, the repository raises `ConflictError` (mapped to
HTTP 412 by the exception handler — see D4).

**Rationale:** Doing the check in Python (SELECT then UPDATE) opens a
TOCTOU window. Doing it in SQL is atomic and identical to the
Session-revoke CAS pattern from α2b.

### D4: HTTP semantics — what carries the version fence?

**Decision:** The version is passed **in the request body**, not in an
`If-Match` header.

**Rationale:**

* `If-Match` with ETags is the RFC 7232 standard, but requires the
  server to serialise the version into an `ETag` header on every GET
  response and the client to round-trip it. That's more machinery than
  a single integer field warrants for a first-cut mutation endpoint.
* Body-embedded versions match the client-mental-model: you got a
  `UserPublic` with a version in it (see D5), you send it back with
  your changes.
* A future migration to `If-Match` remains possible without breaking
  wire compatibility — the body-level `version` becomes optional then
  deprecated.

**Trade-off accepted:** the body DTO has a required `version` field.
Callers must send it. There is no "server uses latest" fallback (see
Q1 for alternative).

### D5: Response DTO — expose `version` on `UserPublic`?

**Decision:** **Yes** — add `version: int` to `UserPublic`.

**Rationale:**

* Clients need the current version to send with any subsequent PATCH.
  Not exposing it forces clients into a "PATCH → 412 → GET → PATCH again"
  loop which is user-hostile.
* `version` is a monotonically-increasing integer, not sensitive data;
  exposing it does not leak internal state.
* Backwards compatibility: this **adds** a field to the response. No
  existing consumer will break; the API_CONTRACT §1.1 envelope shape is
  unchanged.

### D6: HTTP method + status codes

**Decision:**

| Case | Method | Status | `error.code` |
|------|--------|--------|--------------|
| Happy path | PATCH | 200 | — (returns updated `UserPublic`) |
| Body validation failure | PATCH | 422 | `VALIDATION_FAILED` |
| Version mismatch | PATCH | 412 | `VERSION_CONFLICT` |
| Unknown / non-whitelisted field | PATCH | 422 | `VALIDATION_FAILED` |
| Auth failure (any α3 rejection branch) | PATCH | 401 | `UNAUTHENTICATED` |

**Notes.**

* **Success response shape (reviewer refinement R1):** the happy path
  always returns `200 OK` with the API_CONTRACT §1.1 envelope
  `{ "data": UserPublic, "meta": { "request_id": ... } }`. **Never
  `204 No Content`** — the client immediately needs the new `version`
  to send with any subsequent PATCH, and returning the full
  representation saves an obligatory follow-up `GET`.
* **Concurrency-failure body (reviewer refinement R2):** the error
  code is `VERSION_CONFLICT` (API-oriented), not
  `PRECONDITION_FAILED` / `OPTIMISTIC_CONCURRENCY_FAILURE` /
  `CAS_FAILED` (transport- or implementation-oriented). The 412
  status is retained because the semantic — "your precondition
  about the resource version was wrong" — matches RFC 7232's
  `If-Match` failure family. Concrete body:

  ```json
  {
      "error": {
          "code": "VERSION_CONFLICT",
          "message": "Resource has been modified."
      },
      "meta": { "request_id": "..." }
  }
  ```

* Unknown-field rejection is enforced via Pydantic
  `model_config = ConfigDict(extra="forbid")`.

### D6a: Version-increment invariant (reviewer refinement R3)

**Decision:** `users.version` increments **only when at least one
persisted field changes** as a result of a successful mutation
endpoint. It never increments on:

* Authentication (login / refresh / logout).
* Read endpoints (`GET /users/me`).
* Identical PATCHes (same-value no-ops — see D8).
* Failed mutations (validation errors, 412s, 401s).

This becomes a project-wide invariant future write endpoints inherit,
and is the reason D8's same-value no-op does not bump the version.

### D7: Empty PATCH body — 400 or no-op?

**Decision:** Reject with **422 `VALIDATION_FAILED`** if the body
contains no whitelisted mutations.

**Rationale:** An empty PATCH is a bug in the caller, not a legitimate
no-op. Explicit rejection surfaces the bug at development time. This
covers both `{}` and `{"version": 5}` (i.e., version present but no
fields to change).

### D8: Same-value mutation (e.g., `display_name` set to current value)

**Decision:** Treat as a successful no-op — return 200 with the current
`UserPublic`. The repository does not bump `version` when nothing
changes. A `user.profile.update_rejected` log with `reason=same_value_noop`
is emitted for observability.

**Rationale:** A client that PUTs back the same object it just GETted
shouldn't observe a version bump — otherwise every "save" button
churns the version even when the user changed nothing.

**Implementation:** The SQL becomes:

```sql
UPDATE users
   SET display_name = :new_name,
       version      = CASE WHEN display_name = :new_name THEN version
                           ELSE version + 1 END,
       updated_at   = CASE WHEN display_name = :new_name THEN updated_at
                           ELSE NOW() END
 WHERE id      = :user_id
   AND version = :expected_version
```

Or a Python-side compare-then-decide before the UPDATE. Chosen at
implementation time; the acceptance criterion (A8) is what matters.

### D9: `email` field on `UserPublic` and normalisation

**Decision:** No changes. `UserPublic.email` continues to be the
lowercase-normalised value written at register time.

### D10: Implementation checkpoint plan

**Decision:** **Single checkpoint.** All files land together in one
commit / one PR.

**Rationale:** The reference α3.3 slice (~700 LOC) was a single
checkpoint and passed review cleanly. α4's estimated surface (§4) is
comparable. Splitting into α4.1 (repo + use case + unit tests) and α4.2
(HTTP + integration + docs) would introduce mid-slice mypy failures
similar to α3.1's ISessionRepository issue — solvable, but overhead
that doesn't buy us anything at this size.

**Escape hatch:** If actual LOC exceeds 900 or the use-case tests grow
past ~200 LOC, split at the domain/HTTP boundary and land two commits.

---

## Section 3 — Acceptance Criteria

### 3.1 Behavioural (A1–A13)

**A1. Happy path.** `PATCH /api/v1/users/me` with a valid access token,
correct current version, and a non-empty `display_name` returns 200
with the updated `UserPublic` (new `display_name`, incremented `version`).

**A2. Response contract.** The response envelope is
`{ "data": UserPublic, "meta": { "request_id": ... } }` — identical
shape to `GET /users/me`.

**A3. Version fence — success.** The repository CAS increments
`version` from N to N+1 atomically. Two concurrent PATCHes with the
same expected `version` — one succeeds, one gets 412.

**A4. Version fence — failure.** PATCH with a stale `version` value
returns 412 with `error.code == "VERSION_CONFLICT"` and
`error.message == "Resource has been modified."`. No mutation occurs
(verified by follow-up GET returning the pre-PATCH state).

**A5. Auth surface parity.** Every α3 authentication rejection branch
(`missing_header`, `malformed_header`, `verify_failed`, `sid_missing_session`,
`session_revoked`, `session_expired`, `sid_user_gone`) rejects PATCH
with the same generic 401 shape — anti-enumeration invariant preserved.

**A6. Whitelist enforcement.** PATCH bodies containing `email`,
`password`, `password_hash`, `tenant_id`, `id`, `created_at`,
`updated_at`, `deleted_at`, or any other non-whitelisted key return
422 with `error.code == "VALIDATION_FAILED"`.

**A7. Empty-body rejection.** PATCH with `{}` or with only `{"version": N}`
(no whitelisted mutations) returns 422.

**A8. Same-value no-op.** PATCH with `display_name` equal to the
current value returns 200, response `version` equals the request
`version` (not incremented), a `user.profile.update_rejected` log with
`reason=same_value_noop` is emitted.

**A9. Field-shape validation.** `display_name` empty string, string
> 200 chars, or non-string returns 422.

**A10. Soft-deleted-user safety.** `deleted_at IS NULL` filter in
the repository ensures a soft-deleted user (mid-request) sees the
UPDATE affect zero rows → 412 (indistinguishable from stale-version
from the client's perspective, deliberately — anti-enumeration on the
"is this account still valid?" question).

**A11. Structured-log contract.**
* Happy path emits `user.profile.updated` at INFO with `user_id`,
  `changed_fields`, `previous_version`, `new_version`.
* Version mismatch emits `user.profile.update_rejected` at WARN with
  `reason=version_mismatch`, `user_id`, `expected_version`,
  `current_version`.
* Same-value no-op emits `user.profile.update_rejected` at INFO with
  `reason=same_value_noop`.
* No PII in log lines beyond `user_id`.

**A12. `updated_at` bumped on real changes.** After a happy-path PATCH,
the response `UserPublic.updated_at` is > the pre-PATCH value. After a
same-value no-op, it is unchanged.

**A13. Round-trip consistency.** `PATCH /users/me` response body ==
subsequent `GET /users/me` response body. Guards against the "PATCH
returns stale cache while DB has fresh data" bug class.

### 3.2 Engineering (E1–E4)

**E1.** CI gate 10/10 green (all α3 gates preserved, no new stages).

**E2.** No new `noqa`, `type: ignore`, or coverage overrides.

**E3.** Unit-only coverage ≥ 80% total (i.e. the current 81.63%
baseline holds within a ~1 percentage-point drift budget). If the
new API-layer surface pulls it below 80.5%, add targeted unit tests
for `UpdateUserProfile` before commit.

**E4.** `import-linter` contracts stay 5/5 kept. `use_cases/users/`
must not import from `api/` or `infrastructure/`.

---

## Section 4 — File Inventory

### 4.1 New files

| Path | LOC est. | Purpose |
|---|---:|---|
| `backend/app/application/use_cases/users/__init__.py` | 1 | Package marker — created alongside `update_profile.py`, not as a placeholder (reviewer refinement R6) |
| `backend/app/application/use_cases/users/update_profile.py` | ~90 | Use case |
| `backend/tests/unit/application/use_cases/users/__init__.py` | 1 | Package marker — created alongside `test_update_profile.py` |
| `backend/tests/unit/application/use_cases/users/test_update_profile.py` | ~200 | Use-case unit tests |
| `backend/tests/integration/infrastructure/repositories/test_user_repository.py` | ~110 | Repo integration tests (`update_profile` only in α4) |

### 4.2 Modified files (10)

| Path | Change | LOC est. |
|---|---|---:|
| `backend/app/application/interfaces/repositories.py` | Add `IUserRepository.update_profile` ABC | +12 |
| `backend/app/infrastructure/repositories/user_repository.py` | Implement `update_profile` with CAS | +35 |
| `backend/tests/unit/application/use_cases/auth/_fakes.py` | Extend `FakeUserRepository` with `update_profile` | +12 |
| `backend/app/api/v1/schemas/users.py` | Add `UpdateUserProfileRequest`; add `version` to `UserPublic` | +25 |
| `backend/app/api/v1/deps.py` | Add `UpdateUserProfileDep` + container getter | +15 |
| `backend/app/core/container.py` | Wire `get_update_user_profile_use_case` | +18 |
| `backend/app/api/v1/routers/users.py` | Add `PATCH /me` handler | +30 |
| `backend/app/api/v1/routers/auth.py` | Update `_to_payload` to populate the new `UserPublic.version` field | +1 |
| `backend/app/main.py` | Version bump to `0.4.4-phase3-alpha4-dev` | 1 |
| `backend/tests/integration/api/test_users_me.py` | Extend with PATCH tests (H15–H24) + update H1 to assert `version` on `GET` response | +240 |
| `docs/api/AUTH_ENDPOINTS.md` | Add PATCH /users/me sub-section + §8 canonical mutation flow | +90 |

### 4.3 Deliberately NOT touched

* `backend/alembic/**` — no schema change.
* `backend/app/domain/identity/user.py` — the domain entity already
  has `version`. No mutation.
* `backend/app/infrastructure/db/models/identity.py` — ORM row model
  already has `version`. No mutation.
* `backend/app/api/v1/routers/auth.py` — only the `_to_payload`
  helper's field list changes (to include `version`); no endpoint
  logic changes. Existing register/login/refresh responses now carry
  `version` as an additive field. All existing integration tests
  keep passing.
* `docs/engineering/PHASE3_AUTH_RETROSPECTIVE.md` — closed slice.
* `docs/engineering/AUTH_TOKEN_LIFECYCLE.md` — no auth-flow changes.

---

## Section 5 — Test Matrix

### 5.1 Unit tests — `test_update_profile.py`

| # | Case | Assertion |
|---|---|---|
| U1 | Happy path — change `display_name`, correct version | Use case returns updated `User` with `version+1`, `updated_at` bumped |
| U2 | Version mismatch → `ConflictError` | Use case raises; fake repo confirms no state change |
| U3 | Same-value no-op | Use case returns unchanged `User`, `version` unchanged; log carries `reason=same_value_noop` |
| U4 | Empty change set → `ValidationError` | Raised at use-case boundary (defence in depth; DTO already blocks) |
| U5 | User not found (deleted between auth and use case) → `ConflictError` | 412 semantics preserved |
| U6 | Structured log emitted on happy path | `user.profile.updated` with all A11 fields |
| U7 | Structured log emitted on version mismatch | `user.profile.update_rejected` with `reason=version_mismatch` |
| U8 | Display name stripped of surrounding whitespace before check | Confirms Pydantic `str_strip_whitespace` runs before same-value comparison |

### 5.2 Repository integration tests — `test_user_repository.py`

| # | Case | Assertion |
|---|---|---|
| R1 | `update_profile` happy path | Real DB row shows new `display_name`, `version+1` |
| R2 | `update_profile` version mismatch → returns None (or raises per D3) | Row unchanged, no side effects |
| R3 | `update_profile` on soft-deleted row → mismatch signal | `deleted_at` filter honoured |

### 5.3 HTTP integration tests — extended `test_users_me.py`

New H15–H24 alongside the existing H1–H14:

| # | Case | Assertion |
|---|---|---|
| H15 | Happy PATCH: change display_name | 200, response body matches new state, `version+1` |
| H16 | PATCH with no auth header | 401 (same generic message as GET/me H3) |
| H17 | PATCH with wrong version | 412 `PRECONDITION_FAILED`, DB unchanged |
| H18 | PATCH with `email` in body | 422 `VALIDATION_FAILED` |
| H19 | PATCH with empty body `{}` | 422 |
| H20 | PATCH with only `{"version": N}` | 422 |
| H21 | PATCH with same `display_name` | 200, `version` unchanged, no side effects |
| H22 | PATCH → GET round-trip consistency | Response bodies deep-equal |
| H23 | Concurrent PATCH: two clients, same version, one wins | First = 200, second = 412 |
| H24 | PATCH after logout (revoked session) | 401, no repo update reached |

Existing H1 updated to assert the new `version` field is present in
the `GET /users/me` response.

---

## Section 6 — Structured-Log Catalogue (α4 additions)

| Event | Level | Fields |
|---|---|---|
| `user.profile.updated` | INFO | `user_id`, `changed_fields` (list[str]), `previous_version`, `new_version`, `ip`, `request_id` |
| `user.profile.update_rejected` | WARN (for `version_mismatch`) / INFO (for `same_value_noop`) | `user_id`, `reason`, `expected_version`, `current_version` (only when known), `ip`, `request_id` |

**Anti-enumeration:** the `same_value_noop` reason MUST NOT surface to
the client (client sees 200 with the current resource). The log
distinction is server-side only.

---

## Section 7 — Open Questions (all resolved 2026-07-10)

### Q1: Should `version` in the PATCH body be required or optional?
**Resolution: ✅ Required.** Reviewer agreed with the pre-flight
recommendation. Missing `version` → 422. No last-write-wins fallback.
Locked in D4 + A6.


**Options:**

* **A (required):** Client must always send `version`. Missing →
  422. Matches D4 as written. Safest.
* **B (optional):** Missing `version` means "server uses current DB
  value" (last-write-wins). Simpler client code but silently drops
  concurrency protection.

**Recommendation:** A (required). The whole point of the version
column is to give clients a fence; making it optional is a false
economy the first time two tabs / two devices race.

### Q2: Should `display_name` accept an explicit `null` to clear it?
**Resolution: ✅ Reject with 422.** Reviewer agreed with the pre-flight
recommendation. If a "clear display name" UX ever becomes a real
feature, it will be introduced intentionally in a later slice.


**Options:**

* **A:** `null` → 422. `display_name` is required at register time;
  clearing it would leave the user with no name.
* **B:** `null` → set to empty string.
* **C:** `null` → reset to the email local-part.

**Recommendation:** A. If a "clear display name" UX ever becomes a
real feature, revisit; premature to design for it now.

### Q3: Do we log the *value* of the new display_name?
**Resolution: ✅ Log field names only, never values.** Reviewer agreed
with the pre-flight recommendation. `changed_fields=["display_name"]`,
never `display_name="John Smith"`. Value-level audit trail is deferred
to a future DB audit-log table.


**Options:**

* **A:** Log only the field name in `changed_fields`, never the value.
  Trivially GDPR-safe.
* **B:** Log the value for audit-trail completeness.

**Recommendation:** A. Values can appear in the DB audit trail once we
build one (Phase 4+). Application logs stay minimal.

---

## Section 8 — Anti-Scope-Creep Reminders

Same envelope as α3 §11. Repeated here for muscle memory:

* No middleware.
* No RBAC / roles.
* No new DB migrations.
* No new use-case packages beyond `users/`.
* No refactoring of unrelated code paths.
* No `PUT` / `DELETE` methods on `/users/me`.
* No batch or bulk endpoints.

If a reviewer suggests any of the above, defer to a later slice.

---

## Section 9 — Reviewer Sign-Off

**Reviewer verdict — 2026-07-10:** ✅ **Approved with seven in-doc
refinements applied.** Branch cut authorised.

Refinements folded into the doc:

* **R1 — Success response shape (D6):** happy path always returns
  `200 OK` with the full updated `UserPublic` in the API_CONTRACT §1.1
  envelope. Never `204`.
* **R2 — Concurrency-failure body (D6):** error code is
  `VERSION_CONFLICT` with message `"Resource has been modified."`.
  API-oriented, not transport- or implementation-oriented.
* **R3 — Version-increment invariant (D6a):** `users.version`
  increments only when a persisted field actually changes. Never on
  auth, reads, identical PATCHes, or failed mutations.
* **R4 — Architectural invariant (§1.1 thesis):** all future
  authenticated write endpoints follow the pattern introduced by
  `PATCH /users/me` — `CurrentUserDep` → DTO validation → optimistic-
  concurrency check → domain mutation → updated representation
  returned.
* **R5 — Non-goals reinforcement (§1.3):** no avatar, profile
  picture, timezone, locale, bio, or phone. α4 updates exactly one
  field (`display_name`). The goal is to establish the write
  architecture, not to grow profile completeness.
* **R6 — No placeholder package markers (§4.1, §12):** empty
  `__init__.py` files are created alongside the first file that lives
  in the package, not as scaffolding-first placeholders. Prevents
  drift-accumulation of empty markers if a slice ever slips.
* **R7 — Canonical mutation flow doc (§1.2 item 6a):**
  `AUTH_ENDPOINTS.md` gains a new §8 "Canonical authenticated
  mutation flow" that lifts the R4 invariant into a first-class,
  reusable reference — the template every future write endpoint
  copies from, not just prose buried in α4's pre-flight thesis.

Q1 / Q2 / Q3 all resolved per reviewer's recommendations — see §7.

Branch cut authorised: `phase3/alpha4-users-me-patch`.

---

## Section 10 — α4 Exit Criteria

α4 is complete when:

1. `PATCH /api/v1/users/me` is live, tested, and documented.
2. Every future authenticated-mutation endpoint has a clear reference
   implementation in `routers/users.py::update_me` and a documented
   pattern in `AUTH_ENDPOINTS.md`.
3. `UserPublic.version` is exposed and every response returning
   `UserPublic` includes it (register / login / refresh / GET /me
   / PATCH /me).
4. `IUserRepository.update_profile` is the canonical example of a
   version-fenced repository CAS.
5. CI gate 10/10 green with `import-linter` still 5/5 kept.

---

## Section 11 — Post-α4 Backlog Additions

Items surfaced during α4 planning that get logged for later slices,
not shipped now:

* **α5 candidate:** `POST /auth/change-password` with current-password
  re-authentication. Design conversation: does it also rotate the
  session family (defensive), or leave sessions alone (convenience)?
* **α5 candidate:** email-change flow with verification-token round trip.
* **α6 candidate:** `require_role(...)` dependency for the first
  RBAC-gated endpoint (probably `GET /users/{id}` or `GET /users`).
* **Ops:** DB audit-log table + trigger on `users.updated_at` — surface
  the value-level change trail the app logs deliberately don't carry
  (Q3).

---

## Section 12 — Implementation Order (once approved)

1. Cut branch `phase3/alpha4-users-me-patch` off fresh `main`; bump
   `app/main.py` version to `0.4.4-phase3-alpha4-dev` **without**
   creating empty package markers (reviewer refinement R6 — package
   `__init__.py` files land in step 5 alongside the use case they
   host).
2. Extend `IUserRepository` interface (`update_profile`).
3. Extend `UserRepository` with the SQL CAS.
4. Extend `FakeUserRepository` in `_fakes.py`.
5. Create `use_cases/users/` package + `update_profile.py` use case +
   unit tests (`test_update_profile.py`). Package markers are born
   alongside their first inhabitant.
6. Run `pytest -m unit` + mypy — must be green before touching HTTP.
7. Add `UpdateUserProfileRequest` DTO + `version` field on `UserPublic`.
8. Wire container + `UpdateUserProfileDep`.
9. Add `PATCH /me` handler.
10. Update `routers/auth.py::_to_payload` to include `version`.
11. Write H15–H24 integration tests; update H1.
12. Add repository integration tests (R1–R3).
13. Update `AUTH_ENDPOINTS.md` §7.1 (`PATCH /users/me`) + add §8
    (Canonical authenticated mutation flow — reviewer refinement R7).
14. Local CI gate 10/10.
15. Commit, push, PR.
16. Post-merge: tag `v0.4.4-phase3-alpha4`, close α4.
