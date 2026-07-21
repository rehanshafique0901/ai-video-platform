# Phase 3 Slice α7.6 — First Pipeline (mock, synchronous) — the composition slice — Pre-flight

> Status: **SIGNED OFF (2026-07-19)** — see §6. All §4 questions Q1–Q9 approved;
> Q1 refined to **one fully-executable image pipeline + one minimal video pipeline
> that proves the pause seam only**. Two invariants added: **W7.6.1 — the runner
> never interprets provider payloads** and **W7.6.2 — exactly one dispatcher
> invocation per `StepCommand`; retries are the runner's alone.** This is the
> composition slice: it introduces **almost no new infrastructure** and instead
> wires the five seams already built (α7.2 runner · α7.4 dispatcher · α7.5 recorder ·
> α7.3 outbox relay/lock manager · checkpoints) into **one complete, deterministic,
> in-process orchestration loop**. The runtime architecture was signed off in
> [`ADR-0041`](../decisions/ADR-0041-provider-runtime-contract.md); the
> `StepCommand` dispatcher is **D4**, the usage/cost seam is **D13**. This doc
> resolves the **α7.6-specific** open questions (§4). Nothing is implemented yet.
>
> Mirrors the α5/α6/α7.1–α7.5 discipline: ground in the existing contract + code →
> lock decisions → sign-off → branch → implement → CI → merge → tag. Read-only
> planning artefact.
>
> **Predecessors (all released, `main`):**
> * α7.2 (`v0.4.16`) — the **runner**: `AdvanceWorkflowRun`, an imperative shell over
>   pure `(StepContext) -> StepResult` handlers. It **deliberately ignores
>   `StepResult.commands`** (registry.py: *"in α7.2 nothing consumes commands … the
>   α8.x execution layer is what dispatches a command"*).
> * α7.4 (`v0.4.18`) — the **dispatcher**: `ProviderDispatcherPort` +
>   `StepCommandDispatcher` (closed `kind → capability` table), deterministic mocks
>   (image/LLM/voice inline `SUCCEEDED`; **video models the async `IN_PROGRESS` +
>   `provider_job_id` path**), and the neutral `ProviderResponse` / `ProviderUsage`.
> * α7.5 (`v0.4.19`) — the **recorder**: `UsageRecorderPort` /
>   `UsageRecorderService`, pure `account()` / `price()`, idempotent-on-`request_id`
>   `usage_records` writes (ADR-0033), read-only pricing (`ai_model_pricing`).
> * α7.3 (`v0.4.17`) — the **outbox relay + lock manager** (events already emitted by
>   the runner via `uow.outbox.add(...)`).
>
> **This slice proves the seams compose.** Per ADR-0041 the runtime is:
>
> ```
> WorkflowRun → Runner → StepCommand → Dispatcher → Mock Provider → ProviderResponse
>                                                          │
>                                    ┌─────────────────────┼───────────────────────┐
>                                    ▼                     ▼                        ▼
>                             Usage Recorder        Checkpoint (+ output)      Event Outbox
> ```
>
> all inside **one synchronous `advance` call, one UnitOfWork transaction** — no
> HTTP, no external providers, no worker, no scheduler, no Redis, no Celery, no
> webhooks, no polling. Just: **does the orchestration engine actually orchestrate?**

---

## 1. Grounding — what already exists (verified against code)

### 1.1 The runner (`app/application/use_cases/workflow/advance_workflow_run.py`)
- `AdvanceWorkflowRun(uow, registry=WORKFLOW_REGISTRY)`. `execute(...)` runs the whole
  run to a terminal state in **one `async with self._uow:` transaction**, committing
  once at the end (`await self._uow.commit()`), then returns a `WorkflowRunView`.
- Per-step (`_run_single_step`): `mark_step_running` → `result = step_def.handler(ctx)`
  → on `SUCCEEDED`: `mark_step_succeeded(output)` → `append_checkpoint(checkpoint_state)`
  → `emit_workflow_step_completed`. **`result.commands` is never read.** ← the exact
  extension point for α7.6.
- Transient failure → `mark_step_retrying` + loop (bounded by `max_retries`, no
  backoff); terminal → `mark_step_failed` + fail the run.
- Statuses: only `queued`/`running` are advanceable; `paused` exists in the enum
  but is **not produced** and **not advanceable** (α7.2 deferred pause/resume to
  α8.x). The repo has `mark_run_running/succeeded/failed` — **no `mark_run_paused`**.

### 1.2 The pure handler contract (`app/domain/workflow/registry.py`)
- `StepResult` already carries `commands: tuple[StepCommand, ...]` (populated by
  handlers, ignored by the runner today). `StepCommand = {kind: str, args: dict}`.
- `StepContext = {run_input, prior_state, attempt}` — **the handler does NOT see
  `run_id` / `step_index`** (relevant to `request_id` minting — §4 Q3).
- `WorkflowRegistry` / `WorkflowDefinition` / `StepDefinition` / `StepHandler`
  **already provide exactly the "PipelineRegistry / PipelineDefinition /
  StepDefinitions" structure** (key@version → ordered pure steps). Handlers stay
  pure; the runner is the only impure actor.

### 1.3 The dispatcher (`app/infrastructure/ai/dispatcher.py` + `provider_dispatcher.py`)
- `ProviderDispatcherPort.dispatch(StepCommand) -> ProviderResponse`, `supports`,
  `list_capabilities`. Lives in `app.application.interfaces` → the **runner (a
  `use_cases` module) may depend on it** without importing infrastructure.
- The closed table maps `generate_text/image/video` + `synthesize_voice` → capability
  calls. **`dispatch` requires `command.args["request_id"]`** — a missing id is a
  terminal `ProviderValidationError` (not a provider fault). ← §4 Q3.
- `ProviderResponse = {request_id, provider, status, output, provider_job_id, usage,
  error}`. `provider_job_id` is set **iff** `status is IN_PROGRESS`. It carries **no
  `capability` and no `model_id`** (UUID). ← §4 Q4.

### 1.4 The recorder (`app/application/use_cases/usage/…` + `interfaces/usage_recorder.py`)
- `UsageRecorderService.record(RecordUsageCommand)` **opens its own
  `async with self._uow:` and commits** — it is a *standalone unit of work*. Using it
  verbatim inside the runner's open transaction would double-open / early-commit. ←
  §4 Q5.
- Pure `account()` / `price()` are I/O-free and reusable directly.
- `RecordUsageCommand` needs `tenant_id` + **`model_id` (UUID, NOT NULL FK →
  `ai_models`)** + `capability` + `usage`. Terminal-only (`IN_PROGRESS` is rejected).
- `usage_records.model_id` is `NOT NULL` with an `ON DELETE RESTRICT` FK → **usage
  cannot be recorded without a real `ai_models` row.** ← §4 Q4.

### 1.5 The outbox (`app/application/use_cases/workflow/_events.py`)
- `uow.outbox.add(...)` writes events **inside the caller's transaction** (atomic with
  state). The runner already emits `WorkflowRunStarted` / `WorkflowStepCompleted` /
  `WorkflowRunSucceeded` / `WorkflowRunFailed`. α7.3's relay publishes them later.

### 1.6 Wiring (`app/core/container.py`)
- `get_advance_workflow_run_use_case()` currently builds `AdvanceWorkflowRun(uow=…)`
  — **no dispatcher injected**. `get_step_command_dispatcher()` and
  `get_provider_registry()` already exist (α7.4). `get_usage_recorder_service()`
  exists (α7.5).

---

## 2. Scope

### 2.1 α7.6 builds
1. **Two deterministic, provider-backed workflows** registered in the existing
   `WorkflowRegistry` (§4 Q1, refined):
   * **`generate-image@1.0.0` — the fully-executable pipeline:** `prepare-prompt`
     (pure) → `generate-image` (pure handler **emits a `generate_image`
     `StepCommand`**) → dispatch → mock returns `SUCCEEDED` → usage → checkpoint →
     `WorkflowStepCompleted` → run `succeeded`. Byte-reproducible.
   * **`generate-video@1.0.0` — the minimal pause pipeline (async seam only):**
     `generate-video` (pure handler emits a `generate_video` command) → dispatch →
     mock returns `IN_PROGRESS` + `provider_job_id` → **pause** (checkpoint the job
     id, run `→ paused`, `WorkflowRunPaused`) → stop. **Nothing beyond pause** — no
     completion, polling, or webhook (α8.3 owns resumption).
2. **Command execution in the runner** — the shell reads `StepResult.commands`,
   injects a deterministic `request_id`, dispatches each via `ProviderDispatcherPort`,
   and folds the `ProviderResponse` into the step (§3, §4 Q3/Q6).
3. **In-transaction usage recording** — a terminal `ProviderResponse` is priced +
   written to `usage_records` **on the runner's own UoW** (no early commit), reusing
   α7.5's pure `account()` / `price()` (§4 Q5).
4. **Checkpoint carries provider output** — the step's checkpoint state absorbs the
   `ProviderResponse` (e.g. `image_ref`, `request_id`, `provider`) so a resume/replay
   is grounded (§3, ordering per §4 user-Q2).
5. **Async pause seam** — a command returning `IN_PROGRESS` transitions the run to
   `paused` and checkpoints `provider_job_id`; the run stops advancing (still `200`).
   The **resume entrypoint** exists; turning `IN_PROGRESS` into terminal (poll/webhook)
   is **α8.3** (§4 Q2).
6. **DI + tests + docs** — inject the dispatcher into `AdvanceWorkflowRun`; unit tests
   (pure handlers + fakes for dispatcher/recorder) + one end-to-end integration test
   (real DB, seeded `ai_models` + pricing, real dispatcher over mocks) asserting the
   full loop: step succeeded · one priced `usage_records` row · checkpoint with
   `image_ref` · `WorkflowStepCompleted`/`WorkflowRunSucceeded` in the outbox; plus
   the failure-as-a-unit and idempotent-replay cases.

### 2.2 α7.6 explicitly does NOT build
- ❌ HTTP clients / API keys / external calls · ❌ OpenAI / Gemini / Fal / real
  providers (mocks only) · ❌ webhooks / polling / a completion service (α8.3) · ❌
  Celery / Redis / worker / scheduler / daemon (α8.1) · ❌ FFmpeg render / export
  (α8.4/α8.5) · ❌ **Media registration** of the generated image (ADR-0041 D12 is a
  separate seam — recommend defer; §4 Q7) · ❌ `credit_ledger` debit (still α7.x-later;
  `credits_consumed = 0`) · ❌ a new "PipelineRegistry" abstraction (reuse
  `WorkflowRegistry` — §4 Q6) · ❌ **any migration** (all tables/enums exist; `paused`
  is already in `workflow_status`).

---

## 3. Recommended decisions (to be confirmed in §4)

**D1 — Ownership: Runner → Dispatcher → Provider (never Runner → Provider).**
The runner depends only on `ProviderDispatcherPort` (an `app.application.interfaces`
type); it never imports a concrete provider or the registry. Matches the user's
verification #1 and the α7.3 precedent (runner depends on `PublisherPort`, not a
broker). `import-linter` already forbids `use_cases → infrastructure`.

**D2 — Handlers stay pure; the shell dispatches.** Handlers (pure, D3.11) emit
`StepCommand`s; they never call the dispatcher. The runner (impure shell) is the
only place a command becomes a provider call — so the functional core stays
unit-testable and provider-free.

**D3 — Per-step ordering (matches user verification #2):**
```
handler → SUCCEEDED (+commands)
      → for each command: inject request_id → dispatch → ProviderResponse
            → (terminal) record usage on the runner's UoW
            → (in_progress) → PAUSE branch (D6)
      → mark_step_succeeded(output incl. provider output)
      → append_checkpoint(state incl. provider output)   ← resume point
      → emit WorkflowStepCompleted
```
Usage is recorded **before** the checkpoint/outbox and **inside the same
transaction**, so a replay cannot double-charge (idempotent on `request_id`) and a
crash after dispatch but before commit rolls back cleanly.

**D4 — Failure is atomic (user verification #3).** Everything above is in the
runner's single transaction. If the recorder raises, or the provider returns
`FAILED`, or dispatch raises `ProviderValidationError`, the step does not succeed:
- `ProviderResponse.status == FAILED` → record a **terminal `FAILED` usage row**
  (α7.5 records failed calls) then fail the step; transient vs terminal is driven by
  `ProviderError.transient` (transient → `mark_step_retrying`; terminal → fail run).
- Recorder/repository raising → the exception propagates → the whole UoW rolls back
  (no media without accounting, no accounting without the step). No partial commit.

**D5 — `request_id` is runner-minted + deterministic (user verification, replay).**
The shell injects `request_id = f"{run_id}:{step_index}:{command_index}"` into a new
frozen `StepCommand` before dispatch. This keeps handlers pure (they need no
`run_id`), satisfies the dispatcher's required-`request_id` contract, and makes
usage idempotent across replays/retries (same coordinates → same id → recorder
returns the existing row). Recommended over extending `StepContext`.

**D6 — Async pause seam (user verification #4), completion deferred to α8.3.**
A command returning `IN_PROGRESS`: the shell appends a checkpoint carrying
`{provider_job_id, request_id, provider, pending_step_index}`, transitions the run
`running → paused` (new `mark_run_paused` on the repo; `paused` already in the enum),
emits a `WorkflowRunPaused` event, and returns (still `200`, `advanced=True`). **No
usage is recorded for `IN_PROGRESS`** (α7.5 Q6). `paused` becomes advanceable so the
α8.3 completion service can `resume()`; α7.6 does **not** implement the completion
(re-advancing a mock-video pause simply stays paused — that is correct until α8.3).

**D7 — Reuse `WorkflowRegistry` as the pipeline registry (user verification #5).**
It already is `PipelineRegistry` (key@version → ordered `StepDefinition`s). α7.6 adds
one `WorkflowDefinition` whose `generate-image` step emits a `generate_image`
command. No parallel abstraction — that would duplicate α7.2 and drift the design.

**D8 — `model_id` + `capability` come from the command, resolved by the shell.**
The pipeline's `input_snapshot` carries a `model_id` (UUID of a real `ai_models`
row); the pure handler copies it into `StepCommand.args["model"]`/`["model_id"]`.
The shell maps `command.kind → Capability` via a small closed table (mirroring the
dispatcher's) to build the `RecordUsageCommand`. tenant is derived from the run's
project (as the runner already loads the project).

**D9 — Recorder composes in-transaction; α7.5 standalone `record()` is untouched.**
Extract the account→price→insert body into a transaction-participating helper the
runner calls on **its** UoW (no `async with`, no `commit`), reusing pure
`account()`/`price()`. α7.5's `UsageRecorderService.record()` (opens+commits) stays
as-is for standalone callers (α8.3 completion). See §4 Q5 for the two concrete
shapes.

**D10 — No new infrastructure, zero migration.** Only new *code* (a pipeline
definition, the shell's command-execution branch, a repo `mark_run_paused`, a
recording helper, DI). All tables/enums/indexes already exist.

**D11 — W7.6.1: the runner never interprets provider payloads.** The runner knows
only `StepCommand`, `ProviderResponse`, and `ProviderStatus`. It **must never**
inspect `image_ref` / `video_ref` / prompt text / provider JSON / video metadata.
Consequently the checkpoint/output stores the `ProviderResponse` as an **opaque
envelope** — `{provider, request_id, status, output: dict(resp.output)}` passed
through verbatim (no per-key reads) — and usage is built by **forwarding**
`resp.usage` / `resp.status` to the recorder, never by reading response contents.
Payload meaning belongs to the dispatcher/provider adapter, so the runner stays
reusable across every provider. (This slightly revises the §5.2 sketch: the shell
appends an opaque `provider_outputs` list, it does not pull out `image_ref`.)

**D12 — W7.6.2: exactly one dispatcher invocation per `StepCommand`; no retries in
the dispatcher.** `dispatch()` is called **once** per command instance. Retry is the
runner's alone: a transient outcome re-runs the *pure step* (which re-emits the
command with the **same** deterministic `request_id`, D5) and dispatches the fresh
command once more — so the recorder dedupes the replay and no layer double-retries.
Runner owns execution, dispatcher owns translation, provider owns the API — no
responsibility leaks across the boundary.

---

## 4. Open questions for sign-off

**Q1 — The first pipeline's shape.** Recommend a single deterministic
`generate-image@1.0.0`:
`prepare-prompt` (pure echo → prompt string) → `generate-image` (pure; emits
`generate_image` command with the prompt + `model_id`) → succeed. Do you want (a)
this image-only pipeline, or (b) also a second pipeline exercising the **video
`IN_PROGRESS` pause** path in the same slice (recommended: include a minimal
`generate-video` pause pipeline so the pause seam is covered by an e2e test)?

**Q2 — Async scope.** Confirm D6: implement the **pause seam only** (durable `paused`
+ checkpointed `provider_job_id` + resume entrypoint), with the actual
poll/webhook-driven completion deferred to α8.3. (Alternative: also build a
mock-completion `resume()` now — I recommend against, as it front-runs α8.3.)

**Q3 — `request_id` minting.** Confirm D5 (runner injects
`f"{run_id}:{step_index}:{command_index}"`). Alternative: extend `StepContext` so
handlers mint it — not recommended (leaks orchestration identity into the pure core).

**Q4 — `model_id` sourcing for the mock pipeline.** Confirm D8: the run
`input_snapshot` carries a real `ai_models` UUID and the integration test seeds one
`ai_models` row + one `ai_model_pricing` row. (Usage cannot be written otherwise —
`model_id` is `NOT NULL`.) Acceptable that the e2e test seeds a `provider="mock"`
model?

**Q5 — Recorder-in-transaction shape.** Confirm D9. Two concrete options — pick one:
- **(a) Recording helper (recommended):** a small `record_usage_in_uow(uow, command,
  *, default_currency)` used by both the runner and (refactored) `record()`. One
  source of truth; the runner owns the commit.
- **(b) `record(command, *, uow=None)`:** overload `UsageRecorderService.record` to
  accept an ambient uow it won't commit. Fewer new symbols, but overloads the
  transaction contract of a just-shipped method.

**Q6 — Runner extension vs new use case.** Confirm we **extend `AdvanceWorkflowRun`**
to execute commands (it *is* the runner; commands are its unfinished half), rather
than forking a parallel `RunPipeline` use case. `AdvanceWorkflowRun` gains an
optional `dispatcher: ProviderDispatcherPort | None` (and the recording helper);
when `None` (α7.2 deterministic workflows with no commands) behaviour is unchanged.

**Q7 — Media registration.** Confirm the generated `image_ref` is **only
checkpointed/output** in α7.6 (no `Media` row). Registering generated output as Media
(ADR-0041 D12) is its own seam — recommend defer so α7.6 stays "prove orchestration",
not "prove media".

**Q8 — New events.** The pause branch needs a `WorkflowRunPaused` event (D6). Confirm
adding it (and only it) to the `_events.py` set. No `UsageRecorded` event (α7.5 Q8
stands — no consumer).

**Q9 — Failure mapping of provider errors.** Confirm D4's mapping: provider `FAILED`
/ terminal `ProviderError` → record failed usage + terminal step failure; transient
`ProviderError` (`.transient == True`) → retry within the step's `max_retries`
(re-dispatch is idempotent via the stable `request_id`). Missing pricing still never
blocks (α7.5 Q5).

---

## 5. Component / contract sketch (illustrative — not yet implemented)

### 5.1 New pipeline (in `registry.py` or a new `pipelines.py`)
```python
GENERATE_IMAGE = "generate-image"

def _prepare_prompt(ctx: StepContext) -> StepResult:
    prompt = f"{ctx.run_input.get('subject', 'a cat')}, cinematic"
    return StepResult.ok(output={"prompt": prompt}, checkpoint_state={"prompt": prompt})

def _generate_image(ctx: StepContext) -> StepResult:
    prompt = (ctx.prior_state or {}).get("prompt", "")
    # pure: emits a command; the runner injects request_id + dispatches
    return StepResult.ok(
        checkpoint_state={"prompt": prompt},
        commands=(StepCommand(kind="generate_image",
                              args={"prompt": prompt,
                                    "model": str(ctx.run_input["model_id"]),
                                    "model_id": str(ctx.run_input["model_id"])}),),
    )
```

### 5.2 Runner extension (inside `_run_single_step`, after `SUCCEEDED`)
```python
provider_outputs = []
for i, cmd in enumerate(result.commands):
    rid = f"{run.id}:{step.step_index}:{i}"
    resp = await self._dispatcher.dispatch(replace(cmd, args={**cmd.args, "request_id": rid}))
    if resp.status is ProviderStatus.IN_PROGRESS:
        return await self._pause(run, step, resp)                 # D6
    if resp.status is ProviderStatus.FAILED:
        await self._record_usage(run, cmd, resp)                  # terminal FAILED row
        return self._step_failure(step, resp), None               # D4
    await self._record_usage(run, cmd, resp)                      # terminal SUCCEEDED row (D3)
    provider_outputs.append(_response_view(resp))
# then mark_step_succeeded(output + provider_outputs) → checkpoint(+provider_outputs) → event
```

### 5.3 Recording helper (Q5 option a)
```python
async def record_usage_in_uow(uow: IUnitOfWork, command: RecordUsageCommand,
                              *, default_currency: str = "USD") -> UsageRecordView:
    """Account → price (via uow.model_pricing) → idempotent insert (uow.usage).
    Does NOT open a transaction and does NOT commit — the caller owns the UoW."""
```

### 5.4 DI
```python
def get_advance_workflow_run_use_case() -> AdvanceWorkflowRun:
    return AdvanceWorkflowRun(uow=get_unit_of_work(),
                             dispatcher=get_step_command_dispatcher())
```

---

## 6. Reviewer sign-off — **SIGNED OFF (2026-07-19)**

All §4 questions approved as recommended, with one refinement to Q1 and two added
invariants. This is the composition slice; no new infrastructure, zero migration.

| Q | Decision | Resolution |
|---|----------|------------|
| **Q1** | ⚠️ refined | **Two pipelines:** one **fully-executable** `generate-image@1.0.0` (prompt → mock image → usage → checkpoint → complete) **and** one **minimal** `generate-video@1.0.0` that exists **only to prove the pause seam** (mock video → `IN_PROGRESS` → pause; **nothing beyond pause** — no completion/polling/webhook). |
| **Q2** | ✅ accept | Pause seam only: persist `paused` status + `provider_job_id` + checkpoint, then stop. α8.3 owns resumption. |
| **Q3** | ✅ accept | **Invariant:** `request_id = run_id:step_index:command_index`. Runner-minted and deterministic; **providers never generate it**. |
| **Q4** | ✅ accept | Fail fast if `model_id` absent. Integration fixture seeds `ai_models` + `ai_model_pricing`. |
| **Q5** | ✅ accept (a) | Recording helper `record_usage_in_uow(...)`; α7.5's public `record()` stays untouched (no optional-UoW leak). |
| **Q6** | ✅ accept | **Extend `AdvanceWorkflowRun`** (execute step → dispatch commands → record usage → checkpoint → outbox → next step). Do **not** fork a second runner. |
| **Q7** | ✅ accept | No `Media` rows in α7.6 — checkpoint only. α8.4 owns generated-media registration. |
| **Q8** | ✅ accept | Add **exactly one** new event: `WorkflowRunPaused`. Nothing else. |
| **Q9** | ✅ accept | Three buckets only: **Transient → retry**, **Terminal → fail immediately**, **provider `FAILED` → record failed usage → fail workflow**. No new states. |

**Added invariants (reviewer):**
- **W7.6.1 — the runner never interprets provider payloads.** It knows only
  `StepCommand`, `ProviderResponse`, `ProviderStatus`; it must never inspect image
  URLs, prompt text, JSON payloads, or video metadata. The response is checkpointed
  as an **opaque envelope**; payload meaning belongs to the dispatcher/provider
  adapter. (See **D11**.)
- **W7.6.2 — exactly one dispatcher invocation per `StepCommand`.** No retries inside
  the dispatcher; retries are the runner's alone (a transient re-runs the pure step,
  re-emitting the command with the **same** deterministic `request_id`). (See **D12**.)

**On sign-off (this stamp):** create `phase3/alpha7.6-first-pipeline`, bump to
`0.4.20-phase3-alpha7.6-dev`, implement per §2/§3 (pipeline defs, runner
command-execution branch honoring W7.6.1/W7.6.2, `mark_run_paused`, `WorkflowRunPaused`,
`record_usage_in_uow`, DI), add unit + e2e integration tests (full image loop,
failure-as-a-unit, replay idempotency, video pause), update docs (CHANGELOG, pipeline
note, ADR-0041 change-log line), run the full CI gate + integration, pause for release
approval, then finalize `v0.4.20-phase3-alpha7.6`.
