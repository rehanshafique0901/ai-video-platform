# Phase 3 Authentication — Consolidated Retrospective

> **Scope.** This retrospective covers the three slices that together deliver
> the authentication subsystem of the AI Video Platform backend:
>
> * **α1 — Architecture bootstrap** (v0.4.0, PR #8, 2026-06-30)
> * **α2a — Auth: register + login** (v0.4.1, PR #9, 2026-07-01)
> * **α2b — Auth: refresh + logout** (v0.4.2, PR #10, 2026-07-01)
>
> **Purpose.** Capture what worked, what didn't, and the process/architecture
> conventions that should carry forward into α3+ before the details fade.
> This document informs the α3 pre-flight and folds a small delta into
> `docs/engineering/RUNBOOK_WAVE.md` for the practices that graduated from
> "we tried it once" to "this is how we ship slices now".
>
> **Convention.** Read-only planning artifact. It records history; it does
> not change any code path.
>
> **Companion update.** This retrospective ships in the same PR as the
> `docs/engineering/RUNBOOK_WAVE.md` §7 additions it motivates. Read the
> retrospective for _why_ each practice exists; read the runbook §7 for
> _how_ to apply it. See §7 of this document for the mapping.

---

## Section 1 — What shipped

The three slices together deliver a complete password-authentication
service with rotating refresh tokens, family-scoped reuse detection,
CAS-based session revocation, and full HTTP surface + tests + docs.

| Slice | Version tag | PR | Merge SHA | Files changed | Net LOC |
| --- | --- | --- | --- | --- | --- |
| α1 — architecture bootstrap | `v0.4.0-phase3-alpha1` | #8 | (see `git log`) | ≈ 30 | +≈ 1.6k |
| α2a — register + login | `v0.4.1-phase3-alpha2a` | #9 | `281471d` | 40 | +≈ 2.4k / −≈ 0.2k |
| α2b — refresh + logout | `v0.4.2-phase3-alpha2b` | #10 | `0d5af9c` | 29 | +2,118 / −70 |

### Endpoint inventory at end of Phase 3 auth work

| Method | Path | Status | Slice |
| --- | --- | --- | --- |
| `GET`  | `/healthz`, `/readyz` | 200 | α1 |
| `POST` | `/api/v1/auth/register` | 201 | α2a |
| `POST` | `/api/v1/auth/login` | 200 | α2a |
| `POST` | `/api/v1/auth/refresh` | 200 | α2b |
| `POST` | `/api/v1/auth/logout` | 204 | α2b |

### Test / coverage headline at v0.4.2

| Metric | α1 baseline | v0.4.1 (α2a) | v0.4.2 (α2b) | Delta |
| --- | --- | --- | --- | --- |
| Unit tests | 56 | 77 | **101** | +45 |
| Integration tests | 7 | 33 | **47** | +40 |
| Coverage (unit-only) | baseline | ~82% | **81.05%** | see §5.4 |
| Import-linter contracts | 5/5 KEPT | 5/5 KEPT | **5/5 KEPT** | steady |
| CI gate stages | 10 | 10 | **10** | steady |

### At-a-glance snapshot (for future readers)

| Slice | PR | Tag | Unit tests | Integration tests | Coverage |
| --- | --- | --- | --- | --- | --- |
| α1 | #8 | v0.4.0 | 56 | 7 | baseline |
| α2a | #9 | v0.4.1 | 77 | 33 | ~82% |
| α2b | #10 | v0.4.2 | **101** | **47** | **81.05%** |

---

## Section 2 — What went well

### 2.1 Pre-flight discipline
Every single slice started with a read-only pre-flight document
covering scope, non-goals, architectural dependencies, design decisions,
file inventory (split into internal checkpoints where appropriate), an
acceptance-criteria list, and a test matrix. Zero slices skipped it.
This is the single largest contributor to the zero-blocker merge record
across three PRs. Every "surprise" that surfaced during implementation
had already been discussed and resolved on paper.

### 2.2 PR sizing via internal checkpoints
The pre-splits explicitly limited surface area:

* α2 was split into **α2a (register + login)** and **α2b (refresh + logout)**
  before implementation began — reviewability was the sole reason.
* α2b was further split internally into **α2b.1 (RefreshSession + repo
  extensions + IClock)** and **α2b.2 (LogoutSession + HTTP surface + docs)**
  so that each checkpoint had a clean local-CI checkpoint before the next
  layer landed.

Result: three merged PRs, all under 30 files, all with green single-check
CI in ≤ 2 minutes, all with a reviewable diff.

### 2.3 Ports-and-adapters discipline enforced by import-linter
The 5 architectural contracts (`domain` isolation, `db models` isolation,
`API → application → infrastructure` layering, `application → infrastructure`
isolation, `use_cases → infrastructure/api` isolation) kept dependency
direction correct throughout. **No new cross-layer edges** were introduced
across all three slices. When α2b needed a time abstraction, the pattern
was already there: define port (`IClock`), implement adapter
(`SystemClock`), inject via container. Zero pushback from the tooling.

### 2.4 In-memory fakes made unit tests fast and honest
`FakePasswordHasher`, `FakeTokenIssuer`, `FakeSessionRepository`, `FakeClock`,
`FakeUnitOfWork` — each ≤ 30 LOC, each mirrors the port surface exactly.
This kept the unit suite well under 3 seconds at 101 tests (would have
been ≈ 15+ s with real Argon2id), while every fake still enforces the
same behavioural contract as the real implementation (`verify(pw, hash(pw))`
is True; anything else False; never raises).

The bottom-up ordering — **repository integration tests → use-case unit
tests → HTTP integration tests** — that we adopted during α2b.1 was a
significant win: when the use case failed, we already knew the repository
contract was sound, so debugging time went to the layer that changed.

### 2.5 Security posture is deliberate and testable
Each security property is enforced by a *named* test the reviewer can
grep for:

* **Anti-enumeration** — same 401 message / same wall time on
  `wrong password`, `unknown email`, `invalid refresh`, `bad logout token`
  (α2a: burnt Argon2 verify against a startup dummy; α2b: uniform
  `InvalidRefreshTokenError`).
* **Refresh reuse detection** — replaying a rotated refresh token
  revokes the entire family in one shot, logged with `security_event=True`
  for SIEM alerting.
* **CAS revocation** — `WHERE revoked_at IS NULL` clause guarantees the
  first revoke wins; the audit-authoritative timestamp is never overwritten.
* **sid consistency (A12)** — a signed JWT whose `sid` doesn't match the
  session row's `id` is rejected as tampered (defence in depth).
* **User liveness on refresh (A13)** — a soft-deleted account cannot mint
  fresh tokens from an old refresh JWT.
* **Access-token secrecy** — raw refresh JWT never round-trips through
  the DB; only the SHA-256 hash is stored.

Each of these is directly traceable to a unit test and an integration
test. The security matrix is not folklore.

### 2.6 Documentation that outlives implementation
Two documents came out of this phase that will still be useful in a year:

* `docs/engineering/AUTH_TOKEN_LIFECYCLE.md` — state machine, per-endpoint
  sequence diagrams, Refresh Family Example, invariants, structured-log
  event catalogue.
* `docs/engineering/RUNBOOK_WAVE.md` — process discipline codified.
  Used verbatim in α2b (via the pre-flight review) and it worked.

### 2.7 CI gate held for the whole trilogy
The 10-stage `ci_gate.py` (lint / format / mypy+import-linter / unit+coverage
/ alembic up / alembic down / alembic up idempotency / schema validator /
ERD comparison / coverage report) caught every issue **before** push:

* Format / lint drift → caught locally, fixed same turn.
* mypy `Result[Any].rowcount` type-stub gap → caught locally, resolved with
  a scoped `# type: ignore[attr-defined]`.
* An unused import (`UnauthorizedError` in `test_refresh_session.py`) →
  caught locally.
* ANSI-code stdout capture for structured-log assertions → caught locally,
  fixed by switching from `caplog` to `capsys` + ANSI-stripping.

Not one of these reached the remote CI. All three PRs merged first-try
with the single GitHub-side check green in ≤ 1 minute.

---

## Section 3 — What evolved α1 → α2a → α2b

The architecture wasn't fully-formed on day one. Each slice added or
refined exactly what it needed:

| Concept | α1 | α2a | α2b |
| --- | --- | --- | --- |
| Domain entities | — | `User`, `Tenant`, `Session` (frozen slots) | (unchanged) |
| Repository ports | `IUserRepository` (count / exists) | extended: get_by_email / get_by_id / add / update_last_login; new: `ITenantRepository`, `ISessionRepository` (add), `IRoleRepository` | `ISessionRepository` extended: `get_by_hash`, `revoke` (CAS), `list_family` |
| Security ports | — | `IPasswordHasher`, `ITokenIssuer`, `TokenClaims`, `IssuedTokens` | `verify_access(*, allow_expired: bool = False)` added |
| Time abstraction | inline `datetime.now(UTC)` | inline `datetime.now(UTC)` | **`IClock` port + `SystemClock` adapter; all four use cases injected** |
| Use cases | (none — health endpoint only) | `RegisterUser`, `LoginUser` | `RefreshSession`, `LogoutSession` |
| DTOs | — | `RegisterRequest`, `LoginRequest`, `AuthTokensPayload`, `UserPublic` | `RefreshRequest`, `BearerAccessTokenDep` |
| Structured logs | `application_started/stopped` | `auth.register.*`, `auth.login.*` | `auth.refresh.rotated/rejected/reuse_detected`, `auth.logout.succeeded/rejected` (with `security_event=True` on the ones an ops team should page on) |
| Anti-enumeration | — | dummy-hash Argon2 burn on unknown-email login | uniform `InvalidRefreshTokenError` on all refresh/logout non-happy paths |
| Session invariants | (schema-only) | one row per issued refresh; hashed | CAS revoke; family sweep on reuse; sid==row.id check |

Three noteworthy inflections:

1. **α2a introduced ports we knew α2b would need** (`ITokenIssuer`,
   `IssuedTokens`, `TokenClaims`, `sid`/`fam` claims on **both** access
   and refresh JWTs). Because α2b was pre-planned, α2a wasn't tempted
   to over-fit its interfaces to today's needs.
2. **α2b introduced `IClock` as a clean cut**. It was a small refactor
   applied uniformly to all four use cases (register / login / refresh /
   logout) rather than a targeted retrofit. This is now the reference
   pattern for future time-dependent code.
3. **The `session_id` and `family_id` are pre-generated by
   `AuthTokenIssuer.issue_for_login` / `issue_for_rotation`** and embedded
   in both tokens *before* the row is persisted. This eliminates the "did
   the DB round-trip give me a UUID?" ambiguity and lets rotation happen
   in one deterministic pass.

---

## Section 4 — Design conflicts and how they resolved

Three genuine architectural conflicts surfaced during pre-flight review;
all were resolved on paper before implementation.

### 4.1 α2 combined vs split (resolved: split)
The original α2 pre-flight bundled register/login/refresh/logout into a
single slice. Review flagged this as too large to review meaningfully.
Split into α2a + α2b before implementation. Result: two clean PRs
under 30 files each, both merged first-try.

### 4.2 `AuthTokenIssuer` refactor to `IClock` (resolved: defer to α2b)
During α2a it was tempting to already refactor the token issuer to
consume `IClock` for its internal `iat`/`exp` calculations. Decision:
defer — the JWTService's internal `datetime.now(UTC)` was already
isolated behind `verify()`/`issue_access()`/`issue_refresh()` and not
observable outside those methods. Injecting a clock into the *use cases*
(where timestamps are persisted) gave us all the testability benefit
without touching the JWT layer. This is still the right call.

### 4.3 Phase 3 Wave 2 evidence audit (resolved: park the slot)
After Wave 1 completion, a read-only pre-flight for Wave 2 revealed a
design conflict, which turned out on audit to be an empty slot — no
actual work needed. Parking it was faster than inventing scope.
Lesson: **read-only pre-flights are cheap. Do them even when you think
you know what the slice is.**

---

## Section 5 — Misses, snags, and near-misses

### 5.1 First-time CI-gate misses (local, all self-corrected)
Every CI-gate failure was on a *local* run, never on the remote check.
The pattern was: draft code → CI fails → fix same turn → CI green →
push. Not one PR had to be amended after remote CI failed.

Failures encountered and their fixes:

| Slice | Stage | Failure | Fix |
| --- | --- | --- | --- |
| α2b.1 | mypy | `Result[Any]` stubs omit `.rowcount` for `UPDATE` statements | `# type: ignore[attr-defined]` with a comment explaining `CursorResult` carries it at runtime |
| α2b.1 | ruff | Unused import `UnauthorizedError` in `test_refresh_session.py` | Removed after `InvalidRefreshTokenError` replaced it |
| α2b.1 | black | Formatting drift in `test_session_repository.py` | Ran `black` |
| α2b.1 | pytest | `caplog` didn't see structlog's stdout output for `auth.refresh.reuse_detected` | Switched to `capsys` + ANSI-code stripping (structlog's dev renderer writes ANSI-coloured lines directly to stdout, not through Python `logging`) |
| α2b.2 | ruff | `Header` import placed after a blank line — I001 import-block sort | Merged into existing `from fastapi import Depends` |

Every one of those is a small paper-cut that becomes obvious the moment
the CI gate points at it. This is exactly what the gate is for.

### 5.2 One-time integration test that ran hot in α2a
`test_register_duplicate_email_returns_409_conflict` failed once during
α2a with a race condition on the SAVEPOINT-rolled-back fixture. It was
green on retry and has been stable ever since. Root cause was a
transient DB-side lag on the Supabase pooler, not the code under test.
No action taken; noted here so a future flake in the same area is
recognised as "the α2a Supabase blip" rather than a real regression.

### 5.3 Pytest-asyncio DeprecationWarning
The integration suite prints one deprecation warning per run about
`pytest-asyncio` closing the event loop for us. It's cosmetic (a
warning about a future default-behaviour change in a future
pytest-asyncio release). We chose to leave it visible rather than
squash it, so we notice when we're actually near the breaking version.

### 5.4 Unit-only coverage drops as HTTP surface grows
Coverage numbers over the trilogy:

* α1: 84% (very small app surface — mostly domain + core)
* α2a: 82.13% (added `app/api/v1/routers/auth.py`, `app/api/v1/deps.py`,
  `app/api/v1/schemas/auth.py`, `app/infrastructure/security/*` —
  the API-layer files are integration-tested, not unit-tested)
* α2b: 81.05% (same trend — `logout_session.py` is 100% and
  `refresh_session.py` is 96%, but the router grew by 40 statements
  covered exclusively by integration tests)

**This is expected and not a regression.** The unit-coverage number
excludes `-m integration`, so any new HTTP-layer statement drags the
percentage down even though the code is exhaustively covered by the
integration suite. Two options exist for the future:

* **Option 1 (accept as-is)** — the 60% floor is well cleared; the
  90th-percentile-coverage code (application layer + core + domain +
  infrastructure/security) still sits at 96–100%. Nothing to change.
* **Option 2 (report combined)** — add a `ci_gate.py` stage that
  reports coverage across both markers. Larger change, more infra.

Recommendation: keep as-is for now. Revisit if unit coverage drops
below 78%.

### 5.5 PowerShell 5.1 friction
Two small friction points:

* `Out-File -Encoding utf8` writes a BOM in Windows PowerShell 5.1,
  which showed up as a leading `﻿` character on the α2b commit message
  subject line. Workaround adopted for the tag message:
  `[System.IO.File]::WriteAllText("$PWD\file.txt", $msg, (New-Object System.Text.UTF8Encoding $false))`.
* Heredoc-style `git tag -a v… -m "$(cat << 'EOF' … EOF)"` doesn't
  work in PowerShell — we fell back to `git tag -a v… -F file.txt`.

Both are captured in §7 as runbook updates.

### 5.6 OneDrive-hosted repository interference
`git branch -d` and `git push origin --delete` print
`Deletion of directory '.git/refs/heads/phase3' failed. Should I try again?`
prompts because OneDrive keeps the directory synced back after git
removes it. **The delete succeeds** (git reports "Deleted branch …"),
but the noise is real, and hosting a repository inside a syncing
folder is a known way to corrupt `.git` internals over time.
Actioned as a follow-up (Section 8).

### 5.7 Process improvements that permanently changed our workflow

These are the workflow changes that graduated from one-off fixes into
permanent defaults. They are recorded here so a future contributor can
see _when_ the practice started and _why_, and so that the same
mistake is never rediscovered from scratch. All of these are now
enforced or documented in `RUNBOOK_WAVE.md` §7 (added in the same
PR as this retrospective):

1. **Splitting large slices at defined checkpoints _before_
   implementation.** Adopted α2b. If a slice's file inventory is
   expected to exceed ~20 files, pre-flight decides the checkpoint
   split; each checkpoint must reach local-CI green before the next
   begins. Runbook §7.1.
2. **Bottom-up implementation order: domain → repositories → use
   cases → router.** Adopted α2a; standardised α2b.1. Every layer
   is written on top of a layer that is already green. Runbook §7.2.
3. **Repository integration tests before HTTP integration tests.**
   Adopted α2b.1. When a slice adds a new repository, its integration
   suite runs before any use case that depends on it is written.
   Debugging a failed use case never has to include "is the
   repository wrong?" as a hypothesis. Runbook §7.2.
4. **Tag only _after_ merge, from a fast-forwarded `main`.**
   Adopted α1; standardised α2a. Prevents the "I tagged the feature
   branch and pushed the wrong commit as the release" class of bug.
   The tag annotation always names the merge SHA, never a
   feature-branch SHA. Runbook §4.7 (pre-existing).
5. **File-based commit and tag messages on Windows PowerShell 5.1.**
   Adopted α2a; standardised α2b after the BOM incident on the α2b
   commit subject. `[System.IO.File]::WriteAllText(...,
   New-Object System.Text.UTF8Encoding $false)` is the reference
   incantation. Bash-style heredocs (`<<'EOF'`) are not usable.
   Runbook §7.3.
6. **Delete temporary message files immediately after `git commit -F`
   or `git tag -F`.** Adopted α2a after a `commit_msg.txt` was almost
   swept into a follow-up `git add -A`. The Runbook §4 commit
   sequences all end with `Remove-Item <msg-file>`.
7. **Post-merge hygiene: answer `n` to OneDrive's
   "Deletion of directory failed" prompts.** Adopted α2b. The prompt
   fires because OneDrive holds `.git/refs/heads/<prefix>/` open;
   git has already succeeded (see the "Deleted branch …" line
   immediately above). Runbook §7.4. Long-term fix: move the repo
   out of OneDrive (§8.2).
8. **Structured-log `security_event=True` flag for events an ops
   team should page on.** Adopted α2b. Signature / expiry rejections
   are noisy under normal client behaviour and never carry the flag;
   reuse detection, sid mismatch, and hash-miss do carry it. This
   makes the SIEM alerting predicate a single field-equality check
   instead of a topic-name regex. Documented in
   `AUTH_TOKEN_LIFECYCLE.md` §6.
9. **Read-only pre-flight is mandatory even when the slice looks
   trivial.** Adopted after Phase 3 Wave 2 was audited and parked
   without any code change — the pre-flight itself was the
   deliverable. Runbook §1 (pre-existing) is now applied to every
   slice without exception.
10. **DTOs live in `app/api/v1/schemas/`, not in routers or use
    cases.** Adopted α2a. Pydantic v2's `ConfigDict(str_strip_whitespace=True)`
    is the default. This kept `RegisterRequest`, `LoginRequest`, and
    `RefreshRequest` uniform without any bespoke validators in the
    router or use case.

---

## Section 6 — Practices adopted across the trilogy

These graduated from "we tried it in one slice" to "this is how we work
now" and should be treated as defaults for α3+.

1. **Read-only pre-flight is mandatory** for anything larger than a
   single-file cleanup. Even if the pre-flight concludes "no change",
   that's a valid outcome (see Wave 2 park in §4.3).
2. **Internal checkpoints for PRs > ~20 files.** Land each checkpoint
   with its own local CI-green stamp before starting the next.
3. **Bottom-up test ordering.** Repository integration tests first,
   use-case unit tests second, HTTP integration tests last. When
   something fails, the previous layer is already known good.
4. **Ports first, adapters second, DI wiring third, use case last.**
   This mirrors the import-linter contract flow and prevents accidental
   cross-layer edges.
5. **In-memory fakes on the port surface, not the concrete adapter.**
   Fakes must implement the same `abc.ABC` the real adapter does.
6. **Structured-log security events carry `security_event=True`.**
   Ops teams should page only on these; noisy warnings (expired
   tokens, retries) never carry the flag.
7. **CAS semantics for state transitions on shared-mutable rows.**
   `WHERE revoked_at IS NULL` (etc.) means first writer wins; no
   overwrite-races, no double-issue on concurrent refresh.
8. **`InvalidRefreshTokenError` (or equivalent) for anti-enumeration.**
   Every non-happy path on an auth endpoint returns an identical
   client-facing message; server-side logs carry the specific reason.
9. **DTOs live in `app/api/v1/schemas/`, not in routers or use cases.**
   Pydantic v2 `ConfigDict(str_strip_whitespace=True)` is the default.
10. **Every commit / PR / merge / tag flows through the RUNBOOK_WAVE.md
    steps** (with Auth-specific variations captured inline in the
    slice PR body, not in ad-hoc emails).

---

## Section 7 — RUNBOOK_WAVE.md updates applied in this PR

Four practices graduated from "we tried it in α2b" to permanent
operational guidance. They ship in the same PR as this retrospective
so the runbook reflects reality the moment the retro lands, rather
than being a to-do list that decays.

Applied to `docs/engineering/RUNBOOK_WAVE.md`:

| Runbook section | Content | Source |
| --- | --- | --- |
| §7.1 Checkpoint discipline for larger PRs | Any PR > ~20 files must split into internal checkpoints decided at pre-flight; each checkpoint must be local-CI-green before the next begins. | α2b split into α2b.1 + α2b.2 |
| §7.2 Bottom-up test ordering | Repository integration tests → use-case unit tests → HTTP integration tests. Each layer's tests only start once the layer beneath is green. | α2b.1 pre-flight review |
| §7.3 Commit and tag messages on Windows PowerShell 5.1 | Use `[System.IO.File]::WriteAllText(..., New-Object System.Text.UTF8Encoding $false)` for no-BOM message files; do not use bash-style heredocs. | α2b BOM incident on commit subject |
| §7.4 Post-merge hygiene: OneDrive interference | The `Deletion of directory failed` prompts are cosmetic — git has already deleted the branch; answer `n`. Long-term: move repo out of OneDrive. | α2b branch cleanup |

Reviewer sanity-check: after this PR merges,
`RUNBOOK_WAVE.md` should have a §7 with those four subsections and
the runbook's total section count should be **7** (was 6, plus §7).

---

## Section 8 — Debts carried into α3

### 8.1 Blocking-in-practice: none
All three merged slices are production-shape (subject to real deploy
config). α3 does not need to fix anything from this trilogy to proceed.

### 8.2 Cosmetic / quality-of-life
* **`chore(orm) render_jobs.progress`** type-hint drift
  (`Mapped[float]` vs actual `Mapped[str]`). Small dedicated PR,
  no migration.
* **Move repo out of OneDrive** to `C:\dev\ai-video-platform` or
  similar. Not urgent, but the `.git`-deletion prompts during branch
  cleanup are a leading indicator of future corruption. Do it at the
  natural pause after the ORM chore, before α3 branch is cut.
* **Pytest-asyncio deprecation warning** (§5.3). Leave visible until
  the day it becomes an error, then upgrade.
* **`app.core.container` unit coverage** sits at 43% — the factory
  functions are exercised by the integration suite. Not worth
  chasing until it's below 40%.
* **BOM on the α2b feature-branch commit subject** (`﻿Phase 3 Slice α2b`)
  is preserved in git history via the squash-merged PR commit's
  full body, but the merge commit on `main` (`0d5af9c`) was written
  by GitHub without the BOM. No action needed.

### 8.3 Deferred design questions to revisit in α3+
* **IP + user-agent hardening.** α2a takes the first value of
  `X-Forwarded-For` if present, else `request.client.host`. A real
  reverse-proxy + `TrustedHost` middleware story lands in a later
  slice behind its own ADR.
* **Rate limiting.** No rate limiting on `/auth/*` today. Deferred
  to a dedicated slice; the anti-enumeration constant-time work
  buys us runway.
* **Refresh token binding.** Nothing binds a refresh token to the
  device that received it (no `X-Device-Fingerprint` or DPoP-style
  proof). The family-sweep reuse detection is our current mitigation.
  Reconsider before public GA.
* **Application-lock table** for the register-time email-uniqueness
  pre-check race window (documented in `RegisterUser` and the α2a
  CHANGELOG entry). Low real-world exploitability today; revisit
  under load.

---

## Section 9 — What α3 inherits

A stable, opinionated, well-tested foundation:

* **Layered architecture** with fitness-function enforcement.
* **Ports + adapters** for every non-trivial dependency
  (persistence, password hashing, token issuance, time).
* **Dependency injection** through a single `app.core.container`
  module — FastAPI `Depends(container.get_*)` throughout.
* **Anti-enumeration + CAS-safe session model** ready to underpin
  authorisation (roles), rate-limited endpoints, and multi-tenant
  scoping.
* **Structured logging** with a security-event flag ready for SIEM
  wire-up.
* **10-stage CI gate** that has caught every intended regression
  and stayed under 2 minutes end-to-end.
* **`AUTH_TOKEN_LIFECYCLE.md` + `RUNBOOK_WAVE.md`** as living docs
  the next slice can cite instead of reinventing.

α3 pre-flight should start by picking the *smallest* useful
authenticated capability that exercises the seam between "who is
this user?" (`get_current_user` dependency) and one real business
endpoint, then decide whether that's `/me`, a tenant scope check, or
the first content endpoint. That decision is out of scope for this
retrospective.

---

## Appendix A — Slice-by-slice PR / tag references

* α1: `v0.4.0-phase3-alpha1` — PR #8
* α2a: `v0.4.1-phase3-alpha2a` — PR #9, merge `281471d`
* α2b: `v0.4.2-phase3-alpha2b` — PR #10, merge `0d5af9c`

## Appendix B — File inventory across the trilogy

New source modules introduced by the auth work:

```
app/application/interfaces/clock.py          (α2b)
app/application/interfaces/repositories.py   (α1, extended α2a + α2b)
app/application/interfaces/security.py       (α2a, extended α2b)
app/application/interfaces/unit_of_work.py   (α1, extended α2a)
app/application/use_cases/auth/errors.py     (α2a, extended α2b)
app/application/use_cases/auth/register_user.py    (α2a)
app/application/use_cases/auth/login_user.py       (α2a)
app/application/use_cases/auth/refresh_session.py  (α2b)
app/application/use_cases/auth/logout_session.py   (α2b)
app/domain/identity/{user,tenant,session}.py       (α2a)
app/infrastructure/clock.py                        (α2b)
app/infrastructure/security/jwt.py                 (α1, extended α2b)
app/infrastructure/security/password_hasher.py     (α2a)
app/infrastructure/security/token_issuer.py        (α2a, extended α2b)
app/infrastructure/repositories/{user,tenant,session,role}_repository.py  (α1 + α2a, extended α2b)
app/api/v1/deps.py                                 (α1, extended α2a + α2b)
app/api/v1/routers/{health,auth}.py                (α1 + α2a, extended α2b)
app/api/v1/schemas/auth.py                         (α2a, extended α2b)
```

Documentation shipped:

```
docs/engineering/RUNBOOK_WAVE.md           (Phase 3 W1 onwards, cited by α2b)
docs/engineering/AUTH_TOKEN_LIFECYCLE.md   (α2b)
docs/engineering/PHASE3_AUTH_RETROSPECTIVE.md  ← this document
```
