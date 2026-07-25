# Cinematic Storyboard Contract — Planner v2 (α8.7)

> **Type:** Engineering design document (implementation contract). **Not an ADR.**
> The governing decisions live in **ADR-0044** (α8.5x AI runtime architecture) and
> **ADR-0045** (AI runtime core freeze — the three planes). This document is the
> reference engineers consult while building the cinematic planner/storyboard. It
> sits beside `RESOLVER_RUNTIME_CONTRACT.md` (Decision plane) and
> `EXECUTION_RUNTIME_CONTRACT.md` (Execution plane); this is the **Knowledge/Intent
> plane** contract for the planner/storyboard.
>
> **Milestone:** **α8.7 — Planner V2 (Cinematic Storyboard).** α8.6 (execution
> pipeline) is **frozen**; α8.7 is a *new architectural capability*, not an
> increment of α8.6. α8.6 answered "can the system execute a complete generation
> pipeline?"; α8.7 answers "can the system compose a cinematically coherent
> storyboard?"
>
> **Status:** **APPROVED** (sign-off 2026-07-26). Implementation may begin; every
> frozen plane below stays byte-for-byte unchanged (additive evolution).
>
> **One-line purpose:** evolve the planner from "N copies of the same scene" into a
> **cinematic storyboard** — an ordered set of *deliberately different* shots
> (progression, shot taxonomy, camera language, deterministic per-shot seeds) —
> while the resolver, generation, verification, repair, rendering, persistence, and
> execution-runtime planes stay **frozen**.

---

## 0. Why this document exists

Increment 5 (α8.6) shipped the first real vertical slice and, in the live run,
produced a precise, valuable failure: with the **minimal planner**, every shot
carried an identical prompt and seed, so a deterministic provider returned identical
frames and the **timeline duplicate gate correctly rejected the video**. Nothing was
mis-blamed — the generator, resolver, and renderer were fine; the *planner* produced
a weak storyboard and the *verifier* caught it. That is the separation of concerns
ADR-0045 froze, working as designed.

The fix is **not** "add `seed + index`." Randomising the seed of an unchanged scene
would paper over the defect. The real change is that each shot must represent a
**different cinematic moment** of the *same* scene and identity. Planner v2 makes
scene progression and shot taxonomy first-class, and derives a deterministic
per-shot seed as a consequence of each shot being its own generation request.

This contract is the analogue, for the planner, of the specifications we wrote for
the Resolver and Execution planes — so Planner v2 is as well-defined as they are.

---

## 1. The one-sentence boundary (unchanged)

> **The Planner decides _what_ each shot is. The Prompt Builder decides _how_ to
> express it to a generator. The Resolver decides _who_ renders it. Nothing in the
> planner ever contains provider-facing wording or provider names.**

Increment 5 respected this. Planner v2 must keep respecting it, which is exactly why
cinematic intent becomes a **structured value object** (`ShotIntent`) rather than
free text baked into `prompt_text` inside the planner. See **CS-8** (§11) for the
formal "no provider language in the planner" invariant.

---

## 2. The pipeline delta

Today (α8.6):

```
Prompt → Planner → Storyboard → (prompt_text) → Prompt Builder → Prompt
```

α8.7 inserts a typed intermediary — `ShotIntent` — so the planner emits *structured
cinematic intent* and the Prompt Builder remains the **only** component that turns
intent into generator-facing text:

```
Prompt → Planner → (StoryArcTemplate) → Storyboard → ShotIntent → Prompt Builder → Prompt
```

- The **Identity Runtime** (world state) is unchanged and shared across all shots.
- The **scene** is unchanged across all shots (same subject, same world).
- **Only the cinematic intent varies** shot-to-shot.

---

## 3. `ShotIntent` — the new value object (own module — Q6)

A frozen, pure value object produced by the planner and consumed by the Prompt
Builder. It carries **intent**, never provider wording. It lives in its own
first-class domain module so it can grow (validation, serialization, an ML/AI-assisted
planner) without touching storyboard generation:

```
app/domain/generation/
    plan.py
    storyboard.py
    shot_intent.py        # ← new: ShotIntent + the controlled enums
```

| field | type | meaning |
|-------|------|---------|
| `shot_type` | `ShotType` | the taxonomy slot / framing scale (establishing … ending) |
| `camera` | `Camera` | **camera** behaviour (static, push-in, pan, track, crane, …) |
| `movement` | `Movement` | **subject** movement (still, walking, turning, …) — *not* camera movement |
| `subject_focus` | `str \| None` | which identity element the shot centres on (character/location/prop id) |
| `emotional_purpose` | `str` | narrative beat (arrival, exploration, intimacy, resolution) |
| `transition_from_previous` | `Transition \| None` | edit intent from the prior shot |

`ShotIntent` is attached to each `Shot` (planner output). The storyboard passes it to
`build_prompt(...)`, which maps the enums to controlled descriptor phrases and
appends them as the shot's `modifiers` (the seam already exists in
`prompt_builder.build_prompt(..., modifiers=...)`). Enum→phrase mapping lives in the
**Prompt Builder**, not the planner.

> **CS-A:** all taxonomy/camera/movement/transition vocabularies are **controlled
> enums** in `shot_intent.py`. This keeps them testable, diff-able, and free of
> provider-specific phrasing.
>
> **Framing/composition note:** framing scale is expressed by `shot_type`
> (establishing/wide/medium/close-up/detail); composition is an emergent property of
> `shot_type` + `subject_focus` and is **not** a separate enum in α8.7 (it can be
> promoted to its own dimension later without breaking the contract).

---

## 4. Shot taxonomy — `ShotType` (Q3, small & controlled)

```
ESTABLISHING   # sets the world/scene
WIDE           # full context, subject in environment
MEDIUM         # subject-focused, mid-distance
CLOSE_UP       # emotional / detail proximity
DETAIL         # texture / insert shot
ACTION         # the beat of motion / event
ENDING         # resolution / pull-away
```

## 5. Camera, subject movement, transition (Q3)

`Camera` — camera behaviour (deliberately distinct from subject movement):

```
STATIC  PUSH_IN  PULL_BACK  PAN_LEFT  PAN_RIGHT  TRACK  CRANE  HANDHELD
```

`Movement` — **subject** movement (what the subject does, not the lens):

```
STILL  WALKING  RUNNING  TURNING  LOOKING  INTERACTING
```

`Transition` — edit intent from the previous shot:

```
CUT  DISSOLVE  FADE  MATCH_CUT
```

All three map to controlled descriptor phrases **in the Prompt Builder**, never the
planner. Vocabularies are kept small on purpose and grow only by amendment here.

## 6. Story arc — data-driven `StoryArcTemplate` (Q2)

Shot count is still derived from duration/per-shot, but the count selects a
**template** — an ordered taxonomy arc — rather than `[same] * N`. Templates are
**data**, not hardcoded planner branches:

```
Planner → select StoryArcTemplate (by kind + shot count) → instantiate ShotIntent[]
```

Initial `cinematic` templates:

| shots | ordered beats (ShotType) |
|-------|--------------------------|
| 3 | ESTABLISHING → MEDIUM(focus) → ENDING(resolve) |
| 5 | ESTABLISHING → MEDIUM → CLOSE_UP → DETAIL → ENDING |
| 6 | ESTABLISHING → WIDE → MEDIUM → CLOSE_UP → ACTION → ENDING |

A `StoryArcTemplate` is `{ kind, beats: [ShotType + default camera/movement/purpose] }`.
Because arcs are data, later kinds — `interview`, `tutorial`, `cooking`,
`cinematic_trailer`, `product_showcase`, `documentary` — slot in **without changing
planner logic**. (Behaviour for shot counts outside the defined templates is a
follow-up detail; α8.7 targets the `cinematic` kind with the 3/5/6 arcs above.)

## 7. Transition intent

Each non-first shot may declare a `Transition` describing the edit from the previous
shot. This is **planning intent** recorded in provenance; it does not yet drive
ffmpeg (crossfade wiring already exists in the renderer and can consume it later).

## 8. Temporal continuity

The scene, identity, and global style are invariant across shots; the arc must read
as one continuous scene, not disjoint images. Continuity is expressed by keeping the
identity fragment + scene subject constant while only the cinematic dimensions move.

## 9. Deterministic, position-independent shot IDs + seed (Q4)

- Each shot has a **stable, semantic `shot_id`** that **never depends on array
  position**, e.g.:

```
scene-001-establish
scene-001-medium
scene-001-closeup
scene-001-ending
```

  These remain stable even if another shot is later inserted or the arc changes.
- Per-shot seed (approved):

```
shot_seed = H(project_seed, shot_id)      # blake2b(project_seed || shot_id), truncated to signed 64-bit
```

  Not `project_seed + index`: hashing keeps seeds stable under reordering/insertion
  and gives every shot its own reproducible seed because **each shot is its own
  generation request** — not to "fix duplicates," but because that is what a shot *is*.

## 10. Prompt enrichment

The Prompt Builder gains an overload that accepts a `ShotIntent` and expands its
enums into controlled descriptor phrases appended after the identity fragment. The
planner never writes these phrases; it only chooses enum values. `prompt_text` for a
close-up therefore differs from an establishing shot **because the intent differs**,
giving even a deterministic provider a legitimate reason to produce different frames.

---

## 11. Planner invariants (Planner v2)

- **CS-1** The planner emits `ShotIntent` per shot; it never emits provider-facing
  prompt strings and never names a provider (ADR-0045).
- **CS-2** Identity Runtime + scene are constant across all shots of a scene; only
  cinematic intent varies.
- **CS-3** Shot count → a `StoryArcTemplate` (a deliberate arc), never a repeated
  single shot.
- **CS-4** `shot_id` is stable, semantic, and position-independent;
  `shot_seed = H(project_seed, shot_id)`.
- **CS-5** The planner is pure and deterministic: same request → same plan.
- **CS-8 (new — semantic-intent boundary):** the planner produces **semantic
  cinematic intent, never provider/render language.** Terms such as
  `photorealistic`, `8k`, `ultra detailed`, `masterpiece`, `cinematic lighting`,
  and generator/tool names (`SDXL`, `Flux`, `ComfyUI`, …) are **illegal** in planner
  output; they belong **exclusively** to the Prompt Builder. Planner says
  `CLOSE_UP`; the Prompt Builder says "…photorealistic cinematic close-up…". This is
  enforced by a banned-lexicon unit test over planner/`ShotIntent` output.

## 12. Storyboard invariants

- **CS-6** The storyboard is a pure function of the plan (unchanged property).
- **CS-7 (refined — the anti-regression invariant):** **every pair of adjacent shots
  must differ in at least one _primary_ cinematic dimension _and_ at least one
  _secondary_ variation.** Difference is measured on **intent**, not wording or seed.
  - **Primary** (framing/composition/camera/subject): `shot_type` (framing scale),
    `camera`, `subject_focus`.
  - **Secondary** (motion/meaning/edit): `movement`, `emotional_purpose`,
    `transition_from_previous`.

  Requiring a primary change **and** a secondary change means two shots that differ
  *only* by `transition` (a secondary) are **illegal** — structurally preventing the
  Increment 5 duplicate-scene defect by any route. A storyboard violating CS-7 is a
  planner bug and fails a unit test **before any generation runs**.

## 13. Determinism & reproducibility

Same request twice → identical plan, identical `ShotIntent` sequence, identical
`shot_id`s and `shot_seed`s, identical `prompt_text`. (Generated *pixels* may still
differ for a non-deterministic provider; orchestration is deterministic.) This is the
same reproducibility bar Increment 5's e2e already asserts.

---

## 14. Versioning

- Bump **`PLANNER_VERSION`** (shot model + `ShotIntent`/arc changes) and
  **`STORYBOARD_VERSION`** (ShotIntent-driven prompts).
- Bump **`PROMPT_BUILDER_VERSION`** (new ShotIntent overload + enum→phrase mapping).
- All three already flow through `GenerationProvenance` and are persisted unchanged —
  **no schema change**; provenance simply records the new versions.

## 15. Golden strategy (Q3 of the previous round)

- **Freeze Golden V1** ("Minimal Planner" — the architecture-proving milestone) by
  moving it to `tests/fixtures/golden/v1/fox_snowy_forest.json`, kept as a
  **historical artifact only** (no longer replayed by the live planner).
- **Golden V2** ("Cinematic Planner") becomes the **active** regression suite at
  `tests/fixtures/golden/v2/fox_snowy_forest.json`, asserting the full `ShotIntent`
  arc + per-shot seeds + enriched prompts.
- The active unit golden test and the e2e slice assert V2. We do not keep two live
  planners; V1 is history, V2 is production.

## 16. What stays frozen (non-negotiable)

No changes to: the **Resolver**, **Image Generator** port/adapters, **Verifier**,
**Repair**, **Timeline Verifier**, **Slideshow Renderer**, **Video Probe**,
**Execution Runtime store**, **DB schema/migrations**, or the **CI destructive-
migration guard**. The e2e slice from Increment 5 is the proof: it must keep passing
(now with *genuinely* distinct frames from the real provider path, not just the
offline double).

## 17. Test plan

1. **Unit — `shot_intent.py` enums** and the Prompt Builder `ShotIntent` overload
   (enum→phrase mapping is deterministic).
2. **Unit — CS-7 invariant**: adjacent shots differ in ≥1 primary **and** ≥1
   secondary dimension (property-style over several prompts/durations).
3. **Unit — CS-8 banned-lexicon guard**: planner/`ShotIntent` output contains no
   provider/render language.
4. **Unit — deterministic ids/seeds**: semantic `shot_id`s and
   `shot_seed = H(project_seed, shot_id)` stable under reordering/insertion.
5. **Golden V2 unit test** (fast, no DB): planner+storyboard match
   `v2/fox_snowy_forest.json` byte-for-byte; V1 retained as a frozen historical file.
6. **E2E (Stage 13)**: the existing slice now yields distinct frames that **pass** the
   timeline duplicate gate; optionally a gated live-provider run proves real
   deterministic providers no longer duplicate.

## 18. Acceptance criteria

- The fox scenario produces a 6-shot **arc** (establishing→…→ending), not 6 identical
  shots; adjacent shots satisfy CS-7 (primary + secondary difference).
- With a deterministic provider, frames are distinct and the timeline gate passes.
- Reproducibility holds (identical plan/intent/seeds/prompts across two runs).
- No provider/render language appears in planner output (CS-8).
- Provenance records the bumped planner/storyboard/prompt-builder versions.
- Golden V1 preserved as history; Golden V2 is the active regression.
- Every frozen plane is untouched; Increment 5's e2e still green.

## 19. Non-goals (deferred)

- Editing/transition rendering (ffmpeg consuming `Transition`), music/subtitles.
- LLM-driven scene decomposition — Planner v2 is a deterministic, rules-based arc; an
  LLM/AI-assisted planner can slot in behind the same `ShotIntent` contract later.
- Multi-scene storyboards (α8.7 is single-scene, multi-shot).
- Additional arc kinds (interview/tutorial/…): enabled by the data-driven template
  design but out of scope for α8.7.

## 20. Resolved sign-off decisions (2026-07-26)

| # | Question | Decision |
|---|----------|----------|
| 1 | Milestone label | **α8.7 — Planner V2** (α8.6 frozen; new capability, not an increment) |
| 2 | Progression templates | **Data-driven `StoryArcTemplate`** (arcs are data, not planner branches) |
| 3 | Taxonomies | **Approved, small & controlled** — `ShotType`, `Camera`, `Movement` (subject), `Transition` as in §4/§5 |
| 4 | Shot id + seed | **`blake2b` `shot_seed = H(project_seed, shot_id)`** with stable, semantic, position-independent `shot_id`s (`scene-001-establish`, …) |
| 5 | Cinematic difference (CS-7) | **≥1 primary change _and_ ≥1 secondary variation** between adjacent shots (primary: framing/`shot_type`, camera, subject_focus; secondary: movement, emotional_purpose, transition) |
| 6 | `ShotIntent` placement | **Own module** `app/domain/generation/shot_intent.py` (first-class domain concept) |
| + | Extra invariant | **CS-8** — planner produces semantic cinematic intent, never provider/render language |

## 21. Change log

| Date | Author | Change |
|------|--------|--------|
| 2026-07-25 | curator | Initial DRAFT — ShotIntent VO, taxonomy, camera language, progression templates, transition intent, deterministic seeds, planner/storyboard invariants (incl. CS-7), golden V1-freeze/V2-active, open sign-off questions. No code. |
| 2026-07-26 | curator | **APPROVED.** Milestone set to **α8.7**; templates made **data-driven** (`StoryArcTemplate`); taxonomies finalised (`Camera` = camera behaviour, `Movement` = *subject* movement, `Transition` small set); shot ids made **semantic + position-independent** with `blake2b` seed; **CS-7 refined** to primary + secondary difference; `ShotIntent` in its **own module**; added **CS-8** (no provider/render language in the planner). Ready to commit. No code yet. |
