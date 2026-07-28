# ADR-0049 — AI Publish-Metadata Generation Is an Opt-In, Suggestion-Only Bridge From Publishing Into the LLM Plane

**Status:** Accepted (Phase 3, α9.1 — AI Caption & Hashtag Generation, 2026-07-28). Governance that
**precedes** implementation — like ADR-0044/0045/0047/0048 — because it fixes a **bounded-context
boundary and a determinism invariant** before any caption-generation code exists. Accepted after an
α9.1 review amendment that made the **ownership/dependency direction** and the **advisory-only
invariants** explicit (see §Ownership direction & dependency rule and §Invariants); the amendment
introduced **no new architectural question** — it tightened Option A in the conservative direction.
The implementation (a publishing-owned metadata-generator port + an AI-subsystem infrastructure
adapter, a `GeneratePublishMetadata` use case, and an opt-in suggest/preview API) lands in α9.1 and
cites this ADR. Grounded by [`PHASE3_ALPHA9_1_GROUNDING.md`](../engineering/PHASE3_ALPHA9_1_GROUNDING.md).

**Builds on:** **ADR-0041** (the provider-runtime contract — `Capability.LLM`,
`LLMProvider.generate_text`, neutral `GenerateTextRequest/Response`, the mock-first registry),
**ADR-0042** (the orchestration/platform freeze — new capability plugs in **additively**, never by
editing a frozen runtime), **ADR-0046** (execution-runtime boundaries — the media library is fenced
from the execution plane), **ADR-0047** (publishing-credential ownership — destination adapters are
**credential-blind leaves**), and the publishing runtime contract's **PUB-4** ("destinations /
metadata are not AI providers") and **PUB-9** ("metadata generation is deterministic in v1; LLM
metadata is a later slice — §5"). [`PUBLISHING_RUNTIME_CONTRACT.md`](../engineering/PUBLISHING_RUNTIME_CONTRACT.md)
Decision 4 / §14 explicitly pre-declared this as "its own later slice with its own contract."

---

## Context

α9.1 auto-generates a video's **publish metadata** — title, description, hashtags/tags — from the
LLM, as an **opt-in creator convenience**. Grounding established the shape almost entirely from
existing parts, and surfaced exactly one thing that is *not* settled by existing patterns.

What is already settled (verified in grounding, cited there to `file:line`):

- **The LLM capability, mock, and dispatch seam exist** (`Capability.LLM`,
  `LLMProvider.generate_text`, `GenerateTextRequest/Response`, `MockLLMProvider`, the registry). But
  **no production code emits `generate_text` today** — α9.1 is the platform's **first real LLM
  consumer**.
- **The value sink exists and is frozen.** Publish metadata is the immutable `ContentPackage`
  (`title/description/tags/visibility`), built **once** by the deterministic `build_content_package`
  and persisted as `publish_jobs.content_package` JSONB. `CreatePublishJob` already accepts
  **optional caller-supplied overrides** for exactly these fields. The publish *runtime*
  (`ProcessPublishJob`, the YouTube adapter) is frozen and must not change.
- **The insertion point that preserves the frozen architecture is publish-create.** Every earlier
  stage (Planning → Generation → Verification → Repair → Render → Export) is frozen, deterministic
  plane code across other bounded contexts; inserting an LLM there would mutate a frozen runtime and
  cross the `app.domain.generation` ↔ AI-plane and generation ↔ publishing boundaries. The **latest,
  frozen-safe seam** is an opt-in step at publish-create feeding the **existing** override inputs.
- **No migration is required** (grounding §9, proven): values persist via the existing
  `content_package` JSONB, cost via the existing `usage_records`; no new field/table/index/invariant.

**The one thing not settled by existing patterns.** Every prior cross-plane dependency in this
codebase is *away from* the AI plane or fenced *out of* it: destinations are credential-blind leaves
that must **not** import the AI plane (ADR-0047, `pyproject.toml:390`); the publishing **domain** is
an isolated bounded context that must **not** import `app.domain.generation`/`workflow`
(`pyproject.toml:377`); the execution runtime must **not** write the media library (ADR-0046 X8,
`pyproject.toml:411`). α9.1 requires the **first sanctioned dependency from the publishing
application plane *into* the AI LLM plane**, and it changes a **documented determinism invariant**
(PUB-9). Both are architectural boundary changes — per the α8/α9 threshold (an ADR only when a
cross-cutting invariant or bounded-context boundary changes), this earns an ADR.

### The decision point

**How does the deterministic Publishing plane consume the non-deterministic AI LLM plane for publish
metadata, while preserving PUB-4, the credential-blind / bounded-context isolation, and the PUB-9
determinism guarantee?**

- **Option A — Opt-in, suggestion-only bridge via a publishing-owned port + infra adapter.** A new
  `IPublishMetadataGenerator` port (application layer), an infrastructure adapter that reaches the
  LLM capability, a `GeneratePublishMetadata` use case, and an **opt-in suggest/preview API** whose
  output flows through the **unchanged** `build_content_package` overrides. A **deterministic
  template fallback** is mandatory.
- **Option B — Inline generation inside `CreatePublishJob`** behind an opt-in flag (server-side at
  job creation; no preview).
- **Option C — Generate captions in the generation/workflow plane** (emit a `generate_text`
  `StepCommand`) and carry metadata forward from generation into publish.

---

## Options

### Option A — Opt-in, suggestion-only bridge (publishing-owned port + infra adapter)

- **Boundary.** Define `IPublishMetadataGenerator` in `app/application/interfaces/` (owned by the
  publishing application plane). Implement the adapter in `app/infrastructure/` (e.g.
  `infrastructure/publishing/metadata/…` or `infrastructure/ai/metadata/…`), which is the **only**
  new module allowed to depend on both publishing inputs and the AI LLM capability. `app.domain.
  publishing` and the destination leaves import **neither** — pinned by a new import-linter contract
  ("the publish-metadata generator is the sole publishing→AI bridge; destinations and the publishing
  domain never import the AI plane"). This mirrors every existing port/adapter shape
  (`IImageGenerator`, `IDestinationPublisher`) and honours PUB-4: the generator is **not** a
  destination and **not** an AI provider *inside* publishing — it is a bridge adapter.
- **Flow.** `GeneratePublishMetadata` use case (application) depends on the port; a new **opt-in
  suggest/preview** endpoint returns proposed `title/description/tags` (+ a provenance VO). The
  caller may accept and pass them to the **existing** `POST /publish-jobs` overrides → the frozen
  `build_content_package` → `content_package` JSONB. **Nothing in the frozen publish runtime
  changes.**
- **Determinism (PUB-9 preserved).** Generation is opt-in and **suggestion-only**; the
  **deterministic template remains the mandatory fallback** — LLM disabled/unavailable/over-limit ⇒
  the deterministic result. The default publish path is unchanged and still deterministic. Under CI
  the LLM is the deterministic `MockLLMProvider`, so tests stay reproducible.
- **Limits.** The generator targets the **strictest destination limits** (YouTube: title ≤100, tags
  ≤500 total chars) so a suggestion never becomes a permanent `invalid_metadata` publish failure;
  the YouTube adapter remains the sole boundary enforcer (defence in depth — no adapter
  responsibility leaks into generation).
- **Persistence.** None new. Accepted values reuse `content_package` JSONB; LLM cost reuses
  `usage_records`. No migration.

### Option B — Inline generation inside `CreatePublishJob`

Add an opt-in flag to `PublishJobCreateRequest`; when set, `CreatePublishJob.execute` calls the LLM
and fills `title/description/tags` before `build_content_package`.

- Couples the **publish-create write path** to a network LLM call (latency + a new failure/timeout
  surface inside a use case whose determinism and idempotency are load-bearing).
- No **preview / re-roll**: the creator cannot review or regenerate before committing a job.
- Muddies the create-time **idempotency** key semantics (a replay of create must not re-invoke the
  LLM or double-bill).
- Still needs the same port/adapter to avoid `CreatePublishJob` importing the AI plane — so it is
  Option A **plus** an unnecessary coupling. No preview upside.

### Option C — Generate in the generation/workflow plane

Emit a `generate_text` `StepCommand` (reusing the existing dispatcher) inside a workflow, produce
captions during generation, and carry them forward to publish.

- Crosses the **generation ↔ publishing** boundary the platform deliberately isolates
  (`pyproject.toml:377`) and would drag publish-time metadata into the **frozen generation runtime**
  (ADR-0042/0046) — the exact runtime α9.1 must not touch.
- Captions describe the *finished, published* artifact and are a **publish-time** concern; producing
  them mid-generation is semantically premature (title/tags may change per destination/publish) and
  has **no clean owner-scoped publish seam** to land in.
- Over-engineered for an opt-in convenience; maximal blast radius.

---

## Evaluation

| Criterion | Option A — opt-in suggestion bridge | Option B — inline in CreatePublishJob | Option C — generation-plane |
|---|---|---|---|
| **Preserves frozen runtimes (ADR-0042/0046)** | **Yes** — publish/generation runtimes untouched; only additive modules. | Edits the `CreatePublishJob` write path. | Edits the frozen generation runtime. |
| **PUB-4 / credential-blind + isolation (ADR-0047)** | **Honoured** — a single, import-linter-pinned bridge; destinations & publishing domain never import AI. | Honoured only if it *also* uses the port (then it's A + coupling). | **Strained** — pulls AI into generation-for-publishing. |
| **PUB-9 determinism** | **Preserved** — default path deterministic; LLM opt-in with mandatory deterministic fallback. | Weakens create-path determinism. | Weakens generation determinism. |
| **Creator UX (preview / re-roll)** | **Yes** — suggest/preview before committing. | **No** — commit-or-nothing. | No natural publish-time preview. |
| **Idempotency / cost story** | Clean — stateless suggest; usage idempotent on `request_id`; create-path idempotency unchanged. | Muddied — replay-vs-LLM/double-bill in the create key. | Diffuse — usage recorded in generation, detached from publish. |
| **Migration** | **None** (grounding §9). | None. | None, but large code blast radius. |
| **Blast radius / maintenance** | Smallest — new port + adapter + use case + one opt-in endpoint. | Medium — touches a load-bearing use case. | Largest — cross-context, frozen-runtime edits. |
| **YouTube limit safety** | Generator targets strictest limits; adapter still enforces. | Same, but inside create path. | Limits unknown at generation time (destination not yet chosen). |

---

## Decision

**Adopt Option A.** AI publish-metadata generation is an **opt-in, suggestion-only bridge from the
publishing application plane into the LLM capability**, via a **publishing-owned port + an
infrastructure adapter**, with a **mandatory deterministic template fallback**.

This ADR **fixes** the following load-bearing invariants (everything else is deferred to the α9.1
pre-flight):

1. **Boundary & ownership.** A new `IPublishMetadataGenerator` port lives in the **application**
   layer; the **only** module permitted to depend on both publishing inputs and the AI LLM
   capability is its infrastructure adapter. `app.domain.publishing` and
   `app.infrastructure.publishing.destinations` **must never** import the AI plane. A new
   import-linter contract pins this the way ADR-0047 pinned credential-blindness. The generator is
   **not** a destination and **not** an AI provider inside publishing (PUB-4 preserved).
2. **Determinism (PUB-9 preserved for the default path).** The feature is **opt-in and
   suggestion-only**. The **deterministic `build_content_package` template is the mandatory
   fallback**: if the LLM is disabled, unavailable, errors, or returns over-limit content, the
   result is the deterministic template. The frozen publish runtime and the default (non-opt-in)
   publish path remain exactly as today.
3. **No frozen-runtime edit, no migration.** Generated values enter **only** through the existing
   `CreatePublishJob` overrides → `content_package` JSONB. No new persisted field/table/index/
   invariant (grounding §9). `ProcessPublishJob` and the destination adapters are untouched.
4. **Destination-limit safety.** The generator targets the strictest destination limits (YouTube
   title ≤100, tags ≤500 total chars); the destination adapter remains the sole boundary enforcer —
   no adapter responsibility leaks into generation.

Rationale: Option A is the **minimal additive** shape that honours every existing boundary
(ADR-0041/0042/0046/0047, PUB-4) and the determinism invariant (PUB-9) while giving creators a
preview/re-roll. Options B and C both edit a frozen runtime and/or cross an isolated bounded-context
boundary for no offsetting benefit; B is strictly Option A plus an unnecessary coupling, and C has
the largest blast radius and no clean publish-time seam. Grounding uncovered **no** contradiction
that would override this; the recommendation stands at Option A.

### Ownership direction & dependency rule (α9.1 amendment)

The abstraction is **owned by the Publishing application layer**; the AI subsystem **provides the
infrastructure adapter**. The dependency is strictly **one-way**: **the AI bounded context never
depends on Publishing.**

- **Port (owned by Publishing application).** `IPublishMetadataGenerator` and its **neutral** request/
  response DTOs are defined in the shared application-interface layer
  (`app/application/interfaces/…`, the same home as `IImageGenerator` / `IDestinationPublisher`).
  The DTOs are **capability-neutral** — plain `title/description/tags` + a small video/context bag —
  and deliberately **do not reference `ContentPackage`** or any `app.domain.publishing` type, so that
  implementing the port creates **no** dependency on the Publishing bounded context. The Publishing
  `GeneratePublishMetadata` use case depends **only** on this port (never on any AI module).
- **Adapter (provided by the AI subsystem).** The concrete adapter lives under the AI subsystem
  (`app/infrastructure/ai/…`) and implements the port using the existing `Capability.LLM` seam. It
  depends **only** on the AI capability + the neutral port/DTOs. It **must never** import
  `app.domain.publishing`, `app.application.use_cases.publishing`, or `app.infrastructure.publishing`.
- **One-way direction (mechanically enforced).**
  `Publishing use case ──▶ IPublishMetadataGenerator (neutral port) ◀── AI adapter`.
  Publishing points *at the port* (not at AI); AI points *at the port* (not at Publishing). Neither
  bounded context imports the other. A new import-linter contract pins "the AI plane never imports
  Publishing" (the mirror of the existing "Publishing domain is an isolated bounded context" and
  "destination adapters are credential-blind leaves" contracts). Exact module lists are specified in
  the α9.1 pre-flight.

### Invariants (load-bearing — α9.1 amendment)

These are fixed by this ADR and may not be weakened without a superseding ADR:

1. **Advisory-only.** The AI capability is **advisory**. The publishing pipeline must **always** have
   a deterministic path that functions with **no** AI involvement (the existing `build_content_package`
   template).
2. **Never a prerequisite.** AI generation must **never** become a prerequisite for creating a
   `PublishJob`. `POST /publish-jobs` continues to work end-to-end with zero AI calls.
3. **Graceful degradation.** **Any** AI failure — timeout, model error, unavailable/`NoProviderAvailable`
   provider, quota/rate-limit, validation, over-limit output — **degrades to the existing
   deterministic behaviour**; it is never surfaced as a publish-blocking error.
4. **User edits win.** Creator-supplied metadata **always takes precedence** over AI suggestions;
   AI output is only ever a default the creator may accept, edit, or discard.
5. **Only final metadata is persisted.** The **sole** persisted value is the final
   `title/description/tags` the creator selects, stored in the existing `publish_jobs.content_package`
   JSONB. **Prompt/model/template provenance is NOT persisted** unless a **future slice explicitly
   introduces provenance storage** (any provenance VO is response-side/ephemeral only).
6. **Destination-adapter blindness.** The destination adapter (YouTube and any future platform)
   continues to receive **only** the final `ContentPackage`; it remains **completely unaware** of
   whether metadata was AI-generated or hand-written. No AI concept crosses into a destination.
7. **No frozen-runtime edits.** No frozen runtime contract is modified — `ProcessPublishJob`, the
   destination adapters, and the generation/render/export runtimes are untouched; α9.1 is purely
   additive.

---

## What this ADR does *not* decide (deferred to the α9.1 pre-flight)

This ADR fixes **only** the publishing→LLM boundary and the determinism/fallback invariant. The
following remain open and are **not** prejudged:

- The concrete **prompt template(s)** and how title/description/tags are elicited and parsed from a
  single vs. multiple `generate_text` calls.
- The exact **API surface** (endpoint path/shape, request/response DTOs, opt-in mechanism) — expected
  to mirror the owner-scoped read/write patterns already used (`CurrentUserDep`, envelope).
- **Provenance representation** (grounding §5): the **persistence** question is now **decided** by
  Invariant 5 — provenance is **not persisted** in α9.1. What remains open is only the *shape* of the
  optional **response-side / ephemeral** provenance VO (e.g. `generator`, `model`,
  `prompt_template_version`, `generated_at`, `is_fallback`), which adds no table and no column.
- **Metering posture** (grounding §6): whether the suggest call records `usage_records` (needs a
  defined `request_id` + a resolved LLM `model_id`) or is an unmetered v1 preview.
- Whether the adapter calls the LLM via the existing `StepCommandDispatcher`/`ProviderDispatcherPort`
  seam or resolves the provider from the registry directly.
- CI stage additions, test strategy, and documentation wiring (`DECISIONS.md` cross-link, a companion
  `PUBLISHING_RUNTIME_CONTRACT.md` addendum, `SYSTEM_MAP.md`).

---

## Alternatives Considered (beyond A/B/C)

1. **Auto-apply generated metadata (no opt-in).** *Rejected.* Silently replaces deterministic
   metadata, breaks PUB-9 for the default path, and removes creator control. Opt-in + fallback is
   the whole point.
2. **A real LLM provider adapter in α9.1.** *Deferred, not decided here.* The mock stays the CI
   default (mirroring α8.6c's opt-in live posture); a real adapter is a separate, additive wiring
   decision and not required to ship the boundary + use case.
3. **A new `publish_metadata` table / history.** *Rejected.* No consumer needs it; values live in
   `content_package` JSONB and cost in `usage_records`. Adding a table contradicts grounding §9 and
   ADR-0036's "no versioning" posture.
4. **Putting the generator inside a destination adapter.** *Rejected.* Directly violates ADR-0047 /
   PUB-4 (destinations are credential-blind leaves that never import the AI plane).
5. **Extending `ContentPackage` with generation-provenance fields.** *Rejected.* `ContentPackage` is
   an immutable, platform-neutral *value* contract for "what to publish"; provenance is generator
   metadata, not publish payload — it belongs in the response VO (and at most JSONB properties), not
   the value contract.

---

## Consequences

- **Positive.** Creators get opt-in AI captions/hashtags with preview/re-roll; the deterministic
  guarantee (PUB-9) survives via a mandatory fallback; every existing boundary (PUB-4, credential-
  blindness, bounded-context isolation, frozen runtimes) is preserved and mechanically pinned by a
  new import-linter contract; **zero migration**; the change is purely additive (port + adapter +
  use case + one opt-in endpoint) and reuses the entire existing AI/publishing stack.
- **Cost.** A new port, adapter, use case, opt-in endpoint, and one import-linter contract; the
  adapter must enforce the strictest destination limits and the deterministic fallback; a companion
  engineering-contract addendum at implementation time.
- **Boundary added (a future slice may not cross without its own ADR).** *Publish-metadata
  generation is an opt-in, suggestion-only bridge: the sole permitted publishing→AI dependency is the
  `IPublishMetadataGenerator` infrastructure adapter; `app.domain.publishing` and the destination
  adapters never import the AI plane; generated metadata reaches publishing only through the existing
  `CreatePublishJob` overrides; and the deterministic template is always the mandatory fallback so
  the default publish path stays deterministic (PUB-9).*

---

## Change log

| Date | Change |
| --- | --- |
| 2026-07-28 | Proposed (governance, ahead of α9.1). Fixes the publishing→LLM boundary and the determinism/fallback invariant: AI publish-metadata generation is an opt-in, suggestion-only bridge via a publishing-owned `IPublishMetadataGenerator` port + an infrastructure adapter (the sole permitted publishing→AI dependency), feeding the existing `CreatePublishJob` overrides / `content_package` JSONB, with a mandatory deterministic template fallback and no migration. Compares Option A (bridge) vs B (inline in CreatePublishJob) vs C (generation-plane) across frozen-runtime preservation, PUB-4/isolation, PUB-9 determinism, creator UX, idempotency/cost, migration, blast radius, and destination-limit safety; recommends and (pending approval) adopts Option A. Implementation and full doc wiring land in α9.1 and cite this ADR. |
| 2026-07-28 | **Accepted.** Amended per α9.1 review with §Ownership direction & dependency rule (the port is owned by the **Publishing application layer** with **neutral, `ContentPackage`-free** DTOs; the AI subsystem provides the infrastructure adapter under `app/infrastructure/ai/…`; the dependency is strictly one-way — the AI bounded context never imports Publishing — mechanically pinned by a new import-linter contract whose module lists are set in the pre-flight) and §Invariants (advisory-only; never a `PublishJob` prerequisite; graceful degradation on any AI failure; user edits always win; only final creator-selected metadata is persisted and prompt/model provenance is not persisted absent a future slice; the destination adapter receives only the final `ContentPackage` and stays unaware of AI; no frozen-runtime edits). The amendment introduced **no new architectural question** — it tightened Option A conservatively — so the ADR is Accepted and α9.1 proceeds to pre-flight. |
