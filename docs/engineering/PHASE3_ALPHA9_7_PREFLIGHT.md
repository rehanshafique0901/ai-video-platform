# Phase 3 — α9.7 Pre-flight: Generation Ingress

**Slice:** α9.7 — Generation Ingress (creator-triggered video generation over HTTP)
**Baseline (frozen):** `v0.4.49-phase3-alpha9.6`
**Target version:** `0.4.50-phase3-alpha9.7-dev` → `0.4.50-phase3-alpha9.7`
**Governing ADR:** [ADR-0052](../decisions/ADR-0052-generation-ingress-ownership-execution-and-read-contract.md) (Accepted)
**Grounding:** [`PHASE3_ALPHA9_7_GROUNDING.md`](./PHASE3_ALPHA9_7_GROUNDING.md)
**Status:** **Approved** (2026-07-29), with the PF2 amendment incorporated as load-bearing invariant
**GEN-1** (§2.1) and the accompanying execution-runtime contract amendment (§12.1).

This document turns ADR-0052's five accepted decisions into an executable design. It resolves the
mechanical questions the ADR deliberately left to pre-flight, and it flags **five rulings (PF1–PF5)
that narrow or sharpen the ADR**. Those five are the parts most worth your attention; everything else
is the ADR applied mechanically.

---

## 1. What the ADR fixed, and what this document adds

| ADR-0052 | Accepted | Pre-flight must resolve |
|---|---|---|
| **D1-A** | Owner-scoped generations; migration required; no ownership inference | Exact DDL, index shapes, how the runtime stays ownership-blind |
| **D2-B** | Queued worker; lease; `max_attempts = 1`; heartbeat or long lease | How the queued row and the runtime's own `INSERT` coexist; how a crashed run reaches a terminal state |
| **D3-A** | Polling over a curated projection | Exact projection fields; progress computation |
| **D4-A** | Client idempotency key + DB backstop + 201/200 | Constraint shape under nullable owner |
| **D5-C** | `/api/v1/generations`, flat and owner-scoped | Endpoints, status codes, cancel semantics |

---

## 2. Pre-flight rulings that need your explicit sign-off

These five are the only places where the pre-flight makes a call the ADR did not already make.

### PF1 — `project_id` is **not** added to `generations` in v1

ADR-0052's migration summary lists `project_id` as *optional*. This pre-flight declines it.

**Rationale.** Promotion (`PromoteGenerationAssets`) already takes `project_id` as an explicit
argument and authorizes it against the caller (`promote_generation_assets.py:96-116`). Adding a
column would create a second, weaker notion of "the generation's project" that nothing enforces and
that would immediately raise a question the ADR does not answer: does a generation with
`project_id = X` refuse promotion into project `Y`? There is no product requirement forcing that
answer today.

There is also a mechanical reason. Validating a supplied `project_id` at ingress would require
`validate_media_links`, which lives in `app.application.use_cases.media` — a module the
**"Execution Runtime never writes the media library (ADR-0046 X8)"** import contract explicitly
forbids `app.application.use_cases.generation` from importing (`pyproject.toml:415-425`). Adding
`project_id` would therefore either breach that contract or force a duplicate validator. Declining
the column avoids both.

The accepted architecture — *"project promotion authorised by both ownership and project
membership"* — is fully satisfied without the column: **project membership** is checked by the
existing `validate_media_links` call, **generation ownership** by the new owner columns (§8).
`project_id` remains a clean additive option for a later slice.

### PF2 — `IExecutionRuntimeStore.begin()` becomes an idempotent **state-initialisation** operation; the runtime stays ownership-blind

This is the single most load-bearing mechanical consequence of D2-B, and it is not optional.

Today `begin()` performs a blind `INSERT INTO generations` (`generation_ledger_repository.py:25-44`,
`114-156`). Under D2-B the ingress creates the row **first** (status `queued`, with ownership), and a
worker runs `GenerateVideo.execute()` **later** — which calls `begin()` and would hit a primary-key
conflict.

**Ruling.** `begin()` is redefined as an **idempotent state-initialisation operation, not a generic
upsert.** The distinction is the whole point: a generic upsert would be free to write any column,
and nothing but reviewer vigilance would stop a future edit from letting the execution plane
overwrite ingress-owned data. The operation is therefore constrained by contract, not by the shape
of the SQL that happens to implement it today.

`_INSERT_GENERATION_SQL` gains `ON CONFLICT (id) DO UPDATE`, and the `DO UPDATE` clause enumerates
**only** runtime-owned execution fields: `status`, `title`, `execution_tier`, `chosen_provider`,
`chosen_adapter`, `shot_count`, the planner / storyboard / prompt-builder / resolver / verifier /
repair / renderer versions, `score_schema_version`, `catalogue_version`, `manifest_digest`,
`provenance`, `started_at`, and `updated_at`. Every other column is absent from the clause by
construction.

**This is load-bearing invariant GEN-1 (§2.1).** It is stated as an invariant rather than left
implicit in the implementation, because it is the mechanism by which the ingress/runtime boundary
holds.

Two properties follow:

1. **The execution runtime remains completely ownership-blind.** `GenerateVideoRequest` gains no
   tenant/owner field; `IExecutionRuntimeStore`'s signature does not change; no generation-plane
   module learns what a user is. Ownership is written once at ingress and read only by the ingress
   and read paths. This is the same one-way posture ADR-0049 established for the AI plane.
2. **Existing callers are unaffected.** A fresh `generation_id` still takes the `INSERT` branch, so
   `scripts/generate_demo.py` and `test_generation_end_to_end.py` keep working unchanged.

**Is this a new architectural decision?** No. ADR-0052 D2-B accepted "durable queued row is the
replay anchor" and "row stays `queued`; worker claims it". A queued row that the runtime later
adopts is a direct entailment of that, not a fresh choice. What it *does* require is a documentation
amendment to `EXECUTION_RUNTIME_CONTRACT.md` (§10). That is a docs edit inside the slice, not an ADR.

#### 2.1 GEN-1 — the `begin()` state-initialisation contract (load-bearing invariant)

`IExecutionRuntimeStore.begin()`:

1. **may create** the runtime row when it is absent (the unowned path: demo scripts, tests, any
   caller that generates its own `generation_id`);
2. **may initialise runtime-owned execution fields** when the row already exists (the ingress path:
   a `queued` row the worker has just claimed);
3. **must never overwrite ownership, identity, the persisted request, the idempotency key,
   `created_at`, or any other ingress-owned metadata** — these columns are absent from the
   `DO UPDATE` clause and no code path may add them;
4. **must never permit a queued generation to be rebound to another owner or another request** —
   there is no code path by which a `begin()` call can change who owns a generation or what was
   asked for;
5. **must remain safe when called repeatedly for the same `generation_id`** — repeated calls
   converge on the same runtime state and never corrupt or duplicate the row.

The boundary this preserves: **ingress owns identity; the execution runtime owns execution state.**
Neither writes the other's columns.

Enforced by a dedicated unit test that seeds an owned `queued` row, calls `begin()`, and asserts
that `tenant_id`, `owner_user_id`, `request`, `idempotency_key`, and `created_at` are byte-for-byte
unchanged — and a second test that calls `begin()` twice and asserts convergence.

### PF3 — the creator's request is persisted as a `request` JSONB column; v1 identity is seed + style only

`GenerateVideoRequest` cannot be reconstructed from the existing `generations` columns:
`target_duration_seconds`, `per_shot_seconds`, `min_similarity`, `max_attempts`, and `budget` have no
column, and `identity` is a nested `IdentityProfile` of which only `seed` is stored
(`identity.py:135-156`). A worker that claims a queued row must be able to rebuild the request
exactly.

**Ruling.** The migration adds `request jsonb NOT NULL DEFAULT '{}'::jsonb`, holding the creator's
asserted intent verbatim. This is the `publish_jobs.content_package jsonb` precedent
(`0014_publish_jobs.py:66`): the durable, immutable job payload lives with the job.

**Ruling on identity scope.** v1 accepts a **flat scalar request** — `prompt`, and optionally
`title`, `execution_mode`, `aspect_ratio`, `target_platform`, `target_duration_seconds`,
`per_shot_seconds`, `width`, `height`, `fps`, `seed`, `global_style`. The worker builds an
`IdentityProfile(seed=…, global_style=…)` with no characters, locations, props, or reference images.
Full identity authoring (characters, references, world state) is a **separate future slice** — it
needs its own persistence and its own API surface, and folding it in here would triple the slice.
The codec round-trips exactly the v1 field set and rejects unknown keys on read, so a later
identity slice extends it additively rather than reinterpreting old rows.

Note the deliberate redundancy: `prompt`, `execution_mode`, `aspect_ratio`, `width`, `height`, `fps`
exist both as columns (the runtime's provenance record) and inside `request` (the creator's
assertion). They are written with identical values. The columns stay the runtime's; `request` stays
the creator's.

### PF4 — cancel is **queued-only** in v1

ADR-0052 D5 lists `POST /{id}/cancel`, and D2 mentions "cooperative cancellation via the existing
`cancelled` status".

**Ruling.** v1 cancels a generation **only while it is still `queued`**, via a CAS
(`queued → cancelled`). A generation that has been claimed returns `409` with a clear message.

**Rationale.** Mid-run cooperative cancellation requires the generation pipeline to poll a
cancellation flag between shots — a change to `GenerateVideo`'s inner loop, on the plane this slice
is otherwise keeping untouched, for a capability with no queue depth to justify it yet. Queued-only
cancel is fully deterministic, needs zero runtime changes, and is honest about what it does. Mid-run
cancellation stays additive and unblocked.

### PF5 — a crashed run is **terminalized, never retried**, by a reaper inside the worker

ADR-0052's crash matrix says a mid-run crash "settles `failed`". Nothing in the current system
performs that settling — a crashed run would sit in `generating` forever, and a polling client would
never see a terminal state.

**Ruling.** `GenerationWorker.run_once()` runs a **reap phase before its claim phase**. A row is
reaped iff **all three** hold:

1. status is non-terminal **and not `queued`** (i.e. it was claimed), and
2. its `generation:<id>` lease can be acquired — meaning no live worker holds it, and
3. `updated_at` is older than a configurable grace period (default 2× the heartbeat interval).

Reaping writes `failed` with `failure_reason = "worker lost before completion"`. It **never**
re-queues and **never** re-runs: under `max_attempts = 1`, spend already incurred must surface, not
repeat. Condition 3 is belt-and-braces — a live worker refreshes `updated_at` at every phase
transition, so a healthy long run is never mistaken for a dead one even if a lease renewal is
briefly missed.

---

## 3. Migration `0016_generation_ownership`

House style follows `0015` (Alembic ops, module-level name constants, partial indexes via
`postgresql_where`). `down_revision = "0015_analytics_events_source_event_id"`.

```
ALTER TABLE generations
  ADD COLUMN tenant_id       uuid REFERENCES tenants(id) ON DELETE RESTRICT,
  ADD COLUMN owner_user_id   uuid REFERENCES users(id)   ON DELETE RESTRICT,
  ADD COLUMN idempotency_key text,
  ADD COLUMN request         jsonb NOT NULL DEFAULT '{}'::jsonb;

CREATE INDEX ix_generations_owner_created
  ON generations (owner_user_id, created_at DESC, id DESC)
  WHERE owner_user_id IS NOT NULL;

CREATE UNIQUE INDEX uq_generations_owner_idempotency_key
  ON generations (owner_user_id, idempotency_key)
  WHERE owner_user_id IS NOT NULL AND idempotency_key IS NOT NULL;
```

**Why nullable.** Per ADR-0052's migration philosophy: legacy rows have no owner and none may be
invented. Nullable + a partial index + owner-predicated reads is the only truthful shape.
Tightening to `NOT NULL` is a deliberate later migration.

**Why `ON DELETE RESTRICT`.** Matches `publish_jobs` (`0014_publish_jobs.py:54-55`): a generation is
a spend record, so deleting a user must not silently erase it.

**Why the index is `DESC, DESC`.** It matches the keyset order of `GET /generations` exactly
(newest first, `(created_at, id)` total order — `pagination.py:45-55`), so the list is a pure index
scan.

**Why the unique index is partial.** It excludes both NULL owners (legacy rows) and NULL keys
(callers who supplied none), giving render-job semantics — `uq_render_jobs_project_id_idempotency_key`
(`0001_baseline.py:924-925`) — scoped to the owner rather than a project. The constraint, not
application code, owns the concurrent-create race (ADR-0048 posture).

**Downgrade** drops both indexes, then the four columns.

---

## 4. Ports and DTOs

Two new ports, both in `app/application/interfaces/`, both mirroring the existing execution-plane
idiom (`IExecutionRuntimeStore` owns its own session factory because generation is long-running and
no single transaction spans a run).

### `IGenerationJobStore` — the generation plane's ingress/read/claim persistence port

| Method | Purpose |
|---|---|
| `create(...) -> CreatedGenerationJob` | Insert the owned `queued` row; on unique-key conflict return the existing row with `created=False` |
| `get_owned(tenant_id, owner_user_id, generation_id) -> GenerationJobView \| None` | Owner-scoped read (the only supported read model) |
| `list_owned(tenant_id, owner_user_id, status, cursor, limit) -> list[GenerationJobView]` | Owner-scoped keyset page, newest first |
| `claim_next(limit) -> list[UUID]` | FIFO scan of `queued` rows (server-side consumer, not owner-scoped — mirrors `list_claimable`) |
| `claim(generation_id) -> ClaimedGeneration \| None` | CAS `queued → planning`, returning the persisted `request` for reconstruction |
| `cancel_queued(tenant_id, owner_user_id, generation_id) -> CancelOutcome` | CAS `queued → cancelled`, owner-scoped |
| `list_reapable(grace_cutoff, limit) -> list[UUID]` | Non-terminal, non-`queued`, `updated_at` older than the cutoff |
| `mark_lost(generation_id) -> bool` | CAS non-terminal → `failed` with the lost-worker reason |

Implementation: `SqlGenerationJobStore` in `app/infrastructure/generation/`, raw SQL via
`sqlalchemy.text()` — the ORM-less convention `generations` already lives under (ADR-0046 Q2;
allowlisted in `validate_schema.py:165-172`).

### `IGenerationRunner` — the worker's execution seam

```python
class IGenerationRunner(ABC):
    async def run(self, request: GenerateVideoRequest) -> GenerateVideoResult: ...
```

**Why this exists.** `GenerateVideo` needs a live `AsyncSession` (the capability resolver reads the
catalogue through it — `container.py:1533`). Handing a session to a worker in the application layer
would leak an infrastructure type across the seam and would make the worker untestable without a
database. The adapter `SessionScopedGenerationRunner(session_factory, builder)` lives in
`app/infrastructure/generation/`, opens one session per run, and builds the use case via the
`builder` callable the container supplies (`get_generate_video_use_case`). No import cycle: the
container passes a callable into infrastructure; infrastructure never imports the container.

Unit tests inject a fake runner and never touch a database.

---

## 5. Ingress — `CreateGeneration`

`app/application/use_cases/generation/create_generation.py`. Depends only on `IGenerationJobStore`.

1. If `idempotency_key` is supplied, look it up for this owner; on hit return `(row, created=False)`.
2. Otherwise insert `status='queued'`, `tenant_id`, `owner_user_id`, `prompt`, `execution_mode`,
   the serialized `request`, and `idempotency_key`.
3. On unique-violation (concurrent racer won), re-read by key and return `(winner, created=False)` —
   the constraint decides, not application dedup (ADR-0048).

No outbox event in v1. `generation.*` events exist but have no consumers
(`events.py:4-6`, analytics defers them at `event_schema.py:8-9`), and inventing
`generation.requested` with no subscriber would be speculative. Additive later.

---

## 6. Worker — `GenerationWorker`

`app/application/use_cases/generation/generation_worker.py`. Shape mirrors `PublishWorker`
(`publish_worker.py:37-66`): `run_once()`, batch size, injected clock.

```
run_once():
  phase 0 — reap:  for each id in store.list_reapable(cutoff, limit):
                     lease = uow.locks.acquire(f"generation:{id}", owner, lease)   # steal if expired
                     if lease: store.mark_lost(id); uow.locks.release(lease)
  phase 1 — claim: for each id in store.claim_next(batch):
                     lease = uow.locks.acquire(f"generation:{id}", owner, lease)
                     if not lease: continue                    # another worker holds it
                     claimed = store.claim(id)                 # CAS queued → planning
                     if not claimed: release; continue         # lost the CAS race
                     run with heartbeat (below)
```

**Dual guard.** The lease and the CAS both exist and do different jobs: the CAS makes claiming
exactly-once, the lease makes *liveness* observable so a dead worker can be detected. This is the
publish precedent (`process_publish_job.py:98-138` — lock, then `mark_running` CAS), reused rather
than reinvented.

**Heartbeat.** ADR-0052 §D2 requires the lease to outlive the worst-case run or be renewed. We renew:

```
task = asyncio.create_task(runner.run(request))
while not task.done():
    done, _ = await asyncio.wait({task}, timeout=heartbeat_interval)
    if not done:
        lease = uow.locks.renew(lease, lease_for=lease_ttl)   # owner- and live-fenced
```

`renew` already exists and is correctly fenced (`distributed_lock_manager.py:56-66,101-111`), so this
adds no lock machinery. Defaults: lease TTL 300 s, heartbeat 60 s, reap grace 120 s — all
`Settings` values. A run of any length is covered without picking an arbitrary maximum.

**Retries: none.** `max_attempts = 1` is enforced structurally rather than by a counter column: the
CAS is `queued → planning`, and nothing ever writes a row back to `queued`. There is no requeue path
to disable. Shot-level repair (`DEFAULT_MAX_ATTEMPTS = 3`) is untouched — it is intra-run and
already paid for.

**Lease lost mid-run.** Logged, and the run continues to completion. Cancelling would not refund the
spend. The reaper's grace window plus `updated_at` freshness makes a competing terminalization
unlikely; if it does happen, the worker's later `complete()` overwrites `failed` with `completed` —
the truthful final state. Recorded as a residual in §14.

---

## 7. Read model — the curated projection

Per D3-A and ADR-0051's read-model hygiene lesson, the public shape is a projection, **not** a row
dump. `provenance`, the resolution ledger, `chosen_provider`, `chosen_adapter`, `execution_tier`,
`manifest_digest`, every `*_version` column, and the raw `request` blob are **internal** and never
serialized.

`GenerationPublic`:

| Field | Source |
|---|---|
| `id`, `status`, `title`, `prompt` | `generations` |
| `aspect_ratio`, `target_platform`, `width`, `height`, `fps` | `generations` |
| `shot_count`, `shots_accepted` | `shot_count`; `COUNT(*) FILTER (WHERE accepted)` over `generation_shots` |
| `created_at`, `started_at`, `finished_at` | `generations` |
| `duration_seconds` | `generations` (present once `completed`) |
| `failure_reason` | `generations`, terminal only |
| `promotable` | `status = 'completed' AND final_video_asset_id IS NOT NULL` |

`shots_accepted / shot_count` is the coarse progress ADR-0052 specified. `promotable` is a derived
boolean rather than exposing `final_video_asset_id`: the client's next action is
`POST /media/promotions` with the `generation_id` it already has, so the internal asset id has no
business crossing the wire.

---

## 8. Closing the F3 authorization gap

Today `PromoteGenerationAssets` authorizes the **project** but loads the generation by id alone,
with no ownership check (`promote_generation_assets.py:96-116`, and the AP9 comment at lines 24-27
that recorded the deferral). Once `generation_id`s become client-visible — which this slice does —
that is exploitable.

**Fix.** `IGenerationReader.load_final_video` gains `tenant_id` and `owner_user_id`, and
`_LOAD_FINAL_VIDEO_SQL` (`generation_reader.py:26-49`) gains
`AND g.tenant_id = :tenant_id AND g.owner_user_id = :owner_user_id`. A generation the caller does not
own is indistinguishable from one that does not exist: `404`, matching the platform's uniform
not-found posture.

**Consequence, accepted by ADR-0052.** Legacy ownerless generations become non-promotable. This is
the migration philosophy working as intended, not a regression. Existing tests that promote a seeded
ownerless generation must seed ownership — a fixture change, expected and correct.

The AP9 comment is replaced with a note recording that the deferral is now resolved.

---

## 9. API surface

`app/api/v1/routers/generations.py`, registered in `main.py` under `/api/v1`.

| Endpoint | Codes | Notes |
|---|---|---|
| `POST /api/v1/generations` | `201` created / `200` idempotent replay | Exact render-job split (`render_jobs.py:73-100`). `422` on invalid body |
| `GET /api/v1/generations/{id}` | `200` / `404` | Owner-scoped; `404` if missing **or** not owned |
| `GET /api/v1/generations` | `200` | `?limit=1..100`, opaque `?cursor=`, optional `?status=`; `meta.next_cursor` present iff another page exists |
| `POST /api/v1/generations/{id}/cancel` | `200` / `404` / `409` | PF4: `409` if already claimed or terminal |

`GenerationCreateRequest` (Pydantic, all optional except `prompt`): `prompt` (1..2000),
`idempotency_key` (≤255), `title`, `execution_mode`, `aspect_ratio`, `target_platform`,
`target_duration_seconds` (>0, ≤300), `per_shot_seconds` (>0, ≤60), `width`/`height` (positive,
bounded), `fps` (1..60), `seed`, `global_style`. Out-of-range values are `422` before the handler,
per the platform's Pydantic-first validation posture.

Every handler is a thin seam: `CurrentUserDep` → use case → `envelope(...)`. Deps in `deps.py`,
factories in `container.py`, following the publish-job pattern exactly (`deps.py:335-342`,
`container.py:1930-1948`).

---

## 10. Configuration

New `Settings` entries, all defaulted so nothing is required to run:
`generation_worker_batch_size` (5), `generation_lease_seconds` (300),
`generation_heartbeat_seconds` (60), `generation_reap_grace_seconds` (120).

---

## 11. Import-linter

**No new contract is required, and one existing contract constrains us.**

The **"Execution Runtime never writes the media library (ADR-0046 X8)"** contract
(`pyproject.toml:415-425`) forbids `app.application.use_cases.generation` and
`app.infrastructure.generation` from importing the media plane. Every new module in this slice lives
in exactly those two packages, and none needs media — which is precisely why PF1 declines
`project_id`. The contract passes unchanged and now guards more code than before.

The API-layer contract ("API talks to application services, never infrastructure") is satisfied by
routing everything through container-provided use cases.

---

## 12. Gate impact

| Stage | Change |
|---|---|
| **8 — schema validator** | Add `ix_generations_owner_created` and `uq_generations_owner_idempotency_key` to `EXTRA_EXPECTED_INDEXES` (`validate_schema.py:83-89`). `generations` is ORM-less and allowlisted, so its indexes are otherwise unchecked — registering them makes the gate actually assert them |
| **9 — ERD comparison** | Update the `generations` entity block in `docs/database/ERD.md` (Cluster 12, ~lines 1063-1101) with the four new columns. The comparator diffs entities and edges, not columns, so this is honesty rather than necessity; the new cross-cluster FK edges follow the file's existing elision convention and are tolerated as `edges_only_in_generated` |
| **25 — new** | `generation ingress integration` — `tests/integration/api/test_generation_ingress.py`, `requires_db=True`, same `Stage(...)` shape as stage 24 (`ci_gate.py:820-832`). Update the module docstring's stage list |

Docs updated in-slice: `EXECUTION_RUNTIME_CONTRACT.md` (§10 below), `API_CONTRACT` (the new
resource), `CHANGELOG.md`.

### 12.1 Execution-runtime contract amendment (accompanies PF2)

`EXECUTION_RUNTIME_CONTRACT.md` §3 gains the four new `generations` columns, and §4 states plainly:

> The runtime's lifecycle now **begins with an already-created `queued` generation supplied by
> ingress.** The runtime is no longer responsible for establishing the existence of a generation —
> only for executing one. `begin()` is an idempotent state-initialisation operation (GEN-1): it
> creates the row only when no ingress created it, initialises runtime-owned execution fields when
> one exists, and never writes ingress-owned columns.

The unowned direct-invocation path (demo scripts, tests) remains supported and is documented as
such, so the contract describes both entry points rather than implying ingress is mandatory.

---

## 13. Test plan

**Unit**

- `CreateGeneration`: fresh create; replay with the same key returns `created=False`; two concurrent
  creates with one key (simulated unique violation) resolve to one winner; no key still creates.
- `GenerationWorker`: claims and runs a queued row; skips a row whose lease is held; skips a row that
  lost the CAS; **never re-queues after failure**; reaps a stale claimed row to `failed`; does **not**
  reap a row inside the grace window; does **not** reap a `queued` row; renews the lease across a
  run longer than the heartbeat interval; one failing job does not abort the batch.
- **GEN-1** (§2.1): `begin()` over an existing owned `queued` row leaves `tenant_id`,
  `owner_user_id`, `request`, `idempotency_key`, and `created_at` unchanged; `begin()` called twice
  converges; `begin()` on a fresh id still inserts.
- Request codec: round-trips every v1 field; rejects unknown keys; defaults match
  `GenerateVideoRequest`'s defaults exactly.
- Projection: `provenance`, versions, adapter/provider, and `final_video_asset_id` are absent from
  `GenerationPublic` (the ADR-0051 leak-regression test, mirroring the `_email` sanitisation test).
- Cancel: `queued → cancelled`; claimed → conflict; terminal → conflict.

**Integration** (`tests/integration/api/test_generation_ingress.py`, SAVEPOINT harness per
`test_tiktok_destination_runtime.py:420-432`, unique tenant/user per test)

- Full path: `POST /generations` → `201` `queued` → worker `run_once()` with a stubbed runner →
  `GET` shows `completed` and `promotable=true`.
- Owner isolation: user B gets `404` on user A's generation, on both `GET /{id}` and cancel; B's list
  omits it.
- Legacy invisibility: a row inserted with NULL owner is absent from every owner-scoped read and is
  **not promotable**.
- Idempotency: same key twice → `201` then `200`, one row, one run.
- F3 regression: promoting another user's completed generation is `404`.
- Keyset pagination: three pages, no duplicates or gaps, `next_cursor` absent on the last.
- Crash simulation: a row left `generating` with an expired lease and stale `updated_at` is reaped to
  `failed` and **never re-run**.

**Full gate:** all 25 stages green against ephemeral PostgreSQL, including `alembic upgrade head`
followed by `downgrade` of `0016` and re-upgrade, proving the migration is reversible.

---

## 14. Risks and residuals

| Risk | Disposition |
|---|---|
| Zombie worker writes after its lease was stolen and the row was reaped | Final state becomes `completed` — truthful. Accepted; same class of residual as publish today, and fenced writes would be a larger change |
| Crash after render but before persist orphans an object in storage | Pre-existing (ADR-0052 crash matrix); the row records the last reached phase, so it is diagnosable. Out of scope |
| `request` JSONB duplicates six scalar columns | Deliberate (PF3): columns are the runtime's provenance, `request` is the creator's assertion. Written with identical values |
| Legacy ownerless generations become non-promotable | **Intended.** ADR-0052 migration philosophy; no backfill, no inference |
| Long generations hold a lease for their whole duration | Correct — that is what prevents double spend. Heartbeat keeps the window closed without an arbitrary maximum |

---

## 15. Explicitly out of scope

Mid-run cancellation; SSE/WebSocket progress; per-shot detail in the public projection; identity
profile authoring (characters, locations, props, references); `project_id` on generations;
generation outbox consumers, analytics, or notifications; credit/quota enforcement; a standing
worker process (`run_once()` is invoked exactly as render/publish workers are today); `NOT NULL`
tightening of the owner columns; any change to `/workflow-runs`.

---

## 16. Implementation order

1. Migration `0016` + `EXTRA_EXPECTED_INDEXES` + ERD/contract docs.
2. Request codec + `IGenerationJobStore` port and DTOs.
3. `SqlGenerationJobStore` (raw SQL) + the `begin()` upsert (PF2).
4. `IGenerationRunner` port + `SessionScopedGenerationRunner` adapter.
5. `CreateGeneration`, `GetGeneration`, `ListGenerations`, `CancelGeneration`.
6. `GenerationWorker` (reap → claim → run → heartbeat).
7. F3 fix: owner-scoped `IGenerationReader` + `PromoteGenerationAssets` (§8).
8. Pydantic schemas, router, `deps.py`, `container.py`, `main.py` registration.
9. Unit tests; then integration test + CI Stage 25.
10. Full 25-stage ephemeral-PostgreSQL gate; version bump to `0.4.50-phase3-alpha9.7-dev`;
    CHANGELOG; single feature commit; push; open the `-dev` release-review PR.

---

## 17. Architectural decision check

Does anything above surface a decision ADR-0052 did not already make?

| Ruling | New decision? |
|---|---|
| PF1 — no `project_id` | No. ADR-0052 marked it optional; this declines the option and states why |
| PF2 — `begin()` state-initialisation (GEN-1) | No. A direct entailment of D2-B's durable queued row. Requires a **docs** amendment to the runtime contract (§12.1), not an ADR |
| PF3 — `request` JSONB, scalar-only identity | No. Persisting the job payload is the `content_package` precedent; the identity scope is a scope boundary, not an architectural choice |
| PF4 — queued-only cancel | No. A narrowing of D5's cancel endpoint, with mid-run cancellation left additive |
| PF5 — reaper terminalizes | No. ADR-0052's crash matrix already fixed the *outcome* ("settles `failed`"); this supplies the mechanism |

**Conclusion: no new ADR is required.** Every ruling either applies an accepted decision or narrows
scope in a way that leaves the deferred capability additive.

---

**Approved.** Implementation proceeds in the §16 order and stops after the full gate is green, the
feature branch is pushed, and the `-dev` release-review PR is open.
