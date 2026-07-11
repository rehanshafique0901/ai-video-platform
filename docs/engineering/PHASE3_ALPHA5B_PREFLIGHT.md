# Phase 3 Slice α5b — Read-Only Pre-Flight

> **Convention.** This document is a read-only planning artefact per
> `docs/engineering/RUNBOOK_WAVE.md` §1. It records scope, non-goals,
> design decisions, file inventory, acceptance criteria, test matrix,
> and CI impact for **Slice α5b** before any branch is cut or any code
> is written. Approve as-is (or push back) before implementation begins.
>
> **Status.** ✅ **Approved 2026-07-11** — all six open questions (§7)
> resolved per the pre-flight recommendations; no reviewer-added
> decisions. Branch cut authorised (see §9). Scope: PATCH update +
> soft-DELETE together, soft-delete semantics, M3 index folded in.
>
> **Predecessors.**
> α1 (`v0.4.0-phase3-alpha1`, PR #8) — DI container + JWT + UoW,
> α2a (`v0.4.1-phase3-alpha2a`, PR #9) — register + login,
> α2b (`v0.4.2-phase3-alpha2b`, PR #10) — refresh + logout,
> α3 (`v0.4.3-phase3-alpha3`, PR #14) — `CurrentUserDep` + `GET /users/me`,
> α4 (`v0.4.4-phase3-alpha4`, PR #15) — `PATCH /users/me` (canonical mutation, ADR-0034 D2),
> α5a (`v0.4.5-phase3-alpha5a`, PR #18, merge `154cb25`) — `POST/GET /projects` (create + read),
> hygiene (PR #19, merge `3e3f8b2`) — centralized `client_ip` + success `envelope` helpers (M1/M2).
>
> **Companion design doc.** `docs/domain/PROJECT_AGGREGATE.md` — the
> Project aggregate model (ownership, boundary, **lifecycle**, versioning).
> α5b implements the aggregate's **mutate** and **soft-delete** lifecycle
> transitions; re-read its lifecycle section before implementation.
>
> **Reference implementations to mirror.**
> * `PATCH` CAS → `app/infrastructure/repositories/user_repository.py::update_profile`
>   + `app/application/use_cases/users/update_profile.py` (α4 canonical mutation).
> * Owner+tenant scoping / 404-anti-enumeration → `GetProject` /
>   `ProjectRepository.get_owned` (α5a D5).
>
> **Baseline versioning.** `app/main.py` currently reads
> `"0.4.5-phase3-alpha5a-dev"`. The first α5b commit bumps it to
> `"0.4.6-phase3-alpha5b-dev"`; the release tag `v0.4.6-phase3-alpha5b`
> drops the `-dev` suffix on merge.

---

## Section 1 — Scope

### 1.1 One-line thesis

α5b **completes the Project CRUD lifecycle** by adding the two write
operations α5a deferred: `PATCH /api/v1/projects/{id}` (partial,
version-fenced update) and `DELETE /api/v1/projects/{id}` (owner-scoped
soft delete). It brings back **ADR-0034 D2** (optimistic-concurrency CAS)
for a *path-addressed* resource — the first mutation where the target may
legitimately be **not visible to the caller** — and it ships the **M3**
composite pagination index (deferred from α5a §4.3) in the same, now
meaningful, migration.

α5b reuses, unchanged: α3's `CurrentUserDep` seam; α5a's owner+tenant
scoping + 404-anti-enumeration (D5); the α5a `ProjectPublic` projection;
and the α4 CAS-on-`version` / same-value-no-op discipline. It establishes
**one new pattern**: the **404-vs-412 split** for path-addressed
authenticated mutations (§D3), which every future
`PATCH /resource/{id}` inherits.

**Architectural invariant established by α5b:** *A path-addressed
authenticated mutation first establishes caller visibility of the
resource (missing / out-of-scope / soft-deleted → `404`, exactly like the
read path), and only then applies the version fence (`412` on stale
`version`). Visibility is decided before concurrency — a caller can never
learn a resource exists via a `412`.*

### 1.2 What's in

1. **`PATCH /api/v1/projects/{id}`** — partial update of the caller's own
   live project, version-fenced (ADR-0034 D2). Returns `200` with the
   updated `ProjectPublic`; `412 VERSION_CONFLICT` on stale `version`;
   `404 NOT_FOUND` if not owned / missing / soft-deleted; `409 CONFLICT`
   on a rename that collides with another live project; `422` on DTO
   validation.
2. **`DELETE /api/v1/projects/{id}`** — soft delete (set `deleted_at =
   now()`) of the caller's own live project. Idempotent-by-404: first
   call succeeds; subsequent calls (and GET/PATCH) return `404`.
3. **`ProjectUpdateRequest` DTO** (`extra="forbid"`) — partial-update
   surface with tri-state field semantics (absent = leave; explicit
   `null` = clear a nullable field) + a required `version` fence.
4. **`IProjectRepository` extensions**:
   - `update_owned(project_id, tenant_id, owner_user_id, expected_version, changes) -> UpdateOutcome`
   - `soft_delete_owned(project_id, tenant_id, owner_user_id) -> bool`
5. **`ProjectRepository`** concrete implementations of both, mirroring
   `UserRepository.update_profile`'s CAS + `IntegrityError`→`ConflictError`.
6. **Use cases** (`app/application/use_cases/projects/`):
   - `UpdateProject` — fetch-then-fence (§D3), partial-apply, CAS, commit;
     emit `project.updated` / `project.update_rejected`.
   - `DeleteProject` — scoped soft delete; emit `project.deleted` /
     `project.delete_rejected`.
7. **M3 migration `0008`** — add composite partial index
   `ix_projects_owner_created_id`
   `(tenant_id, owner_user_id, created_at DESC, id DESC) WHERE deleted_at IS NULL`
   to `Project.__table_args__` **and** an Alembic migration (upgrade =
   create, downgrade = drop). Serves `list_owned`'s scoped keyset scan.
8. **DI**: container factories (`get_update_project_use_case`,
   `get_delete_project_use_case`) + deps aliases (`UpdateProjectDep`,
   `DeleteProjectDep`).
9. **Router** — two handlers added to the existing
   `app/api/v1/routers/projects.py`; no new router module.
10. **Fakes** — extend `FakeProjectRepository` (`update_owned` +
    `soft_delete_owned`) so unit tests exercise both write paths.
11. **`API_CONTRACT.md`** — annotate §3.2 with the α5b-shipped subset
    (PATCH/DELETE semantics, idempotent-delete-by-404, tri-state PATCH).
12. **`CHANGELOG.md`** `[Unreleased]` α5b entry; **version bump**;
    **`ROADMAP.md`** Phase 3 row updated.

### 1.3 Non-goals (explicit, will NOT ship in α5b)

* **Restore / un-delete endpoint** (`POST /projects/{id}/restore`) — soft
  delete is one-way in α5b; restore is a later slice.
* **Hard delete / purge** — no row removal, ever, in this slice.
* **Bulk delete / bulk update** — single-resource operations only.
* **Archive (distinct from delete)** — no separate `archived_at` state;
  the lifecycle in α5b is `live → soft-deleted` only.
* **Rename history / slug** — no slug column (α5a D15 stands); no
  historical-name ledger.
* **Move-to-folder** — `folder_id` remains create-time-only; PATCH does
  **not** accept `folder_id` in α5b (needs folder-ownership validation,
  deferred).
* **Cascade to children** — no child aggregates exist yet (assets/renders
  are α6/α7); soft-deleting a project touches only the `projects` row.
  `ON DELETE` FK behaviour for future children is out of scope.
* **`current_version_id` / project-versions ledger** — unchanged from α5a
  (NULL, untouched by PATCH).
* **Tenant-wide (shared) edit/delete** — strictly owner-scoped; RBAC is α6+.
* **List filters / new query params** — α5b touches no list surface
  except the index that speeds the *existing* query.
* **`duration_seconds` / `aspect_ratio` mutation** — see Q3 (recommended
  immutable in α5b).

### 1.4 Anti-scope-creep envelope

If any of the following show up in review, push back:

* *"Add a restore endpoint while we're here."* — No; soft-delete is
  one-way in α5b, restore is its own slice (needs its own auth/audit
  story).
* *"Make DELETE a hard delete, it's simpler."* — No; soft delete
  preserves auditability + future child referential integrity (aggregate
  doc lifecycle).
* *"Let PATCH move the project between folders."* — No; `folder_id`
  mutation needs folder-ownership validation that doesn't exist yet.
* *"Cascade-soft-delete children."* — No children exist yet; designing
  cascade now is speculative.
* *"Version-guard the DELETE too / add If-Match."* — See Q1; default is
  unconditional soft-delete. Do not add without a decision.
* *"Bundle a second index / general perf pass into 0008."* — No; the
  migration ships exactly the M3 index.

---

## Section 2 — Design Decisions

### D1: Extend the existing Projects surface (no new packages)
**Decision:** α5b adds methods/use-cases/handlers to the **existing**
`domain/projects` + `use_cases/projects` + `routers/projects.py` +
`ProjectRepository` created in α5a. No new package or router module.
**Rationale:** the aggregate and its bounded context already exist; α5b
is the write half of the same CRUD surface, mirroring how α2b extended
α2a's auth router rather than forking one.

### D2: Reuse α4 CAS-on-`version`, trigger-driven bump
**Decision:** `PATCH` uses the α4 optimistic-concurrency pattern verbatim:
the client sends its last-observed `version`; the repository performs a
compare-and-swap `UPDATE ... WHERE version = :expected` and relies on the
DB triggers (`tg_projects_biu_version_bump`,
`tg_projects_biu_touch_updated_at` — baseline `0001`) to bump `version`
and `updated_at`. **Rationale:** identical fence to
`UserRepository.update_profile`; the triggers already exist for
`projects` (it uses `VersionMixin` + `TimestampMixin`), so the CAS sets
only the changed business columns and lets the DB own the audit columns.

> **Implementation note.** α4's `update_profile` sets `version =
> UserRow.version + 1` explicitly *and* the trigger fires — verify on
> `projects` whether the trigger double-bumps. **The α5b CAS must not
> hand-increment `version`** if `tg_projects_biu_version_bump` already
> does it (that would jump the version by 2). Confirm the trigger
> behaviour against a live upgrade in step 8 before wiring the `.values()`
> (this is a test-matrix item — R-note in §5.3).

### D3: 404-before-412 — visibility precedes concurrency (NEW PATTERN)
**Decision:** `UpdateProject` is **fetch-then-fence**, not blind-CAS:
1. `get_owned(id, tenant, owner)` → if `None`, raise `NotFoundError`
   (`404`) — identical to α5a GET (missing / out-of-scope / soft-deleted
   are indistinguishable).
2. Compare the fetched row's `version` to `expected_version` → mismatch
   raises `VersionConflictError` (`412`).
3. Same-value no-op (no field actually changes) → return the unchanged
   entity (`200`), no write.
4. Real change → CAS `update_owned(..., expected_version, changes)`. If
   the CAS returns "gone" (a concurrent writer bumped `version` **or**
   soft-deleted the row between steps 1–4) → `412 VERSION_CONFLICT`.
**Rationale:** unlike α4 (whose resource is always "self", so
"not-found" is degenerate and collapses to `412`), a project is addressed
by a path `id` that may belong to no-one visible to the caller. Leaking
existence through a `412` would break the α5a anti-enumeration contract.
Visibility (`404`) is therefore decided **before** the version fence
(`412`). The benign step-1→4 race (concurrent delete) resolving to `412`
rather than `404` is accepted and documented (the state genuinely changed
under the caller; "retry with a fresh read" is the correct client action).

### D4: PATCH partial-update surface + tri-state semantics
**Decision:** `ProjectUpdateRequest` (`extra="forbid"`) accepts the
mutable business fields plus a **required** `version`:

| Field | Type | Semantics |
|---|---|---|
| `version` | `int` (`ge=1`) | **required** — OCC fence |
| `name` | `str` | present → set (strip; `1 ≤ len ≤ 200`); absent → unchanged |
| `description` | `str \| None` | present+value → set; present+`null` → clear; absent → unchanged |
| `language` | `str` | present → set (`1 ≤ len ≤ 8`); absent → unchanged |
| `style` | `str \| None` | present+value → set; present+`null` → clear; absent → unchanged |
| `settings` | `dict[str, Any]` | present → replace whole object; absent → unchanged |

Tri-state (absent vs explicit-`null` vs value) is resolved via Pydantic
`model_fields_set` — only fields the client actually sent are applied. At
least one mutable field must be present besides `version` (an empty
patch is a `422`, see Q4). `id`, `tenant_id`, `owner_user_id`,
`created_at`, `updated_at`, `current_version_id`, `folder_id`,
`duration_seconds`, `aspect_ratio` are **not** accepted (`extra="forbid"`
→ `422`). **Rationale:** whitelist the coherent mutable surface; the
tri-state distinction is required so a client can clear an optional field
(`description: null`) without that being confused with "leave it alone".
`settings` is whole-object replace (not deep-merge) in α5b — deep-merge
is a product decision deferred with the settings schema (α5a D12).

### D5: `settings` is replace-not-merge (α5b)
**Decision:** a PATCH containing `settings` replaces the entire JSONB
object. **Rationale:** consistent with α5a D12 (no settings schema yet);
partial/deep-merge semantics need a defined settings shape and a merge
policy, which is a later product slice. Documented so clients don't
assume merge.

### D6: DELETE = owner-scoped soft delete, idempotent-by-404
**Decision:** `DELETE /projects/{id}` sets `deleted_at = now()` on the
caller's own live row. Repository `soft_delete_owned` returns `True` if a
live owned row was found and marked, `False` otherwise. The use case maps
`False` → `NotFoundError` (`404`). Therefore: **first** delete → success;
**second** delete → `404`; GET/PATCH after delete → `404`. **Rationale:**
per the reviewer's explicit call — "don't silently succeed forever";
returning `404` on repeat keeps DELETE consistent with GET/PATCH
visibility and with the α5a 404-anti-enumeration model. Soft delete
(not hard) preserves auditability, future restore capability, and
referential integrity for the child aggregates (assets/renders) arriving
in α6/α7.

### D7: DELETE success status (see Q2)
**Recommended:** `204 No Content` (empty body), mirroring α2b `logout`
(API_CONTRACT §1.1 — envelopes accompany a JSON payload only; a delete
returns nothing meaningful). **Alternative:** `200` with the
tombstone representation. **Locked by Q2.**

### D8: DELETE is unconditional (no version fence) (see Q1)
**Recommended:** DELETE takes **no** `version` / `If-Match` — any live
owned project can be deleted without supplying a version. **Rationale:**
soft delete is not a lost-update hazard the way a field mutation is (the
row isn't being partially overwritten), and requiring a version adds
client friction for little safety; the 404-idempotency already makes
repeat-delete safe. **Alternative:** version-fenced delete for symmetry
with PATCH. **Locked by Q1.**

### D9: Rename collision → `409 CONFLICT`
**Decision:** a PATCH that renames a project to a `name` already held by
another **live** project of the same `(tenant, owner)` violates the
partial-unique index `uq_projects_tenant_id_owner_user_id_name` → the
repository catches `IntegrityError` and raises `ConflictError` (`409`),
identical shape to α5a `add` (D10). No pre-check SELECT (TOCTOU-free; the
DB constraint is the arbiter). A rename to the *same* name the project
already has is a same-value no-op (D3 step 3), not a conflict.
**Rationale:** reuse the α5a/α2a conflict discipline exactly.

### D10: M3 composite pagination index (folded in)
**Decision:** add
`ix_projects_owner_created_id`
`(tenant_id, owner_user_id, created_at DESC, id DESC) WHERE deleted_at IS NULL`
via **both** `Project.__table_args__` (ORM) and Alembic migration `0008`.
It directly serves `list_owned`'s `WHERE tenant_id=? AND owner_user_id=?
AND deleted_at IS NULL ORDER BY created_at DESC, id DESC`. The existing
non-partial `ix_projects_tenant_id_owner_user_id` is **kept** (it may
serve future include-deleted admin/restore queries; dropping it is a
separate, riskier decision — logged to backlog). **Rationale:** α5b is
the meaningful home for this migration (the aggregate is already being
touched, per the reviewer), avoiding a standalone index-only migration.

> **CI mechanics (load-bearing).** The schema validator
> (`scripts/validate_schema.py::check_indexes`) derives its expected set
> from `EXTRA_EXPECTED_INDEXES ∪ every ORM-declared Index`. Declaring the
> index in `__table_args__` **and** creating it in migration `0008`
> keeps stage 8 green with **no hardcoded count to bump**. Stage 9 (ERD)
> compares entities + FKs only, so an index adds **no** ERD drift.
> Stages 5/6/7 exercise upgrade → downgrade → idempotent re-upgrade, so
> the migration needs a correct, reversible `downgrade` (`DROP INDEX`).

### D11: Migration `0008` mechanics — plain `CREATE INDEX` (not CONCURRENTLY)
**Decision:** the migration uses a normal `op.create_index(...)` inside
the default transactional DDL. **Rationale:** Alembic wraps migrations in
a transaction; `CREATE INDEX CONCURRENTLY` cannot run inside one. The
`projects` table is tiny (dev/early), so a brief lock is a non-issue.
`CONCURRENTLY` (with a non-transactional migration) is a production-scale
concern to revisit before GA, not now — logged to backlog.

### D12: `UpdateOutcome` repository result shape
**Decision:** `update_owned` returns a small typed result distinguishing
the three internal outcomes the use case must map — recommended a
`Literal`/enum-tagged result or a lightweight dataclass:
`NOT_VISIBLE` (no live owned row) vs `VERSION_STALE` (CAS matched no row)
vs `updated: Project`. The use case, however, resolves `NOT_VISIBLE` in
**step 1** via `get_owned` (D3), so `update_owned` itself only needs to
signal `VERSION_STALE` vs `updated`. **Decision:** keep `update_owned`
returning `Project | None` (None = version-stale/gone at CAS time), with
the `404` decided by the use case's prior `get_owned`. **Rationale:**
mirrors `update_profile`'s `Project | None` contract; avoids a bespoke
result type; the visibility/concurrency split lives in the use case where
it's unit-testable with the fake.

### D13: Single checkpoint (with the α5a split rule)
**Decision:** all α5b files land in one commit / one PR. **Escape hatch
(same as α5a D13):** if the change set exceeds ~900–1000 lines changed,
split at the write-op boundary:
* **α5b.1** — repo `update_owned`/`soft_delete_owned` + migration `0008`
  + use cases + fakes + unit/repo tests.
* **α5b.2** — DTO + router handlers + DI + HTTP integration tests + docs.
Below that threshold, one slice.

---

## Section 3 — Acceptance Criteria

### 3.1 Behavioural (A1–A18)

**A1. PATCH happy (real change).** `PATCH /projects/{id}` with a valid
token, owned project, correct `version`, and a changed field → `200` with
`ProjectPublic`; `version` incremented by **exactly 1**; `updated_at`
bumped; changed field reflected.

**A2. PATCH envelope.** `{ "data": ProjectPublic, "meta": { "request_id": ... } }`.

**A3. PATCH partial — absent field unchanged.** A patch sending only
`name` leaves `description`/`language`/`style`/`settings` untouched.

**A4. PATCH explicit-null clears.** `{"description": null, "version": v}`
clears `description` (nullable); `version` increments. Sending
`{"name": null, ...}` (non-nullable) → `422`.

**A5. PATCH same-value no-op.** A patch setting fields to their current
values → `200`, `version` **unchanged**, `updated_at` unchanged, no DB
write (mirrors α4 §D6a).

**A6. PATCH stale version → 412.** Correct owned project, but
`version` < current → `412 VERSION_CONFLICT`.

**A7. PATCH not-owned / missing / soft-deleted → 404.** All three
indistinguishable `404 NOT_FOUND` (never `403`, never `412` — visibility
before concurrency, D3).

**A8. PATCH rename collision → 409.** Renaming to a name already held by
another live project of the same owner → `409 CONFLICT`.

**A9. PATCH server-owned/forbidden field → 422.** Body containing `id`,
`tenant_id`, `owner_user_id`, `aspect_ratio`, `folder_id`, `version` typed
wrong, or any non-whitelisted key → `422` (`extra="forbid"`).

**A10. PATCH empty patch → 422.** A body with only `version` and no
mutable field → `422` (Q4).

**A11. PATCH missing version → 422.** Body without `version` → `422`.

**A12. PATCH non-UUID path → 422.** FastAPI path validation.

**A13. PATCH no auth → 401.** Generic 401 via `CurrentUserDep`.

**A14. DELETE happy → soft delete.** `DELETE /projects/{id}` on an owned
live project → success status (Q2); the row's `deleted_at` is set.

**A15. DELETE idempotent-by-404.** A second `DELETE` on the same id →
`404`. `GET` and `PATCH` on a soft-deleted project → `404`. The project no
longer appears in `GET /projects`.

**A16. DELETE not-owned / missing → 404.** Deleting another user's/tenant's
project, or an unknown id → `404` (anti-enumeration).

**A17. DELETE frees the name.** After soft-deleting project "X", creating
a new project named "X" for the same owner succeeds (partial-unique index
excludes soft-deleted rows) — regression guard on α5a A5.

**A18. DELETE no auth / non-UUID path → 401 / 422.** Auth + path parity
with the other endpoints.

### 3.2 Engineering (E1–E6)

**E1.** CI gate 10/10 green, **including the new migration** (stages
5/6/7 upgrade→downgrade→idempotency exercise `0008`).

**E2.** No new `noqa` / `type: ignore` / coverage override (beyond the
pre-existing `errors.py` one).

**E3.** Unit-only coverage ≥ 80% total (holds baseline; new use-case +
fake write-path tests keep it above the floor).

**E4.** `import-linter` 5/5 kept — no new layering edges (α5b touches the
same layers as α5a).

**E5.** Schema validator (stage 8) green: the new ORM `Index` is present
in the DB after `0008`; **no** ERD drift (stage 9) — `0008` adds an index,
not a table/column/FK.

**E6.** `alembic downgrade base` then `upgrade head` (stages 6→7) both
succeed with `0008` in the chain — the `downgrade` `DROP INDEX` is
correct and idempotent-safe.

---

## Section 4 — File Inventory

### 4.1 New files

| Path | LOC est. | Purpose |
|---|---:|---|
| `backend/alembic/versions/0008_projects_pagination_index.py` | ~40 | M3 composite partial index (up=create, down=drop) |
| `backend/app/application/use_cases/projects/update_project.py` | ~90 | `UpdateProject` (fetch-then-fence, partial-apply, CAS) |
| `backend/app/application/use_cases/projects/delete_project.py` | ~55 | `DeleteProject` (scoped soft delete) |
| `backend/tests/unit/application/use_cases/projects/test_update_project.py` | ~160 | Update use-case unit tests (U1–U9 below) |
| `backend/tests/unit/application/use_cases/projects/test_delete_project.py` | ~90 | Delete use-case unit tests (U10–U13) |

### 4.2 Modified files

| Path | Change | LOC est. |
|---|---|---:|
| `backend/app/main.py` | Version bump → `0.4.6-phase3-alpha5b-dev` | +1 |
| `backend/app/infrastructure/db/models/projects.py` | Add M3 `Index(...)` to `Project.__table_args__` | +7 |
| `backend/app/domain/projects/project.py` | (none expected — entity already has all fields; docstring tweak optional) | ~0 |
| `backend/app/application/interfaces/repositories.py` | `IProjectRepository.update_owned` + `soft_delete_owned` (+ docstrings) | +40 |
| `backend/app/infrastructure/repositories/project_repository.py` | Implement `update_owned` (CAS) + `soft_delete_owned` | +70 |
| `backend/app/api/v1/schemas/projects.py` | `ProjectUpdateRequest` (tri-state, `version`, `extra="forbid"`) | +55 |
| `backend/app/application/use_cases/projects/__init__.py` | Export new use cases (if `__all__` present) | +2 |
| `backend/app/core/container.py` | `get_update_project_use_case` + `get_delete_project_use_case` | +14 |
| `backend/app/api/v1/deps.py` | `UpdateProjectDep` + `DeleteProjectDep` | +8 |
| `backend/app/api/v1/routers/projects.py` | `PATCH` + `DELETE` handlers | +55 |
| `backend/tests/unit/application/use_cases/auth/_fakes.py` | `FakeProjectRepository.update_owned` + `soft_delete_owned` | +45 |
| `backend/tests/integration/infrastructure/repositories/test_project_repository.py` | Repo tests for update/soft-delete (R8–R13) | +140 |
| `backend/tests/integration/api/test_projects.py` | HTTP tests for PATCH/DELETE (P17–P32) | +260 |
| `API_CONTRACT.md` | §3.2 α5b subset (PATCH tri-state, DELETE idempotent-404) | +12 |
| `CHANGELOG.md` | `[Unreleased]` α5b entry | +30 |
| `ROADMAP.md` | Phase 3 row α5b status line | +1 |

> **Fakes note (load-bearing, again).** `FakeProjectRepository` must gain
> `update_owned` (with the same-value-no-op + version-stale semantics) and
> `soft_delete_owned`, or every `UpdateProject`/`DeleteProject` unit test
> fails at attribute access. The integration `_TestUnitOfWork` needs **no**
> change (it uses the real `ProjectRepository`).

### 4.3 Deliberately NOT touched

* `backend/app/api/v1/helpers.py` — the M1/M2 helpers are consumed as-is.
* `backend/app/api/v1/routers/{auth,users,health}.py` — untouched.
* `backend/app/application/pagination.py` — the α5a cursor helper is
  unchanged (M3 only adds an index that speeds the *existing* query).
* `backend/app/core/errors.py` — `NotFoundError` / `ConflictError` /
  `VersionConflictError` all already exist.
* All other migrations `0001`–`0007` — `0008` is purely additive.

---

## Section 5 — Test Matrix

### 5.1 Unit — use cases (fakes, no DB)

| # | Case | Assertion |
|---|---|---|
| U1 | `UpdateProject` real change | Returns entity, `version==expected+1`, changed field applied, `commit()` once, `project.updated` logged |
| U2 | `UpdateProject` same-value no-op | Returns unchanged entity, `version==expected`, no CAS write, `changed=False` |
| U3 | `UpdateProject` not visible | `get_owned` None → raises `NotFoundError` (404), no CAS |
| U4 | `UpdateProject` stale version | fetched.version != expected → raises `VersionConflictError` (412), no CAS |
| U5 | `UpdateProject` CAS race | `update_owned` returns None (concurrent bump/delete) → `VersionConflictError` |
| U6 | `UpdateProject` partial (absent) | Only sent fields change; others preserved |
| U7 | `UpdateProject` explicit-null clears nullable | `description=None` clears; version bumps |
| U8 | `UpdateProject` rename collision | Fake raises `ConflictError` → propagated (409), no commit |
| U9 | `UpdateProject` scoping | tenant/owner passed to `update_owned` are the caller's |
| U10 | `DeleteProject` happy | `soft_delete_owned` True → commit once, `project.deleted` logged |
| U11 | `DeleteProject` not visible | False → `NotFoundError` (404), no commit |
| U12 | `DeleteProject` idempotent | Second call on already-deleted → `NotFoundError` |
| U13 | `DeleteProject` scoping | tenant/owner args are the caller's |

### 5.2 Repository integration (real DB, SAVEPOINT rollback)

| # | Case | Assertion |
|---|---|---|
| R8 | `update_owned` real change | Business field updated; `version` +1 (**verify trigger doesn't double-bump** — D2 note); `updated_at` advanced |
| R9 | `update_owned` version-stale | Wrong `expected_version` → returns None, row untouched |
| R10 | `update_owned` wrong owner/tenant | Returns None, row untouched |
| R11 | `update_owned` rename collision | Live-name dup → `ConflictError`, constraint in details |
| R12 | `soft_delete_owned` happy | `deleted_at` set; subsequent `get_owned` → None; `list_owned` excludes it |
| R13 | `soft_delete_owned` wrong owner / already-deleted | Returns False, no change |

### 5.3 HTTP integration — `test_projects.py` (P17–P32)

Same register→token→call style as α5a P1–P16.

| # | Case | Assertion |
|---|---|---|
| P17 | PATCH real change | 200, field changed, `version+1` |
| P18 | PATCH envelope | `data`+`meta.request_id` |
| P19 | PATCH partial (absent unchanged) | untouched fields preserved |
| P20 | PATCH explicit-null clears | `description:null` → null; `name:null` → 422 |
| P21 | PATCH same-value no-op | 200, `version` unchanged |
| P22 | PATCH stale version | 412 VERSION_CONFLICT |
| P23 | PATCH other user's project | 404 (create as B, patch as A) |
| P24 | PATCH unknown / soft-deleted id | 404 |
| P25 | PATCH rename collision | 409 |
| P26 | PATCH forbidden field / missing version / empty patch | 422 (each) |
| P27 | PATCH non-UUID path / no auth | 422 / 401 |
| P28 | DELETE happy | success status (Q2); gone from list |
| P29 | DELETE idempotent-by-404 | 2nd DELETE → 404; GET/PATCH after → 404 |
| P30 | DELETE other user's / unknown | 404 |
| P31 | DELETE frees name (α5a A5 regression) | re-create same name → 201 |
| P32 | DELETE no auth / non-UUID | 401 / 422 |

> **R-note (D2 trigger).** R8 is the load-bearing check: assert the
> post-PATCH `version` is exactly `expected + 1`. If it jumps by 2, the
> CAS is hand-incrementing on top of `tg_projects_biu_version_bump` — drop
> the explicit `version=...+1` from `.values()` and let the trigger own it.

---

## Section 6 — Structured-Log Catalogue (α5b additions)

| Event | Level | Fields |
|---|---|---|
| `project.updated` | INFO | `project_id`, `owner_user_id`, `tenant_id`, `changed_fields`, `previous_version`, `new_version`, `ip`, `request_id` |
| `project.update_rejected` | WARN | `reason` (`version_mismatch`), `project_id`, `owner_user_id`, `expected_version`, `ip`, `request_id` |
| `project.update_rejected` | INFO | `reason` (`same_value_noop`), `project_id`, `owner_user_id` |
| `project.deleted` | INFO | `project_id`, `owner_user_id`, `tenant_id`, `ip`, `request_id` |
| `project.delete_rejected` | WARN | `reason` (`not_visible`), `project_id`, `owner_user_id`, `ip`, `request_id` |

* No project `name`/`description`/`settings` **values** in logs (field
  names / ids only — same GDPR-minimal posture as α4/α5a).
* `changed_fields` is a list of **field names**, never values.

---

## Section 7 — Open Questions (all resolved 2026-07-11)

### Q1: DELETE — unconditional, or version-fenced?
**Resolution: ✅ unconditional** (D8). Reviewer agreed: soft delete is
not a partial overwrite, 404-idempotency already makes repeat-delete
safe, and a required `version` on delete is needless client friction. A
version-guarded delete is logged to the backlog if strict OCC symmetry is
ever wanted.

### Q2: DELETE success status — `204` or `200`?
**Resolution: ✅ `204 No Content`** (D7). Reviewer agreed: matches α2b
logout; nothing meaningful to return on a soft delete.

### Q3: Is `aspect_ratio` mutable via PATCH?
**Resolution: ✅ immutable in α5b** (not in `ProjectUpdateRequest`).
Reviewer agreed: won't paint us into a corner later; making it mutable now
sets a precedent that would need re-litigating once render output depends
on it. Mutable surface = `name`/`description`/`language`/`style`/`settings`.

### Q4: Empty patch (only `version`) — `422` or `200` no-op?
**Resolution: ✅ `422`** (A10). Reviewer agreed: a patch that changes
nothing is almost certainly a client bug; explicit rejection is clearer.

### Q5: `settings` — replace or deep-merge?
**Resolution: ✅ whole-object replace** (D5). Reviewer agreed: consistent
with α5a's "no settings schema yet"; deep-merge waits for a defined
settings shape.

### Q6: Keep or drop `ix_projects_tenant_id_owner_user_id` after M3?
**Resolution: ✅ keep** (D10). Reviewer agreed: non-partial index may
serve future include-deleted admin/restore scans and is cheap; dropping
is a separate, riskier migration decision, logged to backlog.

---

## Section 8 — Anti-Scope-Creep Reminders

* No restore / un-delete / hard-delete / archive.
* No bulk operations.
* No `folder_id` mutation (move-to-folder), no cascade design.
* No new list filters / query params (M3 only speeds the existing query).
* No settings schema / deep-merge.
* No RBAC / tenant-wide edit.
* No refactor of α5a read paths or the M1/M2 helpers.

If a reviewer suggests any of the above, defer to a later slice.

---

## Section 9 — Reviewer Sign-Off

**Reviewer verdict — 2026-07-11:** ✅ **Approved.** All six open questions
resolved per the pre-flight recommendations; no reviewer-added decisions.
Branch cut authorised.

Resolutions (see §7 for detail):

* ✅ **Q1 — DELETE unconditional** (no version fence) — D8.
* ✅ **Q2 — DELETE returns `204 No Content`** — D7.
* ✅ **Q3 — `aspect_ratio` immutable** in α5b — D4 (mutable surface =
  `name`/`description`/`language`/`style`/`settings`).
* ✅ **Q4 — empty patch → `422`** — A10.
* ✅ **Q5 — `settings` whole-object replace** (no deep-merge) — D5.
* ✅ **Q6 — keep `ix_projects_tenant_id_owner_user_id`** — D10.

Branch cut authorised: `phase3/alpha5b-projects-update-delete`.

---

## Section 10 — α5b Exit Criteria

α5b is complete when:

1. `PATCH /projects/{id}` and `DELETE /projects/{id}` are live, tested,
   and reflected in `API_CONTRACT.md` §3.2.
2. The 404-before-412 pattern (D3) has a reference implementation in
   `UpdateProject` + repo, proven by A6/A7 + P22/P23/P24.
3. Soft-delete lifecycle (idempotent-by-404, name freed) is proven by
   A15/A17 + P29/P31.
4. Migration `0008` (M3 index) applies, reverses, and re-applies cleanly;
   schema validator + ERD green (E5/E6).
5. CI gate 10/10 green; `import-linter` 5/5 kept.
6. Project CRUD lifecycle (`create → read → update → soft-delete`) is
   feature-complete — the trigger to **stop deepening CRUD** and pivot to
   the AI pipeline (see §11).

---

## Section 11 — Post-α5b Roadmap (the pivot)

Per the reviewer: after α5b merges green, **stop expanding the Project
aggregate** and move toward the product's core value — the AI generation
pipeline:

* **α6 — Media assets under a project** (`POST/GET /projects/{id}/assets`,
  image/audio upload) — first child aggregate; exercises cascade/soft-delete
  interplay with α5b's soft delete.
* **α7 — Render job creation & status** (`POST /projects/{id}/renders`,
  `GET .../renders/{id}`).
* **α8 — AI provider integration** (Runway / Luma / Veo / etc. behind a
  provider port).
* **α9 — End-to-end generation flow.**
* **β — Real-user testing, bug-fixing, performance** (revisit
  `CREATE INDEX CONCURRENTLY`, the `ix_projects_tenant_id_owner_user_id`
  drop, settings schema, restore endpoint).

**Deferred-from-α5b backlog:** restore endpoint; move-to-folder + folder
validation; tags + `?tag=`/`?folder_id=`/`?query=` filters; settings
schema + deep-merge; version-guarded DELETE (if Q1 chose unconditional);
`CONCURRENTLY` index build; drop of the redundant scoping index.

---

## Section 12 — Implementation Order (once approved)

1. Cut `phase3/alpha5b-projects-update-delete` off fresh `main`; bump
   `app/main.py` to `0.4.6-phase3-alpha5b-dev`.
2. Add M3 `Index(...)` to `Project.__table_args__` + write migration
   `0008` (up=create, down=drop).
3. Run `alembic upgrade head` locally; **verify the version-bump trigger
   behaviour** on a manual `UPDATE projects` (D2 note) before writing the CAS.
4. Extend `IProjectRepository` (`update_owned`, `soft_delete_owned`).
5. Implement `ProjectRepository.update_owned` (CAS, `IntegrityError`→409)
   + `soft_delete_owned`.
6. Extend `FakeProjectRepository` (same semantics) for unit tests.
7. Add `UpdateProject` + `DeleteProject` use cases + unit tests (U1–U13).
8. Run `pytest -m unit` + mypy — green before touching HTTP.
9. Add `ProjectUpdateRequest` DTO (tri-state + `version`).
10. Wire container factories + deps aliases.
11. Add `PATCH` + `DELETE` handlers to `routers/projects.py`.
12. Write repo integration (R8–R13) + HTTP integration (P17–P32).
13. Update `API_CONTRACT.md` §3.2, `CHANGELOG.md`, `ROADMAP.md`.
14. Local CI gate 10/10 (special attention: stages 5/6/7 with `0008`).
15. Commit, push, PR.
16. Post-merge: tag `v0.4.6-phase3-alpha5b`, close α5b; **pivot to α6
    (media assets)** per §11.
