# ADR-0054 — Execution Adapter Dispatch: Executability Authority, Provenance Semantics & Fallback Ownership

**Status:** **Accepted** 2026-07-30 (Phase 3, α9.9 — Execution Adapter Dispatch). Governance that
**precedes** implementation (like ADR-0044/0045/0046/0051/0052/0053): it fixes *how a resolved
adapter becomes a running adapter, and what the system may claim afterwards*, before any dispatch
mechanism exists. Drafted at the post-α9.8 grounding stop after two rounds of read-only
investigation, the second of which **falsified three conclusions of the first** (see Context), then
carried through three independent architecture reviews, an adversarial acceptance review, and two
revision passes. The α9.9 pre-flight follows; **no implementation accompanies this ADR.**

**The decisions below are frozen on acceptance.** The pre-flight converts them into a slice; it does
not revisit them. If implementation discovers a genuine contradiction with this ADR or with another
accepted invariant, work stops and the contradiction is reported rather than resolved locally.

**Scope: the generation Execution plane only.** This ADR does not touch the frozen workflow
orchestration path (ADR-0042), does not introduce a new capability (speech, music, motion), and does
not address spend metering. It settles four questions that any future provider-backed capability
would otherwise have to re-answer.

**Builds on:**
- **ADR-0041** (provider runtime contract — D2's *precedence chain, then fallback chain*, which this
  ADR must reconcile with the resolver's ordered candidate list).
- **ADR-0044** (AI runtime architecture — D2 "invoked through the dispatcher and selected by the
  resolver"; MRC-2's singular "best provider → execute", whose wording this ADR closes).
- **ADR-0045** (**F1** the resolver never executes; **F2** Execution never scores; **F6/F7** adapters
  are additive and no provider-specific logic may enter the Decision plane).
- **ADR-0046** (**X1** Execution consumes the ordered candidate list and never chooses; **X5** every
  execution produces provenance + ledger entries that stay self-explaining as values, not FKs).
- **ADR-0042** (orchestration freeze — verified against `scripts/check_frozen_platform.py`: **none**
  of the files this slice needs is frozen).
- **ADR-0052** (**GEN-2**: one generation execution is one external spend opportunity — the reason
  D4's fallback question is about money, not tidiness).

---

## Context

The Decision plane computes an answer that the Execution plane cannot act on, and the system records
that answer as though it had.

`GenerateVideo` asks the resolver for candidates, records the full ranked list in provenance, takes
`resolution.top`, and then calls a port with exactly one memoised implementation that ignores the
choice entirely. The resolver's eligibility filtering, budget filtering, scoring, tier cascade, and
fallback ordering are computed, persisted, and discarded at the moment of execution.

This was a *documented interim state*, not an oversight: the port docstring says the use case passes
`adapter_id` "so a dispatcher implementation can route to the right provider"
(`application/interfaces/image_generator.py:6-7`), and the Stage 13 fixture states that ignoring it
is what "a single-provider adapter would" do. What has changed is that the interim state has now
produced a second-order defect — the system persists claims about execution that are not true — and
that every new provider-backed capability would inherit the same shape.

### The decisive facts

Eight facts, each independently re-derived during an adversarial second review. Three of them
falsify conclusions drawn in the first review, and two of them are the reason this document is not
simply a pre-flight.

**F1 — Under the default execution mode the resolver names an adapter that cannot run.** The default
is `AUTO` (`use_cases/generation/request.py:26`). `AUTO` cascades local-first —
`_CASCADE = (LOCAL, FREE_REMOTE, COMMERCIAL)` (`domain/generation/execution.py:39-40`) — with
`stop_at_first_available=True`, and `_apply_constraints` returns **only the first non-empty tier**
(`use_cases/generation/capability_resolver.py:77-82`). The sole `LOCAL` image adapter is
`comfyui.flux_schnell`: `pricing: free`, `execution: { local: true, cloud: false }`, therefore
`ExecutionTier.LOCAL` via `_tier_for`. Its `minimum_ram_gb: 16` does **not** exclude it, because the
hardware gate fires only when `request.device is not None` and `_to_request` never populates
`device`. It is `status: planned`, has no `import_path`, and **no ComfyUI implementation exists in
`app/`**. So `AUTO` yields a one-element candidate list naming a non-existent adapter.

**F2 — Execution ignores that answer and always reaches Pollinations.**
`PollinationsImageGenerator.generate()` accepts `adapter_id` and never branches on it
(`infrastructure/generation/pollinations_image_generator.py:33-72`); the container memoises a single
instance (`core/container.py:1480-1494`). Only three call sites touch `.candidates` at all — the
`top` property, the ledger writer, and provenance construction. **Nothing iterates the list**, and
there is no fallback to `candidates[1:]` anywhere.

**F3 — `implemented` is loaded into the domain and never read again.** The sole reference in all of
`app/` is the assignment at `infrastructure/repositories/catalogue_reader.py:80`. Within
`domain/resolver/`, the word appears only as the field declaration at `models.py:78`. Neither
`eligibility.py`, `service.py`, nor `strategy.py` consults it, and the rejection vocabulary contains
no `not_implemented`.

**F4 — `import_path` is not visible to the runtime.** The column exists on `provider_adapters`, but
`_ADAPTERS_SQL` does not select it (`catalogue_reader.py:41-44`), so the resolver snapshot carries no
pointer to executable code. There is **no runtime use of `importlib` anywhere in `app/`**; dynamic
class loading exists only in the CI validator.

**F5 — The catalogue's notion of "implemented" is written in the *other* plane's protocol.**
`image_generation` has `kind: image` (`providers/capabilities.yaml:58-59`), and the validator
requires an implemented adapter's class to expose `("health", "generate_image")`
(`scripts/validate_providers.py:52-57`). `PollinationsImageGenerator` exposes only `__init__` and
`generate`, because the generation plane executes through `IImageGenerator`. **Marking the real
adapter implemented would fail CI today.** The validator currently checks nothing at all, since it
skips every adapter whose status is not `implemented` — and **zero adapters in the entire manifest
are implemented.**

**F6 — Two fallback models exist, and under the default configuration they disagree.** The resolver
emits an ordered `candidates` tuple. Separately, `adapter_fallbacks` is loaded ordered by ordinal
into both `AdapterInfo.fallbacks` and `Candidate.fallbacks` (`catalogue_reader.py:45-47`). Nothing
reads either. Critically, `comfyui.flux_schnell` declares `fallback: [pollinations.image]`
(`providers/providers.yaml:195`). So under `AUTO` the candidate list has length one — walking it is
inert — while the *chain* points at precisely the adapter that actually serves requests today. The
two models are not redundant; only one of them reproduces current behaviour.

**F7 — The default path has never been exercised, and the one end-to-end test encodes the
ignore-the-resolver behaviour.** Stage 13 is the only test driving the real resolver against a
seeded catalogue. Its fixture pins `FREE_REMOTE_ONLY` "so selection is deterministic regardless"
(`tests/fixtures/golden/scenario.py`) and seeds a synthetic `golden_provider.image` with all-100
scores engineered to outscore the entire catalogue. That synthetic adapter is inserted with only
`(id, provider_id, capability_id, execution_mode, enabled)`, so it is **itself `implemented = false`
with no implementing code**. The test then asserts `adapter_used == golden_provider.image` for every
shot while an offline Pillow generator produced the bytes. `AUTO` against a seeded catalogue appears
in no integration test.

**F8 — Nothing this slice needs is frozen.** Verified against `scripts/check_frozen_platform.py`
rather than the ADR-0042 table: `generate_video.py`, `capability_resolver.py`, `domain/resolver/*`,
`IImageGenerator`, and `infrastructure/generation/*` are all absent from the checker.

### What the first investigation got wrong

Recorded because the corrections changed the scope, not merely the prose.

1. **"Faithful dispatch would break the working default path."** There is no working default path.
   Per F7 it is untested and would fail if run. The distinction removes an urgency framing and
   reframes the slice as closing an untested hole rather than repairing a regression.
2. **"Ignoring `adapter_id` is technical debt."** The repository holds a stated, tested position on
   the port shape. The debt is specifically the *executability* gap (F1/F3), not the single-
   implementation port.
3. **"The slice can be behaviour-preserving on the default path."** It cannot preserve all
   *currently encoded* behaviours: per F7, one integration test and the existing provenance
   semantics deliberately depend on today's execution model. The honest question is not *can
   behaviour stay identical* but *which behaviour are we intentionally changing* — answered in
   Consequences.

### Why this is an ADR and not a pre-flight

A pre-flight implements decisions already made. ADR-0046 X1 and ADR-0045 F2 decide the *behavioural
boundary* — Execution consumes and never scores — and that boundary is not in question here. But
four questions beneath it are genuinely open, two have no objectively correct answer, and one is a
correctness matter rather than a design preference:

- **D1** determines whether deployed code may change what the Decision plane returns, which
  interacts directly with X5's requirement that a ledger entry explain itself.
- **D2** is a data-integrity decision. The system currently persists claims about execution that are
  false (F2 + F7), and deciding what those columns *mean* is prior to deciding how to populate them.
- **D3/D4** must reconcile two competing models (F6) and close a live contradiction between two
  signed-off contracts.

Registry class shape, error taxonomy, container wiring, logging fields, and test fixtures are
**explicitly not** ADR material and belong in the α9.9 pre-flight.

### Decision points

| # | Question |
|---|---|
| **D1** | What authority decides whether an adapter may execute — code, the catalogue, or both? |
| **D2** | Does a provenance record describe the decision the system made, or the execution that occurred? |
| **D3** | Which fallback representation is canonical — the ordered candidate list or the adapter chain? |
| **D4** | Which plane owns fallback *execution*, and is walking required, permitted, or forbidden? |

These questions turn on distinctions that ordinary usage blurs — *selected* against *produced*,
*executable* against *executed*. D2 defines a **normative vocabulary** that governs the whole
document; readers should treat those definitions as binding from here on.

---

## D1 — Executability authority

### Options

| | Option | Shape |
|---|---|---|
| **A** | Catalogue authoritative | Filter on `implemented`; add `import_path` to the snapshot; the manifest is the single source of truth |
| **B** | Code authoritative | The set of adapters the deployment can construct defines executability; the catalogue stays descriptive |
| **C** | Hybrid — code gates, catalogue is reconciled *(recommended)* | B at runtime, plus a CI assertion that the two agree |

### Evaluation

| Criterion | A — catalogue | B — code | C — hybrid |
|---|---|---|---|
| **Works against today's manifest** | **No** — zero adapters are `implemented` (F5), so this filters everything and produces a total outage until the manifest is corrected | **Yes** | **Yes** |
| **Blocked by the protocol mismatch (F5)** | **Yes** — requires reconciling `generate` against `generate_image`/`health` first, which is a much larger change | No | No — deferred to the reconciliation check's own slice |
| **Can a DB row change which code runs?** | **Yes** — an operational hazard | No | No |
| **Historical explainability (X5)** | Native — executability is manifest data | **Weakest** — nothing in the record explains why a deployed-but-absent adapter lost | Preserved — the exclusion is recorded per candidate as an `ineligible_reason` |
| **Decision-plane purity** — domain imports no infrastructure | Native | At risk if the domain imports a code registry | Preserved — the set arrives as data, not as an import |
| **Keeps the manifest honest** | By construction | **No** — the manifest stays factually wrong | **Yes** — divergence becomes a build failure |

### Recommendation — **C, realised as B plus a reconciliation check**

The registry of constructible adapters is the runtime gate. The executable set is injected into the
resolver **as data, never as an import**, so the pure Decision plane gains an input rather than a
dependency.

A CI assertion that registry keys and catalogue rows agree converts today's silent divergence into a
build failure. That check is **staged**: it can begin by asserting only that every registry key
exists in the catalogue, deferring the `implemented`/`import_path` direction until F5's protocol
question is settled on its own terms.

Option A is rejected on F5 alone — it is a total outage today and drags an unrelated protocol
reconciliation into this slice. Option B alone is rejected because it leaves the manifest
permanently lying, so nothing ever forces the catalogue to describe reality.

### Where the executable set lives

Not on `CatalogueSnapshot`. That object is identified by `catalogue_version` and `manifest_digest`,
and the executable set varies **per deployment while the digest stays equal** — two deployments of
the same manifest can construct different adapters. Adding it would mean the snapshot's identity
fields no longer identify its contents. ADR-0045 F4 points the same way: the catalogue carries
design-time and seeded metadata, not facts about a running deployment.

Two candidates remain, and the choice is genuinely close:

| | Fit | Objection |
|---|---|---|
| **`RuntimeSnapshot`** | Matches ADR-0045 F5 by ownership and direction — written by Execution, read by Decision. The resolver already *excludes candidates* on runtime facts (`quota_exhausted`, `health_down`), so executability would be a third instance of an established pattern. | Every existing member is a fleet-wide **measurement** materialised from three SQL reads. The executable set is a code-derived property of one process, in no table. Adding it makes the object heterogeneous in origin. |
| **A separate deployment-scoped input** *(chosen)* | The resolver already takes several sibling inputs; a fourth is additive and corrupts no existing object's identity or origin. Keeps "measured fleet state" and "what this build can construct" distinct, which they are. | Introduces one more concept — justified only because neither existing object can hold it without weakening its own definition. |

The tiebreak is **origin coherence**, not taste: `CatalogueSnapshot` is manifest-derived,
`RuntimeSnapshot` is measurement-derived, and the executable set is build-derived. It is a plain set
of adapter ids, not a rich object. `RuntimeSnapshot` is recorded here as a defensible alternative;
what this ADR forecloses is only `CatalogueSnapshot`.

### What is recorded, and why it is not the set

The guarantee at stake is **historical explainability** — a resolution record must explain itself
without re-executing anything — and *not* replay. Replay is unavailable today and this ADR does not
restore it: the resolver already excludes candidates on `RuntimeSnapshot` health and quota, which
are read live and recorded nowhere. Any claim that recording the executable set "keeps resolution
replayable" is false, and an earlier draft of this ADR made it.

So the requirement must be tested directly: **does the executable set answer a question
`ineligible_reason` cannot?** The ledger already stores, per candidate, `eligible`,
`ineligible_reason` and the score `breakdown`.

- For any adapter **in the catalogue**, no — provided executability is evaluated **first** in the
  eligibility sequence. `ineligible_reason` reports only the *first* failing constraint, so position
  matters: checked first, every candidate is either labelled `not_executable` or is known to have
  passed the check; checked later, a candidate labelled `quota_exhausted` might silently also have
  been unconstructible.
- For an adapter **executable but absent from the catalogue**, yes — it never becomes a candidate,
  so it appears nowhere. This is exactly the divergence the staged CI reconciliation check
  forecloses, so in a conformant deployment the residue is empty.

**Therefore the set is not recorded.** Executability is evaluated first and surfaces as a per
candidate `not_executable` reason through the mechanism that already ships. Recording the set as
well would add a second, coarser account of the same fact, justified only by the replay argument
that does not hold.

**Proposed invariant — DISP-1** (final form in *Load-bearing invariants*): resolution is well-formed
only against a declared executable set, from which it follows that the Decision plane never returns
an adapter the Execution plane cannot construct in that deployment. The set reaches the resolver as
data, so the resolver stays pure; each exclusion it causes is recorded, so the decision stays
explainable.

---

## D2 — Provenance semantics: decision or execution?

This is treated as a first-class architectural question rather than a consequence of dispatch,
because the system already persists false claims and will continue to do so under any dispatch
design that does not settle it.

### Normative vocabulary

The terms below are **normative for this ADR and for work that cites it**. Three of them share the
root "execut-" in different senses, which is the most likely source of future confusion.

| Term | Meaning |
|---|---|
| **Decision plane** / **Execution plane** | The architectural planes of ADR-0045. Always capitalised when the plane is meant. For the purposes of this ADR the **Decision plane includes the application-layer resolution service** that turns the pure resolver's output into the final routed decision — `ResolverCapabilityResolver`, including the tier cascade in `_apply_constraints`. Being application code does not make it Execution: the test is whether a component expresses provider *preference*, not which layer it sits in. |
| **execution mode** | The `ExecutionMode` enum (`AUTO`, `FREE_REMOTE_ONLY`, …) — a request constraint. Neither a plane nor an event. |
| **selected** / **chosen** | Decision-sense verbs: the resolver named this adapter. `resolution.top` is *chosen*, not *executed*. |
| **produced** | Execution-sense verb: this adapter returned the bytes. The normative verb for what happened; "ran", "served", and "used" are not used as substitutes. |
| **executable** | *Constructible in this deployment* — a capability, not an event. An adapter can be executable and never produce anything. |
| **executed** | An event: an adapter produced an artefact. Never a synonym for *selected*. |
| **accepted** | A verification outcome. Orthogonal to *produced*: an artefact can be produced and not accepted. |
| **unset** | The field holds no value (`NULL` in the column). The single term for absence; no sentinel value exists. |
| **dispatch binding** | The registry key under which Execution constructed and invoked an adapter. The authoritative assertion of producer identity (D2). |
| **decision field** / **execution field** / **lifecycle field** | The three kinds a field may be. Defined, per field, in *The semantics, stated as a contract* below. |

**D1–D4** are this ADR's *decision points* — the ordinary ADR sense of the word. Where "decision"
carries the normative sense above, it refers to what the Decision plane selected.

### The problem, stated precisely

Three records carry adapter identity, and their names imply execution while their contents record a
decision:

| Record | Column | Populated from | Names |
|---|---|---|---|
| `generation_resolution_ledger` | `chosen_adapter`, `candidate_list` | resolver output | the **decision** — correctly |
| `generations` | `chosen_adapter`, `chosen_provider` | `resolution.top` | the decision, under an execution-shaped name |
| `generation_shots` | `adapter_used` | the same `chosen` value, per shot | **claims execution**, records the decision |

Under F2 these diverge in every environment: any `AUTO` run writes `comfyui.flux_schnell` into all
three while Pollinations produced the bytes. Stage 13 asserts the divergence as correct behaviour
(F7). ADR-0046 X5 requires provenance and ledger entries per execution but does not disambiguate
which of the two concepts they hold.

### Options

| | Option | Shape |
|---|---|---|
| **A** | Everything is a decision record | Rename semantics; accept that no record states what produced the artefact |
| **B** | Split the concepts *(recommended)* | The ledger remains the decision record; `generation_shots.adapter_used` becomes an execution record and must name the adapter that produced the bytes |
| **C** | Everything is an execution record | Write adapter identity only after a successful call; lose the record of what was selected when nothing produced an artefact |

### Recommendation — **B**

The two concepts are genuinely distinct and both are needed. "What did the system decide, and why"
is an explainability concern already met by
`generation_resolution_ledger` with its ranked `candidate_list` and score breakdowns. "What actually
produced this artefact" is a provenance and auditability concern — and it is the one the platform
currently cannot answer truthfully.

`generation_shots.adapter_used` is already **per-shot** and nullable, so a run in which different
shots were produced by different adapters is representable **without a migration**. Option C is
rejected because it discards the decision record precisely in the failure case where it is most
diagnostic. Option A is rejected because it permanently gives up the ability to audit what produced
an artefact, which is the capability the slice exists to create.

### The semantics, stated as a contract

This ruling is about **meaning**, not about storage: each definition below — not the column's
location or name — is what future work must satisfy. The classification is made **per field, not per
record**, because a single table may hold decision, execution and lifecycle fields at once, and
`generation_resolution_ledger` does.

| Field | Kind | Definition |
|---|---|---|
| `generation_resolution_ledger.candidate_list`, `.chosen_adapter` | **decision** | What the Decision plane selected, and the ranked alternatives it selected from — each carrying its `eligible` flag, `ineligible_reason` and score `breakdown`. Written once per resolution, whether or not anything subsequently executed. |
| `generation_resolution_ledger.execution_result` | **decision** *(see ruling)* | The outcome **of the resolution**: whether a selection was produced. Named and typed as though it recorded the run's execution; it does not. |
| `generation_resolution_ledger.start_time` / `.end_time` | **lifecycle** | When resolution ran. Neither a decision nor an execution fact. |
| `generation_shots.adapter_used` | **execution** | What actually produced the artefact for that shot. Meaningless — and therefore unset — where no adapter produced bytes. |
| `generations.chosen_adapter` / `.chosen_provider` | **decision** | `resolution.top` for the run. Named as though they record execution; a run whose shots were produced by different adapters is described by the shot rows, not by these. |

**Ruling on `execution_result`.** It is a **decision field**, resolved the same way as
`generations.chosen_adapter`: the name is wrong, the semantics are settled here, and the name is not
changed. It is populated from whether resolution yielded a selection — `SUCCESS` when one was
produced, `NONE` when none was — and it is written before any adapter is invoked. It therefore
records nothing about the run's execution, and `FAILURE` and `FALLBACK` are **unreachable on this
column by construction**. A future fallback walk records its outcome in execution records, never
here; D4 is amended to match. Without this ruling the column would be decision-populated on today's
path and execution-populated on a future one, which the rule below prohibits.

**Why the misleading names are kept.** Two fields settled here — `execution_result` and
`generations.chosen_adapter` — now carry semantics their names do not suggest, and this ADR
deliberately does **not** rename them. A rename is a migration plus an API-visible change to the
generations read model, and it buys no semantic clarity that this table does not already provide.
Column names are historical artefacts of the increment that created them; **semantic ownership is
defined by the field-classification table above, not by the identifier**. The cost is real and is
accepted: a reader encountering `execution_result` in isolation will guess wrong. The mitigation is
that the guess is cheap to correct and the classification is normative, whereas a rename is
expensive and permanent. The forward-looking rule below prevents the debt from growing — new fields
must be named for what they are.

**Forward-looking rule.** Any field whose name, type or enum implies execution — including fields
naming an adapter, provider, model or outcome — must declare itself a decision, execution or
lifecycle field. A field populated from a decision in some paths and from an execution in others is
prohibited, because it is unusable for either purpose — which is precisely the defect D2 exists to
close. **A field's name is never authoritative for its kind; this table is.**

**One field outside the Generation context.** Promotion copies `generations.chosen_provider` into
`media_assets.provider`, so a decision value crosses into Media under an unqualified name.
`media_assets.provider` is hereby classified a **decision-derived routing value, not an execution
provenance field**. Classifying it is in scope because the value originates here; changing it is
not — the Media bounded context owns its own provenance model, and X8 keeps promotion the single
bridge.

### Population rule: what an execution field means when execution never begins

A decision can exist without an execution ever following it, so the contract is incomplete until the
empty case is defined.

- **A decision field is populated as soon as a decision exists, and only then.** Where resolution
  yields no eligible candidate there is no decision to record, so the decision fields are unset —
  while the resolution ledger still records that resolution occurred and selected nothing.
- **An execution field remains unset until an adapter has produced the artefact it describes**, and
  is **never** populated by copying a decision.
- **There is no third "not executed" state.** A nullable column already distinguishes "nothing
  produced an artefact" from "produced by X"; a sentinel value would carry no extra information and
  would have to be excluded from every query that asks what produced an artefact.

| Situation | Decision fields | `adapter_used` |
|---|---|---|
| Cancelled while `queued` — no resolution occurs | unset | unset |
| Resolution yields no eligible candidate | unset — no selection was possible; ledger outcome `NONE` | unset |
| Decision made; adapter cannot be constructed *(the failure this slice introduces)* | populated | **unset** — must not be back-filled from the decision |
| Decision made; the provider call fails | populated | unset for that shot |
| Bytes produced but rejected by verification | populated | **populated** — an adapter did produce bytes; acceptance is a separate column |

The fourth row is the one this ADR exists to protect, and the fifth is the one most likely to be got
wrong: `adapter_used` records *production*, not *acceptance*. Where three images were produced for a
shot and none was accepted by verification, an adapter still produced them.

Current behaviour is already consistent with this rule by accident rather than by design: a hard
`ImageGenerationError` propagates uncaught out of the shot loop, so no shot row is written at all.
The rule makes that deliberate and extends it to the new construction-failure path.

**The fifth row requires the identity to outlive the artefact.** On today's verification-failure
path the rejected image is discarded, so by the time the shot row is written nothing remains that
names a producer. The contract is unsatisfiable unless producer identity is captured **when the
adapter is invoked** rather than recovered from the returned artefact — and an implementer who
discovers the fact missing at write time has exactly one value to hand, the decision, which DISP-2
forbids. Requiring capture at invocation is what makes rows four and five consistent rather than
merely aspirational.

### Who may assert production

A definition of *what* an execution record means is incomplete without *who* may assert it. The
adapter cannot: `IImageGenerator` implementations receive `adapter_id` as an argument and today's
sole implementation **echoes it back** on the returned artefact while reporting its own
`provider_id`, so a misbound registry entry yields an artefact whose two identity fields contradict
each other. A self-report is therefore not evidence.

**Producer identity is asserted by the dispatch binding** — the registry key under which Execution
constructed and invoked the adapter. Execution knows which entry it resolved and called; that is the
authoritative fact, and it is available at invocation time, which is what the fifth row needs. An
adapter's self-reported identity may be recorded as corroboration but never as the source, and a
disagreement between binding and self-report is a defect in wiring, not a provenance question.

**Proposed invariant — DISP-2** (final form in *Load-bearing invariants*): a record that names an
adapter as having executed names the adapter that produced the bytes, and is unset when nothing did;
producer identity is asserted by the dispatch binding, not by the adapter; decision records and
execution records are never conflated.

**Consequence to accept explicitly:** historical rows written before this slice cannot be trusted to
satisfy DISP-2, and no backfill can repair them, because what actually produced the bytes was never
recorded.
Consistent with ADR-0052's treatment of legacy generations, these are legacy artefacts; no inference
or heuristic attribution should attempt to reconstruct them.

---

## D3 — Canonical fallback representation

### Options

| | Option | Shape |
|---|---|---|
| **A** | Ordered candidate list is canonical *(recommended)* | `CapabilityResolution.candidates`; the adapter chain becomes descriptive catalogue metadata |
| **B** | Adapter fallback chain is canonical | Follow `AdapterInfo.fallbacks` from the chosen adapter |
| **C** | Both, layered | Walk candidates; within each, walk its chain |

### Recommendation — **A**

Per F6 the two disagree under the default mode, so one must win. The candidate list is the output of
the Decision plane and already carries eligibility, budget filtering, tier constraints, and scores;
the adapter chain is design-time metadata authored per adapter with no knowledge of the request. If
the chain were canonical, Execution would follow a route the resolver never evaluated against the
request's constraints — which is materially close to Execution choosing a provider, and sits badly
with ADR-0046 X1 even if it does not literally re-score.

Option C is rejected as strictly worse than either: it multiplies external calls along two
dimensions with no mechanism to bound the product.

**The chain is not discarded — it is reclassified.** `adapter_fallbacks`, `AdapterInfo.fallbacks`,
and `Candidate.fallbacks` are retained in the schema, the snapshot, and the manifest. What changes is
their status: they cease to be an **execution contract** and become **catalogue metadata**. Nothing
in the Execution plane may follow them. They remain a legitimate future **input to the resolver's
ordering**, which is the correct home for an authored preference: expressed in the plane that owns
preference, and thereby subjected to the request's eligibility, budget, and tier constraints before
it can influence anything. Removing them is explicitly *not* proposed.

**Note on why the disagreement disappears in practice.** Once DISP-1 filters `comfyui.flux_schnell`,
`AUTO`'s LOCAL tier empties and the cascade falls to `FREE_REMOTE`, where Pollinations sits. The
chain's declared purpose — comfyui failing over to pollinations — is subsumed by executability
filtering. This is the strongest argument that D3 and D4 can be settled without shipping a walk.

---

## D4 — Ownership of fallback execution

### The contradiction to close

| Source | Assigns "fallback execution" to |
|---|---|
| α8.5d pre-flight §2.1 | the **Resolver** (α8.5e) |
| `RESOLVER_RUNTIME_CONTRACT.md` §8 | the **Execution Runtime** |
| `EXECUTION_RUNTIME_CONTRACT.md` §1 | Execution, which "**may walk** the list" |
| ADR-0044 MRC-2 | "best provider → **execute**" (singular) |

### Recommendation

**Execution owns fallback execution; the Decision plane owns fallback *ordering*.** The α8.5d
wording is read as *computing* the ordering — which is what α8.5e shipped — and
`RESOLVER_RUNTIME_CONTRACT.md` §8 is read as *invoking* it. ADR-0044 MRC-2's singular "best provider"
is resolved as "the first eligible candidate", consistent with `Resolution.top`. This ADR records
that reading so the ambiguity stops propagating.

**Walking is permitted, not required — and this slice does not ship it.** The evidence:

- Under `AUTO` after DISP-1, the candidate list is length one, so a walk is inert by default (F1/F6).
- The generation path writes **no `usage_records`**, so additional provider calls are unmetered.
- There is **no whole-run wall-clock cap**. The lease (300s) is renewed every 60s indefinitely; the
  practical ceiling is the 900s drain budget, which a plain six-shot run at three attempts and a
  120s per-request timeout already exceeds. A walk multiplies that with nothing bounding the product.
- Whether spending at a *second provider* mid-run is consistent with GEN-2 is genuinely unsettled:
  GEN-2's "intra-run repair is already paid for" reasoning was written about seed retries against
  the *same* adapter, not about engaging a new one.

The pre-flight must therefore define the **failure taxonomy** — distinguishing an adapter fault from
a verification failure, which `GenerateVideo` does not distinguish today — so that a future walk is
purely additive.

**Where a future walk records its outcome.** `ExecutionOutcome.FALLBACK` exists in the enum and is
never written. Under D2's ruling it must **stay** unwritten on
`generation_resolution_ledger.execution_result`, which is a decision field: writing it there would
make one column decision-populated on today's path and execution-populated on the walk's, which D2
prohibits. A walk that engages a second adapter records that fact in **execution records** — the
shot rows, which already name the producing adapter per shot. This is a stricter statement than the
earlier "stays unwritten until that slice", and it is what D2 forces.

**Scope of this slice (not an invariant).** Execution stops at the first successful candidate. This
is a *permission* — it cannot be violated, only superseded — so it is recorded here as scope rather
than promoted to the invariant catalogue. Adding a walk later is an Execution-plane change; changing
the order is a Decision-plane change.

**Proposed invariant — DISP-3** (final form in *Load-bearing invariants*): Execution never follows a
provider ordering the Decision plane did not compute.

---

## Compatibility with existing ADRs

| ADR | Interaction |
|---|---|
| **ADR-0042** | No frozen path touched (F8), verified against the checker script. No override required. |
| **ADR-0045 F1/F2** | Unchanged — Execution still never scores or re-orders; DISP-1 filters on data supplied to the resolver, it does not rank. |
| **ADR-0045 F4/F5** | Respected by keeping the executable set off the catalogue (F4 bars deployment facts there) and by treating it as Execution-owned, Decision-read (F5's direction). |
| **ADR-0045 F7** | Untouched — no provider-specific logic enters the Decision plane; the executable set is opaque data, not a provider identity the resolver branches on. Dependency direction is preserved by passing it as data rather than importing a registry into the domain. |
| **ADR-0046 X1** | Satisfied more faithfully than today: Execution begins actually consuming the list it is given. |
| **ADR-0046 X5** | Preserved on the guarantee X5 actually makes — entries stay self-explaining ("values, not FKs"). Executability exclusions are recorded per candidate, so the record explains itself. X5 is not a replay guarantee, and this ADR does not claim to restore replay. |
| **ADR-0052 GEN-2** | Untouched by this slice; D4 records the open question rather than resolving it by implication. |
| **ADR-0041 D2** | Its "fallback chain" is superseded for generation by the resolver's ordered candidates (D3). Recorded explicitly, since ADR-0044 only implied it. |

---

## Rejected alternatives

**Reuse the frozen `ProviderRegistry` / `StepCommandDispatcher`.** Needs an ADR-0042 override; the
four-value `Capability` enum cannot express an `adapter_id`; and it speaks the wrong protocol (F5).

**Runtime `import_path` loading.** No `importlib` precedent anywhere in `app/` (F4); it yields a
class, not a configured instance, while adapters have heterogeneous constructor dependencies; and it
lets database content select executable code.

**Defer the whole question until a second real adapter exists.** Superficially attractive — and the
precedent exists in PUB-4, which deferred a destination catalogue until two real destinations. It
fails here because the defect is not "we cannot choose between adapters"; it is that the system
records false claims about what executed (D2), which is true with exactly one adapter.

**Fix provenance without fixing dispatch.** Would require writing "pollinations" into
`adapter_used` regardless of what the resolver chose — encoding the bypass as intended behaviour and
making the eventual dispatch slice a second breaking change to the same columns.

---

## Consequences

**Intentionally changed behaviour** (the honest framing of what is not preserved):

1. **Stage 13's fixture must change.** Its synthetic adapter is `implemented = false` with no code
   (F7), so DISP-1 filters it under any authority model. The correction is to make the registry
   injectable so the test registers a fake under `golden_provider.image` — which also upgrades the
   test from asserting that `adapter_id` is ignored to proving that dispatch honours it.
2. **Recorded adapter identity changes** in any environment that resolves under `AUTO`: from
   `comfyui.flux_schnell` to the adapter that actually produced the bytes. This is the defect being
   fixed, and it
   is API-visible, since the generations read model exposes provenance and the resolution ledger.
3. **A new terminal failure appears** — an unknown or unconstructible `adapter_id`. The system moves
   from silently succeeding with false provenance to failing closed.
4. **Zero eligible candidates becomes reachable** in configurations where nothing executable matches
   the constraints. `GenerateVideo` already handles the empty case, but it will now occur for a new
   reason.

**Unchanged:** which adapter actually serves requests today (Pollinations), the `IImageGenerator`
port shape, the resolver's scoring, GEN-2, and every publishing, notification, and worker-runtime
surface.

---

## Load-bearing invariants

Each statement below was tested against one question: *would violating it make the architecture
incorrect regardless of how the system is built?* Statements that only describe how this slice
intends to build things were moved into the decision text or the pre-flight instead.

1. **DISP-1 — Resolution is well-formed only against a declared executable set.** The executable set
   is an **input** to resolution, supplied as data rather than imported, and **every exclusion it
   causes is recorded** as that candidate's `ineligible_reason`. It follows that the Decision plane
   never returns an adapter the Execution plane cannot construct *in that deployment*. The
   deployment-relative scope is deliberate: executability is a property of a running system, so the
   guarantee this preserves is that the record **explains itself** (ADR-0046 X5), not that the
   resolution can be recomputed — which unrecorded runtime state already prevents.
2. **DISP-2 — A record that names an adapter as having executed names the adapter that produced the
   bytes, and is unset when nothing did.** Decision records and execution records are distinct
   concepts, are separately defined (D2), and are never conflated — including in fields added later.
   An execution field is never populated by copying a decision. **Producer identity is asserted by
   the dispatch binding** — the registry key under which Execution constructed and invoked the
   adapter — and never by an adapter's self-report, which may echo the requested identity. Rows
   predating this ADR are legacy artefacts; no inference may reconstruct them.
3. **DISP-3 — Execution never follows a provider ordering the Decision plane did not compute.**
   Stated as a prohibition, because the permission it replaces ("Execution may stop at the first
   candidate") is scope, not an invariant. This closes a gap ADR-0046 X1 leaves open: following an
   authored fallback chain is neither scoring nor provider-name branching, so X1 does not forbid it,
   yet it would place provider preference outside the plane that owns preference.

---

## What acceptance freezes

The pre-flight implements this list and does not reopen it. Each row names the section that governs;
**where a row and its governing section differ, the section wins** — this table is an index, not a
second definition.

| # | Frozen decision | Governed by |
|---|---|---|
| 1 | Executability is a deployment-scoped Decision-plane input, distinct from the catalogue and measurement snapshots | D1 |
| 2 | Executability is evaluated **before** all other eligibility checks | D1 |
| 3 | `not_executable` is the explicit rejection reason | D1 |
| 4 | No separate executable-set provenance payload — first-position classification supplies the explanation | D1, DISP-1 |
| 5 | Decision facts and production facts are semantically distinct | D2, DISP-2 |
| 6 | Field semantics come from the normative classification table, not from legacy column names | D2 |
| 7 | `execution_result` is a decision field — the outcome of resolution, not of provider execution | D2, D4 |
| 8 | Producer identity is established by the dispatch binding at invocation, never by echoing a requested routing key | D2, DISP-2 |
| 9 | Production identity stays **populated** when bytes were produced and later rejected by verification | D2 |
| 10 | Production identity stays **unset** when no adapter produced bytes | D2, DISP-2 |
| 11 | The ordered candidate list is the canonical ordering | D3 |
| 12 | Catalogue-authored fallback chains are metadata, never an execution ordering | D3, DISP-3 |
| 13 | The Decision plane owns preference and ordering | D4, DISP-3 |
| 14 | Execution owns provider invocation | D4 |
| 15 | No candidate walking ships in α9.9 | D4 |
| 16 | `media_assets.provider` is decision-derived, not execution provenance | D2 |
| 17 | The application-layer resolver service is Decision whenever it applies provider preference | Vocabulary, DISP-3 |

---

## Open questions for the pre-flight

Implementation questions deliberately excluded from this ADR:

1. The registry's class shape, module home, and port definition — constrained by the import-linter
   contract *"Application use_cases never import infrastructure or api"*, which forecloses a
   use-case-level factory and points at the `DestinationRegistry` shape.
2. The failure taxonomy distinguishing adapter faults from verification failures, which
   `GenerateVideo` does not distinguish today.
3. Container lifecycle for N adapters — `shutdown()` currently closes a single memoised
   `_image_client` through module-global state.
4. The deployment-scoped executable-set input's concrete type, where it is assembled, and how it
   reaches the resolver without the domain importing infrastructure. D1 fixes that it is a distinct
   input and forecloses `CatalogueSnapshot`; the shape is the pre-flight's.
5. The staged reconciliation check's initial strictness, given F5.
6. Test strategy: unit tests inject `FakeCapabilityResolver` and would not catch a dispatch
   regression, so the filter needs coverage at its own layer.
7. Whether `UNIQUE (provider_id, capability_id)` on `provider_adapters` — which permits only one
   adapter per provider per capability — needs revisiting before adapter identity is fixed as the
   dispatch key. Flagged as a future hazard, not a blocker.

---

## Changelog

| Date | Change |
|---|---|
| 2026-07-30 | Drafted as **Proposed** at the post-α9.8 grounding stop, following two read-only investigations. The second, adversarial review falsified three conclusions of the first (recorded in Context) and surfaced the provenance-integrity defect that became D2. |
| 2026-07-30 | Pre-acceptance revision. D2 gains a **population rule** defining execution-field semantics when execution never begins: decision fields are populated once a decision exists, execution fields stay unset until an adapter produces the artefact and are never back-filled from a decision, and no third "not executed" sentinel is introduced. DISP-2 was made total to match. Clarifies that `adapter_used` records production, not acceptance. |
| 2026-07-30 | Pre-acceptance revision, no recommendation changed. **(1)** D2 now defines the semantics of each record as a contract, resolves `generations.chosen_adapter` explicitly as a decision field, and adds a forward-looking rule prohibiting fields that mix the two concepts. **(2)** D3 restates that the fallback chain is reclassified rather than discarded. **(3)** The invariants were tested against "would violating this make the architecture incorrect regardless of implementation?"; DISP-1 was rescoped as a well-formedness condition on resolution inputs, and DISP-3's permission clause was demoted to scope, leaving a pure prohibition. |
| 2026-07-30 | **Accepted.** No decision changed at acceptance. Adds *What acceptance freezes* as an index over the seventeen frozen decisions, and corrects a stale open question that still routed the executable set to `CatalogueSnapshot` after D1 had foreclosed it. Review history: two read-only investigations (the second falsifying three conclusions of the first), two independent external architecture reviews, a reconciliation pass, an adversarial acceptance review that produced six load-bearing findings, and two revision passes closing them. |
| 2026-07-30 | Editorial pass, no decision changed. Adds the rationale for **keeping two misleading column names** (rename cost versus a normative classification table); classifies `media_assets.provider` as a decision-derived routing value rather than execution provenance; and fixes the Decision plane's boundary in the vocabulary so the application-layer resolution service, including `_apply_constraints`, is unambiguously Decision — which is what makes DISP-3 adjudicable. |
| 2026-07-30 | Acceptance-review revision, resolving six load-bearing findings from an adversarial board review. **(L4)** D2 now classifies **per field, not per record**, and rules `execution_result` a decision field; D4 is amended so a future walk records fallback in execution records, never on that column — closing a contradiction in which D2 and D4 assigned it incompatible kinds. **(L1)** `CatalogueSnapshot` is rejected as the executable set's home (it would break `manifest_digest` identity, and F4 bars deployment facts from the catalogue); a separate deployment-scoped input is chosen on origin-coherence grounds, with `RuntimeSnapshot` recorded as a defensible alternative. **(L2/L3)** The replay justification is withdrawn as unsound — unrecorded runtime state already prevents replay — and replaced with historical explainability; applying the test "does the executable set answer a question `ineligible_reason` cannot?" yields *no*, so the set is **not** recorded and executability is instead evaluated first in the eligibility sequence, surfacing as a per-candidate reason. **(L5)** DISP-2 gains an authority clause: producer identity is asserted by the dispatch binding, never by an adapter's self-report, which today echoes the requested id. **(L6)** D2 records that the verification-failure row requires identity captured at invocation, since the rejected artefact is discarded. |
| 2026-07-30 | Terminology pass, no decision changed. D2 gains a **normative vocabulary** table, and the document was made to conform to it. Two substantive inconsistencies were corrected: DISP-1 was stated in its superseded pre-rescoping form in D1 while the invariant catalogue carried the rescoped form, and D2's inline DISP-2 omitted the totality clause. Absence was normalised on *unset* (from a mix of `NULL`, *absent*, *unset*); the execution event was normalised on *produced* (from a mix of *ran*, *served by*, *executed*); DISP-3's "ordering the Decision plane did not produce" became "did not **compute**", since *produce* is now reserved for the execution sense. |
