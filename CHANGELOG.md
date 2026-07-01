# CHANGELOG

> Keep-a-Changelog style. Each completed phase gets one entry. Pre-release work tracked under **[Unreleased]**.

---

## [Unreleased]

### Phase 3 Slice α2b — Auth (refresh + logout) (2026-07-01)

Completes the authentication lifecycle started in α2a. Adds refresh
token rotation with family-level reuse detection, session revocation
via logout, and the `IClock` port used by both new use cases.
Delivered as two internal checkpoints (α2b.1: `ISessionRepository`
extensions + `RefreshSession`; α2b.2: `verify_access(allow_expired)` +
`LogoutSession` + router wiring). **No migration, no ADR** — a direct
application of the α2a auth foundation.

#### Added
- **`IClock` port** (`app/application/interfaces/clock.py`) and
  `SystemClock` implementation (`app/infrastructure/clock.py`). All
  auth use cases (`RegisterUser`, `LoginUser`, `RefreshSession`,
  `LogoutSession`) now take an injected clock instead of calling
  `datetime.now(UTC)` inline. `FakeClock` in the unit fakes supports
  a frozen `fixed_at` + `tick(seconds)` for deterministic time-based
  tests.
- **`ISessionRepository.get_by_hash / revoke / list_family`** —
  three new methods on the α2a port. `revoke` uses a compare-and-swap
  clause (`WHERE revoked_at IS NULL`) so the first revoker wins and
  the original `revoked_at` timestamp is preserved for audit through
  all subsequent no-op calls. `list_family` powers the family sweep on
  reuse detection. `get_by_hash` returns revoked rows too, matching
  the use case's need to inspect `revoked_at` as the reuse signal.
- **`RefreshSession` use case** (`app/application/use_cases/auth/`) —
  orchestrates the full rotation flow: JWT verify → SHA-256 hash lookup
  → sid consistency check (A12) → reuse detection with full-family
  revocation → user liveness check → CAS-revoke old row → mint fresh
  tokens preserving `family_id` → insert new row. Every failure mode
  raises the same client-facing `InvalidRefreshTokenError` for
  anti-enumeration; server-side logs carry the specific reason.
- **`LogoutSession` use case** — CAS-revokes the session identified by
  the access token's `sid` claim. **Accepts expired access tokens**
  (documented prominently in the class docstring and in
  `docs/engineering/AUTH_TOKEN_LIFECYCLE.md`): forcing a refresh before
  logout would defeat the "I am done" intent. Signature and `kind` are
  still strictly enforced. Idempotent: second logout returns 204 and
  preserves the original `revoked_at`.
- **`verify_access(allow_expired: bool = False)`** — new kwarg on
  `ITokenIssuer`, threaded through `JWTService.verify` via PyJWT's
  `options={"verify_exp": False}`. Only `LogoutSession` sets it; every
  other consumer keeps the strict default.
- **`InvalidRefreshTokenError`** (`app/application/use_cases/auth/errors.py`) —
  subclass of `UnauthorizedError` used by both `RefreshSession` and
  `LogoutSession` for uniform 401 envelopes on every non-happy path.
- **`RefreshRequest` DTO** + `BearerAccessTokenDep` (in `app/api/v1/`) —
  the FastAPI dependency parses `Authorization: Bearer <token>`,
  raising 401 for missing / malformed headers.
- **Two new endpoints** — `POST /api/v1/auth/refresh` (200 with the
  rotated pair) and `POST /api/v1/auth/logout` (204 No Content).
- **`docs/engineering/AUTH_TOKEN_LIFECYCLE.md`** — operational spec
  covering the session state machine, endpoint sequence diagrams, the
  Refresh Family Example (visualising why reuse detection nukes the
  whole family), invariants, and the structured-log event catalogue
  including which events carry `security_event=True` for SIEM alerting.
- **Extended tests** — `test_token_issuer.py` +2 (`allow_expired`
  accepts stale / still rejects tampered), `test_refresh_session.py`
  (13 unit tests), `test_logout_session.py` (8 unit tests),
  `test_clock.py` (1 unit test), `test_session_repository.py` +5
  integration tests (get_by_hash / revoke CAS / list_family),
  `test_auth.py` +9 integration tests (refresh happy path, reuse
  detection, garbage token, access-token-as-refresh, sid mismatch,
  logout happy path, logout idempotent, missing header, malformed
  header, refresh-token-as-logout).

#### Changed
- `RegisterUser` and `LoginUser` constructors now take an `IClock`
  parameter. All timestamp assignments (`created_at`, `updated_at`,
  `last_login_at`, `issued_at`, `last_used_at`) go through the clock.
  `FakeTokenIssuer` and `FakeSessionRepository` extended for the new
  port surface.
- Version bumped to `0.4.2-phase3-alpha2b-dev` in `app/main.py`.

### Phase 3 Slice α2a — Auth (register + login) (2026-07-01)

First real business capability shipped on top of the α1 architecture
scaffold. Delivers the password-auth happy path — `POST /api/v1/auth/register`
and `POST /api/v1/auth/login` — end-to-end through the layered
architecture (domain → application → infrastructure → API). Split from
the original combined α2 plan into α2a (register + login) + α2b
(refresh + logout) per the pre-flight review for reviewability. **No
migration, no ADR** (no new architectural trade-off — the plan is a
direct application of ADR-0008 + the α1 DI pattern).

#### Added
- **Domain layer** — `app/domain/identity/{user,tenant,session}.py`.
  Frozen dataclasses with `slots=True`, zero ORM inheritance, zero
  framework dependencies. Enforced by import-linter contract #1.
- **Two new application ports** (`app/application/interfaces/security.py`):
  `IPasswordHasher`, `ITokenIssuer`, plus the `IssuedTokens` and
  `TokenClaims` value objects. Existed to keep unit tests fast (Argon2id
  fake substitution) and to lock the seam for future token-scheme
  swaps (PASETO, opaque tokens).
- **Extended `IUserRepository`** — new methods `get_by_email`,
  `get_by_id`, `add`, `update_last_login`. α1 methods
  (`count`, `exists_by_id`) preserved per the pre-flight review.
- **Three new repository ports** — `ITenantRepository` (add /
  get_by_id / exists_by_slug), `ISessionRepository` (add only in α2a;
  extended in α2b), `IRoleRepository` (assign_role_by_code, idempotent
  via ON CONFLICT DO NOTHING).
- **UoW attribute-style repos** — `IUnitOfWork` now exposes
  `.users`, `.tenants`, `.sessions`, `.roles` populated by the
  concrete UoW on `__aenter__`, so use cases call
  `await uow.users.add(...)` without ever seeing SQLAlchemy classes.
- **Two application use cases** — `RegisterUser` (application-level
  global email-uniqueness pre-check → auto-creates a self-service
  tenant per signup with slug-collision retry → inserts the user →
  assigns the `owner` role → issues tokens → persists the initial
  session), `LoginUser` (get-by-email → constant-time Argon2 verify
  → issue tokens with fresh family/session ids → persist session →
  bump `last_login_at`).

  *Note on the email-uniqueness pre-check:* the auto-tenant-per-signup
  design (Decision 1A) defeats the DB per-tenant unique constraint on
  `(tenant_id, email)` for the "same email registered twice"
  scenario — each signup arrives at a different `tenant_id` so the
  constraint always sees a distinct pair. Without an application-layer
  pre-check, re-registration would silently create a second orphan
  tenant under the same email. `RegisterUser` therefore calls
  `users.get_by_email(email)` inside the same UoW before creating the
  tenant and raises `EmailAlreadyRegisteredError` on a hit. Race
  window between the pre-check and insert is acceptable for α2a; a
  later hardening pass may add an application-level lock table or
  rate-limit if this proves exploitable in practice.

  *Note on the role assignment:* the pre-flight originally called for
  `user + owner`. On implementation this proved to conflate two
  orthogonal concepts — the `roles` table (workspace permissions,
  seeded with `owner, admin, editor, viewer, billing, support`) vs
  the `auth_role` ENUM in `schema.md` §0.1 (plan tiers). `user` lives
  on the ENUM, not the table. Assigning `owner` alone captures the
  intended semantics ("creator owns the tenant they just created");
  "any authenticated user" is enforced by JWT validity, not by a
  role row. Documented in the `RegisterUser` class docstring.
- **Anti-enumeration login path** — `LoginUser` burns one Argon2
  verify against a startup-computed dummy hash when the email is
  unknown or the account is OAuth-only, so wall-time is
  indistinguishable from the wrong-password branch (OWASP ASVS L2 §2.6.3).
- **`AuthTokenIssuer`** (`app/infrastructure/security/token_issuer.py`) —
  wraps the α1 `JWTService` + SHA-256 + up-front `session_id` /
  `family_id` generation into one call. Emits `sid` (session id) and
  `fam` (family id) claims on **both** access and refresh tokens so
  α2b `LogoutSession` can revoke a precise session row from the access
  token alone (no need to accept the refresh token in the logout body).
- **DTOs** (`app/api/v1/schemas/auth.py`) — Pydantic v2 request /
  response models. Request DTOs strip whitespace and lowercase the
  email before it ever reaches the use case (canonical `CITEXT` values).
  `UserPublic` explicitly enumerates public fields — `password_hash`
  cannot leak through DTO drift because it isn't declared.
- **Router** (`app/api/v1/routers/auth.py`) — two POST endpoints,
  mounted under `/api/v1`. Envelope response per API_CONTRACT §1.1.
  Zero try/except — errors surface via the α1 exception-handler chain.
- **DI wiring** — `app.core.container` grows a
  `get_token_issuer` singleton, a pre-computed
  `get_dummy_password_hash` (Argon2 cost paid once at process start,
  not per request), and two use-case factories
  (`get_register_user_use_case`, `get_login_user_use_case`).
- **New unit tests** (~19 across three files):
  `test_register_user.py`, `test_login_user.py`,
  `test_token_issuer.py`. All auth use-case tests use in-memory fakes
  (`tests/unit/application/use_cases/auth/_fakes.py`) — total unit-suite
  runtime stays sub-second because Argon2id verify is stubbed with a
  string comparison.
- **New integration tests** — `test_tenant_repository.py`,
  `test_session_repository.py`, `test_role_repository.py`; extended
  `test_user_repository.py` with α2a method coverage; new
  `tests/integration/api/test_auth.py` (9 scenarios covering register /
  login happy paths, duplicate email → 409, short password → 422,
  email lowercasing, `sid`/`fam` claim presence in JWT, anti-enumeration
  message equality, distinct families per device).
- **Integration test client fixture rebind** —
  `tests/integration/conftest.py::client` now overrides
  `container.get_session` and `container.get_unit_of_work` so mutation
  handlers run inside the test's SAVEPOINT connection. Nothing persists
  across tests; the shared Supabase instance stays clean.
- **New runtime dependency** — `email-validator>=2.2,<3` (required by
  Pydantic `EmailStr` at DTO parse time).
- **Fifth `import-linter` contract** — "Application use_cases never
  import infrastructure or api". Locks the layered boundary the
  moment the layer is introduced.

#### Changed
- **`app/main.py`** — imports and mounts `auth.router` under
  `/api/v1`; health router stays at the root path (API_CONTRACT §2
  designates `/healthz` + `/readyz` as public, versionless). App
  version bumped `0.4.0-phase3-alpha1-dev → 0.4.1-phase3-alpha2a-dev`.
- **`app/infrastructure/security/password_hasher.py`** — now declares
  `class PasswordHasher(IPasswordHasher)` (implements the new port).
  No runtime behaviour change.
- **`app/infrastructure/uow/sqlalchemy_unit_of_work.py`** — `__aenter__`
  populates the four repository attributes from the session it owns.

#### Deferred (Slice α2b)
- `POST /api/v1/auth/refresh` — token rotation with reuse detection.
- `POST /api/v1/auth/logout` — precise per-`sid` revocation using the
  claim shipped in α2a.
- `ISessionRepository` extensions: `get_by_hash`, `revoke`,
  `list_family`. Kept out of α2a intentionally per the pre-flight
  review — repositories in α2a cover only the α2a use cases.
- `IClock` port — introduced in α2b where `RefreshSession` needs it
  for the session-row `expires_at` computation.

#### Deferred (Slice α3+)
- Email verification (`/auth/email/verify`, `/auth/email/resend`) — α3.
- Password reset (`/auth/password/forgot`, `/auth/password/reset`) — α4.
- Google OAuth (PKCE) — α5.
- RBAC enforcement at endpoint boundaries — α6.
- OCC retry on `LoginUser.update_last_login` — retained as a deferred
  optimisation; add only if concurrent-login contention becomes
  observable.

---

### Phase 3 Wave 1.4 — `usage_records` per-partition `(request_id)` uniqueness (ADR-0033) (2026-06-30)

Wave-closing item for Phase 3 Wave 1: promotes a per-partition
partial-unique `(request_id) WHERE request_id IS NOT NULL` index to
every child partition of `usage_records`, resolving `schema.md` §37 q6.
First migration-coupled ADR to reference `docs/engineering/RUNBOOK_WAVE.md`
in place of inlining operational steps (per `CONTRIBUTING.md` §6,
established at `v0.3.3-infra`). **Wave 1 of Phase 3 closes with this
release (`v0.3.4-phase3-w1.4`).**

#### Added
- **`backend/alembic/versions/0007_usage_records_request_id_unique.py`** —
  hand-written single revision (`revision = "0007_usage_records_request_id_unique"`,
  `down_revision = "0006_widen_alembic_version_num"`). Upgrade body
  iterates `pg_inherits` for all current children of `usage_records`
  (26 monthly + 1 DEFAULT today) and creates one partial-unique index
  per child named `uq_<child>_request_id` (e.g.
  `uq_usage_records_y2025m12_request_id`,
  `uq_usage_records_default_request_id`) with predicate
  `(request_id) WHERE request_id IS NOT NULL`. Idempotent via
  `IF NOT EXISTS`. Downgrade mirrors with `DROP INDEX IF EXISTS`.
  Hand-written rather than via `alembic revision --autogenerate`
  because autogenerate cannot express per-child partition-level DDL
  and would not preserve the partial predicate. The per-child
  mechanic is PostgreSQL's standard and correct pattern for
  unique-on-non-partition-key constraints (the parent-level form
  `CREATE UNIQUE INDEX ON usage_records (request_id)` is rejected
  because the unique key omits the `occurred_at` partition key) —
  not a workaround. The 35-char revision ID fits the `VARCHAR(255)`
  ceiling established by `0006_widen_alembic_version_num` (v0.3.3-infra).
- **`docs/decisions/ADR-0033-usage-records-request-id-unique.md`** — new
  ADR (fourth file-per-ADR adopter; first to reference
  `RUNBOOK_WAVE.md` in §Migration Plan rather than inlining operational
  steps). Documents the architectural-review process that preceded the
  ADR, the rejected alternatives (`(provider, request_id)` scope
  expansion deferred to a future separate decision; `(model_id,
  request_id)` invention with no repository support; documentation-only
  closure inconsistent with wave-era planning artifacts; top-level
  parent index too weak; `ON ONLY` + `ATTACH PARTITION` rejected by
  the same partition-key rule), the per-child mechanic, the deliberate
  ORM-absence, the validator-extension rationale, the future-partition
  contract, and a Future Considerations section preserving the broader
  architectural pattern for a separate later decision.
- **`backend/scripts/validate_schema.py::check_usage_records_per_partition_unique_indexes`** —
  new ~120-LoC check function and `run_all_checks` wiring. Scans
  `pg_inherits` for all `usage_records` children and asserts each
  carries `uq_<child>_request_id` with `indisunique = true` and the
  expected `WHERE (request_id IS NOT NULL)` partial predicate. This
  is a CI-visibility addition compensating for the
  `load_snapshot()` bulk-index query that deliberately excludes
  partition children (`NOT EXISTS (SELECT 1 FROM pg_inherits ...)`
  for performance — Supabase round-trip count would otherwise scale
  with partition count). Not a workaround for a PostgreSQL
  limitation; not a substitute for ORM declaration (which is
  impossible by PostgreSQL design). The check passes when
  27/27 partition children carry the expected index after
  `alembic upgrade head`, fails on missing children, missing
  indexes, or wrong predicate.

#### Changed
- **`docs/database/schema.md`** §18 reconciliation note: amended to
  record the §37 q6 resolution with the architectural-review
  conservative wording — "The Phase 3 wave-planning artifacts
  consistently anticipate a `request_id`-based W1.4 implementation.
  Earlier architectural documents describe provider-scoped idempotency
  at the application level. W1.4 implements the scope reflected in
  the Phase 3 planning artifacts without attempting to reconcile that
  broader architectural question." The Step-A `(provider, request_id)`
  design is explicitly described as neither implemented nor
  superseded by W1.4; any future move is reserved for a separate
  decision informed by CR-12 implementation evidence (ADR-0033
  §Future Considerations). The §18 schema box, the §18 indexes line,
  and the §31 CR-12 use-case table row remain unchanged — column
  shape and broader app-layer idempotency are not altered by this
  wave.
- **`docs/database/schema.md`** §37 Q6 row: flipped from `rely on
  idempotency_keys` to **Resolved (Phase 3 W1.4, 2026-06-30)** with
  full constraint details, mirroring the Q8/Q9/Q10 resolved-row
  shape established by W1.1/W1.2/W1.3.
- **`docs/database/schema.md`** §37 Wave 1 epilogue: §18 q6 bullet
  marked **✅ Done — Phase 3 W1.4**, closing the Wave 1 quartet.
- **`docs/database/INDEX_STRATEGY.md`** line 147: status flipped from
  **Deferred (Phase 3)** to **Implemented (Phase 3 W1.4)**; rationale
  expanded to document the per-child mechanic, the PostgreSQL
  partition-key rule, the future-partition contract enforced by the
  validator check, and the explicit non-supersession of broader
  `(provider, request_id)` architectural semantics.
- **`ROADMAP.md`** Wave 1 row: W1.4 annotated **✅ Complete** with
  full ADR + migration cross-reference; "**Wave 1 closes with this
  tag (`v0.3.4-phase3-w1.4`).**" sentence appended.
- **`DECISIONS.md`**: one-line cross-link entry for ADR-0033 appended
  after the ADR-0032 entry, sorted by ADR number. Status initially
  `Proposed`; flipped to `Accepted` on the pre-merge status-flip
  commit.
- **`backend/app/infrastructure/db/models/usage.py`** —
  `UsageRecord.__table_args__` gains a multi-line inline comment near
  the existing `Index("ix_usage_records_request_id", "request_id")`
  declaration documenting that the per-child unique indexes are added
  by migration `0007` and intentionally have no ORM counterpart
  (PostgreSQL's partition-key rule makes a parent-level
  `Index(unique=True, postgresql_where=...)` impossible for
  `(request_id)` because the key omits the `occurred_at` partition
  key, and the children themselves are not ORM-modelled). The
  comment points at ADR-0033 §Implementation Notes and at the
  validator check. No `Index` or `CheckConstraint` declaration is
  added to the ORM.

#### Validated
- **Pre-upgrade safety SELECT** against live Supabase: `SELECT
  request_id, count(*) FROM usage_records WHERE request_id IS NOT NULL
  GROUP BY request_id HAVING count(*) > 1` returned zero rows
  (expected — the table is empty in every current environment; run
  for audit-trail completeness and to prove the production-rollback
  variant is not required).
- `alembic upgrade head` from `0006_widen_alembic_version_num` →
  `0007_usage_records_request_id_unique` applied cleanly; `pg_indexes`
  shows 27 new unique partial indexes (one per child) named per the
  `uq_<child>_request_id` pattern with `indexdef` containing the
  expected `WHERE (request_id IS NOT NULL)` predicate.
- `alembic downgrade -1` reverted cleanly; `pg_indexes` shows the 27
  indexes removed; `ix_usage_records_request_id` (the parent's
  non-unique propagating index) unaffected.
- `alembic upgrade head` re-applied cleanly (idempotency proven via
  `IF NOT EXISTS` guards).
- `python scripts/validate_schema.py` reported **all checks PASS**
  with the new `check_usage_records_per_partition_unique_indexes`
  reporting `27/27 usage_records partition(s) carry uq_<child>_request_id`.
- `python scripts/regenerate_erd.py` + `python scripts/compare_erd.py`
  reported 0 drift (per-child unique indexes are invisible to the ERD
  by design — it tracks entities and FKs, not indexes).
- `python scripts/ci_gate.py` reported **10/10 PASSED** locally
  against Supabase from a cold shell — `RUNBOOK_WAVE.md` referenced
  per ADR-0033 §Migration Plan, success-metric satisfied: W1.4
  required fewer manual steps than W1.3 (no env-load preamble, no
  migration-ID length gymnastics, no `.cursor/` accidents, no
  inline operational-steps duplication in the ADR).
- GitHub Actions on the PR: 10/10 green.

#### Not modified
- `backend/alembic/versions/0001_baseline.py` (no in-place edits to
  merged migrations; the new indexes are owned entirely by `0007`).
- `backend/alembic/versions/0003_export_jobs_partial_unique.py` (W1.1
  territory).
- `backend/alembic/versions/0004_idempotency_keys_invariants.py` (W1.2
  territory).
- `backend/alembic/versions/0005_distributed_locks_lease.py` (W1.3
  territory).
- `backend/alembic/versions/0006_widen_alembic_version_num.py`
  (`v0.3.3-infra` territory).
- `docs/database/ERD.md` (per-child unique indexes are invisible to
  ERD by design — entities + FKs only).
- `docs/database/schema.md` §18 schema box (lines 638–668), §18
  indexes line (line 673), §31 use-case table row (line 1175) —
  column shape and broader app-layer idempotency are unchanged by
  this wave.
- `ARCHITECTURE.md` §8k.1 (CR-12 domain spec), `API_CONTRACT.md`
  line 233 (webhook handlers) — broader architectural pattern
  remains documented; W1.4 neither implements nor supersedes it.
- `backend/app/application/`, `backend/app/api/`,
  `backend/app/infrastructure/ai/` — CR-12 (Usage Recorder
  middleware named in `schema.md` §31 / `ARCHITECTURE.md` §8k.1) is
  not built; W1.4 establishes the DB-level invariant in advance of
  the producer and does not anticipate the producer's design.
- `CONTRIBUTING.md` (file-per-ADR + ADRs-vs-Runbooks conventions
  established in earlier releases; W1.4 is the first migration-coupled
  ADR to exercise the runbook reference convention).
- `pyproject.toml`, dependency manifests.

#### Scope discipline
- One PR, one branch (`phase3/wave1.4-usage-records-request-id-unique`),
  one Alembic revision (`0007`), one ADR (ADR-0033), one validator
  check function. `git diff main...HEAD` touches **only** the files
  enumerated above. No opportunistic refactors, no unrelated cleanup,
  no W2 work; the `v0.3.3-infra` discipline rule held.
- ADR-0033 deliberately reserves provider-scoped DB-level enforcement
  for a separate later decision rather than expanding W1.4 scope —
  preserves the wave-era documented implementation shape exactly,
  preserves the architectural pattern documented elsewhere unchanged,
  and preserves the historical record's accuracy by neither
  rewriting earlier documents nor inventing supersession claims.

---

### Phase 3 Engineering Checkpoint — `v0.3.3-infra` workflow cleanup (2026-06-30)

Non-feature release between W1.3 and W1.4 removing three recurring
engineering friction points discovered while shipping W1.1–W1.3, plus
the first engineering runbook. **Success metric:** W1.4 must require
fewer manual steps than W1.3.

#### Added
- **`backend/alembic/versions/0006_widen_alembic_version_num.py`** —
  Alembic migration widening `alembic_version.version_num` from the
  default `VARCHAR(32)` to `VARCHAR(255)`. The 32-char ceiling was hit
  by W1.3's natural revision ID `0005_distributed_locks_lease_check`
  (34 chars), which had to be renamed in-place to
  `0005_distributed_locks_lease` (28 chars) to fit. The widen removes
  the ceiling globally so W1.4's natural slug
  `0007_usage_records_request_id_unique` (35 chars) and every future
  Wave migration can use descriptive names without abbreviation
  gymnastics. The migration's own revision ID
  (`0006_widen_alembic_version_num`, 30 chars) fits the pre-existing
  limit, so it applies cleanly; the widen DDL and the row insert
  happen in the same `upgrade()` transaction, so no chicken-and-egg.
  Hand-written rather than via `alembic revision --autogenerate`
  because autogenerate does not emit DDL against system tables like
  `alembic_version`.
- **`docs/engineering/RUNBOOK_WAVE.md`** — first entry in a new
  `docs/engineering/` directory for repeatable engineering procedures.
  Six sections: Pre-flight, Development, Verification, Release,
  Recovery, Lessons Learned. Documents the Phase 3 Wave process that
  W1.1–W1.3 executed by hand (with minor per-Wave variation) so W1.4
  onwards can simply reference the runbook rather than have its ADR
  re-describe operational steps. Per `CONTRIBUTING.md` §6, ADRs
  answer WHY a decision was made; runbooks answer HOW it is executed.

#### Changed
- **`backend/scripts/ci_gate.py`** — stages 5–9 now load
  `DATABASE_URL` from `backend/.env.validation` automatically. The
  previous code path checked the FILE'S existence for the
  `db_available` flag but never actually loaded variables from it, so
  the `alembic`, `validate_schema`, and `regenerate_erd` subprocesses
  inherited an empty env and silently fell back to `alembic.ini`'s
  localhost URL — failing to reach Supabase. The 6-line fix imports
  the existing `_load_env.load()` function (already in
  `backend/scripts/_load_env.py` and used by every Step B validation
  script since Phase 2) and calls it when `db_available` but
  `DATABASE_URL` is not yet exported. Idempotent; safe to re-run; no
  behavioral change in GitHub Actions CI (where `DATABASE_URL` is set
  by the service container, so the conditional short-circuits).
- **`backend/scripts/run_ci_gate.ps1`** — header comment near the
  Python invocation clarifies that env loading happens inside
  `ci_gate.py` (no PowerShell-level `.env.validation` sourcing
  required). No behavioral change; the comment exists at the
  call-site so future contributors don't add redundant PowerShell
  env-load logic. Single source of truth for env loading is Python.
- **`.gitignore`** — replaced the partial Cursor ignore
  (`.cursor/state/` + `.cursor/cache/`, lines 114–116) with `.cursor/`
  (whole directory, single rule). The partial ignore left
  `.cursor/rules/`, `.cursor/automations/`, and any future
  Cursor-managed subdirectory exposed to `git add -A` sweeps, which
  caused a pre-commit incident during W1.3's amend cycle. No
  `.cursor/` content has ever been intentionally tracked in practice;
  if a specific rule ever needs sharing,
  `git add -f .cursor/rules/<file>` works for the deliberate case.
- **`CONTRIBUTING.md`** — §6 Documentation Policy extended with an
  "ADRs vs Runbooks (v0.3.3-infra)" paragraph codifying the
  convention: ADRs are for WHY (context, alternatives, consequences);
  runbooks are for HOW (step lists, commands, recovery actions).
  Cross-references `docs/engineering/RUNBOOK_WAVE.md` and notes that
  the W1.4 ADR (ADR-0033) will be the first to reference the runbook
  in place of inlining operational steps.
- **`ROADMAP.md`** — small engineering-checkpoint annotation between
  the Phase 3 wave table's W1 row and the surrounding "each wave
  produces its own ADR(s)" sentence, recording the `v0.3.3-infra`
  release and pointing at the new runbook. The Wave table itself is
  unchanged (the checkpoint is not a Wave).

#### Validated (live, 2026-06-30)
- **Pre-fix reproduction** — confirmed that the v0.3.2 `ci_gate.py`,
  when run from a shell where `DATABASE_URL` is unset, fails stage 5
  (`alembic upgrade head`) with `psycopg.OperationalError` despite
  `backend/.env.validation` being present. This was the original
  W1.2 symptom that required a manual PowerShell env-load workaround.
- **Post-fix reproduction** — same shell, no env vars set, no manual
  PowerShell loader: `scripts/ci_gate.py` reaches 10/10 stages green
  with `DATABASE_URL` loaded from `backend/.env.validation`
  automatically. The success metric — *will W1.4 require fewer manual
  steps than W1.3?* — is satisfied: zero manual steps for env
  loading.
- **Alembic round-trip** — `alembic upgrade head` (applies 0006,
  widens column); inspection of `\d alembic_version` confirms
  `version_num` is now `character varying(255)`; `alembic downgrade -1`
  returns the column to `character varying(32)`; `alembic upgrade head`
  re-applies cleanly (idempotency proven).
- **`.gitignore` enforcement** — `git status` after the new ignore is
  in place no longer lists `.cursor/` as untracked; `git add -A` no
  longer stages anything under `.cursor/`.
- **Full CI gate** — `scripts/ci_gate.py` 10/10 local; matching
  GitHub Actions run on the PR also 10/10 against the pgvector
  service container.

#### Not modified (scope discipline)
- **`backend/app/infrastructure/db/models/*.py`** — no ORM changes.
  `alembic_version` is Alembic's own bookkeeping table and is not
  modelled in the ORM (it is explicitly whitelisted out of the
  schema validator's table-parity check).
- **`docs/database/schema.md`** — no changes (`alembic_version` is
  intentionally not documented there; it is build infrastructure,
  not application data).
- **`docs/database/INDEX_STRATEGY.md`** — no changes (no new indexes
  or unique constraints; the column-type widen does not affect index
  counts).
- **`docs/database/ERD.md`** — no changes (ERD does not include
  `alembic_version`).
- **`DECISIONS.md`** — no new ADR. This is a workflow cleanup, not
  an architectural decision; the rationale lives in this CHANGELOG
  entry and in `docs/engineering/RUNBOOK_WAVE.md` §6 Lessons Learned.
- **`backend/alembic/versions/0001_baseline.py` through
  `0005_distributed_locks_lease.py`** — none amended in place; the
  column widen is added entirely by `0006` and reverted on its
  `downgrade()`.

#### Scope discipline (per the v0.3.3-infra PR scope rule)
- Every changed file either implements one of the five engineering
  improvements (env-load fix, alembic widen, gitignore, runbook,
  ADRs-vs-runbooks convention) or documents the release (this
  CHANGELOG entry, the ROADMAP annotation).
- No feature work. No schema changes other than the
  `alembic_version` column widen. No API changes. No refactors. No
  opportunistic cleanup. No "while we're here" edits.

### Phase 3 Wave 1.3 — `distributed_locks` lease CHECK (2026-06-29, ADR-0032)

#### Added
- **`backend/alembic/versions/0005_distributed_locks_lease.py`** —
  Alembic migration adding a single CHECK constraint
  `chk_distributed_locks_lease_until_after_acquired_at` enforcing
  `lease_until > acquired_at`. Strict greater-than (`>`, not `>=`)
  rejects the degenerate zero-second lease that a buggy `$lease = 0` or
  negative-`$lease` call site would produce. Hand-written rather than via
  `alembic revision --autogenerate` because autogenerate does not
  reliably preserve the exact text of CHECK expressions. Smallest W1.x
  migration to date: one `ALTER TABLE … ADD CONSTRAINT` in `upgrade()`,
  one `ALTER TABLE … DROP CONSTRAINT` in `downgrade()`. Forward + reverse
  + idempotency round-trip validated against Supabase Postgres 17.6 +
  pgvector 0.8.0 via `backend/.env.validation`.
- **`docs/decisions/ADR-0032-distributed-locks-lease-check.md`** — third
  file-per-ADR under `docs/decisions/` (ADR-0030 was the first,
  ADR-0031 the second). Records the promotion of the §37 Q10 invariant
  verbatim — no bundling with `lease_until >= heartbeat_at` or other
  temporal-anchor invariants (those remain future-ADR territory). 7
  alternatives considered, 3-tier rollback plan, 19-item acceptance
  criteria including an explicit pre-upgrade safety SELECT.
- **`DECISIONS.md`** — one-line cross-link entry for ADR-0032 pointing
  at the new file (status flips from `Proposed` → `Accepted` in the
  pre-merge `docs(adr): mark ADR-0032 Accepted` commit).

#### Changed
- **`backend/app/infrastructure/db/models/operations.py`** —
  `DistributedLock.__table_args__` extended with the matching
  `CheckConstraint("lease_until > acquired_at", name="chk_distributed_locks_lease_until_after_acquired_at")`
  declaration so the ORM mirrors the migration exactly. No import changes
  needed; `CheckConstraint` was already imported during W1.2 for
  `IdempotencyKey`. The existing `Index("ix_distributed_locks_lease_until", "lease_until")`
  is preserved unchanged; the new `CheckConstraint` is placed
  immediately before it inside the `__table_args__` tuple per the
  W1.2 ordering precedent (constraint before index).
- **`docs/database/schema.md`** — §32 column block now lists the CHECK
  constraint inline; new **Lease validity invariant (DB-enforced,
  Phase 3 W1.3)** paragraph mirrors §31's W1.2 FSM-invariant paragraph
  and explains the single-predicate scoping decision (and why
  `lease_until >= heartbeat_at` is intentionally deferred); §32
  reconciliation note revised to acknowledge that W1.3 reverses the 2D
  deferral with stated reasoning (the original "harder to diagnose"
  argument inverts in practice once the CHECK has a descriptive name);
  §37 Q10 row marked **Resolved (Phase 3 W1.3, 2026-06-29)** with full
  constraint details; §37 epilogue Wave 1 bullet for §32 q10 marked
  ✅ Done.
- **`ROADMAP.md`** — Phase 3 wave table W1 row annotated with W1.3
  ✅ Complete alongside W1.1 and W1.2; remaining W1.4 split out.

#### Validated (live, 2026-06-29)
- **Pre-upgrade safety check** — `SELECT COUNT(*) FROM
  distributed_locks WHERE NOT (lease_until > acquired_at)` against
  Supabase returned `0`, clearing the gate for `alembic upgrade head`.
  Zero existing rows in the live target, so the gate is trivially
  satisfied — but the SELECT is run for audit-trail completeness and
  to verify the production-rollback variant (`ADD CONSTRAINT … NOT
  VALID` + later `VALIDATE CONSTRAINT`) is not required.
- **Alembic round-trip** — `upgrade head → downgrade -1 → upgrade head`
  clean; `pg_constraint` diff = exactly one CHECK added on forward,
  exactly one removed on reverse; the constraint's `consrc` predicate
  reads exactly `lease_until > acquired_at`.
- **Schema validator** — `scripts/validate_schema.py` 9/9 (no validator
  code change; none of the 9 structural checks inspect CHECK
  constraints by name — the table-parity check passes by construction
  because the ORM and DB agree on the column shape, which is unchanged
  by W1.3).
- **ERD round-trip** — `regenerate_erd.py` + `compare_erd.py` 0 drift
  (ERD ignores CHECK constraints).
- **Full CI gate** — `scripts/ci_gate.py` 10/10 local; matching GitHub
  Actions run on the PR also 10/10 against the pgvector service
  container.

#### Not modified (scope discipline)
- **`docs/database/INDEX_STRATEGY.md`** — no changes (W1.3 adds zero
  indexes and zero unique constraints; only a CHECK constraint, which
  `INDEX_STRATEGY.md` does not track; the 87-index count stays at 87).
- **`docs/database/ERD.md`** — no changes (ERD tracks entities and FKs
  only; CHECK constraints are invisible to it; the 51-entity / 60-edge
  count stays unchanged).
- **`CONTRIBUTING.md`** — no changes (the file-per-ADR convention was
  already documented in W1.1; ADR-0032 is the third adopter, not the
  convention-establisher).
- **`backend/alembic/versions/0001_baseline.py`** — baseline migrations
  are historical and never amended in place; the new CHECK is added
  entirely by migration `0005` and dropped on its downgrade.

#### Scope discipline (per Wave 1 isolation constraint)
- No changes to `export_jobs` (W1.1 territory), `idempotency_keys`
  (W1.2), or `usage_records` (W1.4). W1.4 gets its own branch + ADR.

### Phase 3 Wave 1.2 — `idempotency_keys` mutability + status↔response invariant (2026-06-29, ADR-0031)

#### Added
- **`backend/alembic/versions/0004_idempotency_keys_invariants.py`** — Alembic
  migration applying three coordinated changes to `idempotency_keys` in a
  single transaction: (1) `ADD COLUMN updated_at timestamptz NOT NULL
  DEFAULT now()`, (2) `CREATE TRIGGER tg_idempotency_keys_biu_touch_updated_at`
  bound to the shared `touch_updated_at()` function (already defined in
  the baseline, already wired to 30+ other tables), and (3) `ADD
  CONSTRAINT chk_idempotency_keys_response_hash_matches_status CHECK
  ((status = 'in_flight') = (response_hash IS NULL))`. Hand-written
  rather than via `alembic revision --autogenerate` because autogenerate
  does not emit `CREATE TRIGGER` statements and would not preserve the
  exact text of the CHECK expression or the explicit sequencing of the
  three ops. Forward + reverse + idempotency round-trip validated
  against Supabase Postgres 17.6 + pgvector 0.8.0 via
  `backend/.env.validation`.
- **`docs/decisions/ADR-0031-idempotency-keys-invariants.md`** — second
  file-per-ADR under `docs/decisions/` (ADR-0030 was the first). Records
  the promotion of two long-standing application-layer assumptions to
  the DB: the mixin misclassification that left `idempotency_keys` in a
  "mutable-but-untracked" state, and the unprotected status↔response
  FSM invariant. 8 alternatives considered, 3-tier rollback plan, 17-item
  acceptance criteria including an explicit pre-upgrade safety SELECT.
- **`DECISIONS.md`** — one-line cross-link entry for ADR-0031 pointing
  at the new file (status flips from `Proposed` → `Accepted` in the
  pre-merge `docs(adr): mark ADR-0031 Accepted` commit).

#### Changed
- **`backend/app/infrastructure/db/models/operations.py`** — `IdempotencyKey`
  switched from `CreatedAtOnlyMixin` to `TimestampMixin` (the original
  mixin choice was a Phase 2 Step-A misclassification — `CreatedAtOnlyMixin`
  is documented as "for immutable / append-only tables" but the row IS
  mutated `in_flight → succeeded`/`failed`). `__table_args__` extended
  with the matching `CheckConstraint(..., name="chk_idempotency_keys_response_hash_matches_status")`
  declaration so the ORM mirrors the migration exactly. `CheckConstraint`
  added to the SQLAlchemy import line; `CreatedAtOnlyMixin` removed
  from the mixins import.
- **`docs/database/schema.md`** — §31 column block now lists
  `updated_at`; new **FSM invariant (DB-enforced, Phase 3 W1.2)**
  paragraph explains the CHECK's scope decision (`response_hash` only,
  not `response_payload` or `http_status`); §31 reconciliation note
  updated to acknowledge that W1.2 reverses the 2D `updated_at`
  omission with stated reasoning (the original "audit event covers it"
  rationale conflated audit replay with operational observability);
  §37 Q9 row marked **Resolved (Phase 3 W1.2, 2026-06-29)**; Wave 1
  bullet for §31 q9 marked ✅ Done.
- **`ROADMAP.md`** — Phase 3 wave table W1 row annotated with
  W1.2 ✅ Complete alongside W1.1; remaining W1.3 / W1.4 split out.

#### Validated (live, 2026-06-29)
- **Pre-upgrade safety check** — `SELECT COUNT(*) FROM idempotency_keys
  WHERE (status = 'in_flight') <> (response_hash IS NULL)` against
  Supabase returned `0`, clearing the gate for `alembic upgrade head`.
- **Alembic round-trip** — `upgrade head → downgrade -1 → upgrade head`
  clean; `pg_constraint` diff = exactly one CHECK added on forward,
  exactly one removed on reverse; `pg_trigger` diff = exactly one BIU
  trigger added on forward, exactly one removed on reverse;
  `information_schema.columns` confirms `updated_at` is
  `timestamp with time zone NOT NULL` after upgrade and gone after
  downgrade.
- **Schema validator** — `scripts/validate_schema.py` 9/9 (no validator
  code change; `check_table_parity` picked up the new `updated_at`
  column automatically from ORM metadata; no validator check covers
  CHECK constraints or `_UPDATED_AT_TABLES` membership, so those
  remain green by construction).
- **ERD round-trip** — `regenerate_erd.py` + `compare_erd.py` 0 drift
  (ERD ignores CHECK constraints, triggers, and per-column shape; only
  entity-level changes show up there).
- **Full CI gate** — `scripts/ci_gate.py` 10/10 local; matching GitHub
  Actions run on the PR also 10/10 against the pgvector service container.

#### Not modified (scope discipline)
- **`docs/database/INDEX_STRATEGY.md`** — no changes (W1.2 adds zero
  indexes and zero unique constraints; only a column, a trigger, and a
  CHECK constraint — none of which `INDEX_STRATEGY.md` tracks).
- **`CONTRIBUTING.md`** — no changes (the file-per-ADR convention was
  already documented in W1.1; ADR-0031 is the second adopter, not the
  convention-establisher).
- **`backend/alembic/versions/0001_baseline.py`** — baseline migrations
  are historical and never amended in place; the new trigger is added
  entirely by migration `0004` and dropped on its downgrade.

#### Scope discipline (per Wave 1 isolation constraint)
- No changes to `export_jobs` (W1.1 territory), `distributed_locks`
  (W1.3), or `usage_records` (W1.4). W1.3 / W1.4 each get their own
  branch + ADR.

### Phase 3 Wave 1.1 — `export_jobs` partial-unique constraint (2026-06-29, ADR-0030)

#### Added
- **`backend/alembic/versions/0003_export_jobs_partial_unique.py`** — Alembic
  migration creating the partial-unique index
  `uq_export_jobs_render_job_id_format_quality_orientation` on
  `export_jobs (render_job_id, format, quality, orientation)` with
  `WHERE status IN ('queued','running','succeeded')`. Hand-written rather
  than via `alembic revision --autogenerate` because autogenerate does not
  reliably emit partial-unique indexes via `postgresql_where` (it produces
  a vanilla unique constraint instead). Forward + reverse + idempotency
  round-trip validated against Supabase Postgres 17.6 + pgvector 0.8.0
  via `backend/.env.validation`.
- **`docs/decisions/ADR-0030-export-jobs-partial-unique.md`** — first
  file-per-ADR under the new `docs/decisions/` directory. Records the
  promotion of the `(render_job_id, format, quality, orientation)`
  uniqueness invariant from the use-case layer (where it had no consumer
  yet) directly to the database, with full rationale, 7 rejected
  alternatives, 3-tier rollback plan, and 15-item acceptance criteria.
  ADRs 0001–0029 remain inline in `DECISIONS.md`; all Phase-3-and-later
  ADRs use the file-per-ADR convention.
- **`DECISIONS.md`** — one-line cross-link entry for ADR-0030 pointing
  at the new file (status flips from `Proposed` → `Accepted` in the
  pre-merge `docs(adr): mark ADR-0030 Accepted` commit).

#### Changed
- **`backend/app/infrastructure/db/models/jobs.py`** — `ExportJob.__table_args__`
  extended with the matching `Index(..., unique=True, postgresql_where=text(...))`
  declaration so the ORM mirrors the migration exactly. Same shape as
  the existing partial-unique pattern used across the model layer.
- **`docs/database/schema.md`** — §17 reconciliation note for `export_jobs`
  flipped from "Phase-3 decision" to "Implemented via ADR-0030 / migration
  `0003`"; §37 Q8 row marked **Resolved (Phase 3 W1.1, 2026-06-29)**;
  Wave 1 bullet for §17 q8 marked ✅ Done.
- **`docs/database/INDEX_STRATEGY.md`** — §8 `export_jobs` row moved
  **Deferred (Phase 3)** → **Implemented** with full predicate spelled out;
  §18 reconciliation summary counts updated (indexes 81 → 82,
  unique constraints 23 → 24, Implemented rows 73 → 74,
  Deferred (Phase 3) 21 → 20).
- **`ROADMAP.md`** — Phase 3 wave table W1 row annotated with
  W1.1 ✅ Complete and the remaining W1.2 / W1.3 / W1.4 split out.
- **`CONTRIBUTING.md`** — §1 ground rule 2 and §6 documentation policy
  updated to acknowledge the new `docs/decisions/` file-per-ADR
  convention (introduced by ADR-0030) while preserving compatibility
  with the inline ADRs 0001–0029 in `DECISIONS.md`.

#### Validated (live, 2026-06-29)
- **Pre-upgrade safety check** — `SELECT COUNT(*) FROM export_jobs WHERE
  status IN ('queued','running','succeeded')` against Supabase returned
  `0`, clearing the gate for the in-development upgrade path.
- **Alembic round-trip** — `upgrade head → downgrade -1 → upgrade head`
  clean; `pg_indexes` diff = exactly one row added on forward, exactly
  one row removed on reverse; `indexdef` contains the expected
  `WHERE … status = ANY` predicate.
- **Schema validator** — `scripts/validate_schema.py` 9/9 (no validator
  code change; `check_unique_constraints` and `check_indexes` picked up
  the new `Index(unique=True, postgresql_where=…)` automatically from
  ORM metadata).
- **ERD round-trip** — `regenerate_erd.py` + `compare_erd.py` 0 drift
  (ERD ignores non-FK indexes).
- **Full CI gate** — `scripts/ci_gate.py` 10/10 local; matching GitHub
  Actions run on the PR also 10/10 against the pgvector service container.

#### Scope discipline (per Wave 1 isolation constraint)
- No changes to `idempotency_keys`, `distributed_locks`, or
  `usage_records`. W1.2 / W1.3 / W1.4 each get their own branch + ADR.

### Phase 2D — Documentation Reconciliation (2026-06-29, approved by reviewer; no code changes)

#### Verification
- **Manual spot-check** (8/8 MATCH) — `PHASE2D_SPOT_CHECK.md`. Eight
  models (`tenants`, `projects`, `project_tags`, `workflow_runs`,
  `usage_records`, `credit_ledger`, `audit_log`, `provider_settings`)
  compared by hand against ORM, baseline migration, `schema.md`,
  `ERD.md`, and `INDEX_STRATEGY.md`. Zero semantic mismatches.
- **CI quality gate** — re-run with no code changes; 10/10 stages
  green (5 non-DB + 5 DB; oracle migration round-trip clean; schema
  validator 9/9; ERD compare 0 drift; coverage 100% over Phase 2 scope).
- **Phase 3 wave sequencing** recorded in `ROADMAP.md` and
  `schema.md` §37 (Waves 1–4).
- **Baseline tag (pre-flight)** — deferred to the user. Workspace is
  not yet a git repository; exact `git init`/commit/tag command
  sequence recorded in `ROADMAP.md` Phase 3 Pre-flight section.


#### Changed (docs only)
- **`docs/database/schema.md`** — added a top-of-doc audit-of-truth rule
  ("implementation is the source of truth"); reconciled §16 (workflow
  runs/steps/checkpoints), §17 (render/export jobs), §18 (usage records),
  §19 (cost reconciliations), §20 (plans/subscriptions/invoices), §22
  (feature flags), §25 (event outbox), §26 (event log), §27 (system /
  tenant / provider settings), §31 (idempotency keys), §32 (distributed
  locks), and §33 (audit log) to match the validated ORM column shapes,
  FK shapes, and indexes. Each section carries an inline
  "Reconciled in 2D" note documenting what changed and why. Added §37
  cataloguing the 13 questions deferred to Phase 3 entry (relationship()
  pattern, deferred indexes, `cost_reconciliations` immutability,
  `auth_role` enum retention, ERD cross-cluster elision policy, …).
- **`docs/database/ERD.md`** — added a top-of-doc reconciliation note;
  rewrote the column shapes in Cluster 6 (workflows / render / export),
  Cluster 7 (usage records / cost reconciliations), Cluster 8 (billing),
  Cluster 9 (feature flags / event outbox / event log), and Cluster 10
  (config / operations / audit) to match the ORM. Cross-cluster FK
  elision policy made explicit so `compare_erd.py` continues to report
  zero design-edge drift.
- **`docs/database/INDEX_STRATEGY.md`** — full rewrite. Every row is now
  labeled `Implemented` (matches an ORM index by name), `Renamed` (the
  design name differed; row updated to the actual ORM name), or
  `Deferred (Phase N)` with a Phase-3 entry decision attached. Added
  §16 (Phase 3 index decisions) and §18 (reconciliation summary:
  81 implemented indexes + 23 unique constraints).
- **`docs/database/BACKUP_RESTORE.md`** — `_backup_sentinel` column
  shape updated from the draft `(taken_at, marker)` to the shipped
  `(inserted_at, label, notes)`.
- **`DECISIONS.md`** — renumbered the second ADR-0028 to **ADR-0029**
  ("CI Quality Gate Operational Contract — Phase 2C Ratification") to
  resolve the duplicate ADR id surfaced by the architectural audit.
  ADR-0028 retains its original content. ADR-0029's Context paragraph
  notes the renumber explicitly.

#### Not changed (deferred to Phase 3 entry by reviewer rule)
- ORM models / Alembic migrations / database schema / seed data / CI
  gate remained untouched. The validation harness (`validate_schema.py`)
  and ERD round-trip continue to pass with the same 81 indexes,
  95 FKs, 52 base tables. The architectural audit's recommendations on
  `relationship()` adoption, additional indexes, `cost_reconciliations`
  immutability, `auth_role` retention, and cross-cluster ERD edges
  were deliberately left as Phase-3-entry questions per the reviewer's
  guidance.

### Phase 2C — CI Quality Gate (implementation complete, awaiting reviewer)

#### Added
- **`backend/scripts/ci_gate.py`** — cross-platform 10-stage runner
  (ruff → black → mypy + import-linter → pytest+cov → alembic up → down
  → up → validator → ERD diff → coverage threshold). Stages 5–9 are
  skipped (not failed) when `DATABASE_URL` is absent so the
  laptop-no-Postgres path still works.
- **`backend/scripts/run_ci_gate.ps1`** — PowerShell wrapper for Windows
  developers; thin convenience layer over `ci_gate.py` with stage-range
  pass-through and credential redaction in the banner.
- **`.github/workflows/ci.yml`** — GitHub Actions wiring: triggers on
  PRs and pushes to `main`, runs against a `pgvector/pgvector:pg16`
  service container, uploads validator + ERD + coverage artefacts, and
  appends the coverage report to the job summary.
- **`backend/tests/`** — Phase 2C smoke suite (24 tests, **100 % branch
  coverage** on `app/` for Phase 2C scope):
  - `test_models_import.py` — every model module imports; metadata
    contains the expected aggregate-root subset; `Base` is declarative
    and shares the canonical metadata.
  - `test_metadata.py` — partitioned parents declare
    `postgresql_partition_by`; every FK declares an explicit
    `ON DELETE`; immutable tables have no `updated_at`/`deleted_at`;
    pgvector is scoped to the two approved columns; naming convention is
    populated; no naive `DateTime` columns.
  - `test_mixins.py` — UUID PK, timestamp, soft-delete, version, and
    created-at-only mixins all expose the documented column shapes; the
    UUID PK Python default is the `uuid.uuid4` factory (verified by
    `__module__` + `__qualname__` to survive import-system reloads).
  - `test_enums.py` — enum count pinned at 26, all `native_enum=True`,
    all values lowercase snake_case, no duplicate values, no PG type
    name collisions.
- **`backend/pyproject.toml`** — `black`, `pytest`, `pytest-cov`,
  `pytest-asyncio`, `types-PyYAML`, `import-linter` added to
  `[project.optional-dependencies.dev]`; configs added for
  `[tool.black]`, `[tool.pytest.ini_options]`, `[tool.coverage.*]`,
  `[tool.importlinter]`; existing `[tool.ruff]` extended with
  `SIM/C4/RUF` rule sets and per-file ignores for migrations / tests /
  scripts; `[tool.mypy]` narrowed to `app/` only with strict mode
  preserved.
- **`CI_QUALITY_GATE.md`** — stage map, runtime budgets, local
  invocation contract, failure runbook, and coverage threshold roadmap
  (60 % → 80 % → 85 % across phases).
- **`DECISIONS.md` ADR-0028** — "Mandatory CI Quality Gate Before
  Phase 3" (ratified at close of Phase 2 Step B).
- **Architectural fitness contracts** (`import-linter`, 4 contracts):
  domain layer has no infra / app / api deps; DB models cannot import
  app / api; api layer cannot import infra directly; application layer
  never imports api.
- **`backend/app/{domain,application,api}/__init__.py`** — empty
  package skeletons created at the close of Phase 2 so the
  architectural contracts are live the moment any Phase 3 code lands.

#### Changed
- **`backend/app/infrastructure/db/models/*.py`** — 39 `Mapped[dict]` /
  `Mapped[list]` annotations parameterised to `Mapped[dict[str, Any]]`
  / `Mapped[list[Any]]` (resolved 39 of 44 mypy `--strict` errors);
  three unused `# type: ignore[assignment]` comments removed from
  pgvector fallback branches.
- **`backend/scripts/ci_gate.py`** stage 3 — now invokes both `mypy`
  and `lint-imports` (previously only `mypy` despite the title); the
  `lint-imports` entrypoint is resolved relative to the active venv to
  avoid PATH surprises.

#### Self-tested (local, 2026-06-29)
- Stages 1–4 (lint / format / static analysis / tests + coverage):
  **green** — 24 tests pass, mypy 0 errors, lint-imports 0 violations.
- Stages 8–10 (live schema validator / ERD diff / coverage threshold):
  **green** against Supabase Postgres 17.6 + pgvector 0.8.0 — 9/9
  structural checks pass, 51/51 entities + 58/58 design edges in ERD
  round-trip, coverage 100 % over the 22 `app/` modules currently in
  scope (well above the 60 % Phase 2C threshold).
- Stages 5–7 (alembic up/down/up): deliberately not re-exercised in the
  self-test to avoid re-running migrations against the live target;
  wired identically to the proven Step B validation path and will
  execute against the pgvector service container in CI.

#### Pending (Phase 2C exit criteria)
- Reviewer sign-off on `CI_QUALITY_GATE.md` + ADR-0028 → unlocks
  Phase 3.

---

### Phase 2 — Database, Step B: SQLAlchemy + Alembic — ✅ APPROVED 2026-06-28

#### Added
- `backend/pyproject.toml`, `backend/alembic.ini`, `backend/alembic/env.py`,
  `backend/alembic/script.py.mako`.
- Declarative base + naming convention (`app/infrastructure/db/base.py`).
- Reusable mixins: `UUIDPrimaryKeyMixin`, `TimestampMixin`,
  `SoftDeleteMixin`, `VersionMixin`, `CreatedAtOnlyMixin`.
- Central ENUM registry (`app/infrastructure/db/enums.py`).
- 23 ORM model files (`app/infrastructure/db/models/*.py`) covering every
  table in `docs/database/schema.md`.
- Alembic baseline migration `0001_baseline.py` — extensions, ENUMs,
  helper PL/pgSQL functions, all tables, indexes (incl. imperative
  GIN / HNSW), triggers (`touch_updated_at`, `bump_version`,
  `reject_mutation`, `enforce_credit_ledger_balance`), partition
  bootstrap (current month + 24 forward months + default partitions),
  and the deferred `projects.current_version_id` FK.
- Alembic seed migration `0002_seed_system_data.py` — plans, feature
  flags, provider plugins, AI model catalogue, RBAC roles, and the
  initial system settings rows. Idempotent via `ON CONFLICT DO NOTHING`.
- Schema validator (`backend/scripts/validate_schema.py`) — 9 automated
  checks covering extensions, tables, partitions, FKs, unique
  constraints, indexes, immutability triggers, pgvector column scope,
  and the credit_ledger balance trigger.
- ERD regenerator (`backend/scripts/regenerate_erd.py`) — Mermaid output
  for stable diffs against `docs/database/ERD.md`.
- One-command orchestrator (`backend/scripts/run_validation.py` and
  PowerShell wrapper `run_validation.ps1`) implementing the
  upgrade → downgrade → re-upgrade → introspect → ERD-regenerate cycle.
- `backend/docker-compose.db.yml` — local pgvector Postgres 16.
- `SCHEMA_VALIDATION.md` — methodology, checks, run instructions,
  pending live-run section.
- `PROJECT_STATUS.md` — living project status with version, milestones,
  debt, risks, open questions, and step-level checklist.
- ADR-0027 — Tenant-Scoped Billing Aggregates (`DECISIONS.md`).

#### Changed (during live validation)
- Validator rewritten against `pg_catalog`: a single `load_snapshot(engine)`
  pulls every base table, FK, and index in three bulk queries; per-check
  functions consume the cached snapshot instead of issuing ~400 per-table
  `inspect()` round-trips. Validator runtime against the Supabase pooler:
  **263 s → 17 s.**
- ERD regenerator rewritten against `pg_catalog`; partition children
  excluded at the SQL level so the FK query no longer hits Supabase's
  2-minute `statement_timeout`. ERD generation: **>120 s timeout → 13 s.**
- `alembic_version` whitelisted in `validate_schema.py`'s table-parity check
  (it's Alembic's own bookkeeping; not in the ORM `metadata`).
- `validate_schema.py` redacts the password from the connection URI in
  `schema_validation_report.json`.
- `alembic/env.py` doubles `%` in URL-encoded passwords before handing the
  URI to ConfigParser (fixes `%40` → `@` round-tripping for Supabase URIs).
- Credentials now loaded via `_load_env.py` from `backend/.env.validation`
  (git-ignored); never appear on the shell command line.
- `docs/database/ERD.md` Cluster 8 (Billing) corrected: subscriptions are
  tenant-scoped (not user-scoped); invoices are subscription-scoped;
  `users → credit_ledger` is nullable (SET NULL).
- `docs/database/ERD.md` Cluster 7 (Media/Library): direction of the
  `media_assets ↔ library_assets` edge corrected (library_assets has the
  FK, not the other way around).
- `docs/database/ERD.md` Clusters 5/9: `provider_plugin_registrations →
  ai_models` and `event_outbox → event_log` converted to Mermaid comments
  (logical references — no DB FK).
- `docs/database/schema.md` §20–§21 corrected to match the implementation
  (subscriptions/invoices have no `user_id` column; credit_ledger.user_id
  is nullable with SET NULL).

#### Validated (live, 2026-06-28)
- Target: Supabase managed PostgreSQL 17.6 + pgvector 0.8.0
  (ap-northeast-2 session pooler, IPv4).
- `alembic upgrade head` ✅; `alembic downgrade base` ✅
  (only `alembic_version` retained); `alembic upgrade head` again ✅
  (idempotency proven).
- All 9 structural checks pass: 5 required extensions, 52 ORM tables,
  4 partitioned parents (27 children each), 95 FKs, all declared
  unique indexes, 86 indexes including 5 imperative GIN/HNSW,
  8 immutable-trigger-protected tables, exactly 2 pgvector columns,
  `credit_ledger` balance trigger present.
- ERD round-trip: 51/51 entities match; 58/58 design-declared edges
  present in implementation; 0 design edges missing.

#### Pending
- Reviewer sign-off on `SCHEMA_VALIDATION.md` §6.

### Phase 2 — Database, Step A: Design Documents (APPROVED 2026-06-28, revision 2)

#### Added (initial)
- `docs/database/NAMING_CONVENTIONS.md`
- `docs/database/ERD.md` (Mermaid ER diagram covering every aggregate root)
- `docs/database/schema.md` (full table-by-table schema with FKs / ON DELETE / uniqueness / checks)
- `docs/database/INDEX_STRATEGY.md`
- `docs/database/RETENTION_POLICY.md`
- `docs/database/BACKUP_RESTORE.md`

#### Added (revision 2 — final design CRs)
- **CR-DB-1** First-class Idempotency Framework — `idempotency_keys` table (ADR-0021).
- **CR-DB-2** Database-backed Distributed Locks — `distributed_locks` table with lease + heartbeat (ADR-0022).
- **CR-DB-3** Audit Log — partitioned, immutable `audit_log` table separate from `event_log`, Class C retention (ADR-0023).
- **CR-DB-4** Explicit Configuration Tables — `system_settings`, `tenant_settings`, `provider_settings`; generic `settings` table removed (ADR-0024).
- ADR-0025 — defer `user_preferences` to `users.extra` JSONB.
- ERD cluster 10 (Configuration & Operations) added.
- Index strategy §14a/§14b/§14c added.
- Retention policy updated: `audit_log` → Class C (7 years); `idempotency_keys` / `distributed_locks` → TTL classes.
- Immutability verification job now also covers `audit_log` and `cost_reconciliations`.

#### Pending
- Step A review and approval → unlocks Step B (SQLAlchemy models + Alembic baseline) following the execution order recorded in `ROADMAP.md` Phase 2 Step B.

---

## [Phase 1 — 2026-06-28] — Architecture & Folder Structure (Rev 3, APPROVED)

#### Added
- `rule.md` — governing requirements document with anti-hallucination guardrails.
- `ARCHITECTURE.md` — full system architecture, folder structure, and tech decisions (rev 3).
- `ROADMAP.md` — phased delivery plan with explicit exit criteria.
- `DECISIONS.md` — twenty ADRs (ADR-0001 … ADR-0020).
- `CONTRIBUTING.md` — coding standards and contribution workflow.
- `API_CONTRACT.md` — API surface designed before implementation.
- **CR-1** AI Provider Plugin System (`BasePlugin` + capability ABCs + `@register_plugin`).
- **CR-2** Multiple Rendering Pipelines (Pipeline A stock-footage, B AI-images-motion, C AI-video-clips).
- **CR-3** Split AI orchestration into seven subpackages: `agents`, `providers`, `prompts`, `memory`, `tools`, `chains`, `workflows`.
- **CR-4** Event Bus (Redis Streams default, NATS/Kafka pluggable) with canonical topic registry and transactional outbox.
- **CR-5** Multi-storage Provider plugins (Local / S3 / R2 / Azure Blob / GCS).
- **CR-6** Versioned Projects — immutable `ProjectVersion` snapshots, branching, restore.
- **CR-7** Resumable Workflow Engine with Postgres checkpointer.
- **CR-8** Asset Library — auto-persist every generated artefact.
- **CR-9** Feature Flags — pluggable provider, default DB-backed, optional Unleash.
- **CR-10** Explicit Domain Layer — framework-free `app/domain/` with named aggregate roots.
- **CR-11** AI Model Registry — model catalogue, deprecation lifecycle, default-selection chain.
- **CR-12** AI Cost Tracking — single recorder middleware producing immutable `UsageRecord` per call.
- **CR-13** Five-tier Priority Queues — `critical / high / normal / low / background` with tenant fairness.

#### Approved
- 2026-06-28 — User approved Phase 1 Rev 3; Phase 2 unlocked.

---

## How to Update This Changelog

When a phase is accepted:

1. Move the **Unreleased** section into a new dated entry: `## [Phase N — YYYY-MM-DD]`.
2. Group changes under: **Added**, **Changed**, **Deprecated**, **Removed**, **Fixed**, **Security**.
3. Reference ADRs and CRs by ID.
4. Start a fresh **[Unreleased]** block.

Format example:

```
## [Phase 2 — 2026-MM-DD] — Database

### Added
- Alembic baseline migration.
- ORM models for every aggregate root listed in `ARCHITECTURE.md` §6.
- pgvector extension.

### Security
- Per-row `tenant_id` enforced via DB-level row-level security policies.
```
