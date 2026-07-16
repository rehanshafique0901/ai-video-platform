# ADR-0040 — The `WorkflowRun` Is a Status-Guarded Orchestration Aggregate Driven by a Deterministic Runner of Pure Steps

**Status:** Proposed (documents the pattern shipped in Phase 3 α7.2 — the second
orchestration slice, and the first that *sequences* work). Flips to Accepted on
merge of this ADR PR.
**The second orchestration aggregate.** α7.1's `RenderJob` (ADR-0039) opened the
orchestration era with a single-state lifecycle. `WorkflowRun` is the first
aggregate that **sequences** work: it owns an ordered graph of `WorkflowStep`
children and append-only `WorkflowCheckpoint` children, and a **synchronous,
deterministic runner** advances it through an **in-code workflow definition** of
**pure** step handlers.
**Diverges deliberately from ADR-0039 on concurrency.** `RenderJob` fences on a
real `render_jobs.version` (self-versioned OCC). `workflow_runs` / `workflow_steps`
carry **no `version` column** (they are not in `_VERSION_BUMP_TABLES`), so
`WorkflowRun` uses **status-guarded CAS** transitions + last-writer-wins metadata
instead. This is a documented divergence forced by the baseline schema, not an
oversight.
**Refines / documents:** `docs/domain/WORKFLOW_RUN_AGGREGATE.md`,
`docs/architecture/CONTENT_GENERATION_PIPELINE.md` (§7 event coordination / D9
outbox), `API_CONTRACT.md` §3.2.6 (new Workflow Runs resource), and the α7.2
pre-flight (`docs/engineering/PHASE3_ALPHA7_2_PREFLIGHT.md`, Q1–Q9 + D3.1–D3.11).
Builds on **ADR-0031** (idempotency-keys), **ADR-0032** (distributed-locks lease),
**ADR-0034** (authenticated endpoint pattern), **ADR-0039** (the orchestration
posture it adapts).
**Wave:** Phase 3, orchestration slice α7.2 (`WorkflowRun` aggregate root + the
**synchronous deterministic runner**). Real provider-backed workflow steps, an
asynchronous worker, pause/resume, and a scheduler are α8.x.

---

## Context

α7.2 continues the **orchestration layer** begun by α7.1. Where a `RenderJob` is a
single request with a one-shot lifecycle, a **workflow run** is a *sequence*: an
ordered set of steps, each advancing the run and leaving a resume point. α7.2 ships
**zero migrations** — the `workflow_runs`, `workflow_steps`, and
`workflow_checkpoints` tables, the `workflow_status` / `step_status` ENUMs, the
`uq_workflow_runs_project_id_idempotency_key` and
`uq_workflow_steps_workflow_run_id_step_index` uniques, the dispatch/list indexes,
and the append-only checkpoint trigger all exist in baseline `0001`
(`docs/database/schema.md` §16).

The physical schema signals the posture (α7.2 pre-flight §2):

- `workflow_runs` is `WorkflowRun(UUIDPrimaryKeyMixin, TimestampMixin, Base)` and
  `workflow_steps` is `WorkflowStep(UUIDPrimaryKeyMixin, TimestampMixin, Base)` —
  **neither carries `VersionMixin`** (no `version` column, not in
  `_VERSION_BUMP_TABLES`). Contrast `render_jobs`, which does. Ownership is
  **derived through the project** (`project_id → projects.owner_user_id`); the
  tables carry **no** `tenant_id` / `owner_user_id`.
- `workflow_checkpoints` is `WorkflowCheckpoint(UUIDPrimaryKeyMixin,
  CreatedAtOnlyMixin, Base)` — **append-only**: no `updated_at`, no `deleted_at`,
  and the `tg_workflow_checkpoints_bud_reject_mutation` trigger blocks
  UPDATE/DELETE (ADR-0014).
- There is **no `deleted_at`** on any of the three — a run is a terminal audit
  record; "removal" is the `canceled` status.
- `workflow_key`, `workflow_version`, `status`, and `input_snapshot` are `NOT NULL`
  with no server default — the application supplies them on insert.
- `started_at`, `finished_at`, `output_summary`, and `error` are runner-owned and
  stay null until `advance` settles the run.

Without an ADR, a future contributor sees an orchestration aggregate nested under a
project with **no `version` column** (unlike its α7.1 sibling), a runner that
mutates step rows in place, and pure handlers that return data instead of calling
providers — and cannot tell whether that is a **decision** or an **unfinished**
shape to be "completed" (e.g. by adding a `version` column, by having handlers call
providers directly, or by folding render state into the run). This ADR promotes the
implemented convention to a recorded decision.

---

## Decision

### D1 — `WorkflowRun` is a self-owned orchestration aggregate (pre-flight D3.1/D3.9)

The **aggregate root** is the `WorkflowRun`. It owns the run lifecycle, its ordered
`WorkflowStep` children, and its append-only `WorkflowCheckpoint` children, and it
is the **sole writer** of `workflow_runs.status` / `workflow_steps.status`. It owns
framework-free domain enums `WorkflowRunStatus` (`queued, running, paused,
succeeded, failed, canceled`) and `WorkflowStepStatus` (`pending, running,
succeeded, failed, skipped, retrying`) whose `is_terminal` / `is_cancelable` /
`is_advanceable` / `is_runnable` / `is_done` properties gate transitions in the
domain layer (not with string literals). The governing principle:

> **A `WorkflowRun` is the record of one workflow execution and the orchestration
> graph beneath it. It owns its run/step status machines and coordinates purely
> through its own status + domain events — it never mutates, and is never folded
> into, `projects.version`, `RenderJob`, `MediaAsset`, or `Timeline`.**

### D2 — Status-guarded CAS, not versioned OCC (pre-flight Q2/D3.2 — divergence from ADR-0039)

`workflow_runs` and `workflow_steps` carry **no `version` column**, so `WorkflowRun`
**cannot** use ADR-0039's version-fenced CAS. Instead every lifecycle transition is
a **status-predicated compare-and-swap**: `UPDATE … WHERE status IN
(<allowed_from>) RETURNING *`, which yields the row on success and **no row** when
the guard did not match (the use case re-classifies to `404` / `409` / idempotent
`200`). Non-transition metadata is **last-writer-wins**. This is race-safe at the
DB **without** a numeric token: a run the runner moves to a terminal state between a
cancel's read and its CAS write is never overwritten (zero rows → `None` →
re-classify). There is **no `?version=` on any endpoint and no `412`** — the wire
carries no OCC token. This is the **workflow-specific concurrency model**, a
deliberate, schema-forced divergence from the render job's versioned cancel.

### D3 — The runner is a synchronous, deterministic imperative shell (pre-flight Q1/D3.8)

`advance` (`AdvanceWorkflowRun`) is the runner. It runs a `queued` run to a
**terminal state within one synchronous call**, in **one UnitOfWork transaction**
(the whole run either commits terminal or rolls back). It is the **only** place that
touches the DB / outbox. The aggregate and its runner ship in **one slice** (Q1) —
splitting them would leave a CRUD aggregate that cannot execute. There is **no
async worker, no scheduler, no external provider** in α7.2 (those are α8.x).

### D4 — Steps are pure, deterministic, side-effect-free (pre-flight D3.11 — the load-bearing rule)

A **step handler** is a **pure function** `(StepContext) -> StepResult`. It never
performs I/O and never calls providers or external services — it **returns a
description** of what should happen: the step `output` to persist, the
`checkpoint_state` to append as a resume point, and zero or more declarative
`StepCommand`s that a later slice (α8.x) will dispatch to real providers. The runner
(the imperative shell, D3) interprets the `StepResult`; the handler is the
functional core.

> **A step returns a command/result describing what should happen, rather than
> directly calling a provider. The runner is the only side-effecting component.**

Consequence: the entire runner is unit-testable with pure handlers, run output is
**reproducible to the byte** (derived only from `input_snapshot`, the prior
checkpoint state, and the deterministic `attempt` counter), and the eventual move to
Celery / LangGraph + provider adapters is an **execution concern, not a domain
rewrite** — the handler contract and the run/step state machines are unchanged;
only the interpreter of `StepCommand` changes.

### D5 — Retry accounting is a deterministic counter, no backoff/scheduler (pre-flight Q5/D3.8)

A step definition declares `max_retries` (retries **after** the first attempt; total
attempts = `max_retries + 1`, `0` = one attempt). On a `TRANSIENT_FAILURE` the
runner CAS-transitions the step `running → retrying` (`retries = retries + 1`, DB
increment) and re-runs it while `retries <= max_retries`; on exhaustion it fails the
step (and the run). The `attempt` is the persisted `retries` counter passed into
`StepContext`, so a handler can be deterministically flaky in tests with **no hidden
state**. A backoff policy is **represented** (metadata) but **not** scheduled —
there is no delay, timer, or scheduler in α7.2.

### D6 — Cross-aggregate coordination is event-only; the outbox exercises the full lifecycle (pre-flight Q8/D9)

On every state change the run writes a domain event to the `event_outbox` in the
**same UnitOfWork transaction** as the mutation (the transactional-outbox
guarantee). The Q8 event set — produced from the first slice:

- `WorkflowRunCreated` — a run was queued (steps seeded `pending`).
- `WorkflowRunStarted` — the runner took the run `queued → running`.
- `WorkflowStepCompleted` — a step succeeded (carries `step_index` / `step_name`).
- `WorkflowRunSucceeded` — all steps done; run settled `running → succeeded`.
- `WorkflowRunFailed` — a step failed terminally / exhausted retries; run `→ failed`.
- `WorkflowRunCanceled` — the run was canceled.

α7.2 only **produces** rows (they accumulate with `published_at IS NULL`); the relay
that publishes them is α7.3, and there is **no dispatcher** in α7.2. Event bodies
carry **orchestration fields only** (identity + `workflow_key`/`workflow_version`/
`status`, plus the step coordinates for the step event) — a consumer needing richer
state resolves it from the referenced aggregates (D8). `event_version` starts at
`"1.0"`.

### D7 — In-code workflow registry; DB-backed authoring deferred (pre-flight Q3/D3.9)

Workflow definitions live in an **in-code registry** (`WorkflowRegistry`) mapping
`workflow_key@workflow_version` → an ordered tuple of `StepDefinition`s. α7.2 ships
**deterministic, provider-free** definitions only (`noop-chain`, `retry-succeed`,
`terminal-fail`, `retry-exhaust`) that exercise the runner's full surface — success
chains, retry-then-succeed, terminal failure, retry exhaustion. An unknown
`key@version` on create is a **`422`** (well-formed request, no runnable workflow).
Tests may build an isolated registry and inject it, so the runner never depends on
the module singleton. DB-backed authoring waits for a real authoring/runtime story.

### D8 — Owns only orchestration/graph state (boundary invariant, pre-flight D3.10)

`WorkflowRun` owns *run status, its step sequence + status, and its checkpoints* —
nothing else. A render-producing step (α8.x) creates a `RenderJob` and links it via
`render_jobs.workflow_run_id`; it does **not** fold render state into the run. The
run never mutates `RenderJob` / `MediaAsset` / `Timeline`; it only coordinates via
events (D6).

```
WorkflowRun ── owns orchestration/graph state (run status, step sequence + status, checkpoints)
RenderJob   ── a render-producing step LINKS one (render_jobs.workflow_run_id); state stays there
MediaAsset  ── owns produced assets
Timeline    ── owns edit state
```

### D9 — Project-nested routing; ownership derived through the project (pre-flight D3.2/D3.3)

Ownership is derived through the project, so the surface is **project-nested** and
every access runs a **two-level uniform-`404` gate** (project owned by caller →
workflow run belongs to that project — anti-enumeration).

```
POST   /api/v1/projects/{project_id}/workflow-runs
GET    /api/v1/projects/{project_id}/workflow-runs?status=<workflow_status>
GET    /api/v1/projects/{project_id}/workflow-runs/{workflow_run_id}
POST   /api/v1/projects/{project_id}/workflow-runs/{workflow_run_id}/advance
POST   /api/v1/projects/{project_id}/workflow-runs/{workflow_run_id}/cancel
```

`advance` and `cancel` are `POST` verbs (they change state) that carry **no body**
and **no `version`** (D2). A `DELETE` verb is **not** offered — a run is an audit
record; "removal" is the `canceled` status, so re-cancel is a **`200` no-op**.

### D10 — Idempotency: repeat key returns the existing run; steps resume by index (pre-flight Q7)

`idempotency_key` is **optional** on create. A repeat create with the **same** key
for the project returns the **existing** run with `200` (α7.1 parity), backed by the
race-safe `uq_workflow_runs_project_id_idempotency_key` unique. Step-level
idempotency is **resume-safety by `step_index`**: `advance` skips already-`succeeded`
/ `skipped` steps (threading their checkpoint state forward) and only runs
`pending` / `retrying` steps, so re-advancing never double-runs a completed step
(`uq_workflow_steps_workflow_run_id_step_index` makes seeding resume-safe too).

### D11 — Pause/Resume deferred; distributed-lock convention only (pre-flight Q4/Q6)

`paused` exists in the ENUM but is **not produced** by the α7.2 synchronous runner
(it completes or fails within one invocation) and is **not advanceable** here —
pause/resume belongs with the asynchronous worker (α8.x). The lock key for a run is
`workflow_run:{id}`; α7.2 documents the **convention only** — the status-guarded CAS
already serializes correctness, so live acquisition + lease renewal (over
`distributed_locks`, ADR-0032) are built when multiple workers exist.

---

## Alternatives Considered

1. **Add a `version` column and reuse ADR-0039's versioned OCC.** *Rejected* (D2):
   breaks the zero-migration discipline and `_VERSION_BUMP_TABLES` membership. The
   status-guarded CAS is already race-safe at the DB without a token.

2. **Ship the aggregate now, the runner later.** *Rejected* (D3): a `WorkflowRun`
   without a runner is a CRUD row that cannot execute anything — the runner is what
   gives the aggregate meaning.

3. **Let step handlers call providers directly.** *Rejected* (D4): couples the
   domain to I/O, makes the runner untestable without mocks, and turns the eventual
   Celery/LangGraph move into a domain rewrite. Pure handlers + a side-effecting
   shell keep the core reproducible and the migration an execution concern.

4. **DB-backed workflow definitions now.** *Rejected* (D7): premature without an
   authoring/runtime story; the in-code registry is enough to exercise the runner
   and keeps the slice migration-free.

5. **A `DELETE` verb for cancel / a `?version=` fence.** *Rejected* (D2/D9): there is
   no `deleted_at` and no version token; a canceled run is a retained audit record,
   so cancel is a status transition (re-cancel `200` no-op) with no `412`.

6. **Implement pause/resume + a scheduler now.** *Rejected* (D11): a synchronous
   runner completes or fails in one call; pause/resume and backoff scheduling are
   asynchronous-worker concerns (α8.x).

7. **Defer all outbox production to α7.3.** *Rejected* (D6): then α7.2's writes are
   silent and the D9 coordination mechanism is unexercised. Producing the full
   six-event set now is cheap and validates outbox persistence + event shape.

---

## Consequences

- **Positive — a second orchestration pattern: sequencing.** A status-guarded
  aggregate + an ordered step graph + append-only checkpoints + a deterministic
  runner of pure steps, with zero cross-aggregate mutation. Later sequencing
  aggregates copy this shape.
- **Contract — status-guarded, not version-fenced.** Cancel/advance carry no body
  and no `version`; there is no `412`. A terminal run → `409`; a re-cancel of a
  canceled run → `200` no-op; missing/foreign project or run → uniform `404`; an
  unknown `workflow_key@version` on create → `422`.
- **Correctness — the terminal/runnable guards are race-safe at the DB.** The CAS
  predicates (`status IN (...)`) mean a concurrent advance/cancel never
  double-runs a step or overwrites a settled run; a lost CAS is re-classified.
- **Correctness — runs are reproducible.** Pure handlers + the deterministic
  `attempt` counter make output a function of inputs only.
- **Positive — small, migration-free slice.** α7.2 is the aggregate + ownership gate
  + status-guarded CAS + the registry + the deterministic runner + idempotency +
  the six-event outbox producer — no worker, no providers, no scheduler.
- **Deferred — runner-owned async concerns stay out.** Real providers, an async
  worker, pause/resume, backoff scheduling, distributed-lock acquisition, and
  `StepCommand` dispatch are α8.x.

---

## Pattern Reference (Examples)

- **Domain:** `app/domain/workflow/workflow_run.py` (frozen `WorkflowRun` /
  `WorkflowStep` / `WorkflowCheckpoint`, **no** `version`),
  `app/domain/workflow/workflow_run_status.py` + `workflow_step_status.py` (the two
  status enums), `app/domain/workflow/registry.py` (`WorkflowRegistry`,
  `StepHandler` protocol, `StepContext` / `StepResult` / `StepCommand`, the four
  deterministic workflows).
- **Repository:** `app/infrastructure/repositories/workflow_run_repository.py`
  (`WorkflowRunRepository`: `add` [idempotency unique → `ConflictError`],
  `seed_steps`, `get_by_project_and_key`, `list_by_project` [newest-first + `status`
  filter], `get_owned`, `list_steps`, `latest_checkpoint`, the status-guarded run
  transitions `mark_run_running` / `mark_run_succeeded` / `mark_run_failed` /
  `cancel`, the step transitions `mark_step_running` / `mark_step_succeeded` /
  `mark_step_retrying` / `mark_step_failed`, and append-only `append_checkpoint`).
- **Use cases:** `app/application/use_cases/workflow/*` — `CreateWorkflowRun`
  (idempotent, seeds steps, emits `WorkflowRunCreated`), `ListWorkflowRuns`,
  `GetWorkflowRun`, `CancelWorkflowRun` (status-guarded transition), `AdvanceWorkflowRun`
  (the deterministic runner), `_events.py` (the six-event outbox shapes), `_view.py`
  (`WorkflowRunView` detail read-model). None mutate another aggregate.
- **DTOs / router:** `app/api/v1/schemas/workflow.py`,
  `app/api/v1/routers/workflow_runs.py` (project-nested; 201/200 create split; no
  version on the wire).

New sequencing aggregates copy these shapes rather than reinventing them.

---

## Future Extensions

- **α7.3 — Outbox relay worker** — publishes the six `WorkflowRun*` event types this
  slice produces and stamps `published_at`. Pure consumer-side addition.
- **α8.x — The asynchronous workflow worker** — dispatches `StepCommand`s to real
  providers, drives long-running steps out of a single request, adds pause/resume
  (`paused`), backoff scheduling, and acquires the `workflow_run:{id}` lock
  (ADR-0032). The pure-handler contract (D4) is unchanged; only the `StepCommand`
  interpreter is added.
- **α8.x — Render-producing steps** — a step creates a `RenderJob` and links it via
  `render_jobs.workflow_run_id` (D8), never folding render state into the run.
- **Later — DB-backed workflow authoring** — replaces / augments the in-code
  registry (D7) once an authoring/runtime story exists.
