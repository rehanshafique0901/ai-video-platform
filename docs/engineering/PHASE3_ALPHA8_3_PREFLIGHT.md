# Phase 3 · α8.3 — Completion Service (poll-first) — PRE-FLIGHT

- **Status:** SIGNED OFF (2026-07-21) — poll-first approved; Q3 refined to a lifecycle (`submit`/`resolve`) capability; webhook ingress → α8.3b
- **Slice:** α8.3 (the first slice to move orchestration state since α7.6)
- **Target version:** `0.4.23-phase3-alpha8.3`
- **Predecessors:** α7.6 pause seam (`v0.4.20`), α8.1 OpenAI Images (`v0.4.21`), α8.2 Fal.ai submit-only async video (`v0.4.22`)
- **Contract source:** ADR-0041 **D5** (one completion service, poll-first), **D6** (polling lifecycle), **D7** (webhook lifecycle), **D8** (`workflow_run:<id>` lease)

---

## 0. One-paragraph thesis

α8.2 proved a real async provider can drive the pause seam: `submit → IN_PROGRESS → provider_job_id → paused`. α8.3 closes the loop by adding the **single, idempotent completion service** ADR-0041 D5 mandates — the only writer that turns an in-flight provider job's terminal outcome into aggregate state. The decisive architectural fact, discovered in grounding, is that **the runner already supports resume**: `AdvanceWorkflowRun` re-advances a `running` run and *skips already-`succeeded` steps* (`advance_workflow_run.py:373–381`, docstring §3 "resume-safety, Q7"). So the completion service does **not** re-implement step execution and does **not** re-dispatch the async command. It resolves the job, records terminal usage under the checkpointed `request_id`, marks the paused step `succeeded`, flips `paused → running`, and hands continuation back to the *unchanged* runner. That keeps the moving part tiny and reuses everything α7.x built.

---

## 1. Grounding (what already exists — verified, with line refs)

### 1.1 The pause checkpoint carries everything resume needs
`advance_workflow_run.py:480–499` — on an `IN_PROGRESS` command the runner leaves the step `running`, records **no** usage, and appends a checkpoint whose `state["_paused"]` block is:

```python
"_paused": {
    "provider": cmd_result.pause.provider,          # e.g. "fal-video"
    "request_id": cmd_result.pause.request_id,       # deterministic: run_id:step_index:command_index
    "provider_job_id": cmd_result.pause.provider_job_id,  # Fal request_id
    "pending_step_index": step.step_index,
}
```
alongside `state["provider_outputs"]` — the **opaque, versioned envelope** α8.2 checkpointed (`schema_version: 1`, `status_url`, `response_url`). The completion service reads its resume coordinates from here; it never re-derives them.

### 1.2 The runner resumes by skipping done steps
`advance_workflow_run.py:19–34` (algorithm §2/§3) and `:373–381`: only `queued` (start) or `running` (resume) may advance; **`paused`/terminal → 409**. Pending steps run in order; `is_done` steps are skipped but still thread their checkpoint `state` forward as `prior_state`. ⇒ If α8.3 marks the paused step `succeeded` and flips the run to `running`, a re-invocation of `AdvanceWorkflowRun` continues cleanly from the next step (or straight to `running → succeeded`), with **zero runner changes**.

### 1.3 Repository transitions available today (`repositories.py:1646–1726`)
- `mark_run_running` — CAS **`queued → running`** only (`:1649`). ⚠️ **No `paused → running` transition exists** — α8.3 must add one.
- `mark_run_succeeded` / `mark_run_failed` — CAS from `running` (`:1654/:1661`).
- `mark_run_paused` — CAS `running → paused`, `finished_at` unset, docstring explicitly says *"the α8.3 completion service resumes the run under the checkpointed `provider_job_id`"* (`:1668–1675`).
- `mark_step_succeeded` — CAS **`running → succeeded`** (`:1699`). The paused step is still `running` ⇒ this transition works **as-is**, no new step CAS needed.
- `latest_checkpoint(run_id, step_index)` (`:1636`), `list_steps` (`:1631`), `append_checkpoint` (`:1722`), `list_by_project(status=…)` (`:1619`).

### 1.4 Distributed lock manager is ready (`locks.py`)
`IDistributedLockManager.acquire/renew/release/reclaim_expired` with owner-fencing + steal-after-expiry (`locks.py:52–100`). ADR-0041 D8 fixes the canonical key **`workflow_run:<id>`** (`ADR-0041:226`). This is the exactly-once seam.

### 1.5 Terminal usage recording is ready (`usage_recorder_service.record_usage_in_uow`)
The runner already calls `record_usage_in_uow(...)` for terminal (`SUCCEEDED`/`FAILED`) responses inside its own UoW (`advance_workflow_run.py:57–60`, `:558–566`); `IN_PROGRESS` is deliberately **not** recorded. Usage is **idempotent on `request_id`** (recorder dedupes replays). ⇒ The completion service records the deferred terminal row under the **same** `request_id` the pause checkpointed → naturally idempotent.

### 1.6 Events (`_events.py`)
`WorkflowRunPaused` is emitted with `step_index` + `provider_job_id` (`:110–131`). Terminal `WorkflowRunSucceeded`/`WorkflowRunFailed` emitters exist (`:134–145`). There is **no** resume event yet.

### 1.7 Provider surface has **no** completion/resolve method
`providers.py`: `ProviderStatus = {SUCCEEDED, IN_PROGRESS, FAILED}` (`:40–51`); `VideoProvider` exposes only `generate_video` + `health`. The Fal adapter (`fal/video.py`) is submit-only by construction (**W8.2.2/W8.2.3**). ⇒ Resolving a job (poll) or interpreting a callback (webhook) requires a **new provider method** — the central provider-side decision below.

### 1.8 ADR-0041 completion contract (already blueprinted — this slice implements it)
- **D5** (`:180–190`): one completion service, the sole writer; poll **and** webhook call the *same* idempotent `complete(provider_job_id, outcome)`; idempotent on the job id.
- **D6** (`:192–198`): polling worker queries in-progress jobs, holds the per-job lock, capped backoff; **cadence/cap from config, not constants**. *Built first.*
- **D7** (`:200–208`): webhook `POST /webhooks/providers/{name}`, idempotent on `idempotency_keys(resource_type='webhook')`, verified for authenticity, funnels into the *same* service. Baseline tables `idempotency_keys` / `webhook_deliveries` already exist (`:19`).
- **D11** (`:266–274`): "runner-before-worker" — the worker *body* is defined behind an interface and runs **in-process/synchronously** (a `POST …/advance`-style call or a test loop drives it). Broker/daemon deferred.

---

## 2. Proposed scope (what α8.3 adds — the smallest thing that closes the loop)

```
paused run ──► CompletionService.complete(run_id)         ← new use case (the only new "moving part")
                 │  acquire workflow_run:<id> lease         (D8, existing lock mgr)
                 │  load run + _paused checkpoint            (existing reads)
                 │  guard: already resumed/terminal? → no-op (idempotent, W8.3.1)
                 │  resolve job via provider.<resolve>()     ← new provider method (Q3)
                 │     ├─ still IN_PROGRESS → renew/release, leave paused (backoff)  (D6)
                 │     ├─ SUCCEEDED → record terminal usage (same request_id)        (existing recorder)
                 │     │             mark_step_succeeded + append final checkpoint   (existing CAS)
                 │     │             resume_run (paused → running)                   ← new CAS (Q5)
                 │     │             AdvanceWorkflowRun.continue(run)                 ← REUSED, unchanged
                 │     └─ FAILED → record terminal-failed usage, mark_step_failed,
                 │                 mark_run_failed, WorkflowRunFailed                 (existing)
                 └─ release lease
poll ingress ──► CompletionService.poll_once()   ← lists paused runs, calls complete() per run (D6)
```

**Ingress = poll only for α8.3 (recommended — see Q1).** The webhook receiver (public route + signature verification + `idempotency_keys` plumbing) is a *second ingress to the same `complete()`* and is proposed as **α8.3b** to keep this slice reviewable and honour D5/D6's explicit "poll-first, built first."

**Zero migration.** Every table needed (`workflow_runs`, `workflow_checkpoints`, `distributed_locks`, `usage_records`, `event_outbox`) already exists. The only new persistence surface is **repo methods on existing tables** (Q5).

---

## 3. Genuine design decisions (for sign-off)

> Recommendations are marked **[R]**. Anything you approve becomes an invariant in §5.

### Q1 — Ingress scope: poll-first now, webhook next? **[APPROVED — poll-first as α8.3; webhook receiver as α8.3b]**
D5/D6 sequence polling first and treat webhook as a *second ingress to the identical entrypoint*. Bundling the webhook receiver adds a public unauthenticated route, per-provider signature verification (Fal), and `idempotency_keys(resource_type='webhook')` plumbing — a materially larger, harder-to-review diff that adds **no** new orchestration behaviour beyond what polling already exercises. **Decision:** ship the completion engine + polling ingress (the full lifecycle: resolve → usage → resume → terminal events → exactly-once) as α8.3; α8.3b adds the webhook route as a *thin* second ingress that calls the **same** `complete()` — no divergent logic.

### Q2 — One idempotent entrypoint. **[R: approve]**
`complete(run_id)` (poll passes the run it found; a future webhook maps `provider_job_id → run` then calls the same method). Idempotency is enforced by (a) the per-run lease and (b) a **status guard**: if the run is no longer `paused` or the paused step is already `succeeded`, `complete()` is a **no-op** (a replayed poll/webhook can't double-resume). Usage dedupes on `request_id` as a second backstop.

### Q3 — Model async providers as a **lifecycle** (the central provider-side decision). **[APPROVED — lifecycle `submit`/`resolve`, not `get_video_result`]**
Rather than a provider-specific `get_video_result()`, the async capability protocol represents the natural job lifecycle every future async provider shares (Runway, Kling, Pika, Luma, …): `submit → job id → poll/webhook → result`. The `VideoProvider` (D1 async capability) protocol becomes:
```python
class VideoProvider(Protocol):
    metadata: ProviderMetadata
    async def submit(self, req: GenerateVideoRequest) -> GenerateVideoResponse: ...   # was generate_video — returns IN_PROGRESS + provider_job_id
    async def resolve(self, *, provider_job_id: str, envelope: Mapping[str, Any]) -> GenerateVideoResponse: ...  # NEW — terminal SUCCEEDED/FAILED, or IN_PROGRESS if still running
    async def health(self) -> ProviderHealth: ...
```
`submit` is the **renamed** α8.2 method (it is already submit-only by construction); `resolve` is the **new** completion half. Both return the **neutral** `GenerateVideoResponse` DTO (unchanged). Fal implements `resolve` by GETting the checkpointed `status_url`/`response_url` from the α8.2 envelope; `MockVideoProvider` resolves deterministically (tests need no network). Reuses α8.1/α8.2's HTTP-status → typed-`ProviderError` map. **Config-blind** (W8.1.1).

**Blast radius of the rename (contained, honest):** `VideoProvider` protocol (`ports.py`) + its two impls (`fal/video.py`, `mock_video.py`) + the **one** dispatcher call site (`dispatcher.py:100` `provider.generate_video` → `provider.submit`) + their tests. **Unchanged:** the `StepCommand` **kind** stays `"generate_video"` (a *workflow* verb, not a provider method — the dispatch **contract** and closed `kind` table are untouched), the neutral `GenerateVideoRequest`/`GenerateVideoResponse` DTOs, the pure step handlers, the `generate-video` pipeline, the runner, the recorder, and the `ProviderRegistry` class. Sync capabilities (`generate_image`/`generate_text`/`synthesize_voice`) keep their names — they are *submit-that-completes-inline*; only async capabilities adopt the two-verb lifecycle. `resolve()` is **dispatcher-independent** — the completion engine calls it directly with the checkpointed coordinates.

### Q4 — Resume by delegation, never re-dispatch. **[R: approve]**
On `SUCCEEDED`: record terminal usage (same `request_id`) → `mark_step_succeeded(step, output)` appending the terminal envelope to `provider_outputs` → `resume_run` (paused → running) → call **`AdvanceWorkflowRun`** to continue. The runner skips the now-`succeeded` step (§1.2) and drives to `succeeded` (or the next step). The completion service **never** re-runs the pure handler and **never** re-submits to the provider (W8.3.3).

### Q5 — New repo methods (no schema change). **[R: approve]**
- `resume_run(workflow_run_id) -> WorkflowRun | None` — CAS **`paused → running`** (returns `None` if not `paused`; the completion guard/idempotency hinges on this).
- `list_paused() -> list[WorkflowRun]` — global read for the poll ingress (existing `list_by_project` is per-project).
- *(α8.3b only)* `find_paused_by_provider_job_id(job_id)` for the webhook `job → run` map.

### Q6 — Exactly-once via the per-run lease. **[R: approve]**
Wrap `complete()` in `acquire(key=f"workflow_run:{run_id}", owner="completion:<uuid4>", lease=<config>)` (D8). Concurrent polls (or a poll racing a future webhook) contend on the lease; the loser gets `None` and backs off. Combined with the Q2 status guard, resume happens **at most once** per job.

### Q7 — Driver model: library-only, synchronous, no broker. **[R: approve]**
`CompletionService` is a library callable (`complete()` + `poll_once()`), driven synchronously by a test loop or a trigger — **no Celery/Redis/daemon** (consistent with runner-before-worker, D11, and every slice through α8.2). Poll cadence/cap/lease are **config** (`completion_poll_interval_seconds`, `completion_poll_max_attempts`, `completion_lock_lease_seconds`) — *represented*, not a scheduler (D6/D10).

### Q8 — Terminal outcome mapping. **[R: approve]**
Provider `SUCCEEDED` → step `succeeded` → run continues → `WorkflowRunSucceeded` (if last step). Provider `FAILED` → terminal-failed usage row → `mark_step_failed` → `mark_run_failed` → `WorkflowRunFailed`. Still `IN_PROGRESS` on a poll → release the lease, leave the run `paused`, no state change (backoff to the next tick).

### Q9 — Resume observability event. **[R: add `WorkflowRunResumed`]**
Emit a `WorkflowRunResumed` (paused → running, carrying `step_index`/`provider_job_id`) for lifecycle completeness; terminal events reuse the existing `WorkflowRunSucceeded`/`WorkflowRunFailed`. `event_version` starts `"1.0"`. *Alternative:* no resume event (terminal event only) — rejected; the lifecycle diagram treats Resume as a distinct transition.

### Q10 — No media rows / no `video_ref` yet. **[R: approve]**
D5 mentions a `media_assets` row on completion, but generated-media registration + `video_ref` is **D12 / α8.4**. α8.3 completion resolves to **aggregate state + terminal usage + events + final checkpoint only**. The terminal provider output stays an opaque checkpoint envelope; **no `media_assets` write, no `video_ref` materialization** in this slice.

---

## 4. Explicitly forbidden in α8.3 (scope fences)

Celery · Redis · any broker/daemon/scheduler · **webhook receiver + signature verification** (→ α8.3b per Q1) · media registration / `GeneratedMedia` / `video_ref` (→ α8.4) · FFmpeg · storage / download of provider output · export/publishing (→ α8.5) · new providers · provider selection / fallback / precedence / health-ordering · rate limiter · circuit breaker · **any change to the pure step handlers or the `StepCommand` dispatch contract** · **schema migrations**.

## 4b. Must NOT change (proves the abstraction held)
The pure runner step-execution loop (`_run_single_step`), the dispatcher's **closed `kind` table / dispatch contract** (only the VIDEO call-site's *method name* changes — `generate_video` → `submit`), the `ProviderRegistry` class, the neutral `GenerateVideoRequest`/`GenerateVideoResponse` DTOs, the `generate-video` **pipeline definition** and its pure handlers, the relay, the lock-manager implementation, and the usage recorder's public API. The completion engine **reuses** `AdvanceWorkflowRun` for continuation rather than modifying it. The α8.2 Fal *submit behaviour* is unchanged — only its method **name** moves under the lifecycle protocol.

---

## 5. New invariants (proposed)

- **W8.3.1 — Single idempotent completion entrypoint.** Poll (and, later, webhook) call one `complete()`; a replay for an already-resumed/terminal run is a **no-op** (status guard + `request_id` usage dedupe).
- **W8.3.2 — Exactly-once resume.** The per-run `workflow_run:<id>` lease + the `paused → running` CAS guarantee a job resumes its run **at most once**, even under concurrent ingresses.
- **W8.3.3 — Completion delegates, never re-dispatches.** The service completes the paused step and hands continuation to the **unchanged** `AdvanceWorkflowRun`; it never re-runs a handler or re-submits to a provider.
- **W8.3.4 — Orchestration stays provider-agnostic.** The service resolves via the neutral capability method and reads only `ProviderResponse.status`/`usage`/`output` (extends W7.6.1); the adapter owns all provider-payload parsing (extends W8.1.1 — config-blind resolve).

---

## 6. Provisional file map (subject to sign-off)

| Area | Change |
|---|---|
| `app/application/use_cases/completion/completion_service.py` | **NEW** — the completion **engine**: one public `complete(run_id)` (acquire lock → load paused run → resolve → terminal usage → checkpoint → resume → emit) + a `poll_once()` ingress that scans paused runs and calls `complete()`. Every ingress calls exactly one public method. |
| `app/application/use_cases/completion/_events.py` *(or extend workflow `_events.py`)* | `WorkflowRunResumed` emitter (Q9) |
| `app/application/interfaces/providers.py` + `app/infrastructure/ai/providers/ports.py` | `VideoProvider` async lifecycle: `generate_video` → **`submit`**, add **`resolve`** (Q3) |
| `app/infrastructure/ai/dispatcher.py` | one call-site rename `provider.generate_video(...)` → `provider.submit(...)` (`kind` table unchanged) |
| `app/application/interfaces/repositories.py` | `resume_run` + `list_paused` abstract methods (Q5) |
| `app/infrastructure/repositories/workflow_run_repository.py` | Implement the two CAS/read methods (no migration) |
| `app/infrastructure/ai/providers/fal/video.py` | Rename submit method → `submit`; implement `resolve` (GET status/response URL → neutral terminal response) |
| `app/infrastructure/ai/providers/mocks/mock_video.py` | Rename → `submit`; deterministic `resolve` for tests |
| `app/core/config.py` + `.env.example` | `completion_poll_interval_seconds` / `_max_attempts` / `completion_lock_lease_seconds` |
| `app/core/container.py` | Compose `CompletionService` (UoW + registry + lock manager + `AdvanceWorkflowRun`) |
| `backend/tests/unit/...` | idempotent `complete`, exactly-once under lock, resolve→terminal, no-re-dispatch, terminal usage dedupe, mock/real resolve equivalence |

> Expectation (the α7 abstraction test): the diff should be dominated by **one new use case + one new provider method + two repo methods + DI wiring**, with the runner, dispatcher, recorder, relay, and lock-manager implementations untouched.

---

## 6b. Implementation-grounding addendum (2026-07-21, discovered during impl grounding)

Grounding the exact code paths surfaced two forks that refine §1.2/§2's "runner untouched" framing. Both are minimal and preserve *one copy of step-execution*; noted here so the design-of-record matches the build.

- **Fork 1 — terminal usage needs `model_id`/`capability` from the handoff.** `record_usage_in_uow` requires `model_id` + `capability` (+ `tenant_id`/`project_id`), but the `_paused` block (`advance_workflow_run.py:489–494`) carries only `provider`/`request_id`/`provider_job_id`/`pending_step_index`. **Resolution (A):** additively extend the `_paused` handoff with `model_id`, `capability`, `command_index` (all in scope at the pause point) — a ~4-line, backward-compatible extension of the *handoff payload* (not step-execution control flow). `project_id` comes from `run`; `tenant_id` is resolved from the owning project at completion. *(Alt B — re-derive by re-running the pure handler — rejected: duplicates the runner's request-id/model-resolution.)*
- **Fork 2 — atomic resume+continue must go through a public seam, NOT runner internals (revised per sign-off).** `AdvanceWorkflowRun.execute()` treats `paused` as **not advanceable** (`:269–275`) and opens its **own** UoW (`:224`); "resume then call `execute()`" would be two transactions with a stuck-`running` crash window invisible to the paused-only poller. **Rejected resolution:** a private `_settle_and_continue_in_uow` the completion engine reaches into — this couples another service to runner internals. **Approved resolution:** a **new public use case `ResumeWorkflowRun`** owns the atomic transaction — `BEGIN → resume_run() → record terminal usage → mark step succeeded → delegate continuation to the runner → COMMIT`. The layering is `CompletionEngine → ResumeWorkflowRun → AdvanceWorkflowRun`. The runner exposes a **public** transaction-participating continuation entrypoint (`continue_paused_run_in_uow(...)`, run already `running`, drives remaining steps + settles on the caller's open UoW, no commit); `execute()` delegates to the same private core, so behaviour is identical and **exactly one copy of step-execution** remains. Every future completion mechanism (webhook, manual resume, admin replay) converges on the single public `ResumeWorkflowRun` seam — a stable orchestration API, not a private helper.

**Revised delta (still zero-migration):** **completion engine** (poll ingress + provider resolve) · **`ResumeWorkflowRun`** public use case · `VideoProvider.submit`/`resolve` lifecycle · Fal/mock `resolve` · `ProviderDispatcherPort.resolve_job` (additive) + one dispatcher call-site rename · `resume_run`/`list_paused` repos · `WorkflowRunResumed` event · **additive `_paused` handoff fields** · runner **public** `continue_paused_run_in_uow` (behaviour-preserving) · config + DI. Unchanged: dispatch `kind` contract, neutral DTOs, `generate-video` pipeline + pure handlers, `ProviderRegistry` class, relay, lock-manager impl, recorder public API, and the runner's step-execution semantics.

## 7. Sign-off resolution (2026-07-21)

- **Q1 — APPROVED:** poll-first. α8.3 ships the completion engine + polling ingress; **α8.3b** adds the webhook route as a thin second ingress to the same `complete()`.
- **Q3 — APPROVED (refined):** model the async capability as a **lifecycle** — `VideoProvider.submit()` (renamed from `generate_video`) + `resolve()` (new). No provider-specific `get_video_result`. Generalizes to all future async providers; keeps orchestration provider-agnostic.
- **Q9 — APPROVED:** add `WorkflowRunResumed`.
- **Q2/Q4/Q5/Q6/Q7/Q8/Q10 — APPROVED** as recommended (single idempotent entrypoint; delegate-never-re-dispatch; two no-migration repo methods `resume_run`/`list_paused`; per-run lease; library-only synchronous driver, config-represented cadence; terminal outcome mapping; no media/`video_ref` until α8.4).
- **Framing:** "Completion **engine**" — one public method (`complete`); every ingress (poll now, webhook in α8.3b) calls exactly that one method. Completion → resume is split across two public seams: `CompletionEngine` (resolve the provider job under the per-run lease) → `ResumeWorkflowRun` (atomic resume + usage + step-succeeded + delegate continuation) → `AdvanceWorkflowRun` (unchanged step-execution semantics via a public continuation entrypoint).
- **Fork 1A — APPROVED:** persist `model_id`, `capability`, `command_index` in the `_paused` handoff (keep `request_id`, `provider_job_id`, `schema_version`). No handler re-execution.
- **Fork 2 — APPROVED (revised):** new public `ResumeWorkflowRun` use case owns the atomic transaction; no service depends on runner privates.

Invariants **W8.3.1–W8.3.4** adopted. **Zero migration.**

---

*Signed off → branch `phase3/alpha8.3-completion-service`, bump `0.4.23-phase3-alpha8.3-dev`, implement — same pre-flight → sign-off → implementation → gate → release cadence used α7.1–α8.2.*
