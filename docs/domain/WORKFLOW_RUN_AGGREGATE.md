# WorkflowRun Aggregate

> **Convention.** This is a domain design document (companion to
> `docs/domain/RENDER_JOB_AGGREGATE.md`, `docs/domain/TIMELINE_AGGREGATE.md`, and
> `docs/domain/PROJECT_AGGREGATE.md`). It defines the **WorkflowRun** aggregate —
> its identity, boundary, **derived** (project-scoped) ownership, and the defining
> stance: the **second orchestration aggregate** (after `RenderJob`) and the first
> that **sequences** work — a record of one workflow execution that owns an ordered
> graph of `WorkflowStep` children and append-only `WorkflowCheckpoint` children,
> advanced by a **synchronous, deterministic runner** of **pure** step handlers,
> coordinated purely through its status + **domain events on the `event_outbox`**.
> It is the design authority for Phase 3 **α7.2** (`WorkflowRun` CRUD + cancel +
> the deterministic runner — **no worker, no providers**). Read it alongside the
> α7.2 pre-flight (`docs/engineering/PHASE3_ALPHA7_2_PREFLIGHT.md`), the pipeline
> blueprint (`docs/architecture/CONTENT_GENERATION_PIPELINE.md`), and **ADR-0040**.
>
> **Grounding.** Every schema claim is checked against the live ORM
> (`backend/app/infrastructure/db/models/workflows.py`) and the baseline migration
> (`backend/alembic/versions/0001_baseline.py`), not an idealised model. Three
> baseline facts anchor the design: `workflow_runs` / `workflow_steps` carry **no
> `VersionMixin`** (no `version`; not in `_VERSION_BUMP_TABLES`) and **no
> `SoftDeleteMixin`** (no `deleted_at`); `workflow_checkpoints` is **append-only**
> (`CreatedAtOnlyMixin` + the `tg_workflow_checkpoints_bud_reject_mutation`
> trigger); and none of the three carries `tenant_id` / `owner_user_id` (ownership
> is derived through the project). So the run is **status-guarded** (not
> versioned), **not soft-deletable**, and project-scoped.

---

## 1. Purpose & position in the model

A **WorkflowRun** is the **record of one workflow execution** and the orchestration
graph beneath it. It is the second **orchestration** aggregate (after `RenderJob`,
ADR-0039) and the first that **sequences** work: contrast the α5–α6 *domain-model*
aggregates (Project, Scene, Prompt, Media, Timeline), which hold persistent
editorial/composition state. A WorkflowRun holds *runtime orchestration* state:
which workflow, at what status, its ordered steps and their statuses, and the
append-only checkpoints that mark each step's resume point.

Position in the hierarchy (baseline schema fact):

```
Project (α4/α5)
  └── WorkflowRun (α7.2)                       ← project-scoped orchestration
        ├── WorkflowStep      (ordered children, workflow_steps.step_index)
        └── WorkflowCheckpoint (append-only children, resume points)

WorkflowRun ──(a render-producing step, α8.x)──▶ RenderJob (render_jobs.workflow_run_id)
```

The critical separation this document establishes (ADR-0040, pre-flight D3.10):

> **A WorkflowRun owns only orchestration/graph state — run status, its step
> sequence + status, and its checkpoints. A render-producing step creates a
> `RenderJob` and links it by FK; it does not fold render state into the run. The
> run never mutates `RenderJob` / `MediaAsset` / `Timeline`; it coordinates via
> events.**

This is a **status-guarded CAS** posture — a deliberate divergence from
`RenderJob`'s self-versioned OCC (ADR-0039), forced by the schema: `workflow_runs`
has **no `version` column**, so transitions fence on **status**, not a token.

---

## 2. Aggregate boundary

### 2.1 The aggregate root — `WorkflowRun`

The domain `WorkflowRun` is a slim, frozen view of the physical `workflow_runs` row:

- `id` — durable UUID, server-minted.
- `project_id` — the ownership anchor (FK → `projects.id`, `ON DELETE CASCADE`).
  Ownership is **derived** through the project — the table carries **no**
  `tenant_id` / `owner_user_id`.
- `workflow_key` / `workflow_version` — resolve to an **in-code** workflow
  definition (§ registry); an unknown pair on create is a `422`. **Set on create,
  immutable thereafter.**
- `status` — the `workflow_status` ENUM (`queued, running, paused, succeeded,
  failed, canceled`), no server default (the app sets `queued`). **The run lifecycle
  token (§4).** `paused` is not produced by the α7.2 runner.
- `started_at` / `finished_at` — runner-set timestamps. **Null until `advance`.**
- `triggered_by_user_id` — the actor (FK → `users.id`, `ON DELETE SET NULL`).
- `idempotency_key` — optional client dedupe token; unique per project
  (`uq_workflow_runs_project_id_idempotency_key`).
- `input_snapshot` — JSONB, `NOT NULL`, the frozen inputs the run executes against.
- `output_summary` — JSONB, runner-set on success (`{step_count, completed_steps}`).
  **Null until succeeded.**
- `error` — JSONB, runner-set on failure (`{step_index, step_name, reason, error}`).
  **Null unless failed.**
- `created_at` / `updated_at` — timestamps; `updated_at` is trigger-owned.

There is **no `version`** field (§4) and **no `deleted_at`**.

### 2.2 The children — `WorkflowStep` (ordered) and `WorkflowCheckpoint` (append-only)

- **`WorkflowStep`** — a row of `workflow_steps`: `id`, `workflow_run_id` (FK, `ON
  DELETE CASCADE`), `step_index` (ordinal; `uq_workflow_steps_workflow_run_id_step_index`),
  `step_name`, `status` (the `step_status` ENUM: `pending, running, succeeded,
  failed, skipped, retrying`), `started_at` / `finished_at`, `retries` (server-default
  `0`), `input` / `output` / `error` (JSONB), timestamps. **No `version`.** Steps are
  seeded `pending` on create and transition via status-guarded CAS.
- **`WorkflowCheckpoint`** — a row of `workflow_checkpoints`: `id`,
  `workflow_run_id` (FK, `ON DELETE CASCADE`), `step_index`, `state` (JSONB), and
  `created_at` only. **Append-only** (`CreatedAtOnlyMixin` + the
  `tg_workflow_checkpoints_bud_reject_mutation` trigger blocks UPDATE/DELETE,
  ADR-0014). One is appended per successful step as its resume point.

### 2.3 The defining facts: status-guarded, append-only checkpoints, not soft-deletable

`WorkflowRun` / `WorkflowStep` are `UUIDPrimaryKeyMixin + TimestampMixin` — **no
`VersionMixin`, not in `_VERSION_BUMP_TABLES`** (contrast `render_jobs`). Neither
carries `SoftDeleteMixin`: there is no `deleted_at`. A run is an operationally
terminal **audit record** — "removing" it is a `cancel` *status transition*, and a
canceled run stays `GET`-able. Checkpoints are immutable resume points.

---

## 3. Ownership, scoping & anti-enumeration

Ownership is **derived through the project** (the workflow tables have no owner
columns). Every endpoint is authenticated (`CurrentUserDep`) and runs a **two-level
gate**:

- **Project gate** — `IProjectRepository.get_owned(project_id, tenant, owner)`;
  `None` → uniform `404 NOT_FOUND` (missing / soft-deleted / not the caller's).
- **Workflow-run gate** — `WorkflowRunRepository.get_owned(project_id,
  workflow_run_id)`; `None` → uniform `404` (unknown, or under another user's
  project — anti-enumeration, pre-flight D3.3).

On **create**, the workflow definition is resolved **before** any DB work (pure
registry lookup); an unknown `workflow_key@workflow_version` is a **`422`** (the
request is well-formed but names no runnable workflow — the project IS visible, so
not a `404`).

`idempotency_key` uniqueness is enforced by the unique constraint; a repeat key
returns the **existing** run (`200`, §6), and a unique-violation race surfaces as
`ConflictError` resolved by returning the winner.

---

## 4. Concurrency & lifecycle: status-guarded state machines (ADR-0040)

There is **no `version` token**. `status` is the lifecycle, and every transition is
a **status-predicated CAS** (`UPDATE … WHERE status IN (<allowed_from>) RETURNING
*`) that yields the row on success and `None` when the guard did not match (the use
case re-classifies). Non-transition metadata is last-writer-wins.

### 4.1 Run state machine (α7.2 subset — synchronous runner)

```
             create
        ∅ ───────────▶ queued
                          │  advance (queued → running, WorkflowRunStarted)
                          ▼
                       running ──── advance runs all steps ────┐
                          │                                     │
              cancel      │                        all steps ok │  a step fails terminally
        ┌─────────────────┼───────────────┐                    ▼         / exhausts retries
        ▼                 ▼                ▼                 succeeded        ▼
     canceled        (paused — not         (advance settles)  (200)        failed (200)
     (200 no-op       produced in α7.2)
      on re-cancel)
```

- **Create** → `status = queued`; steps seeded `pending`. Emits `WorkflowRunCreated`.
- **Advance** (`POST …/advance`, **no body**) — the deterministic runner (§5):
  - not `queued`/`running` (terminal / `paused`) → **`409`**;
  - `queued` → CAS `queued → running` (+ `WorkflowRunStarted`), then runs steps;
  - each step: skip already-`succeeded`/`skipped` (resume-safety), else CAS `→
    running`, call the pure handler, and on success CAS `→ succeeded` + append a
    checkpoint + emit `WorkflowStepCompleted`;
  - all steps done → CAS `running → succeeded` (+ `output_summary`,
    `WorkflowRunSucceeded`); a terminal step failure/exhaustion → CAS `running →
    failed` (+ `error`, `WorkflowRunFailed`). Returns **`200`** either way (the run
    ran to a terminal state; success/failure is in the body).
- **Cancel** (`POST …/cancel`, **no body**) — a status-guarded CAS, decided
  **404-before-classify**:
  - project/run not visible → `404`;
  - already `canceled` → **`200`** idempotent no-op (no event);
  - `succeeded`/`failed` → **`409`** (completed work is not cancelable);
  - `queued`/`running`/`paused` → **`200`** with `status = canceled` (+
    `WorkflowRunCanceled`). A lost CAS (the runner raced the run terminal) is
    re-classified against a re-read.

There is **no `412`** (no version token) and **no `DELETE`** (no `deleted_at`), which
is why re-cancel is a `200` no-op.

### 4.2 Step state machine + retry accounting (pre-flight Q5)

```
pending ─▶ running ─▶ succeeded
             │  transient failure & retries < max
             ▼
          retrying ─▶ running ─▶ …            (retries += 1 each cycle, DB increment)
             │  transient failure & retries == max      terminal failure
             ▼                                                 ▼
           failed  ◀──────────────────────────────────────────┘
```

`max_retries` is declared per step (retries **after** the first attempt; total
attempts = `max_retries + 1`). The `attempt` passed to a handler is the persisted
`retries` counter, so flakiness is deterministic with no hidden state. No
backoff/delay/scheduler in α7.2 — retries are in-process and immediate.

A `WorkflowRun` mutation **never** bumps `projects.version` and never reaches into
another aggregate.

---

## 5. The runner: a deterministic imperative shell over pure steps (ADR-0040 D3/D4)

`AdvanceWorkflowRun` is the **only** side-effecting component. A **step handler** is
a **pure function** `(StepContext) -> StepResult` (D3.11): it never performs I/O and
never calls providers — it **returns a description** of what should happen:

- `StepContext` = `{ run_input, prior_state (the preceding step's checkpoint), attempt }`.
- `StepResult` = an `outcome` (`SUCCEEDED` / `TRANSIENT_FAILURE` / `TERMINAL_FAILURE`)
  + `output` (persisted on the step) + `checkpoint_state` (appended as the resume
  point) + declarative `StepCommand`s (dispatched to real providers in α8.x — nothing
  consumes them in α7.2) + an `error` envelope on failure.

The runner interprets the result: persists the step output, appends the checkpoint,
emits the events, and handles retries. Because handlers are pure and the `attempt`
is deterministic, **run output is reproducible to the byte** — a function of
`input_snapshot`, the prior checkpoint state, and the retry counter only.

α7.2 ships four **deterministic, provider-free** workflows in the in-code registry
(`workflow_key@1.0.0`): `noop-chain` (3-step success), `retry-succeed` (a flaky step
that succeeds on attempt 2), `terminal-fail` (a step that fails unrecoverably), and
`retry-exhaust` (a step that exhausts its retry bound). They exercise the runner's
full surface with no I/O.

---

## 6. Cross-aggregate coordination: the transactional outbox (D9)

A WorkflowRun coordinates with the rest of the system **only** through domain events
written to the `event_outbox` in the **same UnitOfWork transaction** as the state
change (blueprint §6 / D9 — the transactional-outbox guarantee). The Q8 set:

| Event | When |
|---|---|
| `WorkflowRunCreated` | on create (steps seeded `pending`) |
| `WorkflowRunStarted` | first `queued → running` transition |
| `WorkflowStepCompleted` | a step succeeds (carries `step_index` / `step_name`) |
| `WorkflowRunSucceeded` | all steps done; run `→ succeeded` |
| `WorkflowRunFailed` | a step fails terminally / exhausts retries; run `→ failed` |
| `WorkflowRunCanceled` | on a real cancel (not on an idempotent no-op) |

Event bodies carry **orchestration fields only** (`workflow_run_id`, `project_id`,
`workflow_key`, `workflow_version`, `status`, plus the step coordinates for the step
event); a consumer needing richer state resolves it from the referenced aggregates
(§1 boundary). `event_version` starts at `"1.0"`.

α7.2 only **produces** rows — they accumulate (`published_at IS NULL`) until the α7.3
relay ships. There is **no dispatcher** in α7.2; no aggregate directly mutates
another's state.

---

## 7. Idempotency & resume-safety

`idempotency_key` is **optional** on create (Q7). A repeat create with the **same**
key for the project returns the **existing** run with `200` (α7.1 parity), backed by
`uq_workflow_runs_project_id_idempotency_key`; a unique-violation race is resolved by
returning the winner. Step-level idempotency is **resume-safety by `step_index`**:
`advance` skips already-`succeeded`/`skipped` steps (threading their checkpoint state
forward) and only runs `pending`/`retrying` steps, so re-advancing never double-runs
a completed step. `uq_workflow_steps_workflow_run_id_step_index` makes seeding
resume-safe too.

---

## 8. Structured-log posture

WorkflowRun lifecycle events are logged with identifiers + field names only:

- `workflow_run.created` (INFO) — `workflow_run_id`, `project_id`, `workflow_key`,
  `workflow_version`, `step_count`, `idempotency_key`, `owner_user_id`, `ip`.
- `workflow_run.create_idempotent_replay` / `workflow_run.create_idempotent_race`
  (INFO) — `workflow_run_id`, `project_id`, `idempotency_key`, `owner_user_id`, `ip`.
- `workflow_run.advanced` (INFO) — `workflow_run_id`, `project_id`, `status`,
  `owner_user_id`, `ip`. `workflow_run.advance_noop_terminal` (INFO) on a terminal
  advance.
- `workflow_run.canceled` (INFO) / `workflow_run.cancel_noop` (INFO, idempotent
  re-cancel) / `workflow_run.cancel_rejected` (WARNING, `reason=terminal_state`) —
  `workflow_run_id`, `project_id`, `status`, `owner_user_id`, `ip`.

---

## 9. Open evolution (explicitly out of α7.2)

- **The asynchronous workflow worker (α8.x).** Dispatches `StepCommand`s to real
  providers, runs long steps out of a single request, adds pause/resume (`paused`),
  backoff scheduling, and acquires the `workflow_run:{id}` lock (ADR-0032). The
  pure-handler contract (§5) is unchanged; only the `StepCommand` interpreter is added.
- **Render-producing steps (α8.x).** A step creates a `RenderJob` and links it via
  `render_jobs.workflow_run_id` — render state stays on the `RenderJob` (§1 boundary).
- **DB-backed workflow authoring.** Replaces/augments the in-code registry once an
  authoring/runtime story exists.
- **Outbox relay (α7.3).** Publishes the six `WorkflowRun*` events this slice
  produces; stamps `published_at`.

---

## 10. Change log

| Date | Change |
|---|---|
| 2026-07-16 | Initial authoring for Phase 3 α7.2 (`WorkflowRun` aggregate + the synchronous deterministic runner — the first sequencing orchestration slice). Establishes the status-guarded CAS concurrency model (no `version` column; a deliberate divergence from `RenderJob`'s versioned OCC — ADR-0040 D2), derived (project-scoped) ownership + two-level visibility gate, the in-code workflow registry (unknown `key@version` → `422`), pure/deterministic/side-effect-free step handlers returning command/results (D3.11), the deterministic runner (`advance`) with resume-safety by `step_index` and a deterministic retry counter (Q5), append-only checkpoints (ADR-0014), idempotent create (repeat key → existing run `200`), and the six-event transactional-outbox coordination (`WorkflowRunCreated`/`WorkflowRunStarted`/`WorkflowStepCompleted`/`WorkflowRunSucceeded`/`WorkflowRunFailed`/`WorkflowRunCanceled` produced now, relay α7.3). No worker, no providers, no scheduler, pause/resume deferred (α8.x). Adopts ADR-0040. |
