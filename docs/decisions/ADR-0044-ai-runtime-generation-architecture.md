# ADR-0044 — α8.5x AI Runtime & Generation Architecture: Plan → Generate → Verify → Repair, Capability-First and Local-First

**Status:** **Accepted** (rulings signed off 2026-07-25 — forks X-A…X-G decided;
AR16–AR18 added; a **Minimum Runtime Contract (MRC)** carved out as the Phase-1
production scope). A **governance / design-lock** decision — **docs-only. No
application code, no schema migration, no runtime behaviour change, no app version
bump.** Mirrors the ADR-0041 / ADR-0042 / ADR-0043 docs-only precedent. Every
α8.5x slice (α8.5d onward) is evaluated against it, exactly as α8.4e/α8.5a were
evaluated against ADR-0043.

**Scope split (the load-bearing ruling).** The goal is a platform *a
non-technical user can actually use in 1–2 days*. So AR1–AR18 are the **long-term
architecture**, and the **Minimum Runtime Contract (MRC-1…MRC-8)** in **D7** is
the **only** thing α8.5d/α8.5e + the first runtime slice must implement now.
Phase 2 (D8) lands the remaining AR depth incrementally. The MRC is a *subset*,
not a compromise: every MRC piece is a thin, correct realisation of its AR that
Phase 2 deepens without redesign.

**A boundary + a contract, not a freeze.** ADR-0042 *froze* the orchestration
core; ADR-0043 drew the *render composition* boundary. This ADR does the same for
the **AI generation runtime**: it names *how AI generation works* — the pipeline
shape, the requirements every provider and slice must honour, and the local-first
execution model — so the many slices that follow (seed, resolver, planner,
verifier, repair, local execution, publishing) grow **additively** without
eroding the platform earned across α7.1–α8.5c. It ships **no guard/CI job** (it
governs *new* growth, like ADR-0043) and freezes nothing.

**The inflection point.** α7.1→α8.4 built and froze the orchestration substrate
(ADR-0042) and drew the render boundary (ADR-0043). α8.5a–b.3r delivered the
delivery/distribution stage. **α8.5c** inverted provider truth to *capability →
providers* as a validated design-time spec. The next step is not another leaf
adapter — it is the **runtime that plans, selects, verifies, and repairs**
generation across many capabilities and providers. That runtime touches *every
future provider*, so its contract is locked **before** α8.5d/α8.5e/α8.6 are
implemented, not discovered slice-by-slice.

**Builds on:** **ADR-0041** (provider runtime contract — capability protocols,
registry, dispatcher; the deferred D2 *selection precedence* is realised here),
**ADR-0042** (orchestration freeze — still binding; α8.5x is entirely additive to
it), **ADR-0043** (render composition boundary — generation/verification sit
*outside* it), **ADR-0040** (WorkflowRun + pure `StepCommand` runner — plans feed
it; resume reuses its pause/checkpoint machinery), **ADR-0039** (RenderJob),
**ADR-0038** (Timeline), **ADR-0037** (MediaAsset — the canonical media boundary
and the asset-reuse substrate), and the **α8.5c** Capability Catalogue & Provider
Registry (the vocabulary + manifest this runtime consumes via the DB).
**Refines:** `docs/architecture/CONTENT_GENERATION_PIPELINE.md` §13 (sequencing).

**Wave:** Phase 3, governance slice after **α8.5c**, before **α8.5d**.

---

## Context

Today the implemented pipeline is essentially linear and trusting:

```
Prompt → Generate Image → Generate Video → Done
```

The target is a **verify-driven, capability-composed** pipeline where *the AI
never trusts itself*:

```
User Prompt
     │
     ▼
  Planner AI ──────────────┐
     │                     │
 Scene Planner      Character Planner
     └─────────┬───────────┘
               ▼
         Asset Planner
               ▼
      Image Generation ─▶ Character Verification ─▶ (Repair loop)
               ▼
      Video Generation ─▶ Frame Verification ─▶ (Repair loop)
               ▼
   Music ▶ Voice ▶ Captions ▶ Render ▶ Export ▶ Publish
```

Two properties must hold and must not decay:

1. **Capability-first (ADR-0041 / α8.5c).** Orchestration asks for a *capability*
   (`image_generation`, `text_to_speech`, …); the resolver picks the provider.
   No business logic ever names a provider.
2. **Never trust, always verify.** Every generated artifact is checked against the
   plan + the locked identity before it is accepted; failures repair, bounded, or
   fail loud — they are never silently accepted.

This ADR captures those as a numbered requirement set (AR1–AR15) with a matching
enforceable invariant each (W8.5x.1–W8.5x.15), reconciles every requirement with
an **existing** platform construct so α8.5x is *additive*, and fixes the
local-first execution model and slice sequencing.

---

## Decision

### D1 — What α8.5x *is* (and what it is not)

α8.5x is the **AI generation runtime**: the set of **new, additive** bounded
contexts and workers that turn a user request into verified media by *composing
capabilities*. It is layered around — never inside — the frozen core:

| Layer | Role | Relationship to the frozen core |
| --- | --- | --- |
| **Planner** (new, *upstream*) | Prompt → structured, immutable `GenerationPlan` (duration, scenes, characters, mood, mode, target). | Produces the *input* the existing runner/Timeline consume. Does **not** modify the runner. |
| **Identity & Scene** (new domain) | `CharacterSheet` + `Scene` state — locked identity + inherited scene state. | New aggregates; feed generation as references. |
| **Resolver** (ADR-0041 D2, realised) | Capability + active strategy → provider/adapter, local-first. | The α7.4-deferred precedence layered into `ProviderRegistry.resolve()` *without changing callers*. |
| **Generators** (existing leaves) | Capability adapters (image/video/voice/…). | Unchanged contract; new adapters plug in. |
| **Verify / Repair** (new, *downstream*) | Check each artifact vs plan+identity; bounded repair loop. | Downstream workers/aggregates, kin to α8.4 enrichment. Never mutate orchestration. |
| **Execution env** (new) | Detect hardware; choose local backend + parameters. | New adapters behind a port; invisible to callers. |

α8.5x is **not** a rewrite. The planner is upstream of the frozen runner; verify/
repair are downstream of it; the resolver is the *already-designed* extension
point of the provider registry. **ADR-0042 Gate 1 = No frozen surface changed.**

### D2 — The generation pipeline is a composition of single-responsibility capabilities

Every stage in the DAG is a **capability** in the α8.5c catalogue (or a new,
additive one), invoked through the dispatcher and selected by the resolver. No
stage does two jobs (AR12). The DAG is *data* (a plan), not hardcoded control
flow — which is what makes long-video decomposition (AR13), resume (AR14), and
mode selection (AR11) fall out naturally.

### D3 — Architecture Requirements (AR1–AR18) and their invariants

Each AR is **normative**; each has a matching enforceable invariant **W8.5x.N**
and an **owning slice**. "Builds on" names the existing construct so the slice is
additive.

| AR | Requirement | Builds on / realised as | Invariant **W8.5x.N** | Owner |
| --- | --- | --- | --- | --- |
| **AR1** | **Planning before generation.** Never generate ad-hoc; first produce a structured, validated, immutable `GenerationPlan`. | New `GenerationPlan` aggregate + `PlanGeneration` use case (planner = an `llm`/planning capability). Plan is an immutable input like a Timeline (ADR-0038) / workflow. | **W8.5x.1** — no generation step executes without a persisted, schema-valid plan it derives from. | α8.5x-plan |
| **AR2** | **Character identity lock.** Generate a `CharacterSheet` (seed, style, palette, proportions, features); scenes reference `CharacterID`, never re-derive identity from the prompt. | New `CharacterSheet` aggregate; identity carried as capability inputs already in the catalogue (`reference_image`, `seed`). | **W8.5x.2** — a scene references characters by id; identity assets are **reused**, never regenerated from a free prompt. | α8.5x-identity |
| **AR3** | **Scene state.** Each `Scene` carries positions / expressions / camera / lighting / props / background / time; scene N+1 inherits scene N's state. | New `Scene` entity within the plan; deterministic state propagation. | **W8.5x.3** — scene N+1's initial state is derived from scene N; no field changes without an explicit plan directive. | α8.5x-scene |
| **AR4** | **Visual verification.** After each generated artifact, verify it still matches the plan + locked identity. | New `IVerifier` capability (CLIP/SigLIP embedding similarity) → new catalogue capabilities (`identity_verification`, `visual_consistency_check`). Downstream verifier, kin to α8.4 enrichment. | **W8.5x.4** — every generated scene/frame is verified against its plan + identity **before acceptance**. | α8.5x-verify |
| **AR5** | **Automatic repair loop.** `generate → analyze → repair → … → accept`, bounded, configurable max retries. | Repair use case orchestrating verifier + generator; reuses the runner's retry/park semantics conceptually (does not alter them). | **W8.5x.5** — acceptance requires passing verification **or** exhausting bounded retries then failing/parking — never a silent accept. | α8.5x-repair |
| **AR6** | **Asset reuse.** Reuse existing backgrounds / characters / props / voices / music instead of regenerating. | Content-addressed reuse over the existing `MediaAsset` registry (ADR-0037) keyed by `(character/prompt/params)` hash — an additive reuse index. | **W8.5x.6** — before generating, resolve an equivalent existing asset; identical inputs **reuse**, never regenerate. | α8.5x-reuse |
| **AR7** | **Local-first preference.** Decision order: **local GPU → local CPU → free cloud → paid cloud → fail**; the *planner/resolver* chooses, never hardcoded. | Resolver strategy + provider `execution`/locality + `pricing` metadata (α8.5c `pricing`; new `execution: local\|cloud`). | **W8.5x.7** — the resolver honours the local→free→paid order under the active strategy; no provider is hardcoded. | α8.5e-resolver |
| **AR8** | **Provider scoring.** Select by capability + score under the active strategy — never `if provider == X`. | The α8.5c score vector + routing strategy; the ADR-0041 D2 deferred precedence. | **W8.5x.8** — selection is by capability + score/strategy; orchestration contains no provider-name branch. | α8.5e-resolver |
| **AR9** | **Intel-Mac optimization (today).** Detect hardware (CPU/GPU/RAM); pick smaller models, lower batch, sequential rendering, disk caching, frame batching. | New `IExecutionEnvironment` / `HardwareProfile` port + adapter. | **W8.5x.9** — local execution parameters are a **function of the detected hardware profile**, not hardcoded constants. | α8.5x-exec |
| **AR10** | **Apple-Silicon optimization (later).** On M-series, switch to Metal / CoreML / MPS / unified memory with **no architecture change**. | A second execution adapter behind the same port. | **W8.5x.10** — adding a hardware backend is a new adapter only; no pipeline/domain change. | α8.5x-exec |
| **AR11** | **Generation modes.** Planner picks `quick \| balanced \| quality \| ultra` — not always maximum. | A `mode` dimension on the plan → resolves to model + params + strategy. | **W8.5x.11** — output quality/cost is governed by the plan's `mode`, never fixed at max. | α8.5x-plan |
| **AR12** | **Step-by-step pipeline.** One module = one job (planner / optimizer / image / verify / repair / animate / music / voice / subtitle / render / export). | Mirrors the existing bounded-context discipline; each stage is one capability. | **W8.5x.12** — no module spans two capabilities. | all α8.5x |
| **AR13** | **Long-video strategy.** Generate + verify scene-by-scene, *then* render — so crashes are recoverable. | Plan decomposes into independently-verifiable scenes; render consumes accepted scenes (ADR-0039/0043). | **W8.5x.13** — long outputs are decomposed into per-scene generate+verify units before render. | α8.5x-scene |
| **AR14** | **Resume capability.** On failure, resume from the last **verified** scene, not scene 1. | Reuses the WorkflowRun pause → checkpoint → resume machinery (ADR-0040 / α8.3) at scene granularity. | **W8.5x.14** — a failed run resumes from the last accepted scene; accepted scenes are never regenerated. | α8.5x-scene |
| **AR15** | **Cost optimizer.** Planner estimates: local-feasible? → free? → paid; the user pays only when necessary. | Plan carries a feasibility/cost estimate driving the AR7 order. | **W8.5x.15** — the plan carries a cost/feasibility estimate; paid providers are used only when local + free cannot satisfy the plan under the active mode. | α8.5x-plan |
| **AR16** | **Project memory.** Every project has persistent memory — characters, locations, props, music, voices, generated assets, provider history, costs, failures, seeds, prompts, reference images, verification scores — so *"continue yesterday's project"* reloads everything, losing nothing. | New `Project` memory aggregate/read-model over existing `MediaAsset` (ADR-0037), `usage_records`, and the α8.5x plan/identity/scene state; additive tables. | **W8.5x.16** — a project's state is fully reconstructable from persisted memory; resuming a project loses no character/asset/seed/prompt/cost/score information. | α8.5x-memory *(MRC-4/-8: partial — Phase 1)* |
| **AR17** | **Workflow checkpoints (first-class for generation).** Make the platform's checkpoint machinery first-class per generation stage: Planning ✓ · Characters ✓ · Scene 1..N ✓ · Render · Publish — resume from the first incomplete stage after a crash. | Reuses the WorkflowRun pause → checkpoint → resume machinery (ADR-0040 / α8.3) at *generation-stage* granularity; additive stage-status projection. | **W8.5x.17** — every generation stage is individually checkpointed; a crash resumes from the first incomplete stage, never from the start. | α8.5x-scene *(MRC-7: Phase 1)* |
| **AR18** | **Provider transparency.** For every project, store the full provider ledger — planner / images / voice / music / renderer / exporter / publisher — for debugging, reproducibility, cost analysis, provider comparison, and support. | Derived from the α8.5e resolver decisions + `usage_records` (ADR-0033); an additive per-project provenance read-model. | **W8.5x.18** — every generated artifact records which adapter produced it and at what cost; the full provider/cost ledger is queryable per project. | α8.5x-memory *(MRC: capture from day one)* |

### D4 — Local-first execution & hardware abstraction (AR7 / AR9 / AR10)

Local execution is modelled the platform way — **as providers/adapters in the
capability registry**, not a special path:

- Local engines (Ollama LLM, local SDXL/FLUX-schnell, Real-ESRGAN, RIFE, Kokoro,
  CLIP/SigLIP) register as **adapters** with `pricing: free`, `authentication:
  none`, and a new additive `execution: local` flag (+ hardware hints). Cloud
  providers are `execution: cloud`.
- A new **`IExecutionEnvironment`** port exposes a detected `HardwareProfile`
  (arch, cores, GPU, VRAM/RAM). Adapters that can run locally receive
  environment-derived parameters (model size, batch, tiling, sequential vs
  parallel). The **caller never knows** — it asks for a capability.
- The resolver's local-first ordering (AR7) is a **routing strategy** over
  `execution` + `pricing` + score. Apple-Silicon support (AR10) is a *second
  execution adapter* — additive, no pipeline change.

### D5 — Recommended local model strategy (additive defaults, Intel-first)

Non-binding defaults for the initial local set (each ships as an additive adapter;
none is required by this ADR):

| Task | Preferred local model |
| --- | --- |
| LLM planner / prompt refinement | Qwen3 4B–8B GGUF **or** Gemma 3 4B via Ollama |
| Image generation | SDXL / FLUX-schnell (reduced settings on Intel) |
| Upscaling | Real-ESRGAN |
| Face restoration | CodeFormer / GFPGAN |
| Video interpolation | RIFE |
| Speech (TTS) | Kokoro (local) |
| Music | free cloud initially; local later if hardware allows |
| Verification | CLIP / SigLIP embedding similarity |

Heavy video diffusion stays **cloud (free-first)** on Intel; planning,
orchestration, verification, and identity tracking stay **local**.

### D6 — Freeze & boundary compliance (the two gates)

Every α8.5x slice's pre-flight answers both, exactly as α8.4e→α8.5b.3r did:

- **Gate 1 (ADR-0042):** *Does it change the frozen orchestration surface?* For
  α8.5x the answer is **No** — the planner is upstream (produces plans), verify/
  repair are downstream workers, the resolver is the *designed* extension point of
  `ProviderRegistry.resolve()` (D2 precedence "layered on without changing
  callers"), and execution is new adapters. If any slice *appears* to need a
  frozen-surface change, that earns its own ADR (ADR-0042 §D2) — it does not slip
  into an α8.5x branch.
- **Gate 2 (ADR-0043):** *Does it respect the render composition boundary?*
  Generation/verification/identity are **not** composition; render still consumes
  only Timeline + accepted `MediaAsset`s (RC1–RC6 intact).

### D7 — The Minimum Runtime Contract (MRC): Phase-1 production scope

The MRC is the **only** runtime α8.5d/α8.5e + the first generation slice must
ship for a usable MVP. Each MRC item is a **thin, correct** realisation of its
AR(s) — Phase 2 (D8) deepens it without redesign.

| MRC | Phase-1 deliverable | Realises (thin) | Deliberately deferred to Phase 2 |
| --- | --- | --- | --- |
| **MRC-1 — Planner** | Prompt → a **structured `Project`**: title, target platform, duration, scenes, characters, assets required, music, narration, subtitles. | AR1, AR11 (single default mode) | Multiple planners, quality modes, cost estimator (AR15) |
| **MRC-2 — Capability resolver** | `capability → resolver → best provider → execute`. This *is* α8.5d + α8.5e. | AR8 | Adaptive/operational-health routing |
| **MRC-3 — Local-first** | `need X → can local? → local, else free cloud, else paid`. | AR7 | GPU orchestration, hardware auto-tuning depth (AR9/AR10 stay basic) |
| **MRC-4 — Character memory** | `Project → CharacterSheet → seed + reference images + style + voice`; every generation references it. Eliminates most face drift. | AR2 (+ AR16 partial) | Full scene-state inheritance (AR3) |
| **MRC-5 — Scene-by-scene** | Never generate the whole video at once; generate per scene. | AR13 | — |
| **MRC-6 — Basic verification** | **Threshold-based** CLIP / face / prompt similarity → below threshold ⇒ regenerate. No AI critics. | AR4 (thin) | Multi-agent verification, AR5 bounded *repair-strategy* loop (Phase-1 is regenerate-only) |
| **MRC-7 — Resume** | If scene 7 fails, resume scene 7 — never restart. Mandatory. | AR14, AR17 | Cross-machine/distributed resume |
| **MRC-8 — Asset cache** | Reuse existing background / voice / music instead of regenerating. | AR6 (+ AR16 partial) | Content-addressed dedup across projects |

**Captured from day one (cheap now, invaluable later):** **AR18 provider
transparency** — every artifact records which adapter produced it and its cost.
This is a write, not a framework, so it ships with the MRC.

### D8 — Phase 2 (deferred, non-blocking)

Valuable but **not** shippable-MVP blockers, landed incrementally after the MRC:
advanced/bounded **repair strategies** (AR5 beyond regenerate), **multiple
planners**, **quality modes** (AR11 full), **multi-agent verification** (AR4
depth), **GPU orchestration** + Apple-Silicon tuning (AR9/AR10 depth), **adaptive
routing** (operational health), **distributed rendering**, and the full **project
memory** aggregate (AR16) beyond the MRC-4/-8 slice.

---

## Sequencing (refines CONTENT_GENERATION_PIPELINE §13)

α8.5x is a **program** delivered **MRC-first** (X-F), in dependency order. The
merge order that lands governance/tooling before runtime behaviour (X-B/X-F):

1. **CI hardening chore → main** (tooling; already committed).
2. **α8.5c Provider Registry → main** (tooling; already committed).
3. **ADR-0044 → main** (governance-only, **no version bump**).
4. **α8.5d — Seed.** YAML (α8.5c) → validator → **seeder + additive migration** →
   DB (the DB becomes the *populated* runtime source of truth). Carries the
   **execution/mode/hardware metadata** in the provider/runtime schema (X-G).
   *(migration: additive)*
5. **α8.5e — Resolver.** Capability + strategy → provider, **local-first /
   free-first** (AR7/AR8), from the DB. Realises ADR-0041 D2. *(= MRC-2/-3)*
6. **α8.5x-mrc — MRC implementation (the MVP).** The Phase-1 slice(s): MRC-1
   planner → MRC-4 character memory → MRC-5 scene-by-scene → generate → MRC-6
   threshold verification → MRC-7 resume → MRC-8 asset cache → render → export,
   capturing AR18 provenance throughout. This is the *usable-in-1–2-days* goal.
7. **α8.6 — Publishing.** `PublishJob` + `SocialAccount` + destination OAuth — a
   separate bounded context with its **own** (parallel) registry + shared α8.5c
   tooling. **After** the runtime (X-C): the runtime must reliably *produce*
   assets before we publish them.
8. **Phase 2 (D8) + UI/end-to-end.** Deepen the AR set incrementally; wire and
   polish the studio surface.

> **MVP realism (resolved via the MRC).** A full AR1–AR18 build is a multi-slice
> program, not a 1–2-day effort at this project's quality bar (each slice carries
> a pre-flight, two gates, tests, and the CI gate). The **MRC (D7)** is the
> answer: it is the *only* runtime α8.5d/α8.5e + the first generation slice must
> ship now; Phase 2 (D8) broadens coverage without redesign.

---

## Consequences

**Positive:** provider-agnostic, free-/local-first generation; character
consistency and verification make outputs trustworthy; scene decomposition +
resume make long videos reliable and crash-recoverable; asset reuse cuts cost;
everything remains additive to the frozen core and below/above the render
boundary; the capability graph opens the door to an autonomous planner.

**Negative / cost:** several new bounded contexts (plan, identity, scene, verify,
execution) — real design + test surface; a verify/repair loop adds latency and
compute; hardware abstraction adds a port + adapters to maintain.

**Neutral:** no runtime change or version bump lands with *this* ADR; each
requirement is realised by its own additive slice with its own pre-flight.

---

## Rulings (signed off 2026-07-25)

- **X-A — Artifact.** ✅ **Approved as an ADR** (governance artifact).
- **X-B — New bounded contexts.** ✅ **Approved** — introduce planner/runtime as
  separate additive bounded contexts (Planning, Character Identity, Scene,
  Verify/Repair, Execution, Project-Memory), phased per Sequencing.
- **X-C — Sequencing.** ✅ **Publishing (α8.6) stays after the runtime** — the
  runtime must reliably produce assets before publishing them.
- **X-D — Freeze posture.** ✅ **Keep it additive** — no changes to the ADR-0042
  frozen orchestration surface; **zero freeze overrides**.
- **X-E — Invariant numbering.** ✅ **Approved** — now **W8.5x.1–W8.5x.18**
  (AR16–AR18 added).
- **X-F — Scope / staging.** ✅ **Stage the work** — deliver the **MRC (D7)**
  first, then implement the remaining AR requirements incrementally (D8).
- **X-G — Execution/hardware metadata.** ✅ **Store in the provider/runtime schema
  introduced with α8.5d** (execution preferences + hardware metadata + generation
  modes), not scattered across slices.
- Also accepted: **AR16 Project Memory**, **AR17 Workflow Checkpoints**, **AR18
  Provider Transparency** (D3); AR1–AR18 as the α8.5x requirement set; the local
  model strategy (D5) as non-binding defaults.

## Sign-off checklist

- [x] **X-A** artifact = ADR-0044
- [x] **X-B** new bounded contexts accepted (phased)
- [x] **X-C** sequencing (publishing after the runtime) confirmed
- [x] **X-D** additive freeze posture (zero overrides) confirmed
- [x] **X-E** invariants W8.5x.1–W8.5x.18 accepted
- [x] **X-F** MRC-first staging accepted (MRC then Phase 2)
- [x] **X-G** execution/mode/hardware metadata → α8.5d provider/runtime schema
- [x] AR1–AR18 (D3) accepted as the α8.5x requirement set
- [x] Local model strategy (D5) accepted as non-binding defaults

---

## References

- **ADR-0041** provider runtime contract (D2 selection precedence — realised
  here) · **ADR-0042** orchestration freeze (still binding) · **ADR-0043** render
  composition boundary · **ADR-0040** WorkflowRun + pure `StepCommand` runner ·
  **ADR-0039** RenderJob · **ADR-0038** Timeline · **ADR-0037** MediaAsset.
- `docs/engineering/PHASE3_ALPHA8_5c_PREFLIGHT.md` — Capability Catalogue &
  Provider Registry (the vocabulary + manifest this runtime consumes via the DB).
- `docs/architecture/CONTENT_GENERATION_PIPELINE.md` §13 (sequencing).

## Change log

| Date | Change |
|---|---|
| 2026-07-25 | Initial authoring — **DRAFT** governance/design-lock of the **α8.5x AI Runtime & Generation Architecture** (docs-only; no code/migration/version bump). Locks the verify-driven, capability-first, local-first generation pipeline (D1/D2), the requirement set **AR1–AR15** with invariants **W8.5x.1–W8.5x.15** (D3), the local execution/hardware abstraction (D4) and recommended local model defaults (D5), and freeze/boundary compliance via the two gates (D6). Sequences the program α8.5d (seed) → α8.5e (resolver) → plan/identity/scene → verify/repair/reuse → exec → α8.6 publishing → UI, with a thin-vertical-slice MVP. Forks **X-A…X-G** raised for sign-off. Builds on ADR-0037/0038/0039/0040/0041/0042/0043 and α8.5c. |
| 2026-07-25 | **α8.5d addendum** (docs-only). The α8.5d pre-flight (`docs/engineering/PHASE3_ALPHA8_5d_PREFLIGHT.md`, SIGNED OFF) introduces the **concrete catalogue schema** realizing the runtime ARs: adapter **runtime** (execution + hardware) + **resource estimation** (AR9/AR10), **cost** hints + derived estimates (AR15), **feature matrix**, **output characteristics**, **capability dependencies**, curated **device profiles**, and **local providers as ordinary adapters** (AR7/AR8) — all seeded into the DB (W8.5c.2 upheld). Confirms **X-G** (execution/hardware/mode metadata lives in the α8.5d provider/runtime schema, not scattered) and the **scope guard** (α8.5d = catalogue metadata only; the resolver α8.5e owns scoring/routing/health/selection/fallback-execution; the planner owns workflow/dependency/requests). Since `main` was held unpushed, these were **amended into the α8.5c manifests/schema/validator in place** rather than a follow-up commit (offline gate stages 0–4 PASS). α8.5d carries its own **W8.5d.1–10** (W8.5d.10 added at this checkpoint: runtime operational state shall never be stored in catalogue metadata) plus an engineering blueprint `docs/engineering/PROVIDER_RUNTIME_DATA_MODEL.md` (ownership matrix + runtime flow) that is the implementation reference for migration 0010 / the seeder. |
| 2026-07-25 | **Accepted** — rulings signed off (X-A…X-G all approved; publishing stays after the runtime; additive/zero-override freeze posture; execution/hardware/mode metadata folded into the α8.5d provider/runtime schema). Added **AR16 Project Memory**, **AR17 Workflow Checkpoints**, **AR18 Provider Transparency** (invariants extended to **W8.5x.1–W8.5x.18**). Carved out the **Minimum Runtime Contract MRC-1…MRC-8** (D7) as the Phase-1 production scope — the *only* runtime α8.5d/α8.5e + the first generation slice must ship (MRC = thin, correct realisations of AR1/AR2/AR4/AR6/AR7/AR8/AR11/AR13/AR14/AR16/AR17/AR18; AR18 provenance captured from day one) — and **Phase 2** (D8) for the deferred depth (advanced repair, multiple planners, quality modes, multi-agent verification, GPU orchestration, adaptive routing, distributed rendering, full project memory). Refined the sequencing to the MRC-first merge order (chore → α8.5c → ADR-0044 → α8.5d → α8.5e → α8.5x-mrc → α8.6 → Phase 2/UI). |
