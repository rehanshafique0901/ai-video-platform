# ADR-0043 — The Render Composition Boundary: Composition Is a Pure `Timeline + MediaAssets → Video` Transform

**Status:** Accepted (a **governance** decision — docs-only. **No application code,
no schema migration, no runtime behaviour change, no app version bump.** Mirrors the
ADR-0041/ADR-0042 docs-only precedent.) Every α8.4e composition slice cites it.

**A boundary, not a freeze.** ADR-0042 *froze* the orchestration core because it was
feature-complete. The render layer is the opposite: α8.4e is about to **grow** it
(audio mixing, transitions, effects, quality tuning). So this ADR does **not** ship a
guard, a CI job, or a `CODEOWNERS` entry, and it does **not** freeze any render module.
It draws a **design boundary** — what belongs *inside* composition versus what stays
downstream — so the render layer can evolve without eroding the separation earned
across α8.4a–d. It is the contract the α8.4e pre-flight is evaluated against, exactly
as α8.4a–d were evaluated against ADR-0042.

**The inflection point.** α8.4a–d were all **downstream** of a finished render: ingest
(α8.4a), render (α8.4b), enrich (α8.4c), derive previews (α8.4d). α8.4e is the **first
post-freeze slice to change *how media is composed*** rather than adding a downstream
capability. Before touching the render domain, name its boundary — the same discipline
that made ADR-0042 valuable for orchestration.

**Builds on:** **ADR-0038** (Timeline self-contained OCC aggregate — the composition
input), **ADR-0039** (RenderJob orchestration aggregate — the render lifecycle),
**ADR-0037** (MediaAsset — the canonical media boundary), **ADR-0042** (orchestration
freeze — still binding; composition sits *outside* it). **Refines:**
`docs/architecture/CONTENT_GENERATION_PIPELINE.md` §13 (slice sequencing);
consolidates the α8.4b/α8.4c/α8.4d invariants into a single domain boundary.

**Wave:** Phase 3, governance slice after **α8.4d**, before **α8.4e**.

---

## Context

The dependency graph the media slices converged on is:

```
Provider → Completion → Generated-Media Ingestion → MediaAsset
        → Timeline → Render Engine → Output MediaAsset → Enrichment → Derived MediaAssets
```

Two properties made this clean and must not decay when the render layer grows:

1. **`MediaAsset` is the canonical boundary.** Nothing provider-specific (URLs,
   request IDs, provider job IDs, checkpoints, webhook payloads) flows past ingestion.
   The renderer already consumes only Timeline data + `MediaAsset` bytes (W8.4b.2).
2. **Each stage is a pure, deterministic transform** with a single responsibility:
   render composes, enrichment derives, export delivers — and none reaches sideways
   into another's concern.

α8.4e (audio mixing, transitions, effects, render quality tuning) is the first slice
that legitimately *expands* the renderer's inputs and behaviour. The failure mode is
**scope bleed**: composition starts reading orchestration/provider state "just this
once", or enrichment starts doing composition, or export starts re-encoding rendered
media. This ADR names the boundary so those pressures surface as conscious decisions.

---

## Decision

### D1 — What the render (composition) layer *is*

The composition domain is exactly these components (they are the growth surface for
α8.4e — editable, **not** frozen):

| Component | Path |
| --- | --- |
| `IRenderer` port + neutral render DTOs | `backend/app/application/interfaces/renderer.py` |
| FFmpeg renderer adapter | `backend/app/infrastructure/render/ffmpeg_renderer.py` |
| `ProcessRenderJob` (render worker body) | `backend/app/application/use_cases/render/process_render_job.py` |
| `RenderWorker` (poll ingress) | `backend/app/application/use_cases/render/render_worker.py` |
| Render lifecycle events | `backend/app/application/use_cases/render/_events.py` |
| Timeline aggregate (read model for composition) | `backend/app/domain/timeline/…` (ADR-0038) |
| RenderJob aggregate (render lifecycle) | `backend/app/domain/render/…` (ADR-0039) |

α8.4e is expected to **extend** `RenderSpec` / `IRenderer` / `FfmpegRenderer` (e.g. add
audio tracks, transition/effect descriptors, quality knobs) and to read more of the
Timeline. That is allowed and anticipated — the render layer is not frozen.

### D2 — The composition boundary principles (RC1–RC6)

These are the invariants α8.4e (and every later render change) must preserve. They
generalize the α8.4b/α8.4c/α8.4d invariants into one domain contract.

- **RC1 — Timeline is the sole composition input.** What appears in the output, in
  what order, trimmed how, with what tracks/effects, is derived **only** from the
  Timeline (its tracks, clips, and their referenced `MediaAsset` ids) plus injected
  configuration. The renderer takes no "side channel" input. (Generalizes W8.4b.2.)
- **RC2 — Renderers never access orchestration or provider state.** No render
  component reads or mutates `WorkflowRun`, checkpoints, the completion lifecycle,
  provider adapters, `provider_job_id`s, request IDs, or webhook payloads. Composition
  is provider-agnostic and orchestration-agnostic. (Generalizes W8.4b.1 + upholds
  ADR-0042.)
- **RC3 — Composition is deterministic from `Timeline + MediaAssets + configuration`.**
  Given the same Timeline, the same referenced `MediaAsset` bytes, and the same render
  configuration, the composition is reproducible. No hidden state, wall-clock
  branching, or render-history dependence. (This is what makes deterministic output
  keys and idempotent re-render safe.)
- **RC4 — Composition and enrichment are disjoint.** The renderer **composes** a
  Timeline into an output `MediaAsset`; it never derives thumbnails/previews/waveforms.
  Enrichment **derives** artifacts from a finished `MediaAsset`; it never composes,
  trims, mixes, or re-times media (W8.4c.1 / W8.4d.1). Neither reaches into the other.
- **RC5 — Rendered media is immutable downstream.** Enrichment, export, and publishing
  **read** an output `MediaAsset`; they never re-encode, re-compose, or overwrite it.
  A new composition is a new render job producing a new output `MediaAsset`, never an
  in-place edit of an existing one.
- **RC6 — Renderer purity.** Given identical Timeline, referenced `MediaAsset`s,
  `RenderSpec`, **renderer version**, and configuration, the renderer shall produce
  **functionally equivalent** output. This does *not* require bit-for-bit identical
  encoding (different FFmpeg/codec builds may vary slightly) — it makes reproducibility
  an explicit architectural **goal**, not just an implication of RC3. It is what makes
  retries safe, output caching sound, distributed rendering valid, and a future GPU (or
  otherwise substituted) renderer a drop-in with no orchestration change. Any behaviour
  that would break functional equivalence for the same inputs+version (hidden global
  state, wall-clock/random branching that changes the visible result, machine-specific
  output) is outside the boundary.

### D3 — What α8.4e may change vs must not

**May change (inside the boundary):** `RenderSpec` / `RenderInput` / `RenderResult`
shapes, the `IRenderer` contract, the FFmpeg adapter's filter graph (audio mixing,
transitions, effects, quality/bitrate tuning), and how `ProcessRenderJob` reads the
Timeline (e.g. audio tracks, per-clip effects) — provided RC1–RC6 hold.

**Must not change:** anything in the ADR-0042 frozen orchestration surface (the freeze
guard still runs and must stay green with zero overrides); the `MediaAsset` canonical
boundary (ADR-0037); the enrichment layer's role (ADR-0043 RC4); export/publishing
independence (RC5). If α8.4e appears to need an orchestration change, that is an
ADR-0042 signal — stop and revisit, exactly as α8.4a–d did.

### D4 — This is deliberately *not* mechanically enforced (yet)

Unlike ADR-0042's `check_frozen_platform.py`, this ADR ships **no guard**. The render
layer is under active development; a tripwire now would fight the α8.4e work it is
meant to guide. Enforcement is by **pre-flight review against RC1–RC6** and by the
still-active orchestration freeze guard at the render/orchestration boundary. Should
the render layer later reach feature-completeness (analogous to α8.3 for
orchestration), a *future* ADR may freeze it and add a guard — this ADR is the design
contract that would make that freeze mechanical later.

### D5 — What this ADR intentionally does **not** do

No app version bump, no migration, no code change, no new guard/CI/CODEOWNERS, and it
does **not** freeze any render module. It does not weaken ADR-0042 (orchestration stays
frozen). It does not pre-judge α8.4e's design forks — it only bounds them.

---

## Consequences

**Positive.** α8.4e can be evaluated by a single question — "does this stay within
RC1–RC6, and clear of the ADR-0042 frozen surface?" Composition can grow richer
(audio, transitions, effects) while the `MediaAsset` boundary, enrichment/export
separation, and deterministic reproducibility stay intact. The render layer gets the
architectural clarity of ADR-0042 without the friction of a freeze it isn't ready for.

**Negative / cost.** RC1–RC6 are review-enforced, not machine-enforced, so they rely on
the pre-flight discipline already established. A genuinely cross-cutting composition
need (e.g. rendering that must consult non-Timeline state) would require revisiting this
ADR rather than quietly widening the input.

**Neutral.** This ADR consolidates W8.4b/W8.4c/W8.4d into a named domain boundary; those
per-slice invariants remain valid and are now framed as instances of RC1–RC6.

---

## Change log

- **2026-07-24 — RC6 (renderer purity) added.** Made reproducibility an explicit
  architectural goal — same Timeline + `MediaAsset`s + `RenderSpec` + renderer version +
  configuration ⇒ *functionally equivalent* output (not bit-identical). Enables safe
  retries, output caching, distributed rendering, and drop-in GPU/alternate renderers.
  Folded in before the α8.4e pre-flight; no code/behaviour/schema change.
- **2026-07-24 — Accepted.** Render composition boundary defined immediately after
  `v0.4.28-phase3-alpha8.4d`, before the α8.4e pre-flight. Defines the composition
  domain (D1), boundary principles RC1–RC6 (D2), the α8.4e change envelope (D3), the
  deliberate absence of a guard (D4), and scope (D5). No code/behaviour/schema change.
