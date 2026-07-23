# Platform Status — Architectural Baseline

> **Purpose.** A single, concise snapshot of what is considered **frozen platform
> infrastructure** versus **unfrozen feature surface**, plus the capability
> lifecycles completed so far. This exists so a future contributor (or your future
> self) can reconstruct the architectural state *without* reading dozens of ADRs
> and changelog entries.
>
> **This document is descriptive, not normative.** The **authoritative** sources
> remain [`ADR-0042`](../decisions/ADR-0042-orchestration-platform-freeze.md) (the
> freeze), [`ADR-0041`](../decisions/ADR-0041-provider-runtime-contract.md) (the
> provider runtime contract), [`CONTENT_GENERATION_PIPELINE.md`](CONTENT_GENERATION_PIPELINE.md)
> (the pipeline blueprint), and [`backend/scripts/check_frozen_platform.py`](../../backend/scripts/check_frozen_platform.py)
> (the machine-readable frozen-path list). If this file and those disagree, **they
> win** — update this file.
>
> **Keep it current:** refresh the *Current baseline* line and the *Completed
> capability lifecycles* table at the end of each runtime slice.

---

## Current baseline

| | |
|---|---|
| **Application version** | `0.4.28-phase3-alpha8.4d` |
| **Latest runtime tag** | `v0.4.28-phase3-alpha8.4d` |
| **Phase** | Phase 3 — orchestration era (α7+) |
| **Orchestration core** | **Frozen** since `v0.4.23` (ADR-0042, 2026-07-22) |
| **Freeze overrides used to date** | **0** (α8.3b, α8.4a, α8.4b, α8.4c, α8.4d all shipped additively) |

The project has crossed from *building the orchestration engine* to *building
capabilities on top of a stable platform*. Every slice since the freeze has been
strictly additive — no frozen path changed, no `Freeze-Override:` trailer used.

---

## Frozen orchestration platform (ADR-0042 §D1)

These modules are the **stable platform API**. Changing any of them trips the
freeze guard and requires a `Freeze-Override: ADR-XXXX …` commit trailer backed by
a new ADR (see *Change policy* below). The list below mirrors `FROZEN_PATHS` in
`check_frozen_platform.py` — the mechanical source of truth.

**Runner + resume + completion (the async orchestration loop)**
- `application/use_cases/workflow/advance_workflow_run.py` — the deterministic runner
- `application/use_cases/workflow/resume_workflow_run.py` — atomic resume seam
- `application/use_cases/workflow/completion_engine.py` — the single completion entrypoint
- `application/use_cases/workflow/_events.py` — workflow event shapes

**Dispatch + provider contracts (ports & neutral DTOs)**
- `infrastructure/ai/dispatcher.py` — the `StepCommand` → capability dispatcher
- `infrastructure/ai/providers/ports.py` — capability protocols
- `infrastructure/ai/providers/registry.py` — the provider registry class
- `application/interfaces/providers.py` — neutral provider DTOs
- `application/interfaces/provider_dispatcher.py` — the runner-facing dispatcher port

**Usage recording (service + pricing + port)**
- `application/use_cases/usage/usage_recorder_service.py`
- `application/use_cases/usage/accounting.py`
- `application/interfaces/usage_recorder.py`

**Relay + distributed locks**
- `application/use_cases/relay/relay_service.py`
- `infrastructure/repositories/distributed_lock_manager.py`
- `application/interfaces/locks.py`

**Workflow registry + aggregate + status enums (lifecycle + checkpoint owner)**
- `domain/workflow/registry.py`
- `domain/workflow/workflow_run.py`
- `domain/workflow/workflow_run_status.py`
- `domain/workflow/workflow_step_status.py`

### Platform guarantees (ADR-0042 §D3, G1–G10)

Single dispatch per command · deterministic request IDs · exactly-once completion
under distributed locks · provider-agnostic orchestration · exactly-once usage ·
versioned checkpoint envelopes · resume never re-dispatches provider work ·
configuration-blind providers (credentials injected, never fetched) · runner owns
orchestration / providers own external communication · two public resume seams.

### Change policy (ADR-0042 §D2)

- **Allowed without an ADR:** bug fixes, security fixes, performance improvements,
  observability, documentation.
- **Requires a new ADR + freeze override:** public method signature changes, DTO
  changes, checkpoint-schema changes, workflow-lifecycle changes, retry-semantics
  changes, provider-protocol changes, usage-recording-semantics changes.

### Enforcement (ADR-0042 §D4)

- `backend/scripts/check_frozen_platform.py` — diffs against a base ref; fails on a
  frozen-path change lacking a `Freeze-Override:` trailer (or `ALLOW_FROZEN_CHANGES=1`
  for local iteration). Byte-identical local ↔ CI.
- A fast, DB-free `freeze-guard` CI job (separate from the ADR-0028 gate).
- `.github/CODEOWNERS` review requirement on frozen paths.

---

## Completed capability lifecycles

The runtime pipeline is complete through rendering. Each boundary is clean —
providers know nothing about rendering, rendering knows nothing about providers,
orchestration doesn't know FFmpeg exists, completion doesn't know storage exists,
storage doesn't know timelines exist:

```
Provider → Completion → Generated Media Ingestion → MediaAsset → Timeline → Render Engine → Output MediaAsset
```

| Lifecycle | Status | Slice(s) | Notes |
|---|---|---|---|
| Workflow foundations + deterministic runner | ✅ | α7.1 / α7.2 | `RenderJob` aggregate; checkpointed runner |
| Outbox relay + distributed locks | ✅ | α7.3 | `RelayService` + lock manager |
| Provider architecture (ports/registry/dispatcher) | ✅ | α7.4 | four capability ports; typed errors |
| Usage recorder | ✅ | α7.5 | exactly-once priced `usage_records` |
| First end-to-end pipeline (mock) | ✅ | α7.6 | dispatch + terminal usage + checkpoint |
| Real synchronous provider (OpenAI Images) | ✅ | α8.1 | configuration-blind adapter (W8.1.1) |
| Real async provider (Fal.ai Video, submit-only) | ✅ | α8.2 | pause + `provider_job_id` |
| Completion engine (poll-first) | ✅ | α8.3 | `CompletionEngine.complete()`; exactly-once resume |
| Webhook completion ingress | ✅ | α8.3b | thin second ingress → same `complete()` |
| Generated media ingestion | ✅ | α8.4a | download / store / register `MediaAsset` |
| Render engine | ✅ | α8.4b | Timeline → FFmpeg → output `MediaAsset` |
| Media enrichment (thumbnail + metadata) | ✅ | α8.4c | derived-media poll worker; pure function of the parent `MediaAsset` |
| Derived previews (preview clip / GIF / waveform) | ✅ | α8.4d | enricher pipeline; versioned marker + backfill; derived media terminal |

### Invariant catalog

Behavioural invariants adopted alongside the frozen guarantees. Each is enforced
by review + tests, not the mechanical guard:

- **W7.5.1** — the usage recorder is purely observational (writes only `usage_records`).
- **W7.6.1** — the runner never interprets provider payloads (opaque envelopes).
- **W7.6.2** — no dispatcher-side retry; retries stay the runner's.
- **W8.1.1** — adapters are configuration-blind (credentials injected, never fetched;
  public JWKS verification keys are permitted trust anchors, α8.3b clarification).
- **W8.1.2** — exactly one real capability per real adapter slice; others stay mock.
- **W8.1.3** — observational equivalence between real and mock provider responses.
- **W8.3.1–W8.3.4** — single idempotent completion entrypoint; exactly-once resume
  (lease + CAS); completion delegates and never re-dispatches; orchestration stays
  provider-agnostic.
- **W8.3b.1** — webhook payloads never directly mutate workflow state (signal, not source).
- **W8.4.1** — generated-media ingestion is strictly downstream of the frozen pipeline.
- **W8.4.2** — ingestion is observational (never mutates orchestration state).
- **W8.4b.1** — the render worker is a pure Timeline → Media transform; it neither
  reads nor mutates orchestration state, checkpoints, provider state, workflow
  status, or the completion lifecycle.
- **W8.4b.2** — the renderer consumes only `MediaAsset` identifiers + Timeline data;
  never provider outputs, URLs, checkpoints, request IDs, provider job IDs, or webhooks.
- **W8.4c.1** — media enrichment is observational and downstream; it may derive
  artifacts + augment the owning `MediaAsset`'s `source_metadata`, but never mutates
  orchestration state, checkpoints, provider state, workflow/render lifecycle,
  Timeline definitions, or renderer inputs.
- **W8.4c.2** — the enricher consumes only `MediaAsset` bytes + identifiers; never
  provider outputs, URLs, checkpoints, request IDs, provider job IDs, webhooks, or
  Timeline internals.
- **W8.4c.3** — derived media is reproducible from its parent `MediaAsset` alone;
  enrichment never depends on provider payloads, checkpoints, Timeline state, or
  render-job history — `MediaAsset → Thumbnail` is a pure function of the parent.
- **W8.4d.1** — derived media is terminal. A derived `MediaAsset` SHALL NOT participate
  as the source of further enrichment; enrichment operates exclusively on primary
  generated/rendered assets. Derived artifacts are observational outputs only (the
  derivation graph is a shallow tree, never a cycle).

---

## Unfrozen surface (safe to extend)

Everything **not** in the frozen list above is the intentional growth surface that
new slices plug into — additive by construction:

- **Concrete provider adapters** (`infrastructure/ai/providers/openai/…`,
  `…/fal/…`) — new models/providers behind the frozen ports.
- **Ingress + downstream use cases** — completion ingresses (poll/webhook),
  media ingestion, the render worker, and future consumers.
- **Ports + adapters for new concerns** — `IObjectStorage`, `IMediaDownloader`,
  `IRenderer`, `IWebhookVerifier`, and their infrastructure leaves.
- **Repositories** — additive, non-frozen methods (e.g. `get_ownership`,
  `find_paused_by_provider_job_id`, `get_by_storage_coords`, the render-job worker
  transitions), and new repositories entirely.
- **Routers, DI/container wiring, config, tests** — the composition layer.

New subscribers can attach to the existing event stream (`WorkflowRunSucceeded`,
`RenderJobSucceeded`, …) without the orchestration core ever knowing — the property
the freeze was designed to preserve.

---

## Remaining roadmap

| Slice | Scope |
|---|---|
| **α8.4e** | Render composition enhancements — audio mixing, transitions/effects, FFmpeg quality tuning (changes *what the render is*, not a transform of an existing asset) |
| **α8.5** | Export & publishing — `export_jobs`, storage providers, publishing/notifications, downstream integrations |

All remaining work is **downstream of the frozen orchestration platform** — new
capabilities composed on stable seams, not redesigns of the workflow engine.
