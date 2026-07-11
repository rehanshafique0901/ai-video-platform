# Phase 3 Slice α5a — Read-Only Pre-Flight

> **Convention.** This document is a read-only planning artefact per
> `docs/engineering/RUNBOOK_WAVE.md` §1. It records scope, non-goals,
> design decisions, file inventory, acceptance criteria, test matrix,
> and CI impact for **Slice α5a** before any branch is cut or any code
> is written. Approve as-is (or push back) before implementation begins.
>
> **Status.** ✅ **Approved 2026-07-11** — all five open questions
> resolved per reviewer recommendations, two reviewer-added decisions
> folded in (D14 deterministic ordering, D15 project slug policy). Branch
> cut authorised (see §9).
>
> **Predecessors.**
> α1 (`v0.4.0-phase3-alpha1`, PR #8) — DI container + JWT + UoW,
> α2a (`v0.4.1-phase3-alpha2a`, PR #9) — register + login,
> α2b (`v0.4.2-phase3-alpha2b`, PR #10) — refresh + logout,
> α3 (`v0.4.3-phase3-alpha3`, PR #14, merge `02185cf`) — `CurrentUserDep` + `GET /users/me`,
> α4 (`v0.4.4-phase3-alpha4`, PR #15, merge `1462307`) — `PATCH /users/me` (canonical mutation),
> ADR-0034 (PR merged `c844aa6`) — authenticated endpoint pattern (D1 + D2).
>
> **Companion design doc.** `docs/domain/PROJECT_AGGREGATE.md` — the
> Project aggregate model (ownership, boundary, lifecycle, versioning).
> **Read it first**; this pre-flight assumes its scoping + invariants.
>
> **Baseline versioning.** `app/main.py` currently reads
> `"0.4.4-phase3-alpha4-dev"`. The first α5a commit bumps it to
> `"0.4.5-phase3-alpha5a-dev"`; the release tag `v0.4.5-phase3-alpha5a`
> drops the `-dev` suffix on merge.

---

## Section 1 — Scope

### 1.1 One-line thesis

α5a delivers the **first product aggregate** — `Project` — via its
**creation + read** surface: `POST /api/v1/projects`,
`GET /api/v1/projects`, and `GET /api/v1/projects/{id}`. It is the
first endpoint family that is neither auth nor self (`/users/me`), and
it establishes four patterns every future resource endpoint reuses:

1. **Tenant + owner scoping** — every query filtered by the caller's
   `tenant_id` **and** `owner_user_id` (from `CurrentUserDep`), with
   out-of-scope access reported as `404` (anti-enumeration).
2. **Resource creation** — `POST` → `201` with the created representation
   in the API_CONTRACT §1.1 envelope; duplicate-name → `409 CONFLICT`.
3. **Cursor pagination** — the list endpoint implements the
   API_CONTRACT §1.1/§6 cursor contract (`?cursor=&limit=`,
   `meta.next_cursor`) via keyset pagination.
4. **Reads through the application layer** — `GetProject` / `ListProjects`
   use cases own the scoping + not-found decisions; the router stays thin.

α5a reuses α3's `CurrentUserDep` seam verbatim and reuses ADR-0034 D1
(authentication) but **not** D2 (mutation/CAS) — creation is an insert,
not a version-fenced update. The `version` fence and same-value no-op
machinery from α4 return in **α5b** (`PATCH /projects/{id}`).

**Architectural invariant established by α5a:** *All future
owner-scoped resource endpoints follow the pattern introduced by
`/projects` — `CurrentUserDep` → DTO validation → use case that scopes
by `(tenant_id, owner_user_id)` and raises `NotFoundError` for
out-of-scope reads → representation in the API_CONTRACT §1.1 envelope,
with lists using keyset cursor pagination.*

### 1.2 What's in

1. **`Project` domain entity** (`app/domain/projects/project.py`) — a
   new `domain/projects/` package; frozen dataclass mirroring the
   `projects` row (see `PROJECT_AGGREGATE.md` §3/§5), framework-free.
2. **`IProjectRepository`** port (`app/application/interfaces/repositories.py`)
   with three methods:
   - `add(project) -> Project` — insert; raises `ConflictError` on the
     `(tenant_id, owner_user_id, name)` partial-unique violation.
   - `get_owned(project_id, tenant_id, owner_user_id) -> Project | None`
     — scoped single-row fetch (`deleted_at IS NULL`).
   - `list_owned(tenant_id, owner_user_id, limit, cursor) -> ProjectPage`
     — keyset-paginated, owner-scoped list.
3. **`ProjectRepository`** concrete SQLAlchemy adapter
   (`app/infrastructure/repositories/project_repository.py`).
4. **UoW wiring** — add `projects: IProjectRepository` to `IUnitOfWork`
   and populate it in `SqlAlchemyUnitOfWork.__aenter__`, the integration
   `client` fixture's `_TestUnitOfWork`, and `FakeUnitOfWork`.
5. **Use cases** (`app/application/use_cases/projects/` — new package):
   - `CreateProject` — generate `id`, build entity, `add`, commit; emit
     `project.created`.
   - `GetProject` — scoped fetch; raise `NotFoundError` if absent/out-of-scope.
   - `ListProjects` — scoped keyset page; decode/encode cursor.
6. **DTOs** (`app/api/v1/schemas/projects.py`): `ProjectCreateRequest`
   (`extra="forbid"`), `ProjectPublic` (includes `version`).
7. **Cursor helper** (`app/api/v1/pagination.py`) — `encode_cursor` /
   `decode_cursor` + a typed `Cursor` + a small `Page[T]` result shape.
   Minimal (~50 LOC), reusable by every future list endpoint.
8. **Router** (`app/api/v1/routers/projects.py`, prefix `/projects`,
   tag `projects`) with the three handlers; registered under `/api/v1`
   in `app/main.py`.
9. **DI**: container factories (`get_create_project_use_case`,
   `get_get_project_use_case`, `get_list_projects_use_case`) + deps
   aliases (`CreateProjectDep`, `GetProjectDep`, `ListProjectsDep`).
10. **`API_CONTRACT.md`** — no wire change (§3.2 already sketches these
    routes); add a note recording the α5a-shipped subset + the
    `NOT_FOUND` (not `PROJECT_NOT_FOUND`) code decision (§D9).
11. **`CHANGELOG.md`** `[Unreleased]` entry following the α4 template.
12. **Version bump** in `app/main.py` (see baseline versioning above).
13. **`ROADMAP.md`** Phase 3 row updated with the α5a status line.

### 1.3 Non-goals (explicit, will NOT ship in α5a)

* **`PATCH /projects/{id}`** — the update path (version-fenced CAS,
  same-value no-op, partial-update DTO) is **α5b**. This is where
  ADR-0034 D2 returns.
* **`DELETE /projects/{id}`** — soft delete + the archived/soft-deleted
  lifecycle transitions are **α5b**.
* **`POST /projects/{id}/duplicate`** and **`/autosave`** — later slices.
* **Project versions (CR-6)** — the `project_versions` snapshot ledger,
  `current_version_id` management, restore/diff. New projects leave
  `current_version_id` NULL.
* **Folder assignment** — `folder_id` is left NULL at create; move-to-folder
  and folder-ownership validation are α5b+.
* **Tags** — `project_tags` join, tag creation, `?tag=` filter. Later.
* **List filters** `?folder_id`, `?tag`, `?query`** — α5a lists all of the
  caller's live projects, newest first, with cursor paging only. Filtering
  arrives with folders/tags/search.
* **Tenant-wide (shared) visibility** — α5a is strictly owner-scoped.
  Broadening the owner filter for multi-user tenants needs RBAC (α6+).
* **New DB migrations** — the `projects` table already exists (baseline
  `0001`). α5a is application-layer only, like α3.
* **Rate limiting / idempotency-key** — same posture as α3/α4.
* **`duration_seconds`** — server-derived from content later; not a
  create input.

### 1.4 Anti-scope-creep envelope

If any of the following show up in review, push back:

* *"While we're adding the repo, add `update`/`delete` too."* — No, that's
  α5b by explicit design (keeps OCC/soft-delete/cascade in their own slice,
  mirroring the α2a/α2b split).
* *"Build a generic paginated-list framework / `ListEndpoint[T]` base."* —
  No. Ship the minimal `pagination.py` helper; generalise after two or
  three real list endpoints share shape.
* *"Add `?folder_id`/`?tag`/`?query` now."* — No. Those need folders/tags/
  search aggregates that don't exist yet.
* *"Let list return the whole tenant's projects."* — No. Owner-scoped in
  v1; tenant-wide needs a role check (α6+).
* *"Use offset pagination, it's simpler."* — No. API_CONTRACT §1.1/§6
  pins cursor-based; offset drifts under concurrent inserts.

---

## Section 2 — Design Decisions

### D1: Aggregate design doc precedes code
**Decision:** `docs/domain/PROJECT_AGGREGATE.md` is authored and approved
**before** α5a code. **Rationale:** Projects are the root every future
aggregate hangs off; pinning ownership, boundary, lifecycle, and the two
versioning mechanisms up front prevents each child slice from
re-deriving (and diverging on) the scoping model. It is the Projects-side
`AUTH_ENDPOINTS.md`.

### D2: New `domain/projects/` package
**Decision:** Add `app/domain/projects/project.py` (frozen `Project`
dataclass), not a field on any identity entity. **Rationale:** Projects
are a distinct bounded context from identity. Mirrors the α4 D1 rationale
for putting profile updates under `use_cases/users/` rather than `auth/`.

### D3: Use-case-per-operation (reads included)
**Decision:** Three use cases — `CreateProject`, `GetProject`,
`ListProjects` — each taking a UoW. Reads are **not** done inline in the
router (unlike α3's `GET /users/me`, where `CurrentUserDep` already *is*
the read). **Rationale:** Project reads carry real business logic
(tenant+owner scoping, the 404-vs-return decision, cursor
encode/decode). That belongs in the application layer, keeping the router
a thin envelope+projection shell and keeping `import-linter` boundaries
crisp. `GET /users/me` stays the exception because "the authenticated
user" is already resolved by the dependency.

### D4: Targeted, scoped repository methods
**Decision:** `add`, `get_owned(project_id, tenant_id, owner_user_id)`,
`list_owned(tenant_id, owner_user_id, limit, cursor)`. Scoping arguments
are explicit parameters, not implicit. **Rationale:** Follows α4 D2
(targeted over generic). The repository answers persistence questions
only; it does not read `CurrentUserDep`. The use case passes the caller's
identity in, so the "what scope?" question is answered in the application
layer and is trivially testable with the fake.

### D5: Tenant + owner scoping, 404 for out-of-scope
**Decision:** All three operations scope by **both** `tenant_id` and
`owner_user_id` from the caller. `GetProject` on a project that is
missing **or** outside the caller's scope raises `NotFoundError` → `404`
(never `403`). **Rationale:** `PROJECT_AGGREGATE.md` §2/§7 — anti-
enumeration parity with α3. Deriving scope from the token (never the
body/path) is the security-critical invariant of this slice.

### D6: Cursor pagination — keyset, opaque, base64
**Decision:** List orders by `(created_at DESC, id DESC)` and paginates
by **keyset**, not offset. The cursor is an **opaque** base64url token
encoding the last-seen `(created_at, id)`; the client treats it as
opaque and echoes it in `?cursor=`. `meta.next_cursor` is present iff a
further page exists (detected by fetching `limit + 1` rows), absent on
the last page.

- **Encoding:** `base64url(json({"v":1,"created_at":<iso8601>,"id":<uuid>}))`.
  A `v` field allows the shape to evolve without breaking old clients.
- **Query:** `WHERE (created_at, id) < (:c_created, :c_id)` (with the
  scope filters), `ORDER BY created_at DESC, id DESC LIMIT :limit + 1`.
- **`limit`:** default `20`, validated `1 ≤ limit ≤ 100` (API_CONTRACT
  max 100) → out-of-range is `422` (see Q2).
- **Malformed / undecodable cursor** → `422 VALIDATION_FAILED`.

**Rationale:** Keyset is stable under concurrent inserts (offset
double-counts/skips); it matches the API_CONTRACT §6 example verbatim;
`(created_at, id)` is a total order (id breaks created_at ties). The
`id DESC` tiebreak is why a plain `created_at` index is insufficient — see
E-note in §4.3.

### D7: Create request surface (α5a)
**Decision:** `ProjectCreateRequest` (`extra="forbid"`) accepts:

| Field | Type | Required | Default | Validation |
|---|---|---|---|---|
| `name` | `str` | ✅ | — | strip whitespace; `1 ≤ len ≤ 200` |
| `aspect_ratio` | `Literal["horizontal","vertical","square"]` | ✅ | — | enum (clean 422 before the DB CHECK) |
| `description` | `str \| None` | ✗ | `None` | `len ≤ 2000` |
| `language` | `str` | ✗ | `"en"` | `1 ≤ len ≤ 8` |
| `style` | `str \| None` | ✗ | `None` | `len ≤ 100` |
| `settings` | `dict[str, Any]` | ✗ | `{}` | must be a JSON object |

`tenant_id`, `owner_user_id`, `id`, `version`, timestamps,
`current_version_id`, `folder_id`, `duration_seconds` are **not** accepted
(`extra="forbid"` rejects them with 422). **Rationale:** whitelist the
minimum coherent create surface; everything server-derived or deferred is
excluded at the DTO boundary (same discipline as α4's
`UpdateUserProfileRequest`).

### D8: `ProjectPublic` response DTO
**Decision:** Expose `id`, `tenant_id`, `owner_user_id`, `folder_id`,
`name`, `description`, `aspect_ratio`, `language`, `style`, `settings`,
`created_at`, `updated_at`, `version`. Omit `current_version_id`
(internal pointer, NULL until versioning) and `duration_seconds` (NULL
until content exists) — or expose them as always-null; **decision: omit**
in α5a to avoid promising fields the slice never populates.
**Rationale:** `version` is exposed for the same reason as α4's
`UserPublic.version` — α5b's `PATCH` needs the client to round-trip it as
the OCC fence. Explicit-field Pydantic (not `exclude`) gives compile-time
control over the projection.

### D9: Not-found error code — `NOT_FOUND`, not `PROJECT_NOT_FOUND`
**Decision:** Use the existing `NotFoundError` (`code = "NOT_FOUND"`,
404) from `app/core/errors.py`, with `details = {"project_id": ...}`.
**Rationale:** API_CONTRACT §1.2's canonical initial code set lists
generic `NOT_FOUND`; the `PROJECT_NOT_FOUND` string in the §1.1 *example*
is illustrative, not a second code. Introducing per-resource codes would
fork the taxonomy. Record this reconciliation as a one-line note in
`API_CONTRACT.md`.

### D10: Duplicate name → `409 CONFLICT`
**Decision:** The partial-unique index
`uq_projects_tenant_id_owner_user_id_name` (`WHERE deleted_at IS NULL`)
is the authoritative gate. On `IntegrityError`, the repository raises
`ConflictError` (→ 409) with the offending constraint name in `details`
— identical shape to `UserRepository.add`. No pre-check SELECT (TOCTOU
avoided; the DB constraint is the race-free arbiter). **Rationale:**
mirrors the α2a register conflict handling exactly.

### D11: No `version` fence on create
**Decision:** Creation is a plain insert; `version` is DB-defaulted to
`1`. No CAS, no `expected_version` input. **Rationale:** there is nothing
to fence against on an insert — OCC applies to updates (α5b).

### D12: `settings` validation depth
**Decision:** α5a validates only that `settings` is a JSON **object**
(`dict`), stored verbatim as JSONB. No key/shape schema. **Rationale:**
project-level settings schema is a product concern that will grow with
the editor; over-constraining now would churn. A typed settings schema is
a future slice.

### D13: Single checkpoint (with a named split rule)
**Decision:** All α5a files land in one commit / one PR. **Rationale:**
comparable surface to α3/α4 single-checkpoint slices. **Escape hatch
(reviewer-confirmed):** if the change set exceeds roughly **900–1000
lines changed**, split at the domain+repo / HTTP boundary into two
commits:
* **α5a.1** — domain entity + `IProjectRepository` + `ProjectRepository`
  + UoW/fakes wiring + use cases + their unit tests.
* **α5a.2** — DTOs + `pagination.py` + router + DI + repo/HTTP
  integration tests + docs.

Below that threshold, keep it as one slice.

### D14: Deterministic ordering guarantee (reviewer-added)
**Decision:** Project listings **SHALL** be ordered by
`created_at DESC, id DESC` to guarantee stable cursor pagination. The
secondary `id DESC` key is a hard requirement, not a nicety: when two
projects share a `created_at` timestamp, `id` provides the total order
the keyset cursor relies on, so no row is duplicated or skipped across
page boundaries. **Rationale:** promotes the ordering from an
implementation detail in D6 to an explicit, testable contract (asserted
by R7 / P14). A `created_at`-only sort would be non-deterministic under
timestamp ties and would silently corrupt pagination.

### D15: No project slug in α5a — UUID addressing + documented future policy (reviewer-added)
**Decision:** Projects are addressed by **UUID**, per API_CONTRACT §1
("IDs: UUIDv4. URLs use the ID as-is (no slugs)"). The `projects` table
has **no `slug` column** and α5a introduces none — `GET /projects/{id}`
takes a UUID path param. **Rationale:** the contract already mandates
UUID addressing; adding a slug now would be scope creep with a migration.

**Documented future policy (recorded now to avoid a later migration
surprise — see `PROJECT_AGGREGATE.md` §2.1):** if a human-readable slug
is ever introduced (cosmetic pretty-URL feature), the recommended policy
is:
* **Derivation:** slugify(`name`) → e.g. `"My First Video"` → `my-first-video`.
* **Uniqueness:** **per `(tenant_id, owner_user_id)`**, mirroring the
  existing `name` uniqueness constraint (not global, not tenant-wide).
* **Mutability:** **regenerated when `name` changes** (mutable). No
  historical-slug redirect table in v1 — canonical addressing stays the
  UUID, so a changed slug never breaks a stored link.
* **Migration:** additive nullable `slug` column + a partial-unique index
  matching the `name` index predicate. Its own slice, not α5a.

---

## Section 3 — Acceptance Criteria

### 3.1 Behavioural (A1–A16)

**A1. Create happy path.** `POST /api/v1/projects` with a valid access
token and a valid body returns `201` with `ProjectPublic` (server `id`,
`version == 1`, `owner_user_id`/`tenant_id` from the token, echoed
`name`/`aspect_ratio`/etc.).

**A2. Create envelope.** Response is `{ "data": ProjectPublic, "meta": {
"request_id": ... } }`.

**A3. Create — server-owned fields ignored.** A body containing
`tenant_id`, `owner_user_id`, `id`, `version`, `created_at`,
`current_version_id`, or any non-whitelisted key returns `422
VALIDATION_FAILED` (`extra="forbid"`).

**A4. Create — field validation.** Missing `name`, empty/whitespace
`name`, `name` > 200 chars, missing `aspect_ratio`, `aspect_ratio` not in
the enum, non-object `settings`, `language` > 8 chars → `422`.

**A5. Duplicate name.** Creating a second live project with a `name` that
already exists for the same `(tenant, owner)` returns `409 CONFLICT`. A
name equal to a **soft-deleted** project's name succeeds (partial index).

**A6. Get happy path.** `GET /projects/{id}` for a project the caller
owns returns `200` with the matching `ProjectPublic`.

**A7. Get — not found.** `GET /projects/{unknown_uuid}` returns `404
NOT_FOUND`.

**A8. Get — out-of-scope is 404.** `GET /projects/{id}` for a project in
another tenant **or** owned by another user returns `404` (never `403`,
never `200`) — anti-enumeration.

**A9. Get — malformed id.** A non-UUID path segment returns `422`
(FastAPI path validation).

**A10. List happy path.** `GET /projects` returns `200` with `data` = an
array of the caller's live projects, newest-first, and
`meta.request_id`.

**A11. List — owner/tenant scoped.** The list contains only projects with
`tenant_id == caller.tenant_id AND owner_user_id == caller.id`; other
users'/tenants' projects never appear.

**A12. List — soft-deleted excluded.** A soft-deleted project does not
appear in the list.

**A13. List — cursor pagination.** With N > limit projects, the first
page returns `limit` items + a `meta.next_cursor`; fetching with that
cursor returns the next page; the final page omits `next_cursor`. No item
appears twice and none is skipped across pages.

**A14. List — limit bounds.** `?limit=0`, `?limit=101`, `?limit=-1`, or a
non-integer `limit` returns `422`. Absent `limit` defaults to 20.

**A15. List — malformed cursor.** A `?cursor=` value that is not a valid
encoded cursor returns `422 VALIDATION_FAILED`.

**A16. Auth surface parity.** All three endpoints reject every α3
rejection branch (`missing_header`, `malformed_header`, `verify_failed`,
`sid_missing_session`, `session_revoked`, `session_expired`,
`sid_user_gone`) with the same generic `401` — `CurrentUserDep` reused
unchanged.

### 3.2 Engineering (E1–E5)

**E1.** CI gate 10/10 green (all prior gates preserved, no new stages, no
new migration).

**E2.** No new `noqa`, `type: ignore`, or coverage overrides (beyond the
one pre-existing `type: ignore[arg-type]` in `errors.py`).

**E3.** Unit-only coverage ≥ 80% total (holds the ~80.67% baseline within
a ~1 pp drift budget). New use-case + pagination-helper unit tests keep it
above 80.5%.

**E4.** `import-linter` contracts stay 5/5 kept: `domain/projects` imports
nothing from application/infrastructure/api; `use_cases/projects` imports
neither `api/` nor `infrastructure/`; `api/` reaches infrastructure only
via the container.

**E5.** No ERD/schema drift (stage 9) — α5a adds no tables, columns, or
constraints; the `projects` table is unchanged from baseline.

---

## Section 4 — File Inventory

### 4.1 New files

| Path | LOC est. | Purpose |
|---|---:|---|
| `backend/app/domain/projects/__init__.py` | 1 | Package marker (born with `project.py`) |
| `backend/app/domain/projects/project.py` | ~35 | Frozen `Project` entity |
| `backend/app/application/use_cases/projects/__init__.py` | 1 | Package marker |
| `backend/app/application/use_cases/projects/create_project.py` | ~70 | `CreateProject` use case |
| `backend/app/application/use_cases/projects/get_project.py` | ~45 | `GetProject` use case |
| `backend/app/application/use_cases/projects/list_projects.py` | ~70 | `ListProjects` use case (cursor) |
| `backend/app/infrastructure/repositories/project_repository.py` | ~140 | SQLAlchemy `ProjectRepository` |
| `backend/app/api/v1/schemas/projects.py` | ~90 | `ProjectCreateRequest` + `ProjectPublic` |
| `backend/app/api/v1/pagination.py` | ~60 | Cursor encode/decode + `Page` |
| `backend/app/api/v1/routers/projects.py` | ~120 | The three handlers |
| `backend/tests/unit/application/use_cases/projects/__init__.py` | 1 | Package marker |
| `backend/tests/unit/application/use_cases/projects/test_create_project.py` | ~120 | Create use-case unit tests |
| `backend/tests/unit/application/use_cases/projects/test_get_project.py` | ~70 | Get use-case unit tests |
| `backend/tests/unit/application/use_cases/projects/test_list_projects.py` | ~130 | List/cursor use-case unit tests |
| `backend/tests/unit/api/test_pagination.py` | ~70 | Cursor helper unit tests (encode/decode round-trip, tamper→422) |
| `backend/tests/integration/infrastructure/repositories/test_project_repository.py` | ~150 | Repo integration (add/get_owned/list_owned + scoping) |
| `backend/tests/integration/api/test_projects.py` | ~320 | HTTP integration (P1–P16 below) |

### 4.2 Modified files

| Path | Change | LOC est. |
|---|---|---:|
| `backend/app/application/interfaces/repositories.py` | Add `IProjectRepository` ABC (+ import `Project`) | +45 |
| `backend/app/application/interfaces/unit_of_work.py` | Add `projects: IProjectRepository` attribute + import | +3 |
| `backend/app/infrastructure/uow/sqlalchemy_unit_of_work.py` | Wire `self.projects = ProjectRepository(...)` in `__aenter__` | +3 |
| `backend/app/core/container.py` | 3 use-case factories + import | +30 |
| `backend/app/api/v1/deps.py` | `CreateProjectDep` / `GetProjectDep` / `ListProjectsDep` | +12 |
| `backend/app/main.py` | Register `projects.router` under `/api/v1`; version bump | +2 |
| `backend/tests/unit/application/use_cases/auth/_fakes.py` | Add `FakeProjectRepository` + wire into `FakeUnitOfWork` | +55 |
| `backend/tests/integration/conftest.py` | Wire `projects` into `_TestUnitOfWork.__aenter__` | +2 |
| `API_CONTRACT.md` | Note α5a-shipped subset of §3.2 + `NOT_FOUND` code reconciliation (D9) | +6 |
| `CHANGELOG.md` | `[Unreleased]` α5a entry | +30 |
| `ROADMAP.md` | Phase 3 row α5a status line | +1 |

> **Fakes note (load-bearing).** `_fakes.py::FakeUnitOfWork` and
> `integration/conftest.py::_TestUnitOfWork` both hard-list the four
> existing repositories. Adding `uow.projects` **requires** editing both
> or every project test fails at `uow.projects` attribute access. This is
> the single easiest thing to forget in this slice.

### 4.3 Deliberately NOT touched

* `backend/alembic/**` — no schema change (E5).
* `backend/app/infrastructure/db/models/projects.py` — the ORM row model
  already has every column α5a needs (`version`, `deleted_at`, the partial
  unique index). No mutation.
* `backend/app/core/errors.py` — `NotFoundError` / `ConflictError` already
  exist; no new error classes.
* `backend/app/api/v1/routers/{auth,users,health}.py` — untouched.
* `docs/api/AUTH_ENDPOINTS.md`, `docs/decisions/ADR-0034-*.md` — closed.

> **Index E-note.** The keyset order `(created_at DESC, id DESC)` is
> served acceptably by the existing `ix_projects_tenant_id_owner_user_id`
> for the scoped prefix; `created_at` ordering is a sort on the scoped
> subset (small per-owner in v1). If per-owner project counts ever grow
> large, a `(tenant_id, owner_user_id, created_at DESC, id DESC)` index is
> the follow-up — logged to backlog (§11), **not** added in α5a (no
> migration).

---

## Section 5 — Test Matrix

### 5.1 Unit — use cases (fakes, no DB)

| # | Case | Assertion |
|---|---|---|
| U1 | `CreateProject` happy | Returns `Project` with generated `id`, `version==1`, caller tenant/owner; `commit()` called once; `project.created` logged |
| U2 | `CreateProject` duplicate name | Fake repo raises `ConflictError`; use case propagates; no commit |
| U3 | `CreateProject` defaults | `language` defaults `en`, `settings` `{}`, `folder_id`/`description`/`style` None |
| U4 | `GetProject` owned | Returns the entity |
| U5 | `GetProject` missing | Raises `NotFoundError` |
| U6 | `GetProject` other owner / other tenant | Raises `NotFoundError` (scope enforced by the args passed to the fake) |
| U7 | `ListProjects` first page | Returns ≤ limit items, newest-first, `next_cursor` present when more exist |
| U8 | `ListProjects` last page | `next_cursor` is None |
| U9 | `ListProjects` scoping | Only caller's rows returned |
| U10 | `ListProjects` bad cursor | Raises `ValidationFailedError` |

### 5.2 Unit — pagination helper

| # | Case | Assertion |
|---|---|---|
| C1 | encode→decode round-trip | Recovers `(created_at, id)` exactly |
| C2 | tampered / non-base64 cursor | `decode_cursor` raises → mapped to 422 |
| C3 | wrong version prefix | Rejected cleanly |

### 5.3 Repository integration (real DB, SAVEPOINT rollback)

| # | Case | Assertion |
|---|---|---|
| R1 | `add` happy | Row present with `version==1`, `deleted_at IS NULL` |
| R2 | `add` duplicate live name | `ConflictError`; constraint name in details |
| R3 | `add` name reused after soft-delete | Succeeds |
| R4 | `get_owned` match | Returns entity |
| R5 | `get_owned` wrong owner/tenant | Returns None |
| R6 | `get_owned` soft-deleted | Returns None |
| R7 | `list_owned` newest-first + keyset page | Correct order; page boundaries exact; scope honoured |

### 5.4 HTTP integration — `test_projects.py` (P1–P16)

Maps 1:1 to the α3.1/α4 authenticated-request test style (register →
obtain access token → call endpoint):

| # | Case | Assertion |
|---|---|---|
| P1 | Create happy | 201, `ProjectPublic`, `version==1` |
| P2 | Create envelope shape | `data`+`meta.request_id` |
| P3 | Create with server-owned field (`tenant_id`) | 422 |
| P4 | Create missing `name` / bad `aspect_ratio` | 422 |
| P5 | Create duplicate live name | 409 CONFLICT |
| P6 | Create no auth | 401 (generic) |
| P7 | Get owned | 200, matches created |
| P8 | Get unknown id | 404 NOT_FOUND |
| P9 | Get other user's project | 404 (create as user B, read as user A) |
| P10 | Get non-UUID path | 422 |
| P11 | Get no auth | 401 |
| P12 | List owned, newest-first | 200, correct order |
| P13 | List excludes other user's + soft-deleted | scope honoured |
| P14 | List cursor paging round-trip | pages tile the full set, no dup/skip |
| P15 | List bad `limit` (0/101/neg/non-int) + bad cursor | 422 |
| P16 | Create → Get round-trip consistency | bodies deep-equal |

---

## Section 6 — Structured-Log Catalogue (α5a additions)

| Event | Level | Fields |
|---|---|---|
| `project.created` | INFO | `project_id`, `owner_user_id`, `tenant_id`, `aspect_ratio`, `ip`, `request_id` |
| `project.create_rejected` | WARN | `reason` (`duplicate_name`), `owner_user_id`, `tenant_id`, `ip`, `request_id` |

* No project `name`/`description`/`settings` **values** in logs (field
  names / ids only — same GDPR-minimal posture as α4 Q3).
* Reads (`GET`) are not logged at INFO per-request beyond the existing
  `auth.request.authenticated` from `CurrentUserDep` (avoids log spam on
  list/get hot paths).

---

## Section 7 — Open Questions (all resolved 2026-07-11)

### Q1: Owner-scoped vs tenant-scoped list/get?
**Resolution: ✅ owner-scoped** (`tenant_id AND owner_user_id`). Reviewer
agreed: safest starting point; authorization can be extended for
collaborative projects later without breaking the model. Locked in D5.

### Q2: `limit` out of range — 422 or silent clamp?
**Resolution: ✅ 422** (Pydantic `Query(ge=1, le=100)`). Reviewer agreed:
silently rewriting `limit=5000 → 100` is hidden behaviour; validation
belongs at the API boundary. Locked in D6 + A14.

### Q3: `aspect_ratio` — required or default to `horizontal`?
**Resolution: ✅ required** (`Literal`, no default). Reviewer agreed: a
hidden default becomes technical debt fast as the format set grows
(21:9, 4:5, custom); explicit input is better. Locked in D7.

### Q4: Expose `current_version_id` / `duration_seconds` as always-null?
**Resolution: ✅ omit both** from `ProjectPublic`. Reviewer agreed:
expose only what the client needs; adding fields later is backwards
compatible, removing them isn't. Locked in D8.

### Q5: Cursor ordering — `created_at DESC` (newest-first) confirmed?
**Resolution: ✅ newest-first, `created_at DESC, id DESC`.** Reviewer
agreed and asked that the deterministic secondary key be recorded as an
explicit decision — done in **D14**. Locked in D6 + D14.

---

## Section 8 — Anti-Scope-Creep Reminders

Same envelope as α4 §8. Repeated for muscle memory:

* No `PATCH`/`DELETE` on `/projects` (that's α5b).
* No folders, tags, or list filters.
* No project-versions ledger / `current_version_id`.
* No new DB migrations.
* No generic pagination/list framework beyond the minimal helper.
* No middleware, no RBAC, no rate limiting.
* No refactoring of unrelated code paths.

If a reviewer suggests any of the above, defer to a later slice.

---

## Section 9 — Reviewer Sign-Off

**Reviewer verdict — 2026-07-11:** ✅ **Approved.** All five open
questions resolved per the pre-flight recommendations; two reviewer-added
decisions folded in. Branch cut authorised.

Resolutions (see §7 for detail):

* ✅ **Q1 — owner-scoped** (`tenant_id AND owner_user_id`) — D5.
* ✅ **Q2 — 422 on invalid `limit`** (`1 ≤ limit ≤ 100`) — D6 / A14.
* ✅ **Q3 — `aspect_ratio` required** (no hidden default) — D7.
* ✅ **Q4 — omit `current_version_id` / `duration_seconds`** from
  `ProjectPublic` — D8.
* ✅ **Q5 — newest-first ordering** — D6 / D14.

Reviewer-added decisions:

* **D14 — Deterministic ordering:** project listings SHALL be ordered by
  `created_at DESC, id DESC` to guarantee stable cursor pagination
  (secondary `id` key is a hard requirement, not a nicety).
* **D15 — Project slug policy:** α5a introduces no slug (UUID addressing
  per API_CONTRACT §1); the recommended *future* slug policy is
  documented now (per-owner unique, mutable-on-rename, additive
  migration) to avoid a later surprise. See also `PROJECT_AGGREGATE.md`
  §2.1.
* **D13 split rule confirmed:** if the change set exceeds ~900–1000 lines,
  split into α5a.1 (domain + repo) and α5a.2 (API + HTTP tests);
  otherwise ship as one slice.

Branch cut authorised: `phase3/alpha5a-projects-create-read`.

---

## Section 10 — α5a Exit Criteria

α5a is complete when:

1. `POST /projects`, `GET /projects`, `GET /projects/{id}` are live,
   tested, and reflected in `API_CONTRACT.md`'s shipped-subset note.
2. Every future owner-scoped resource endpoint has a reference
   implementation in `routers/projects.py` + `use_cases/projects/` and a
   documented model in `docs/domain/PROJECT_AGGREGATE.md`.
3. Cursor pagination has a reusable helper (`api/v1/pagination.py`) with
   unit tests — the template every future list endpoint copies.
4. Tenant+owner scoping with 404-anti-enumeration is proven by P8/P9/P13.
5. CI gate 10/10 green; `import-linter` 5/5 kept; no new migration.

---

## Section 11 — Post-α5a Backlog

* **α5b:** `PATCH /projects/{id}` (ADR-0034 D2 CAS on `projects.version`,
  same-value no-op, partial-update DTO) + `DELETE /projects/{id}` (soft
  delete + archived/soft-deleted lifecycle).
* **α5b+:** move-to-folder (`folder_id` + folder-ownership validation),
  tags + `?tag=`, `?folder_id=`/`?query=` list filters.
* **α6 candidate:** media assets under a project (`POST/GET
  /projects/{id}/assets`) — first child aggregate.
* **CR-6 slice:** `project_versions` ledger + `current_version_id` +
  restore/diff.
* **Perf:** `(tenant_id, owner_user_id, created_at DESC, id DESC)` index
  if per-owner project counts grow (see §4.3 E-note).
* **Product:** `duplicate` + `autosave` endpoints.

---

## Section 12 — Implementation Order (once approved)

1. Cut `phase3/alpha5a-projects-create-read` off fresh `main`; bump
   `app/main.py` to `0.4.5-phase3-alpha5a-dev` (no empty package markers
   ahead of their inhabitants — α4 R6).
2. Add `Project` domain entity (`domain/projects/`).
3. Add `IProjectRepository` port + `projects` on `IUnitOfWork`.
4. Implement `ProjectRepository` (add / get_owned / list_owned keyset).
5. Wire `projects` into `SqlAlchemyUnitOfWork`, `FakeUnitOfWork`, and the
   integration `_TestUnitOfWork` (the §4.2 fakes note).
6. Add `pagination.py` helper + its unit tests.
7. Create `use_cases/projects/` (create/get/list) + unit tests.
8. Run `pytest -m unit` + mypy — green before touching HTTP.
9. Add `ProjectCreateRequest` + `ProjectPublic` DTOs.
10. Wire container factories + deps aliases.
11. Add `routers/projects.py` (3 handlers); register in `main.py`.
12. Write repo integration tests (R1–R7) + HTTP integration (P1–P16).
13. Update `API_CONTRACT.md` note, `CHANGELOG.md`, `ROADMAP.md`.
14. Local CI gate 10/10.
15. Commit, push, PR.
16. Post-merge: tag `v0.4.5-phase3-alpha5a`, close α5a; open α5b pre-flight.
