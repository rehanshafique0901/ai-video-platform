# Phase 3 — α10.0 Grounding: selecting the slice after α9.9

> **Status:** Discovery + selection. **No implementation, no schema, no ADR.** This document
> selects the next vertical slice and records the facts that constrain it; it decides nothing.
>
> **Outcome:** the selection was accepted. The contract is
> [`PHASE3_ALPHA10_0_PREFLIGHT.md`](./PHASE3_ALPHA10_0_PREFLIGHT.md) — **Approved 2026-07-31** —
> governed by [**ADR-0055**](../decisions/ADR-0055-identity-runtime-authoring.md) — **Accepted
> 2026-07-31**. Where this report and those documents differ, they win; the findings below are
> preserved as the evidence they were, not as current architecture.
>
> **Baseline:** `v0.4.52-phase3-alpha9.9` (merged, tagged, documentation synchronised). α9.9 is
> frozen and is not reopened here.
>
> **Method:** direct inspection of the repository — domain / application / infrastructure / api
> layers, Alembic migrations `0001`–`0016`, the provider catalogue, the CI gate, and the accepted
> ADRs. Every claim below names a file, line, column, table, or contract section.

---

## 1. Where the platform stands after α9.9

The generation pipeline is real and complete end to end. `GenerateVideo`
(`backend/app/application/use_cases/generation/generate_video.py`) plans a shot arc, builds a
storyboard, resolves a capability to an ordered candidate list, dispatches the winner through the
α9.9 registry, verifies each frame, repairs by re-seeding, assembles the accepted frames with
ffmpeg into an `mp4`, probes it, and writes the whole run — shots, assets, resolution ledger,
outbox events — to Postgres and object storage. α9.7 put an owner-scoped HTTP resource in front of
it; α9.8 gave it a worker process to run in; α9.9 made the adapter it dispatches to, and the
provenance it records about that adapter, honest.

What the creator can actually ask for, however, is one paragraph of prose and a colour palette.
`GenerationCreateRequest` (`backend/app/api/v1/schemas/generations.py:28–50`) exposes exactly two
identity fields — `seed` and `global_style` — and the codec builds the runtime identity from
nothing else:

```105:108:backend/app/application/use_cases/generation/request_codec.py
    return GenerateVideoRequest(
        prompt=spec.prompt,
        identity=IdentityProfile(seed=spec.seed, global_style=GlobalStyle(spec.global_style)),
        generation_id=generation_id,
```

## 2. The candidate set

Six rows remain in the *Remaining roadmap* table of
[`PLATFORM_STATUS.md`](../architecture/PLATFORM_STATUS.md), plus the deferrals α9.9 itself recorded
in §10 of its pre-flight. (The top-level `ROADMAP.md` stops at α7.4 and is stale as a planning
surface; `PLATFORM_STATUS.md` is the live one.)

| Candidate | Substrate that exists | Why it is not α10.0 |
|---|---|---|
| **α8.4f** render composition — transitions, effects, subtitle burn-in | `transitions` table + `clips.transition_in_id` / `transition_out_id` / `effects` columns (`0001_baseline.py:744–752`, `810–812`); read-only on `ClipPublic` | **Blocked, and the blocker is a different slice.** No write path exists for any of those columns — they are absent from `ClipCreateRequest` / `ClipUpdateRequest` and from `CreateClip` / `UpdateClip`. α8.4f cannot compose what nobody can author, so the authoring slice would have to come first |
| **α8.5b.4′** push / websocket notifications | `INotifier` port + email adapters (α9.5) | Needs new persistence for device tokens *and* an external push provider account. Low creator value per unit of external risk |
| **α8.6d″** Instagram / Facebook destinations | Proven `IDestinationPublisher` seam, two real adapters | **Externally gated** on App Review plus public-URL / resumable-upload prerequisites. The risk lives outside the codebase, so it cannot be scheduled honestly |
| **Identity-Runtime authoring** | See §4 — the entire domain model, the prompt path that consumes it, and a reserved column | **Selected.** See §3 |
| **Worker deployment artefacts** | α9.8 entrypoint, selector, liveness markers, exit-code contract | Real and needed, but it is an operations slice: no Dockerfile, compose, or manifest exists anywhere in the repo. It ships no creator-visible capability and is not a vertical slice |
| **Mid-run generation cancellation** | `cancelled` status, queued-only cancel, lease + heartbeat | Needs a spend-accounting ruling first (what is owed for a part-run), which GEN-2 does not settle. The roadmap itself says it "needs explicit design rather than an incremental patch" |
| **α9.9 deferrals** — second real adapter, candidate walking, usage metering | Registry, ordered candidate list, unused `ExecutionOutcome.FALLBACK` | Walking is ADR-0054 frozen decision 15 and is gated on three prerequisites it names: usage metering, a whole-run wall-clock cap, and a GEN-2 ruling on second-provider spend. Scheduling it immediately after α9.9 would reopen a decision that was just frozen |

## 3. The selection — Identity-Runtime authoring

**Four facts, none of them a matter of taste, point at this slice.**

**The seam is reserved, in schema, and inert.** `generations.identity_id` has existed since
migration `0012` (`0012_execution_runtime.py:104`) and is written `None` on every run that has ever
executed (`generation_ledger_repository.py:179`). This is the same shape as the
`PublishGenerationAssets` seam that became α8.8: a column named and left for the slice that would
fill it.

**α9.7 was designed to be extended by exactly this slice, and said so in code.** The request codec
does not merely tolerate a future identity payload — it rejects unknown keys specifically so that
this extension stays additive:

```11:16:backend/app/application/use_cases/generation/request_codec.py
**v1 scope.** :class:`GenerationRequestSpec` is deliberately *flat and scalar*. The identity
built from it carries only `seed` + `global_style` — no characters, locations, props, or
reference images. Full Identity-Runtime authoring needs its own persistence and API surface and
is a separate slice; folding it in here would triple this one. :func:`decode_spec` therefore
**rejects unknown keys** rather than ignoring them, so a later identity slice extends the
payload additively instead of silently reinterpreting rows written by this one.
```

**The capability is already built and already consumed — it is only unauthorable.**
`backend/app/domain/generation/identity.py` defines `Character`, `Location`, `Prop`,
`ReferenceImage` and `IdentityProfile` in full, each with a deterministic `prompt_fragment()`. The
planner casts characters and a location into every `Shot` (`planner.py:120–123`), and the prompt
builder composes their fragments into the string the adapter actually receives
(`prompt_builder.py:83–96`). None of it is dead code — it is exercised by the golden fixtures and
the demo script. The only missing pieces are a table to keep a profile in and a route to author it
with. That is an unusually favourable ratio of shipped substrate to new work.

**It is the product's central promise.** A tool that generates a six-shot video in which the
protagonist's face, clothing and setting change every three seconds has not made a video. Character
and world consistency is the difference between a slideshow of related images and a story, and it
is the one thing this pipeline is structurally ready to deliver.

Two further properties make it the right *shape* for a slice: it has **no external dependency
whatsoever** — no new provider, no API key, no app review, no account — and it touches **no frozen
path**. `check_frozen_platform.py` guards nineteen orchestration paths, none of them under
`app/domain/generation/` or the generation ingress.

## 4. What exists today, precisely

| Concern | State | Evidence |
|---|---|---|
| Identity value objects | Complete and immutable | `domain/generation/identity.py:62–189` |
| Characters reach the prompt | Yes — name, age, appearance, clothing, accessories | `prompt_builder.py:85–88`, `identity.py:96–106` |
| Location reaches the prompt | Yes — the **first** location only, anchoring every shot | `planner.py:121–123`, `prompt_builder.py:90–93` |
| Props reach the prompt | Yes — **every** prop, in **every** shot | `prompt_builder.py:95–96` |
| Project look (camera / lighting / palette / style) | Reaches the prompt | `prompt_builder.py:104–111` |
| Per-shot seed derivation | Deterministic from the profile seed | `shot_intent.py:407–416` |
| Reference images | Plumbed end to end **and ignored by the only adapter** | `identity.py:170–189` → `storyboard.py:51` → `generate_video.py:368` → `pollinations_image_generator.py:45–47` |
| Durable storage of any of it | **Does not exist** — no `characters`, `locations`, `props`, or profile table in any migration | verified across `0001`–`0016` |
| Authoring API | **Does not exist** | no route, use case, or repository references identity |
| `generations.identity_id` | Exists, always `NULL` | `0012:104`, `generation_ledger_repository.py:179` |

## 5. Seven findings that constrain the design

1. **`identity_id` is currently classified as runtime-owned, and the runtime nulls it.** It appears
   in the execution store's upsert enumeration — `identity_id = EXCLUDED.identity_id`
   (`generation_ledger_repository.py:61`) — while `begin()` always supplies `None`. If ingress
   wrote the column today, the first `begin()` of the run would erase it. Under GEN-1 the column
   must be reclassified and removed from that list.
2. **`decode_spec` hard-rejects any version but `1`** (`request_codec.py:77–82`), so a richer
   payload requires a version bump *with* an explicit compatibility path for rows α9.7 already
   wrote.
3. **The planner casts every character into every shot** (`planner.py:120`), and the prompt builder
   emits every prop (`prompt_builder.py:95`). Authoring more world state therefore degrades every
   prompt monotonically unless cardinality is bounded.
4. **Only the first location is ever used** (`planner.py:123`). A second authored location is inert.
5. **The sole executable adapter ignores reference images.** `PollinationsImageGenerator` accepts
   `reference_image_refs` and discards them; no img2img-capable adapter is registered, and
   `comfyui.flux_schnell` has no code.
6. **Identity is mutable creator state; a generation is an immutable record.** The catalogue solved
   the same tension with `catalogue_version` + `manifest_digest` pinning, and ADR-0046 X5 makes the
   rule general: provenance is carried as **values, not FKs**.
7. **No byte-upload surface exists** for creator-supplied images into object storage; the media
   library (α9.2) wraps `media_assets` but the generation plane has no upload route.

## 6. What this report is not

No schema, no API shapes, no ADR, and no authorisation to write code. Findings 1–7 are inputs to
the pre-flight's rulings, not decisions themselves. The pre-flight resolved them (PF1–PF10) and
ADR-0055 ratified the decisions they implied; implementation opens on the governance baseline
commit that carries all three documents, not on this report.
