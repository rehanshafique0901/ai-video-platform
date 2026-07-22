# ADR-0042 — The Orchestration Platform Is Frozen; New Capability Plugs In, It Does Not Reshape the Core

**Status:** Accepted (a **governance** decision — it ships a short guard script,
one CI job, and a `CODEOWNERS` entry, but **no application code, no schema
migration, and no runtime behaviour change**). Flips to Accepted on merge of this
ADR. Every α8.3b→α8.5 slice cites it and must stay *additive* to the frozen
surface.

**The inflection point.** α7.1→α7.6 built the orchestration substrate; α8.1–α8.3
proved it drives **both synchronous** (OpenAI images, α8.1) **and asynchronous**
(Fal video submit → pause → poll → resolve → resume, α8.2/α8.3) **real
providers** without duplicating orchestration or exposing runner internals. With
`v0.4.23-phase3-alpha8.3` the async loop closed end-to-end:

```
Workflow → Runner → Dispatcher → Real Provider → IN_PROGRESS → Pause → Checkpoint
        → Poll → Resolve → Usage Recorder → ResumeWorkflowRun → AdvanceWorkflowRun
        → Workflow Complete → Outbox
```

That was the **last architectural milestone**. The remaining Phase 3 work —
α8.3b (webhook ingress), α8.4 (generated media + FFmpeg), α8.5 (export /
publishing) — is **integration work built on top of stable seams**, not
orchestration design. This ADR makes that boundary explicit and, crucially,
**mechanically enforced**, so a feature slice cannot quietly reshape the core to
take a shortcut. If a slice *appears* to need a core change, that is a signal to
surface — a genuine architectural gap that earns its own ADR — not something that
slips into a feature branch.

**Builds on:** **ADR-0040** (WorkflowRun + pure `StepCommand` runner),
**ADR-0041** (provider runtime contract — the seams this ADR now freezes),
**ADR-0031** (idempotency-keys FSM), **ADR-0032** (distributed-locks lease
CHECK), **ADR-0033** (usage-records `request_id` unique). **Refines:**
`docs/architecture/CONTENT_GENERATION_PIPELINE.md` §13 (slice sequencing).

**Wave:** Phase 3, governance slice after **α8.3**. Zero migration; no app
version bump (a governance decision does not change runtime behaviour, mirroring
ADR-0041's docs-only precedent).

---

## Context

Over ten slices (α7.1 → α8.3) the platform crossed a boundary:

- **α7.x** built the orchestration substrate — the `WorkflowRun` aggregate and
  pure deterministic runner, the outbox relay, the distributed lock manager, the
  provider skeleton (ports + registry + dispatcher + mocks), and the priced,
  idempotent usage recorder.
- **α8.1–α8.3** proved the substrate is provider-agnostic and correct against
  *real* systems: a synchronous image provider, a submit-only async video
  provider, and finally the single idempotent completion engine that resumes a
  paused run under a per-run lease — **without re-dispatching provider work and
  without reaching into runner internals** (ADR-0041 D5/D6/D8; W8.3.1–W8.3.4).

The core is now feature-complete. The failure mode from here is **erosion**: a
media or publishing slice finds it *slightly* easier to widen a public signature,
mutate the checkpoint envelope, add a second dispatch path, or special-case usage
recording — and the clean separation earned across α7.1–α8.3 decays one
convenient shortcut at a time. Convention alone ("please don't touch these
files") relies on memory. This ADR replaces memory with a **contract plus a
tripwire**.

---

## Decision

### D1 — The frozen public orchestration surface

The following modules constitute the **stable platform API**. They are *frozen*:
their public behaviour, method signatures, DTOs, and persisted schemas (checkpoint
envelopes, event shapes) are a contract downstream slices depend on.

| Platform component | Frozen path(s) |
| --- | --- |
| `AdvanceWorkflowRun` (runner) | `backend/app/application/use_cases/workflow/advance_workflow_run.py` |
| `ResumeWorkflowRun` | `backend/app/application/use_cases/workflow/resume_workflow_run.py` |
| `CompletionEngine` | `backend/app/application/use_cases/workflow/completion_engine.py` |
| Workflow lifecycle events | `backend/app/application/use_cases/workflow/_events.py` |
| `StepCommandDispatcher` | `backend/app/infrastructure/ai/dispatcher.py` |
| Provider capability ports (protocols) | `backend/app/infrastructure/ai/providers/ports.py` |
| Provider registry | `backend/app/infrastructure/ai/providers/registry.py` |
| Provider neutral DTOs / dispatcher port | `backend/app/application/interfaces/providers.py`, `backend/app/application/interfaces/provider_dispatcher.py` |
| Usage recorder (service + port) | `backend/app/application/use_cases/usage/usage_recorder_service.py`, `backend/app/application/use_cases/usage/accounting.py`, `backend/app/application/interfaces/usage_recorder.py` |
| Relay service | `backend/app/application/use_cases/relay/relay_service.py` |
| Distributed lock manager (impl + port) | `backend/app/infrastructure/repositories/distributed_lock_manager.py`, `backend/app/application/interfaces/locks.py` |
| Workflow registry + aggregate + status enums | `backend/app/domain/workflow/registry.py`, `backend/app/domain/workflow/workflow_run.py`, `backend/app/domain/workflow/workflow_run_status.py`, `backend/app/domain/workflow/workflow_step_status.py` |

**Explicitly *not* frozen** — these are the growth surfaces new capability plugs
into: concrete provider *adapters* (`providers/openai/`, `providers/fal/`,
`providers/mocks/`) — new providers and their `submit`/`resolve` bodies are
*expected* to grow behind the frozen `ports.py` protocol; new *ingress* use cases
(e.g. an α8.3b webhook handler that calls `CompletionEngine.complete()`); new
*downstream* use cases (media registration, FFmpeg, export); repositories and
API routers for those new capabilities; the DI container wiring that composes new
use cases; tests.

### D2 — Change policy

Changes to a frozen path fall into two classes.

**Allowed without a new ADR** (the change must not alter observable contract):

- bug fixes that restore intended behaviour,
- security fixes,
- performance improvements,
- observability (logging, metrics, tracing, richer events *additive* to existing shapes),
- documentation / comments / type-only refinements.

**Require an explicit ADR** (contract-affecting):

- public method signature changes,
- DTO changes (fields added/removed/retyped on neutral provider DTOs, events),
- checkpoint schema changes (the `_paused` handoff / terminal envelope, including `schema_version`),
- workflow lifecycle changes (states, transitions, ordering),
- retry semantics,
- provider protocol changes (`submit` / `resolve` shape or lifecycle),
- usage recording semantics (what is recorded, when, and the exactly-once key).

When in doubt, it needs an ADR. The guard (D4) forces the author to make that
call consciously rather than by omission.

### D3 — Platform guarantees (the invariants downstream work must preserve)

These are more valuable than a file list: they describe *why* the surface is
frozen. Any change — allowed or ADR-gated — must preserve every one of them.

- **G1 — Single dispatch per command.** Each `StepCommand` is dispatched exactly
  once; resume/completion **never** re-dispatches provider work (W8.3.3).
- **G2 — Deterministic request IDs.** A step's `request_id` is derived
  deterministically and is stable across pause → resume.
- **G3 — Exactly-once completion under distributed locks.** Concurrent ingresses
  (poll, webhook, manual) contend on the per-run `workflow_run:<id>` lease; the
  `paused → running` CAS is the backstop. Exactly one transition wins
  (W8.3.1/W8.3.2, ADR-0032).
- **G4 — Provider-agnostic orchestration.** The runner, dispatcher, and
  completion engine read only neutral DTOs (`ProviderResponse.status/usage/output`);
  all payload parsing lives in the adapter (W8.3.4).
- **G5 — Usage recorded exactly once.** Terminal usage is idempotent on
  `request_id` (ADR-0033); replays are no-ops.
- **G6 — Checkpoint envelopes are versioned.** The `_paused` handoff and terminal
  envelope carry `schema_version`; readers tolerate and gate on it.
- **G7 — Resume never re-dispatches.** `ResumeWorkflowRun` resolves, records
  terminal usage, marks the step, and *continues* the unchanged runner — it does
  not re-run the async command (restatement of G1 at the resume seam).
- **G8 — Providers are configuration-blind.** Credentials are injected into
  adapters at the composition root; adapters receive, never fetch (W8.1.1).
- **G9 — Runner owns orchestration; providers own external communication.** The
  boundary between "what happens next in the workflow" and "how we talk to an
  external system" does not move.
- **G10 — Two public resume seams, no private reach-through.** Every completion
  mechanism converges on `CompletionEngine.complete()` → `ResumeWorkflowRun`; no
  service depends on a private method of `AdvanceWorkflowRun`.

### D4 — Mechanical enforcement (lightweight, byte-identical local + CI)

Enforcement is deliberately small, matching the repo's single-entrypoint gate
philosophy:

1. **`backend/scripts/check_frozen_platform.py`** — a dependency-free guard that
   diffs the current change set against a base ref and **fails** if any frozen
   path (D1) is modified *without* a conscious override. The frozen list lives in
   that script as the single machine-readable source of truth (kept in sync with
   the D1 table).
2. **Override marker** — a frozen-path change is authorised by either:
   - a commit-message trailer `Freeze-Override: ADR-XXXX <reason>` in the range, or
   - the environment variable `ALLOW_FROZEN_CHANGES=1` (for local, pre-commit iteration).
   The marker's whole purpose is to make the change *conscious*; it should cite
   the ADR that authorised the contract change (D2).
3. **CI job `freeze-guard`** in `.github/workflows/ci.yml` runs the guard on every
   pull request (diffing the PR base) and every push to `main` (diffing the pushed
   range). It is a separate job from the ADR-0028 ten-stage quality gate — this is
   *governance*, not quality.
4. **`.github/CODEOWNERS`** requires the platform owner's review for the frozen
   paths — a second, GitHub-native layer so the change is *seen*, not only
   *flagged*.

Locally, before fast-forwarding a feature branch into `main`, run:

```
python backend/scripts/check_frozen_platform.py --base main
```

### D5 — What this ADR intentionally does **not** do

No app version bump, no migration, no change to any frozen module, no new
runtime behaviour. It does not freeze the *adapters* or any new-capability
surface (D1). It does not introduce branch-protection rules (that is a repo
setting, out of scope for a code ADR).

---

## Consequences

**Positive.** The orchestration core becomes a stable platform API with a written
contract and a tripwire. α8.3b/α8.4/α8.5 can be evaluated by a single question —
"does this stay additive to the frozen surface?" — and any pressure to modify the
core is surfaced as an explicit, ADR-worthy decision rather than an invisible
regression. `v0.4.23-phase3-alpha8.3` is hereby the point at which the
orchestration platform is **feature-complete**.

**Negative / cost.** A genuine future contract change now costs an ADR and an
override marker. This is intentional friction: the friction is the feature. There
is a small maintenance duty to keep the guard's frozen list in sync with the D1
table when the platform is *intentionally* extended by a future ADR.

**Neutral.** The guard is advisory-by-construction for the solo push-to-main flow
(the override is self-served), but it converts silence into an explicit,
logged decision — which is the entire point.

---

## Accepted risks (platform validation, pre-α8.4, 2026-07-22)

A grounded validation pass after `v0.4.24-phase3-alpha8.3b` confirmed the platform
is sound where it matters: freeze-guard coverage is complete (self-test green,
lock-step "every frozen path exists"), crash recovery self-heals (`poll_once()` →
`list_paused()` re-discovers every paused run; the webhook is best-effort, polling
is the backstop), the completion flow is correct, and every public seam has a DI
factory. Two **known limitations** are recorded here as *accepted risks* — neither
is a current defect, neither blocks α8.4, and both are tracked so they surface as
intentional decisions rather than drift:

- **AR-1 — the dispatcher lacks structured dispatch logging.** `dispatcher.py`
  (the single-dispatch seam, G1) emits no structlog events, unlike every other
  orchestration seam (completion engine / resume / runner / relay). This affects
  **observability, not correctness**. Closing it is permitted under D2 without a
  new ADR (observability is an allowed change). *Re-evaluate when operational
  telemetry requirements increase.*
- **AR-2 — the `_paused` checkpoint block is implicitly versioned.** The Fork 1A
  handoff (`provider` / `request_id` / `provider_job_id` / `pending_step_index` /
  `command_index` / `capability` / `model_id` / `tenant_id` / opaque `envelope`)
  carries no top-level `schema_version`. It has exactly **one producer**
  (`AdvanceWorkflowRun`) and is read defensively by its consumers, so this is a
  *future evolution* concern, not an active incompatibility. **Per D2, any
  incompatible change to this block is a checkpoint-schema change and requires a
  dedicated ADR** (introducing an explicit `schema_version`) *before*
  implementation — it must not be reshaped inside a feature slice. The α8.4
  pre-flight's **first** design question is therefore "does α8.4 need to change the
  `_paused`/checkpoint contract?" — if no, proceed additively; if yes, stop and
  write the ADR first.

---

## Change log

- **2026-07-22 — Accepted risks recorded.** Post-`v0.4.24-phase3-alpha8.3b`
  validation pass documented **AR-1** (dispatcher observability gap, D2-allowed to
  fix) and **AR-2** (`_paused` block implicitly versioned; incompatible evolution
  requires a dedicated ADR). No code/behaviour/schema change; carried forward as
  explicit α8.4 pre-flight design checkpoints.
- **2026-07-22 — Accepted.** Freeze established immediately after
  `v0.4.23-phase3-alpha8.3`. Ships `check_frozen_platform.py`, the `freeze-guard`
  CI job, and `CODEOWNERS`; defines the frozen surface (D1), change policy (D2),
  platform guarantees G1–G10 (D3), and enforcement (D4). No code/behaviour/schema
  change.
