# Phase 3 Slice α7.2 — Workflow Runner (`WorkflowRun` Aggregate) — Pre-flight

> Status: **DRAFT — AWAITING SIGN-OFF.** The orchestration-era architecture and
> its runtime decisions (D1–D9) were signed off in
> [`docs/architecture/CONTENT_GENERATION_PIPELINE.md`](../architecture/CONTENT_GENERATION_PIPELINE.md)
> (2026-07-16) and first exercised by α7.1 (`RenderJob`). This doc resolves the
> **WorkflowRun-specific** open questions (§4). Nothing is implemented yet.
>
> Mirrors the α5/α6/α7.1 discipline: ground in the physical schema → lock
> decisions → sign-off → branch → implement → CI → merge → tag. Read-only
> planning artefact.
>
> **Predecessor.** α7.1 (`v0.4.15`, `main` @ `c5197f0`) — `RenderJob`, the first
> orchestration aggregate; established self-versioned OCC over an existing table,
> project-derived ownership, idempotent create, a fenced/guarded cancel, and D9
> outbox production (`RenderJobCreated` / `RenderJobCanceled`) with no dispatcher.
>
> **This is the second orchestration slice — the runner.** α7.1 proved a *single*
> orchestration aggregate. α7.2 introduces the **workflow layer that sequences
> work**: `WorkflowRun` → ordered `WorkflowStep`s → append-only
> `WorkflowCheckpoint`s. Per the sequencing rationale, the **runner is built
> before external providers**, so state transitions, checkpoint persistence,
> retry accounting, idempotency, and (optionally) the run lock are exercised with
> **deterministic in-process step handlers** — no AI providers, no real I/O, no
> background loop.
>
> **Baseline versioning.** `main` is at `0.4.15` (tag `v0.4.15-phase3-alpha7.1`).
> First α7.2 commit bumps `backend/app/main.py` → `"0.4.16-phase3-alpha7.2-dev"`.
> **Zero migrations** (D7) — `workflow_runs`, `workflow_steps`, and
> `workflow_checkpoints` (+ their ENUMs, indexes, uniques, and the checkpoint
> `reject_mutation` trigger) already exist in baseline `0001`
> (`docs/database/schema.md` §16).

---

## Section 1 — Scope

### 1.1 One-line thesis

α7.2 introduces the **`WorkflowRun` aggregate** — a project-scoped, **status-guarded**
record of a workflow execution that owns an ordered graph of `WorkflowStep`s and
append-only `WorkflowCheckpoint`s. It owns its **own** run/step lifecycle state
machines (D9) and coordinates purely through its status + domain events; it never
mutates `projects.version` and never reaches into `RenderJob` / `MediaAsset` /
`Timeline`. A **synchronous, deterministic runner** advances a run through an
**in-code workflow definition** of pure step handlers — **no external providers,
no async worker, no scheduler**. Provider-backed step handlers (and any
background driver) are a later slice (α8.x).

### 1.2 What's in

1. **WorkflowRun create + read + lifecycle**:
   - `POST /projects/{id}/workflow-runs` → create (`status=queued`; seeds the
     step rows for the chosen `workflow_key@workflow_version` as `pending`).
   - `GET  /projects/{id}/workflow-runs` → list (owner-scoped, newest-first).
   - `GET  /projects/{id}/workflow-runs/{run_id}` → read one (run + steps +
     latest checkpoint).
   - `POST /projects/{id}/workflow-runs/{run_id}/cancel` → `canceled` (guarded).
   - *(pause/resume — see §4 Q4.)*
2. **The deterministic runner** (`POST …/workflow-runs/{run_id}/advance`, or run
   synchronously on create — see §4 Q1): drives the run through its `pending`
   steps in `step_index` order using **in-code, side-effect-free step handlers**,
   marking each `running → succeeded`, appending a `WorkflowCheckpoint`, and
   settling the run to `succeeded` / `failed`. **Resumable + idempotent by
   `step_index`** (already-`succeeded` steps are skipped on re-invocation).
3. **Retry accounting** — a handler may signal a *transient* failure; the runner
   increments `workflow_steps.retries`, sets `retrying`, and retries up to a
   definition-declared bound, then settles the step `failed` → run `failed`.
   **Deterministic, in-process; no backoff timing / no scheduler** (§4 Q5).
4. **Ownership** via `project_id → projects.owner_user_id` (WorkflowRun has no
   owner column — the α7.1 reachability pattern).
5. **Concurrency** via **status-guarded CAS** on transitions + **last-writer-wins**
   for metadata (there is **no `version` column** — §2, §4 Q2).
6. **Idempotency** — run-level `UNIQUE(project_id, idempotency_key)` (repeat →
   existing run, `200`); step-level `UNIQUE(workflow_run_id, step_index)`
   (resume-safe, no duplicate steps/checkpoints) (§4 Q7).
7. **In-code workflow registry** — a framework-free catalogue mapping
   `workflow_key@workflow_version` → an ordered list of step definitions
   (name + handler + max-retries), with **≥1 deterministic test workflow** (§4 Q3).
8. **D9 outbox production** — `WorkflowRunStarted`, `WorkflowStepCompleted`,
   `WorkflowRunCompleted`, `WorkflowRunFailed`, `WorkflowRunCanceled` written to
   `event_outbox` in the same UoW transaction (no dispatcher — §4 Q8).
9. **Domain** `WorkflowRun` / `WorkflowStep` / `WorkflowCheckpoint` entities +
   `WorkflowRunStatus` / `WorkflowStepStatus` enums; **repository**
   `IWorkflowRunRepository` + impl; **use cases** (create / list / get / cancel /
   advance); **DTOs**; **router** (new `workflow_runs.py`); DI wiring; unit +
   integration tests; docs (`API_CONTRACT.md`, `CHANGELOG.md`, `ROADMAP.md`, a new
   `docs/domain/WORKFLOW_RUN_AGGREGATE.md`, ADR-0040).

### 1.3 What's out (deferred)

- **Provider-backed step handlers / real work** — script/storyboard/image/video/
  voice generation, and any step that spawns a `RenderJob` (via
  `render_jobs.workflow_run_id`). α7.2 handlers are pure/deterministic; α8.x
  swaps in provider adapters behind the same handler protocol.
- **Background/async execution & scheduling** — no worker loop, no queue
  dispatch, no retry backoff timing. Advancement is a synchronous, explicitly
  invoked (or on-create) operation with deterministic handlers.
- **Distributed-lock acquisition** (`workflow_run:{id}`) — convention only, unless
  §4 Q6 accepts in-process acquisition now.
- **Full idempotency-key ledger** (`idempotency_keys`, response-hash replay) —
  α7.5. α7.2 uses only the table-level uniques (§4 Q7).
- **Outbox relay worker** — α7.3 (α7.2 only *produces* rows).
- **`workflow_version` semantics / migration between definition versions** — the
  key is stored and validated against the registry; migration tooling is later.
- **Zero migrations.**

---

## Section 2 — Grounded facts (the physical workflow tables)

From `backend/app/infrastructure/db/models/workflows.py` + baseline `0001`
(`schema.md` §16).

### 2.1 `workflow_runs` — `WorkflowRun(UUIDPrimaryKeyMixin, TimestampMixin, Base)`

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | `gen_random_uuid()` |
| `project_id` | UUID FK → `projects.id` | `ON DELETE CASCADE` — the ownership anchor |
| `workflow_key` | Text NOT NULL | workflow type id (e.g. `full-generation`) — **no server default** |
| `workflow_version` | Text NOT NULL | definition semver — **no server default** |
| `status` | `workflow_status` ENUM NOT NULL | `queued, running, paused, succeeded, failed, canceled` — **no server default** (app sets `queued`) |
| `started_at` / `finished_at` | timestamptz, nullable | runner-set |
| `triggered_by_user_id` | UUID FK → `users.id`, nullable | `ON DELETE SET NULL` |
| `idempotency_key` | Text, nullable | client dedupe token |
| `input_snapshot` | JSONB **NOT NULL** | the frozen request inputs (must be supplied on create) |
| `output_summary` | JSONB, nullable | runner-set on completion |
| `error` | JSONB, nullable | `{code,message,trace_id,…}` on failure |
| timestamps | | `TimestampMixin` (`created_at`/`updated_at`) |
| **no `version`** | | **not** in `_VERSION_BUMP_TABLES` — no numeric OCC token |
| **no `deleted_at`** | | audit record; cancel is a *status*, not a delete |

Constraints / indexes: `uq_workflow_runs_project_id_idempotency_key`;
`ix_workflow_runs_project_id_status`; `ix_workflow_runs_workflow_key_workflow_version`.

### 2.2 `workflow_steps` — `WorkflowStep(UUIDPrimaryKeyMixin, TimestampMixin, Base)`

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `workflow_run_id` | UUID FK → `workflow_runs.id` | `ON DELETE CASCADE` — child of the run |
| `step_index` | Integer NOT NULL | ordered position; **`UNIQUE(workflow_run_id, step_index)`** |
| `step_name` | Text NOT NULL | from the definition |
| `status` | `step_status` ENUM NOT NULL | `pending, running, succeeded, failed, skipped, retrying` — no default (app sets `pending`) |
| `started_at`/`finished_at` | timestamptz, nullable | runner-set |
| `retries` | Integer NOT NULL, server_default `0` | retry counter |
| `input`/`output`/`error` | JSONB, nullable | runner-set |
| timestamps | | `TimestampMixin`; **no `version`, no `deleted_at`** |

### 2.3 `workflow_checkpoints` — `WorkflowCheckpoint(UUIDPrimaryKeyMixin, CreatedAtOnlyMixin, Base)`

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `workflow_run_id` | UUID FK → `workflow_runs.id` | `ON DELETE CASCADE` |
| `step_index` | Integer NOT NULL | which step this resume-point is for |
| `state` | JSONB **NOT NULL** | the resume state |
| `created_at` | timestamptz | `CreatedAtOnlyMixin` — **no `updated_at`** |

**Append-only, DB-enforced.** `workflow_checkpoints` is in the baseline
`reject_mutation` trigger set (`tg_workflow_checkpoints_bud_reject_mutation`) —
UPDATE/DELETE are rejected at the DB (ADR-0014). Checkpoints are *written once*.

### 2.4 Key consequences

- **No `version` column anywhere in the workflow tables.** Unlike `RenderJob`
  (self-versioned) and unlike the timeline (borrowed token), `WorkflowRun` has
  **no OCC counter**. Migration-free (D7) means we cannot add one now. → transitions
  must be **status-guarded CAS**; non-transition metadata is **last-writer-wins**
  (§4 Q2). No `?version=` query param on transition endpoints.
- **No soft-delete** → cancel is a status transition; a canceled run stays
  `GET`-able (α7.1 shape). No `DELETE` verb.
- **`workflow_key`/`workflow_version`/`status` have no server default** → the app
  supplies them; an unknown `workflow_key@workflow_version` (not in the registry)
  is a **`422`** before the write (§4 Q3).
- **`input_snapshot` is `NOT NULL`** → create must persist the (validated) request
  inputs; this is the run's frozen input contract.
- **Checkpoints are immutable** → the runner *appends* one per completed step;
  resume reads the latest per `step_index`. It never updates a checkpoint.
- **`render_jobs.workflow_run_id`** already links RenderJob → WorkflowRun
  (`ON DELETE SET NULL`); α7.2 does **not** create render jobs (no execution), it
  only establishes the aggregate a later render-producing step will attach to.

---

## Section 3 — Decisions (recommended)

- **D3.1 — Self-owned orchestration aggregate (D9).** `WorkflowRun` is its own
  aggregate root owning `WorkflowStep` (ordered children) and `WorkflowCheckpoint`
  (append-only children). It is the sole writer of `workflow_runs.status` /
  `workflow_steps.status`. It never touches `projects.version`; cross-aggregate
  coordination is event-only (blueprint §7.1). Extends the α7.1 `RenderJob` posture.
- **D3.2 — Concurrency = status-guarded CAS + LWW (no version token).** With no
  `version` column, every lifecycle transition is a status-predicated
  compare-and-swap — `UPDATE … SET status=<to> WHERE id=? AND status IN
  (<allowed_from>)`; zero rows → re-classify (`404` / `409` / idempotent `200`).
  Non-transition metadata writes are last-writer-wins. This is race-safe at the DB
  without a numeric token. **No `?version=` on transition endpoints** (a documented
  divergence from `RenderJob`'s fenced cancel, forced by the schema).
- **D3.3 — Routing.** New router `app/api/v1/routers/workflow_runs.py`, prefix
  `/projects/{project_id}/workflow-runs`. Project-nested (the ownership anchor).
  *(Note: the CR-7 blueprint also sketches a top-level `/workflows` catalogue;
  α7.2 ships the project-scoped run resource, not the global catalogue.)*
- **D3.4 — Visibility gate / ownership.** Two-level uniform `404`: project (owned
  by caller) → workflow run (belongs to that project). Ownership via
  `project.owner_user_id`; no owner column on the run.
- **D3.5 — Run state machine (α7.2).** `create → queued`; runner `queued →
  running → {succeeded, failed}`; `cancel: {queued, running, paused} → canceled`.
  `paused`/`resume` per §4 Q4. Cancel from a terminal state is idempotent/`409`
  per D3.7.
- **D3.6 — Step state machine.** `pending → running → {succeeded, failed}`;
  transient failure: `running → retrying → running` up to the bound, then
  `failed`. `skipped` reserved for definition-level conditional skips (not used by
  the α7.2 test workflow unless Q3 says so).
- **D3.7 — Cancel semantics (mirror α7.1 D3.5/D3.6).** `POST …/cancel`:
  `404`-before-classify; `{queued,running,paused} → canceled`; re-cancel of
  `canceled` → `200` idempotent no-op; cancel of `succeeded`/`failed` → `409`. No
  `DELETE` verb (no `deleted_at`). Because there is no version token, cancel is
  status-guarded (D3.2), **not** version-fenced.
- **D3.8 — The runner is synchronous + deterministic.** A use case
  (`AdvanceWorkflowRun`) loads the run + its steps, and for each `pending`/
  `retrying` step in `step_index` order: mark `running` (set `started_at`) → call
  the registry handler with `(input_snapshot, prior checkpoint state)` → on success
  set `succeeded` + `output` + `finished_at`, **append a checkpoint**, continue; on
  transient failure bump `retries`/`retrying` and retry to the bound; on terminal
  failure set the step `failed` + run `failed` + `error`, stop. All steps done →
  run `succeeded` + `output_summary`. Handlers are **pure** (no I/O) in α7.2.
- **D3.9 — Framework-free status enums.** `WorkflowRunStatus` (6 values) and
  `WorkflowStepStatus` (6 values) in `app/domain/workflow/`, so the use cases guard
  transitions in the domain layer, not with string literals.
- **D3.10 — Boundary invariant (extends α7.1 D3.10).** `WorkflowRun` owns
  **orchestration/graph state only** — run status, step sequence + status, and
  checkpoints. It does **not** own rendered/exported files (`MediaAsset`), the
  render lifecycle (`RenderJob`), or timeline edits (`Timeline`); it *references*
  and later *coordinates* them via events (D9). A render-producing step (α8.x)
  will create a `RenderJob` and link it by `render_jobs.workflow_run_id`, **not**
  fold render state into the run.

  ```
  WorkflowRun ── owns orchestration/graph state (run+step status, checkpoints)
  RenderJob   ── owns a single render's lifecycle (referenced via workflow_run_id)
  MediaAsset  ── owns produced assets
  Timeline    ── owns edit state
  ```

---

## Section 4 — Open questions for sign-off

**Q1 — Slice shape: aggregate + runner, aggregate-only, or split?**
α7.1 shipped an aggregate with **no** worker. α7.2 is asked to prove the *runner*.
Options: (a) **aggregate + synchronous deterministic runner** in one slice
(create/list/get/cancel **and** `advance`); (b) **aggregate-only** now (lifecycle +
seeded steps, no advancement) and defer the runner to α7.2b; (c) **split** into
α7.2a (aggregate) + α7.2b (runner) up front.
**Recommend (a)** — the runner mechanics (transitions, checkpoints, retries, resume,
idempotency) are the point of building it before providers, and they're fully
exercisable with deterministic handlers. It is a larger slice than α7.1 but
self-contained and migration-free. *(If you prefer smaller increments, (c).)*

**Q2 — Concurrency model without a `version` column (confirm D3.2).**
The schema has no OCC token and D7 forbids a migration. **Recommend confirm:**
status-guarded CAS for transitions + LWW for metadata; **no `?version=`** on
transition endpoints. Flagging because it visibly diverges from `RenderJob`'s
version-fenced cancel. *(Alternative: add a `version` column via migration —
rejected, breaks D7.)*

**Q3 — Where do step definitions come from?**
The runner needs an ordered step list per `workflow_key@workflow_version`.
**Recommend: an in-code workflow registry** (a framework-free module) with **≥1
deterministic test workflow** (e.g. `noop-chain@1.0.0`, 3 pure steps). An unknown
key/version → `422`. *(Alternative: a DB-backed catalogue — defer; needs schema/
seed work and isn't required to prove the runner.)*

**Q4 — pause / resume in α7.2?**
The `workflow_status` enum includes `paused`, and CR-7 sketches
`/workflows/{id}/pause|resume`. Options: (a) include `pause`/`resume`
(`running ⇄ paused`) now; (b) defer to keep the state machine minimal.
**Recommend (b) defer** — pause/resume is meaningful mainly for a long-running/async
runner; the synchronous α7.2 runner completes in one call. Ship `queued/running/
succeeded/failed/canceled`; add `paused` with the async driver (α8.x). *(If you
want the full state machine now, (a).)*

**Q5 — Retry policy shape.**
**Recommend:** max-retries is a **per-step definition constant** in the registry;
the runner increments `workflow_steps.retries`, uses `retrying`, and settles
`failed` when exhausted. **No backoff/delay** (deterministic, no scheduler). A step
handler signals transient-vs-terminal via a typed domain result/exception. *(This
keeps retries observable and testable without timing.)*

**Q6 — Distributed lock (`workflow_run:{id}`) in α7.2.**
The synchronous runner could acquire a `workflow_run:{id}` lock around `advance`
to prevent concurrent advancement of the same run. Options: (a) **acquire now**
(build a thin lock helper over the baseline `distributed_locks` table); (b)
**convention only**, like α7.1 Q5 (acquisition with the async driver, α8.x).
**Recommend (b) convention only** — a synchronous single-request advance already
serializes via the status-guarded CAS (a second concurrent advance finds no
`pending` step in the expected state and no-ops), so a lock adds little before the
async driver exists. *(If you want to prove the lock now, (a) — but it's the one
piece that YAGNI applies to until concurrency is real.)*

**Q7 — Idempotency behaviour.**
**Recommend:** (i) run-level — `idempotency_key` optional on create; a repeat with
the **same** key for the project returns the **existing** run `200` (α7.1 Q4
parity); unique-violation race → `409` resolved by returning the winner. (ii)
step-level — `UNIQUE(workflow_run_id, step_index)` makes step seeding + advancement
**resume-safe**: re-invoking `advance` skips already-`succeeded` steps and never
duplicates steps/checkpoints. Full `idempotency_keys` ledger → α7.5.

**Q8 — Outbox event set (D9).**
**Recommend produce now** (same UoW txn as the state change; accumulate until the
α7.3 relay): `WorkflowRunStarted` (first `queued→running`), `WorkflowStepCompleted`
(per step success), `WorkflowRunCompleted`, `WorkflowRunFailed`,
`WorkflowRunCanceled`. Payloads carry orchestration fields only (`workflow_run_id`,
`project_id`, `workflow_key`, `workflow_version`, `status`, `step_index`/`step_name`
where relevant); `event_version="1.0"`. *(Confirm the exact set — e.g. whether to
also emit `WorkflowRunCreated` on create, or treat `WorkflowRunStarted` as the
first event.)*

**Q9 — Version number.** Continue the `0.4.x` slice cadence →
`0.4.16-phase3-alpha7.2-dev`. **Recommend `0.4.16`** (monotonic; still Phase-3
orchestration infrastructure, not a product milestone — same reasoning as α7.1 Q7).

---

## Section 5 — Planned surface (pending §4)

```
POST   /api/v1/projects/{id}/workflow-runs
  body:  { workflow_key, workflow_version, input_snapshot, idempotency_key? }
  → 201  { data: WorkflowRunPublic }   (status=queued, steps seeded pending)
  → 200  { data: WorkflowRunPublic }   (idempotent replay, same idempotency_key — Q7)
  → 404 (project missing/not owned) · 422 (unknown workflow_key@version / bad body) · 401

GET    /api/v1/projects/{id}/workflow-runs
  → 200  { data: [WorkflowRunPublic … created_at DESC] } · 404 · 401

GET    /api/v1/projects/{id}/workflow-runs/{run_id}
  → 200  { data: WorkflowRunPublic (with steps[] + latest checkpoint) } · 404 · 401

POST   /api/v1/projects/{id}/workflow-runs/{run_id}/advance
  → 200  { data: WorkflowRunPublic }   (runner ran to succeeded/failed; resumable)
  → 404 · 409 (already terminal) · 401

POST   /api/v1/projects/{id}/workflow-runs/{run_id}/cancel
  → 200  { data: WorkflowRunPublic }   (status=canceled; idempotent if already canceled)
  → 404 (missing/not owned) · 409 (already succeeded/failed) · 401
```

`WorkflowRunPublic` surfaces: `id`, `project_id`, `workflow_key`,
`workflow_version`, `status`, `input_snapshot`, `output_summary` (null until
done), `error` (null unless failed), `started_at`/`finished_at`,
`triggered_by_user_id`, `idempotency_key`, `created_at`/`updated_at`, and
`steps: [WorkflowStepPublic …]` (`step_index`, `step_name`, `status`, `retries`,
`started_at`/`finished_at`, `output`/`error`) — **no `version`** field
(none exists).

Implementation order (mirrors α7.1): domain `WorkflowRun`/`WorkflowStep`/
`WorkflowCheckpoint` + status enums + workflow registry → `IWorkflowRunRepository`
+ impl + fakes/UoW wiring → use cases (create/list/get/cancel/**advance**) + outbox
producer (Q8) → DTOs + container factories + deps aliases → new `workflow_runs.py`
router → unit tests (state machine, runner advance, retry, resume, idempotency,
outbox shapes) → integration tests (API + repository + runner end-to-end on the
deterministic workflow) → docs (`WORKFLOW_RUN_AGGREGATE.md`, ADR-0040,
`API_CONTRACT.md`, `CHANGELOG.md`, `ROADMAP.md`) → CI gate → merge → tag
`v0.4.16-phase3-alpha7.2`.

---

## Section 6 — Reviewer sign-off

**SIGNED OFF (2026-07-16).** All nine §4 questions accepted, plus one added
design rule (D3.11):

- **Q1 — Slice boundary:** ✅ Keep the `WorkflowRun` aggregate **and** the
  synchronous deterministic runner in a single α7.2 slice — the runner is what
  gives the aggregate meaning; splitting leaves a CRUD aggregate that can't
  execute.
- **Q2 — Concurrency:** ✅ Status-guarded CAS transitions + last-writer-wins
  metadata; **no `version` column, no `?version=`**. Documented as the
  workflow-specific concurrency model (follows directly from the schema +
  zero-migration discipline).
- **Q3 — Workflow definitions:** ✅ In-code workflow registry with deterministic
  test workflows. DB-backed definitions wait for a real authoring/runtime story.
- **Q4 — Pause/Resume:** ✅ Defer. A synchronous runner completes or fails within
  one invocation; pause/resume belongs with asynchronous workers.
- **Q5 — Retry policy:** ✅ Deterministic retry counter + configurable max
  attempts; a backoff policy is **represented** (metadata) but **not** provider-
  driven — no scheduling yet.
- **Q6 — Distributed locks:** ✅ Convention only (`workflow_run:{id}`). CAS
  transitions already serialize correctness; acquire locks when multiple workers
  exist.
- **Q7 — Idempotency:** ✅ Same as α7.1 — a duplicate `idempotency_key` returns
  the existing `WorkflowRun` (`200`) instead of creating another.
- **Q8 — Outbox events:** ✅ Emit immediately (relay later). Minimum set:
  `WorkflowRunCreated`, `WorkflowRunStarted`, `WorkflowStepCompleted`,
  `WorkflowRunSucceeded`, `WorkflowRunFailed`, `WorkflowRunCanceled`.
- **Q9 — Version:** ✅ `0.4.16-phase3-alpha7.2-dev` (stay on 0.4.x; still Phase-3
  orchestration infrastructure).

- **D3.11 — Steps are deterministic and side-effect-free (added at sign-off).** A
  step handler is a **pure function** that returns a **command/result** describing
  what should happen — it does **not** call providers or external services
  directly. The runner (the imperative shell) interprets the result: persists the
  step output, appends the checkpoint, and later (α8.x) dispatches any provider
  commands. This keeps the runner fully testable and makes the eventual move to
  Celery/LangGraph an **execution concern, not a domain rewrite**. ADR-0040 records
  this as a load-bearing rule.

**Signed-off implementation order** (supersedes §5's ordering; layer-by-layer to
avoid revisiting earlier layers):

1. `WorkflowRun` aggregate (domain entities + status enums)
2. Repository + Unit of Work wiring
3. CRUD endpoints (create / list / get)
4. Status-guarded CAS transitions (cancel)
5. Workflow registry (in-code definitions + step-handler protocol, D3.11)
6. Deterministic runner (`advance`)
7. `WorkflowStep` persistence
8. Checkpoint append-only persistence
9. Retry accounting
10. Outbox event production (Q8 set)
11. Unit tests
12. Integration tests
13. Docs (ADR-0040 / API_CONTRACT / ROADMAP / CHANGELOG / `WORKFLOW_RUN_AGGREGATE.md`)

Proceed: branch `phase3/alpha7.2-workflow-runner`, bump `app/main.py` →
`0.4.16-phase3-alpha7.2-dev`, implement in the order above, full quality gate,
fast-forward `main`, drop `-dev`, tag `v0.4.16-phase3-alpha7.2`.
