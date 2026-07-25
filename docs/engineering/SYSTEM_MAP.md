# System Map — How One Prompt Becomes One Exported Video

**Status:** Engineering overview / navigation map. **Not a contract** — it does not
add invariants; it *routes* you to the ADRs and contracts that do. Written after
**α8.7** (Planner v2). Companion to
[`AI_RUNTIME_PLANES.md`](./AI_RUNTIME_PLANES.md), which explains *why* the planes
exist; this map shows *where a request goes*.

If you are new to the generation pipeline, read this first, then follow the links
into the governing documents for the stage you care about.

> **SYSTEM_MAP is a navigation document.** It does not define architectural rules or
> behavioural guarantees — those remain the responsibility of the **ADRs** (*why the
> architecture exists*) and the **engineering contracts** (*what each subsystem must
> do*). This map only shows *how everything connects*. Where this document and an
> ADR/contract ever disagree, the ADR/contract is authoritative.
>
> ```
> ADRs        → why the architecture exists
>   Contracts → behavioural guarantees per subsystem
>     SYSTEM_MAP → how everything connects
>       Code
> ```

---

## Architecture at a glance

*Read this in under a minute; the rest of the document is detail.*

Three planes, each with its own mutability model (full rationale:
[`AI_RUNTIME_PLANES.md`](./AI_RUNTIME_PLANES.md)):

```
  Knowledge plane   — what CAN exist       provider catalogue, device profiles
        │
        ▼
  Decision plane    — what SHOULD happen   planner · storyboard · resolver ·
        │                                  verification · repair · timeline
        ▼
  Execution plane   — what DID happen      generation · ffmpeg · execution
                                           runtime · asset store · publisher*

  * Publishing is a planned Execution-plane capability, not a separate plane.
```

…and the single request that flows through them, end to end:

```
  Prompt
    ↓  Planner
    ↓  Storyboard
    ↓  Resolver
    ↓  Generation
    ↓  Verification
    ↓  Repair
    ↓  Timeline
    ↓  FFmpeg
    ↓  Execution Runtime
    ↓  Asset Store
    ↓  Publisher   (planned)
```

Everything below expands these two pictures: the annotated flow (§1), the per-stage
table with code seams + governing docs (§2), and the deeper references (§6).

---

## 1. The pipeline at a glance

One `GenerateVideoRequest` flows top-to-bottom. The **plane** column shows which
mutability model owns each step (Knowledge = *what can exist*, Decision = *what
should happen*, Execution = *what did happen*).

```
                                                        plane
  Prompt  (GenerateVideoRequest + IdentityProfile)      input
    │
    ▼
  Planner            prompt → GenerationPlan (ShotIntent arc)   Decision
    │
    ▼
  Storyboard         plan → ShotPrompt[] (Prompt Builder)       Decision
    │
    ▼
  Resolver           capability → ordered candidates            Decision
    │                   ▲ reads Knowledge (catalogue) + runtime state
    ▼
 ┌───────────────── Generation Runtime (per shot) ─────────────┐
 │  Image Generator   adapter_id + prompt + seed → bytes        │  Execution
 │        │                                                     │
 │        ▼                                                     │
 │  Verification      observed features → pass / fail           │  Decision (pure)
 │        │                                                     │
 │        ▼                                                     │
 │  Repair            fail → retry w/ fresh seed | give up      │  Decision (pure)
 └──────────────────────────────┬──────────────────────────────┘
                                 ▼
  Timeline gate      frames → duplicate/order/duration check    Decision (pure)
    │
    ▼
  FFmpeg             frames → MP4  →  ffprobe verifies output    Execution
    │
    ▼
  Execution Runtime  status machine + ledger + outbox events    Execution
    │
    ▼
  Asset Store        frames + final video (execution-owned)     Execution
    │
    ▼
  Publisher          MP4 → YouTube / TikTok / Instagram         Execution  ⟢ planned
```

Every arrow is a **stable seam** (a port or a pure function). New capability plugs
into a seam; it does not reshape the flow (ADR-0042, ADR-0045).

---

## 2. Stage by stage

| Stage | Plane | What it does | Code seam | Governed by | Status |
|---|---|---|---|---|---|
| **Prompt** | input | The request + the project *world state* (characters, locations, style, seed). | `GenerateVideoRequest`, `IdentityProfile` | [ADR-0044](../decisions/ADR-0044-ai-runtime-generation-architecture.md) | ✅ |
| **Planner** | Decision | Decomposes the prompt into a deterministic cinematic arc of `ShotIntent`s (establishing → … → closing); assigns semantic shot ids + derived seeds. Never names a provider. | `plan_from_prompt` (`domain/generation/planner.py`), `shot_intent.py` | [CINEMATIC_STORYBOARD_CONTRACT](./CINEMATIC_STORYBOARD_CONTRACT.md) · [ADR-0045](../decisions/ADR-0045-ai-runtime-core-freeze.md) | ✅ α8.7 |
| **Storyboard** | Decision | Turns each `ShotIntent` into a concrete `ShotPrompt` via the Prompt Builder (the sole place intent becomes generator-facing wording, CS-8). | `build_storyboard`, `prompt_builder.py` | [CINEMATIC_STORYBOARD_CONTRACT](./CINEMATIC_STORYBOARD_CONTRACT.md) | ✅ α8.7 |
| **Resolver** | Decision | Given a *capability* + constraints, returns explainable, ordered adapter candidates. Pure; reads immutable Knowledge + runtime snapshots. | `ICapabilityResolver` / `ResolverCapabilityResolver`; `domain/resolver` | [RESOLVER_RUNTIME_CONTRACT](./RESOLVER_RUNTIME_CONTRACT.md) · [ADR-0045](../decisions/ADR-0045-ai-runtime-core-freeze.md) | ✅ α8.5e |
| **Generation Runtime** | Execution | Composes the pipeline: executes the top eligible adapter per shot, drives the model cache for local tiers. No provider branching, never scores. | `GenerateVideo` (`application/use_cases/generation/generate_video.py`); `IImageGenerator` | [ADR-0044 (MRC)](../decisions/ADR-0044-ai-runtime-generation-architecture.md) · [ADR-0046](../decisions/ADR-0046-execution-runtime-boundaries.md) | ✅ α8.6 |
| **Verification** | Decision (pure) | A policy over *observed features* (extracted by infra): resolution, aspect, blank/blur/watermark, cross-shot similarity. Decides pass/fail; never sees raw bytes. | `verify_image` (`domain/generation/verification.py`); `IImageFeatureExtractor` | [ADR-0045](../decisions/ADR-0045-ai-runtime-core-freeze.md) | ✅ v1 |
| **Repair** | Decision (pure) | On failure, retries the *single* shot with a fresh derived seed up to a cap, else gives up (fails the run). | `decide_repair` (`domain/generation/repair.py`) | [ADR-0045](../decisions/ADR-0045-ai-runtime-core-freeze.md) | ✅ v1 |
| **Timeline gate** | Decision (pure) | Pre-render check: missing / duplicate / out-of-order frames, duration & aspect. Cheaply rejects a broken timeline before ffmpeg. | `verify_timeline` (`domain/generation/timeline_verification.py`) | [ADR-0043](../decisions/ADR-0043-render-composition-boundary.md) | ✅ α8.6 |
| **FFmpeg** | Execution | Renders accepted frames into an MP4; ffprobe measures the output for verification. | `FfmpegSlideshowRenderer`, `FfprobeVideoProbe` (`infrastructure/render/`) | [ADR-0043](../decisions/ADR-0043-render-composition-boundary.md) | ✅ α8.6 |
| **Execution Runtime** | Execution | Persists the whole run incrementally: `generations` + `generation_shots`, the `status` state machine, the resolution ledger, and lifecycle events via the transactional outbox. | `SqlExecutionRuntimeStore`; migration `0012` | [EXECUTION_RUNTIME_CONTRACT](./EXECUTION_RUNTIME_CONTRACT.md) · [ADR-0046](../decisions/ADR-0046-execution-runtime-boundaries.md) | ✅ α8.6 |
| **Asset Store** | Execution | Registers execution-owned artefacts (`generation_assets`, with a `parent_asset_id` lineage) and stores bytes behind object storage. Promotion to `media_assets` is an explicit later use case. | `generation_assets`; `IObjectStorage` | [ADR-0046](../decisions/ADR-0046-execution-runtime-boundaries.md) · [ADR-0037](../decisions/ADR-0037-media-generation-outputs.md) | ✅ α8.6 |
| **Publisher** | Execution | Uploads the final MP4 to social platforms behind per-platform adapters. | *(future port)* | [AI_RUNTIME_PLANES roadmap](./AI_RUNTIME_PLANES.md) | ⟢ planned |

Legend: ✅ implemented · ⟢ planned.

---

## 3. The three planes in one breath

- **Knowledge** — *what can exist*: the provider catalogue (`capabilities/providers/
  routing/devices` YAML → `0010` tables), authored offline, seeded, read-only at
  request time. See [`PROVIDER_RUNTIME_DATA_MODEL.md`](./PROVIDER_RUNTIME_DATA_MODEL.md).
- **Decision** — *what should happen*: Planner, Storyboard, Resolver, Verification,
  Repair, Timeline. **Pure functions**; same inputs ⇒ identical output. Frozen by
  [ADR-0045](../decisions/ADR-0045-ai-runtime-core-freeze.md).
- **Execution** — *what did happen*: Generation Runtime, FFmpeg, Execution Runtime,
  Asset Store, Publisher. Stateful, side-effecting, records provenance. Frozen by
  [ADR-0046](../decisions/ADR-0046-execution-runtime-boundaries.md).

Full rationale: [`AI_RUNTIME_PLANES.md`](./AI_RUNTIME_PLANES.md).

The α8.7 result is the cleanest illustration of the separation: the storyboard went
from six near-identical scenes to a genuine establishing→closing arc, and Stage 13
turned green **without touching the generator, verifier, repair loop, renderer, or
persistence** — only the Planner (a Decision-plane change) improved.

---

## 4. The golden thread — provenance & events

Two things flow *alongside* the pipeline so any run is explainable and replayable
long after the catalogue changes:

- **Provenance** (`GenerationProvenance`, persisted on `generations`): the chosen
  adapter/provider/tier, the full ranked `candidate_list`, `catalogue_version`,
  `manifest_digest`, and a **version stamp for every component** — `planner`,
  `storyboard`, `prompt_builder`, `verifier`, `repair`, `renderer`, `resolver`,
  `score_schema` (see `domain/generation/versions.py`). A defect found later is
  attributable to an exact component revision.
- **Lifecycle events** (transactional outbox): `generation.started`,
  `generation.shot_generated` (×N), `generation.video_rendered`,
  `generation.export_completed` — the run is observable from the database alone.

---

## 5. How the pipeline maps to CI

Validation mirrors the plane boundaries (see [`../CI_QUALITY_GATE.md`](../CI_QUALITY_GATE.md)):

- **Decision plane** → fast pure unit tests (planner, storyboard, verification,
  repair, timeline, resolver scoring) + the byte-for-byte **Golden V2** regression.
- **Knowledge plane** → provider manifest validation + seed round-trip (Stage 0, Stage 11).
- **Execution plane** → **Stage 12** (runtime infrastructure, frozen) and **Stage 13**
  (the Generation Runtime end-to-end slice) against an ephemeral Postgres + real ffmpeg.

Governance principle: infrastructure stages stay stable; each **new vertical slice**
(Verification v2, Repair v2, Publishing, …) earns its **own** stage (14, 15, 16, …)
rather than expanding Stage 13 into a catch-all.

---

## 6. Where to go deeper (governing documents)

**Freezes (ADRs — enforce boundaries):**
- [ADR-0042](../decisions/ADR-0042-orchestration-platform-freeze.md) — orchestration platform is frozen.
- [ADR-0043](../decisions/ADR-0043-render-composition-boundary.md) — render composition is a pure `Timeline + assets → video` transform.
- [ADR-0044](../decisions/ADR-0044-ai-runtime-generation-architecture.md) — AI runtime & generation architecture (Plan→Generate→Verify→Repair; Minimum Runtime Contract).
- [ADR-0045](../decisions/ADR-0045-ai-runtime-core-freeze.md) — AI runtime core freeze (the three planes; Decision-plane invariants F1–F7).
- [ADR-0046](../decisions/ADR-0046-execution-runtime-boundaries.md) — Execution Runtime boundaries (X1–X8; ships with Increment 4).

**Contracts (engineering — implementation reference):**
- [CINEMATIC_STORYBOARD_CONTRACT](./CINEMATIC_STORYBOARD_CONTRACT.md) — Planner v2 (α8.7): ShotIntent, story arcs, CS-7/CS-8.
- [RESOLVER_RUNTIME_CONTRACT](./RESOLVER_RUNTIME_CONTRACT.md) — α8.5e resolver.
- [EXECUTION_RUNTIME_CONTRACT](./EXECUTION_RUNTIME_CONTRACT.md) — α8.6 Increment 4 persistence.
- [PROVIDER_RUNTIME_DATA_MODEL](./PROVIDER_RUNTIME_DATA_MODEL.md) — the Knowledge-plane catalogue.
- [AI_RUNTIME_PLANES](./AI_RUNTIME_PLANES.md) — why Knowledge/Decision/Execution are separate.

**Data:**
- [Database ERD](../database/ERD.md) — clusters incl. Execution Runtime & Provenance (Cluster 12).
- [Content Generation Pipeline blueprint](../architecture/CONTENT_GENERATION_PIPELINE.md) — the Phase-3 architectural blueprint this runtime realises.
