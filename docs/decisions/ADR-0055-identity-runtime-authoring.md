# ADR-0055 — Identity-Runtime Authoring: Bounded-Context Ownership, Snapshot Semantics & Request-Payload Evolution

**Status:** **Accepted** 2026-07-31 (Phase 3, α10.0 — Identity-Runtime Authoring). Governance that
**precedes** implementation (like ADR-0044/0045/0046/0051/0052/0053/0054): it fixes *what a
creator-authored world is, who owns it, and what a generation may claim about the world it ran
with*, before any authoring surface exists. **No implementation, migration, or code change
accompanies this ADR.**

**Ordering note.** Unlike α9.9, the grounding *and* the pre-flight were produced before this
document, at the reviewer's direction to select the slice on evidence first. The pre-flight's
rulings **PF1–PF10 were therefore provisional** until acceptance. The required reconciliation pass
was performed at acceptance and is recorded in
[`PHASE3_ALPHA10_0_PREFLIGHT.md`](../engineering/PHASE3_ALPHA10_0_PREFLIGHT.md) §14: no ruling
contradicted a decision here, and four frozen decisions absent from the pre-flight were folded into
it. Where the two ever differ, this ADR wins.

**The decisions below are frozen on acceptance.** The pre-flight converts them into a slice; it
does not revisit them. If implementation discovers a genuine contradiction with this ADR or with
another accepted invariant, work stops and the contradiction is reported rather than resolved
locally.

**Scope: the authoring and binding of creator world state.** This ADR does not change shot
planning, prompt composition, resolution, dispatch, verification, repair, rendering, or any
publishing surface. It introduces no capability (speech, music, motion), no provider, and no
external dependency. It settles five questions that any future creator-authored input to generation
— brand kits, style presets, voice profiles — would otherwise have to re-answer.

**Builds on:**
- **ADR-0044** (**AR2** character identity lock: "scenes reference `CharacterID`, never re-derive
  identity from the prompt" — this ADR discharges AR2's *authoring* half and explicitly leaves its
  reference-asset half open).
- **ADR-0045** (**F3/F7** the planner names no provider and carries no provider-specific logic;
  **F4/F5** design-time knowledge and operational measurement stay separate — the separation IDENT-4
  extends to creator knowledge).
- **ADR-0046** (**X2/X3** Execution consumes an immutable plan and never re-plans; **X5** provenance
  is self-explaining, carried as **values, not FKs**).
- **ADR-0049** (the one-way posture GEN-1 cites: no execution-plane module knows what a user is).
- **ADR-0052** (**GEN-1** ingress owns identity, the runtime owns execution state — the boundary
  D4 corrects; **GEN-3** ownership is never inferred; **D4-A** idempotency is client-supplied and
  never content-derived).
- **ADR-0054** (**DISP-1** the Decision plane never offers what the deployment cannot execute;
  **DISP-2** and its field-classification table, which D4 extends with a third class).
- **ADR-0042** (orchestration freeze — verified against `scripts/check_frozen_platform.py`: **none**
  of the nineteen frozen paths is touched by this slice).

---

## Context

The platform can compose a world it has no way of being told about.

`domain/generation/identity.py` defines `Character`, `Location`, `Prop` and `IdentityProfile` in
full, each with a deterministic `prompt_fragment()`. The planner casts characters and a location
into every `Shot`; the prompt builder composes their fragments into the exact string the adapter
receives. None of this is speculative code — it is exercised by the golden fixtures and the demo
script, and it is the mechanism by which CS-2 ("Identity Runtime + scene are constant across all
shots") is meant to hold.

What is missing is a table to keep a world in and a route to author it with. Every creator arriving
through `POST /api/v1/generations` gets a world consisting of one integer and one enum, and
therefore gets six shots whose protagonist need not be the same person twice.

This is a capability gap rather than a defect, with one exception recorded as F4 below — a latent
defect that would surface the moment the gap is closed carelessly.

### The decisive facts

**F1 — The consuming path is built and live.** `planner.py:120–123` assigns
`character_ids = tuple(c.id for c in request.identity.characters)` and the first location to every
shot; `prompt_builder.py:83–111` emits character, location, prop and project-look fragments in a
fixed order. An authored world reaches the adapter with no change to either module.

**F2 — The authoring surface admits two scalars.** `GenerationCreateRequest`
(`api/v1/schemas/generations.py:28–50`) exposes `seed` and `global_style`; `to_runtime_request`
(`request_codec.py:105–108`) builds `IdentityProfile(seed=…, global_style=…)` and nothing else.

**F3 — The provenance column exists and has never been written.** `generations.identity_id text`
has been present since migration `0012` (`0012_execution_runtime.py:104`) and is supplied as `None`
on every run (`generation_ledger_repository.py:179`).

**F4 — Writing that column from ingress today would silently erase it.** The execution store's
upsert enumerates `identity_id = EXCLUDED.identity_id`
(`generation_ledger_repository.py:61`) while `begin()` always supplies `None`. The first status
write of a claimed run would null an ingress-written value. GEN-1 states the prohibitions "hold by
construction rather than by convention" *because* the `ON CONFLICT` clause enumerates only
runtime-owned columns; this column is the one place that is not true.

**F5 — The request payload was designed to be extended by exactly this slice, and refuses to be
extended sloppily.** `decode_spec` rejects unknown keys and any version but the current one, and
the module docstring names the reason: "a later identity slice extends the payload additively
instead of silently reinterpreting rows written by this one" (`request_codec.py:11–16`, `77–89`).

**F6 — The Decision plane's casting rules bound how much world is useful.** Every character appears
in every shot (`planner.py:120`), only the first location is ever read (`:123`), and every prop is
emitted in every prompt (`prompt_builder.py:95`). World state beyond those limits does not enrich
output; it dilutes it.

**F7 — Reference images are plumbed end to end and discarded at the last step.**
`reference_refs_for` → `ShotPrompt.reference_image_refs` → `IImageGenerator.generate(...)` →
`PollinationsImageGenerator`, which accepts the argument and ignores it
(`pollinations_image_generator.py:45–47`). No image-conditioning adapter is registered;
`comfyui.flux_schnell` has no code.

**F8 — The platform has twice solved "mutable source, immutable record" the same way.** The
catalogue is pinned into each run by `catalogue_version` + `manifest_digest` rather than by FK;
`publish_jobs.content_package` stores the resolved package verbatim. Both are values, both are
self-explaining after the source changes, and X5 generalises the rule.

---

## Decision points

| # | Question |
|---|---|
| **D1** | Where does creator-authored world state live, and which plane owns it? |
| **D2** | Is a generation's identity a live reference or a captured value — and where exactly is the boundary? |
| **D3** | How does the persisted request payload evolve, and what compatibility is guaranteed? |
| **D4** | What kind of fact is identity in the provenance model, and which plane may write it? |
| **D5** | What bounds authorable state — product judgement or executable capability? |

---

## D1 — A new owner-scoped bounded context in the Knowledge plane

**Options.**

**A. Extend the Projects aggregate.** World state becomes columns and children of `projects`.
Cheapest wiring; no new context.
**B. No durable state — richer request payload only.** The creator posts the full world with each
generation. No tables, no CRUD, no ownership questions.
**C. A new bounded context, `identity`, owner-scoped, relationally modelled.**

**Evaluation.** A fails on a fact: generations are not project-scoped. Migration `0016` gave
`generations` `tenant_id` and `owner_user_id` and no `project_id`, so binding a world to a project
would invent a relationship the consuming side cannot use, and would make every generation request
carry a project it does not otherwise need. B fails the purpose of the slice — consistency *across*
generations is the product value, and a world retyped per request is not durable world state; it
also multiplies payload size without giving the creator anything to edit. C matches what the domain
model already is: `IdentityProfile` is an aggregate with children, and the repository's established
recipe for owner-scoped aggregates (α9.2's library) applies unchanged.

**Recommendation: C.** `app/domain/identity/`, owned by `(tenant_id, owner_user_id)`, persisted
relationally — a profile root with character, location and prop children. The relational shape is
chosen over a JSONB body because this is *mutable* creator state needing partial edits and
per-profile uniqueness of a child's stable key; JSONB in this repository is reserved for frozen
snapshots, which is exactly what D2 makes the *binding* rather than the source.

**Plane classification.** Identity Runtime is **Knowledge** — it declares what exists in the
creator's world, the way the catalogue declares what exists in the provider world. It is *consumed*
by the Decision plane (planner, prompt builder) and never by Execution. This classification is
load-bearing rather than taxonomic: it is what makes IDENT-4 follow. Just as F4 bars operational
columns from the catalogue and F5 keeps measurement out of design-time knowledge, an identity
profile never accumulates execution outcomes — no success rate, no "this character renders well on
adapter X", no quality feedback. The moment it did, a Decision-plane input would become a function
of Execution results, which is the coupling the deferred verification-to-routing guard exists to
prevent.

### What Identity Runtime does not own

Stated as a prohibition, because a bounded context is defined as much by what it refuses as by what
it holds, and because each item below is a plausible future addition that would look locally
reasonable. Identity Runtime does **not** own, and its tables never gain a column for:

- **execution history** — which generations used a world, when, or how often;
- **planner decisions** — which character appears in which shot, shot counts, arcs, or casting;
- **rendering or adapter preferences** — a profile never names, ranks, or hints at a provider,
  adapter, model, or tier;
- **adapter health, quota, latency or availability** — operational state is ADR-0045 F5's, read only
  by the Decision plane;
- **generation success statistics** — success rates, acceptance ratios, or "which world renders
  well";
- **verification outcomes** — scores, similarity, or any judgement about produced bytes.

The first two are owned by the Decision plane, the next two by Knowledge-adjacent operational state
and the catalogue, and the last two by Execution and verification respectively. A profile that
acquired any of them would stop being a declaration of what exists and become a record of what
happened — a different kind of object, in a different plane, with different invariants.

---

## D2 — A generation's identity is a value captured at acceptance

**Options.**

**A. Live reference.** `generations.identity_id` is an FK; the worker loads the profile when it
claims the row.
**B. Snapshot at acceptance.** The resolved world is serialised into the ingress-owned
`generations.request` payload; `identity_id` records only where it came from.
**C. Hybrid — reference plus version pin.** Store `identity_id` + `version` and reconstruct by
lookup, refusing to run if the profile has moved on.

**Evaluation.** A is unacceptable on three counts, each independently sufficient. It breaks replay:
α9.7's contract is that "a claimed row always replays to the identical request", and a live lookup
makes the replay a function of when it happens. It breaks provenance: a completed generation would
no longer describe the world it actually ran with, which is DISP-2's failure mode one layer up —
a record that names something other than what happened. And it forecloses deletion: a profile
referenced by an FK cannot be removed without either cascading into history or a tombstone regime.

C preserves correctness but converts every ordinary edit into a failed generation, and it still
cannot answer "what world did this run use?" after the profile is deleted. It buys nothing over B
and costs the creator their queue.

B is what the platform already does twice (F8), what X5 prescribes (values, not FKs), and what
lets D1's hard-delete answer be simple.

**Recommendation: B.** At acceptance the named profile is read once, validated, and serialised in
full into `generations.request`. The `generations.identity_id` column records **which** profile the
snapshot was taken from, and the profile's `version` at the moment of capture is recorded **inside**
the snapshot. Both are provenance values with **no foreign key**.

### The snapshot boundary

Six questions, settled here rather than left to implementation because each is expensive to change
afterwards.

| Question | Ruling | Why it follows |
|---|---|---|
| **Is the snapshot immutable after request acceptance?** | **Yes — written once, never rewritten.** No code path updates `generations.request` after the create transaction commits | It lives in an ingress-owned column that GEN-1 forbids the runtime to write; immutability is enforced by the ownership boundary, not by discipline |
| **Does a retry use the original snapshot?** | **There is no job-level retry** (GEN-2: claiming is a one-way CAS, `max_attempts = 1` is structural). Intra-run shot repair re-uses the same decoded profile held for the duration of the run and never re-reads the source | Preserves GEN-2 unchanged: this slice adds no new execution attempt and no new spend opportunity |
| **Does replay use the original snapshot?** | **Yes, always.** Replay decodes the stored payload; that is the payload's entire purpose | Any other answer would make a claimed row replay to a request the creator never made |
| **Are profile deletions allowed after generations reference them?** | **Yes — hard delete, no tombstone, no reference counting.** The `identity_id` in past rows becomes a provenance value that no longer resolves | Made safe by the snapshot: history is self-contained. The dangling value is the honest record — that world existed and no longer does |
| **Are profile edits ever visible to queued jobs?** | **Never.** The snapshot is taken before the row becomes claimable | A queued generation is an accepted request, not a standing subscription to the creator's current world |
| **Does payload versioning cover the snapshot or only the ingress schema?** | **The whole payload, including the nested snapshot — one envelope, one version.** The profile's own `version`, recorded inside the snapshot, is *provenance* (which revision of the world this was), not a payload format version, and the two never interact | A second, independently evolving version inside the payload would make the compatibility matrix a product rather than a list |

**Failure mode.** A payload that cannot be decoded — unsupported version, unknown key, missing
field — fails loudly and terminalises the generation. Guessing a world would generate something
the creator never asked for and, once metering exists, bill them for it. This preserves the
existing behaviour of `decode_spec` rather than adding to it.

---

## D3 — Version the payload; accept both versions; never rewrite a row

**Options.**

**A. Add keys under version 1.** Foreclosed by F5: `decode_spec` rejects unknown keys by design.
**B. Bump to version 2 and migrate rows in place**, rewriting every stored payload.
**C. Bump to version 2, accept `{1, 2}` on read, emit only 2 on write, and never rewrite a row.**

**Evaluation.** B is a data migration over an ingress-owned, creator-asserted column, performed to
satisfy a reader's convenience. It converts a reversible schema change into an irreversible
content change, and if the rewrite is wrong it is wrong about what a creator asked for. There is no
operational need: the population of v1 rows is finite, closed the moment v2 ships, and drains
naturally. C costs one branch in the decoder.

**Recommendation: C**, with these compatibility guarantees:

1. **Readers accept every version ever written; writers emit only the current one.** A v1 payload
   decodes to a spec whose identity is absent and reconstructs today's `seed` + `global_style`
   profile *exactly*.
2. **A version is retired only when no stored row carries it**, and retirement is its own decision,
   never a side effect of another slice.
3. **A version bump is required for any change that alters the meaning of an existing key**;
   adding an optional key that older readers would reject is itself such a change.
4. **The version covers the entire payload**, per D2's sixth ruling.

**Migration strategy.** The schema migration (`0017`) is **additive only**: new tables, new indexes,
symmetric `downgrade()`, no change to any existing column, and no destructive operation for the CI
guard to catch. There is **no data migration**: no stored payload is rewritten, no legacy generation
is given an identity, and no world is inferred for a run that predates authoring — the direct
application of GEN-3's rule to a second kind of fact. A profile is not created for an existing
generation's `seed`; historical rows keep the world they had, which was none.

---

## D4 — Identity is a *request fact*: a third class in the provenance model

ADR-0054 D2 classifies each persisted field as a **decision fact** (what the Decision plane
concluded) or a **production fact** (what an adapter actually did), and forbids populating one from
the other. Identity is neither. It is not concluded by the resolver and not produced by an adapter;
it is **asserted by the creator and accepted by ingress** before either plane runs.

**Recommendation: introduce a third class, *request facts*,** and place `identity_id`, the
snapshotted world, the persisted `request` payload, `idempotency_key`, and ownership in it. The
rules that give the class meaning:

1. **Request facts are written once, at acceptance, by ingress alone.** The execution runtime never
   writes them and must not enumerate them in its upsert — which requires removing
   `identity_id = EXCLUDED.identity_id` from `_INSERT_GENERATION_SQL` (F4). This is a **correction
   of a boundary violation**, not a widening of the runtime's contract: it restores the property
   GEN-1 already claims.
2. **A request fact is never inferred, reconstructed, or back-filled** (GEN-3, generalised).
3. **Request facts are carried as values, not FKs** (X5), and therefore survive deletion of their
   source.
4. **No field may mix classes.** A column that means "what the creator asked for" in one row and
   "what the system chose" in another is the defect D2 was written to prevent.

**Checked and clear — `generations.seed`.** Both planes write it: ingress persists the resolved
seed at create, and the runtime writes it during `begin()`. They cannot disagree, because the
runtime's value is decoded *from* the ingress-persisted request rather than read from any other
source. α10.0 must preserve exactly that property: a profile's seed reaches the runtime only
through the snapshot, never by a second lookup.

**Checked and clear — idempotency.** `identity_id` does **not** participate in the idempotency key.
D4-A ruled that only a client-supplied key collapses two requests, and that content is never
hashed; a replayed key therefore returns the original generation with its original snapshot, even
if the caller names a different profile the second time. Making identity part of the key would
reintroduce content-derived idempotency through the side door.

---

## D5 — Executable capability bounds what is authorable

**Options.**

**A. Product judgement.** Expose the whole domain model — references, voice, personality,
expressions, poses, music and subtitle style — and let each consumer use what it can.
**B. Executable capability.** A field is authorable only when some path in this deployment
demonstrably consumes it.

**Evaluation.** A is how most systems accumulate dead controls, and here it has a precise
architectural cost. DISP-1 was accepted one slice ago on the principle that the system must not
offer what the deployment cannot execute; a creator who attaches a face reference that
`PollinationsImageGenerator` discards (F7) has been sold image conditioning the deployment cannot
perform. That is the same defect class α9.9 removed from provenance, relocated to the authoring
surface. The argument is strongest precisely where A is most tempting: reference images are already
plumbed end to end, so exposing them is nearly free — and would be nearly free to get wrong.

**Recommendation: B.** Concretely, on acceptance:

- **Reference images are not authorable.** `ReferenceImage` stays a domain value object with no
  table, no route, and no request field, until an adapter whose executable capability includes
  reference conditioning is registered. DISP-1's executable set is then the mechanism that decides
  whether the control may be offered, per deployment.
- **Cardinality is bounded by the planner's casting rules** (F6), not by taste. The ADR fixes the
  *rule*; the pre-flight fixes the numbers, and they move when the rule's premise moves.
- **Voice, personality, expressions, poses, music style and subtitle style are not authorable.** No
  path consumes them; they belong to the audio and lip-sync slices that will.
- **The gate lifts by evidence.** A field becomes authorable in the slice that gives it a consumer,
  in that slice's contract — never as a convenience addition.

### Capability answers *can*, never *should*

The rule above is a gate on what may be **offered**, and nothing more. Executable capability answers
exactly one question — *can this deployment produce this kind of asset?* — and never *should it*.

Authoring must therefore not infer **quality, preference, routing, ranking, cost, historical
success, or any planner decision** from executable capability, and must not expose any of them.
"This deployment can do X" is a Knowledge fact about a running system; "X is the right choice here"
is a Decision fact produced by the resolver from catalogue metadata and operational state. They are
computed by different planes from different inputs, and a system that lets the first stand in for
the second has moved preference out of the plane that owns it — the failure DISP-3 prohibits on the
execution side, arriving instead through the authoring surface.

Two consequences worth stating, because they are where the blur would occur. Registering a second
adapter unlocks a **control** — nothing more: no default, no recommendation, no ordering, and no
change to what the resolver ranks. And an authoring surface never sorts, scores, or annotates
options by what tends to work; if a creator's choice needs guidance, that guidance is a Decision
concern with its own contract, not a property of the identity model.

---

## Compatibility with existing ADRs

| ADR | Interaction | Verdict |
|---|---|---|
| **ADR-0042** (orchestration freeze) | None of the nineteen paths in `check_frozen_platform.py` is touched; no workflow, dispatcher, relay, lock or usage module changes | **Clear** |
| **ADR-0044** (**AR2** character identity lock) | Discharges AR2's authoring half — scenes reference a character by stable key instead of re-deriving identity from the prompt. AR2's reference-asset ("character sheet") half remains open by D5 | **Advances, does not close** |
| **ADR-0045** (**F3/F7**) | The planner and prompt builder are not modified; no provider identity or provider-specific logic enters either. Identity supplies world state, never routing | **Clear** (IDENT-3 makes it explicit) |
| **ADR-0045** (**F4/F5**) | The knowledge/measurement separation is extended to creator knowledge by IDENT-4 | **Extends by analogy** |
| **ADR-0046** (**X2/X3**) | Execution still consumes an immutable plan; identity arrives *inside* it, never as a live lookup | **Clear** |
| **ADR-0046** (**X5**) | Identity provenance is a value, not an FK — X5's rule applied to a new fact | **Conforms** |
| **ADR-0046** (**X7**) | Status writes are unchanged; D4's correction removes a column from an upsert, adding no new write | **Clear** |
| **ADR-0049** (one-way posture) | `IdentityProfile` gains no tenant or owner field; `GenerateVideoRequest` gains no new field at all. No execution-plane module learns what a user is | **Clear** |
| **ADR-0052 GEN-1** | The `identity_id` upsert enumeration is corrected so the invariant holds by construction, as it already claims | **Repairs** |
| **ADR-0052 GEN-2** | No retry, requeue, or additional provider call. The snapshot removes a would-be second read, not adds one | **Clear** |
| **ADR-0052 GEN-3** | Extended: identity, like ownership, is never inferred or back-filled | **Extends** |
| **ADR-0052 D4-A** | Identity is excluded from the idempotency key; content-derived idempotency stays rejected | **Clear** |
| **ADR-0053** (worker host) | No new worker, no registration or supervision change | **Clear** |
| **ADR-0054 DISP-1** | Identity is **not** a resolver input. `resolve()` keeps its four inputs and `ExecutableAdapters` keeps coming from the registry's keys. DISP-1's principle governs D5 | **Clear; principle inherited** |
| **ADR-0054 DISP-2** | Untouched, and mirrored: identity provenance is asserted by what was captured at acceptance, never re-derived later. The field-classification table gains a third class (D4) | **Extends** |
| **ADR-0054 DISP-3** | No ordering, preference, candidate handling, or fallback is introduced anywhere in this slice | **Clear** |

---

## Rejected alternatives

1. **A live FK from `generations` to `identity_profiles`.** Breaks replay, misstates provenance
   after any edit, and forecloses deletion. Rejected in D2.
2. **Identity as a JSONB body** (the `templates` shape, or a single-column profile table). Suits
   immutable snapshots, not mutable state needing partial edits and per-child key uniqueness.
   Rejected in D1 — and adopted, deliberately, for the *binding* instead.
3. **Rewriting stored v1 payloads to v2.** An irreversible content migration over creator-asserted
   data, performed for a reader's convenience. Rejected in D3.
4. **Back-filling identity for pre-α10.0 generations** by synthesising a profile from the recorded
   seed. This is exactly the inference GEN-3 forbids, applied to a new fact. Rejected in D3.
5. **Letting identity influence adapter selection** — "this character renders better on adapter X",
   or an identity-derived hint reaching the resolver. It would place creator state inside routing,
   make Decision inputs depend on Execution outcomes, and add a fifth resolver input that DISP-1's
   well-formedness condition would then have to account for. Rejected in D1/D5; prohibited by
   IDENT-3 and IDENT-4.
6. **Shipping reference images now, relying on adapters to ignore what they cannot use.** The
   domain docstring sanctions ignoring at the *adapter* layer; that is not a licence to advertise
   the control at the *authoring* layer. Rejected in D5.
7. **Per-child optimistic concurrency.** A snapshot must not straddle two states, and "the world
   changed under me" is the only conflict a creator can act on. Rejected in D1 (the profile root
   carries the version).
8. **Binding identity to projects.** Generations are not project-scoped; the relationship would be
   unusable by the consuming side. Rejected in D1.

---

## Consequences

**Intentionally changed behaviour:**

1. **`generations.identity_id` becomes populated** for requests that name a profile, after being
   `NULL` for the column's entire existence. It is API-invisible (the read projection excludes it)
   but present in provenance and in the durable record.
2. **A new terminal rejection at ingress:** naming a profile that does not exist, or is not the
   caller's, is a uniform 404 — the anti-enumeration behaviour every owner-scoped resource already
   has.
3. **The persisted request payload grows and reaches version 2.** v1 rows remain valid and are read
   forever, or until retirement is decided on its own terms.
4. **A deleted profile leaves a `identity_id` that resolves to nothing** in historical rows. This is
   the intended record, and the reason the value is not an FK.
5. **The execution store stops enumerating `identity_id`** in its upsert — the F4 correction.

**Unchanged:** every generation submitted without an identity behaves byte-for-byte as it does
today; the planner, storyboard, prompt builder, resolver, registry, dispatch, verification, repair,
renderer and probe are not modified; `GenerateVideoRequest` gains no field; the execution plane
learns nothing about users, profiles, or ownership; GEN-2 spend semantics are untouched; no frozen
path is modified.

---

## Load-bearing invariants

Each was tested against one question: *would violating it make the architecture incorrect
regardless of how the system is built?* Statements that merely describe this slice's construction
were left in the decision text or the pre-flight.

1. **IDENT-1 — A generation's world is a value captured at acceptance, never a live reference.**
   Mutating or deleting an identity profile never changes the meaning, provenance, or replay of any
   existing generation. The record is self-contained after the source is gone, which is X5's rule
   applied to creator state, and it is what makes editing and deletion ordinary operations rather
   than history-altering ones.
2. **IDENT-2 — Only honourable state is authorable.** No identity field is accepted through the API
   unless some path in the deployment consumes it. Executable capability, not product taste, sets
   the boundary — the DISP-1 principle applied to the authoring surface, where the failure mode is
   selling a control that is silently discarded. **Capability answers *can*, never *should*:** it
   gates what may be offered and never expresses quality, preference, routing, ranking, cost,
   historical success, or a planner decision, every one of which belongs to the Decision plane.
3. **IDENT-3 — Authoring supplies world state, never policy.** Shot casting, ordering, routing,
   preference and decomposition remain the Decision plane's. Identity never becomes a lever over
   them, and never becomes an input to resolution.
4. **IDENT-4 — Identity is knowledge, never measurement.** A profile records what the creator
   declares exists; it never accumulates execution outcomes, quality feedback, usage statistics, or
   per-adapter tuning, and no Decision-plane behaviour may become a function of such data. This is
   F4/F5's separation extended to creator knowledge, and it forecloses the most plausible future
   coupling between Execution results and Decision inputs.

---

## What acceptance freezes

The pre-flight implements this list and does not reopen it. Each row names the governing section;
**where a row and its section differ, the section wins** — this table is an index, not a second
definition.

| # | Frozen decision | Governed by |
|---|---|---|
| 1 | Creator world state lives in its own owner-scoped bounded context, not in Projects and not only in a request | D1 |
| 2 | Identity Runtime is a **Knowledge**-plane context, consumed by Decision, never by Execution | D1, IDENT-4 |
| 3 | The profile is the aggregate root; children are written through it and it carries the version | D1 |
| 4 | A generation binds identity by **snapshot at acceptance**, never by live reference | D2, IDENT-1 |
| 5 | The snapshot is immutable once written, and is the only world a run or replay ever sees | D2 |
| 6 | Profile edits are never visible to an accepted generation; hard delete is permitted | D2 |
| 7 | An undecodable payload fails loudly; no world is ever guessed | D2 |
| 8 | The payload version covers the whole payload, including the nested snapshot | D2, D3 |
| 9 | Readers accept every version ever written; writers emit only the current one | D3 |
| 10 | No stored payload is ever rewritten, and no legacy generation is given an identity | D3, GEN-3 |
| 11 | Schema change is additive only; there is no data migration | D3 |
| 12 | Identity is a **request fact** — written once by ingress, never by the runtime, never inferred, carried as a value | D4 |
| 13 | `identity_id` is removed from the execution store's upsert enumeration | D4, GEN-1 |
| 14 | Identity does not participate in the idempotency key | D4, ADR-0052 D4-A |
| 15 | A field is authorable only when a consuming path exists in the deployment | D5, IDENT-2 |
| 16 | Reference images are not authorable until an adapter with reference conditioning is registered | D5 |
| 17 | Identity never influences resolution, casting, ordering, or routing | D5, IDENT-3 |
| 18 | Executable capability gates what may be **offered** and never expresses quality, preference, routing, ranking, cost, historical success, or a planner decision — *can*, never *should* | D5, IDENT-2 |
| 19 | Identity Runtime owns no execution history, planner decision, rendering or adapter preference, adapter health, success statistic, or verification outcome | D1, IDENT-4 |

---

## Open questions for the pre-flight

Implementation questions deliberately excluded from this ADR:

1. **The cardinality numbers.** D5 fixes that caps derive from the planner's casting rules; the
   specific limits (the pre-flight proposes four characters, one location, six props) are the
   pre-flight's, and move when the rule's premise moves.
2. **Whether a child's stable key is creator-supplied or derived** from its name, and how it is
   validated — it appears in shot records, so it is durable, but its provenance is not
   architectural.
3. **Child-write granularity** — per-child endpoints versus whole-profile replacement. Both satisfy
   frozen decision 3.
4. **Whether a profile may be created inline with a generation** as a convenience. Not foreclosed;
   not required.
5. **Which slice lifts the location cap.** Per-shot casting is a Decision-plane change with its own
   contract; this ADR only records that the cap is its premise.
6. **When v1 payload retirement becomes worthwhile**, and what evidence would justify it (frozen
   decision 9 makes it a separate decision, not a deadline).

---

## Changelog

| Date | Change |
|---|---|
| 2026-07-31 | **Accepted.** No decision changed at acceptance. The nineteen frozen decisions are now normative for α10.0; the pre-flight's PF1–PF10 were reconciled against them in a recorded pass (pre-flight §14) that found no contradiction and folded in four frozen decisions the pre-flight had not yet carried (7 loud decode failure, 14 idempotency exclusion, 18 *can* not *should*, 19 negative ownership). Review history: an evidence-first grounding that selected the slice, a pre-flight that surfaced the rulings needing governance, and two independent architecture reviews — the first approving the selection in principle and requiring this ADR before implementation, the second approving the decisions and requesting the two clarifications recorded below. |
| 2026-07-31 | Pre-acceptance revision from architecture review; **no decision changed**. **(1)** D5 gains *Capability answers `can`, never `should`* — a prohibition separating the Knowledge fact "this deployment can produce X" from the Decision fact "X is the right choice here", barring authoring from inferring or exposing quality, preference, routing, ranking, cost, historical success, or planner decisions, and stating that a newly registered adapter unlocks a control rather than a default or a recommendation. IDENT-2 carries the same clause. **(2)** D1 gains *What Identity Runtime does not own*, an explicit negative-ownership list (execution history, planner decisions, rendering/adapter preferences, adapter health, success statistics, verification outcomes) with the plane that owns each. Frozen decisions 18–19 index both. |
| 2026-07-31 | Drafted as **Proposed** after the α10.0 grounding selected the slice on evidence and the pre-flight surfaced the rulings needing governance. Records the ordering inversion (grounding → pre-flight → ADR) and subordinates PF1–PF10 to the decisions here. Five decisions: a Knowledge-plane bounded context (D1), snapshot binding with an explicit six-part boundary (D2), payload versioning with read-compatibility and no data migration (D3), identity as a third provenance class plus the GEN-1 upsert correction the grounding uncovered (D4), and capability-bounded authorability (D5). Introduces IDENT-1…IDENT-4. |
