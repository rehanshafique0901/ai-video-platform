# Phase 3 — α8.4e Pre-flight: Render Composition — Audio Mixing

> Status: **SIGNED OFF.** First slice to exercise **ADR-0043** (render composition
> boundary, RC1–RC6). Companion to `PHASE3_ALPHA8_4d_PREFLIGHT.md`.
>
> **Rulings:** Gate 1 (ADR-0042) PASS · Gate 2 (ADR-0043) PASS · **A1** (audio only)
> · **B1** (deterministic `amix`: `clip.volume` + `track.muted` + audio tracks + video-clip
> audio; **no ducking / side-chain / normalization / fades / DSP**) · **C1** (extend the
> render contract; renderer never reaches back into the Timeline) · **D1** (keep
> sequential-concat semantics; no absolute-time placement) · subtitle burn-in **deferred
> (α8.4f)** · loudness normalization **deferred** · **add invariant W8.4e.1**.

---

## 0. Two mandatory gates (answered first)

### Gate 1 — ADR-0042 (orchestration freeze)

> **Does α8.4e touch any frozen orchestration module, checkpoint contract,
> orchestration state, provider protocol, or workflow lifecycle?**

**Answer: No.** α8.4e changes only the **render composition** layer (`IRenderer` +
FFmpeg adapter + `ProcessRenderJob`'s Timeline read). It reads Timeline audio data +
`MediaAsset` bytes and produces an output `MediaAsset` — the same seam α8.4b already
uses. The freeze guard must stay green with **zero override markers**.

### Gate 2 — ADR-0043 (render composition boundary, RC1–RC6)

Every proposed change must satisfy RC1–RC6. Explicit matrix:

| Feature | RC1 | RC2 | RC3 | RC4 | RC5 | RC6 | Result |
|---|:--:|:--:|:--:|:--:|:--:|:--:|---|
| **Audio mixing (α8.4e)** | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | **Allowed** |
| Crossfades / transitions | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | Allowed **but α8.4f** (blocked on α6.4 authoring) |
| Color grading / effects | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | Allowed **but α8.4f** (blocked on α6.4 authoring) |
| Provider callback during render | ✗ | ✗ | ✗ | — | — | — | **Reject** (violates RC1/RC2/RC3, ADR-0042) |

RC1 Timeline-only input · RC2 no orchestration/provider state · RC3 deterministic from
Timeline+MediaAssets+config · RC4 composition ≠ enrichment · RC5 rendered media
immutable downstream · RC6 renderer purity (same inputs+version ⇒ functionally
equivalent output).

---

## 1. The gating question is the scope filter (Fork A)

Litmus test (ADR-0043): *does this change **what the render is** from data that can be
**authored today**?* Audio mixing passes both. Transitions/effects/color-grading pass
RC1–RC6 **but cannot be authored yet**: `clip.transition_in_id` /
`transition_out_id` / `effects` are **read-only, write paths deferred to α6.4**
(`domain/timeline/clip.py`). You cannot render what cannot be authored → they move to
**α8.4f** (gated behind the α6.4 authoring write paths), not α8.4e.

α8.4e = **audio mixing only**.

---

## 2. Thesis

The α8.4b renderer is **video-only** — its `filter_complex` ends `concat=…:v=1:a=0`,
silently discarding all audio. Yet the Timeline already models audio: **audio-kind
tracks** (`track.kind="audio"`, `track.muted`) and **per-clip `volume` (0–4)**. α8.4e
teaches the renderer to **mix audio** into the output:

```
Timeline
  ├── video tracks  → composed video (α8.4b, unchanged)
  ├── video-clip audio → travels with each clip (at clip.volume, unless track muted)
  └── audio tracks (music / voiceover) → mixed as overlays at their timeline offset
        ↓  IRenderer (amix)
  Output MediaAsset(video + mixed audio)
```

A pure `Timeline + MediaAssets + config → video-with-audio` transform (RC1/RC3),
provider- and orchestration-agnostic (RC2), never enrichment (RC4), producing a new
immutable output asset (RC5), deterministic for a given renderer version (RC6).

---

## 3. Grounding (what exists / what changes)

- **Renderer drops audio.** `FfmpegRenderer` builds `[i:v]trim…concat=n:v=1:a=0` and
  maps only `[outv]`. Adding audio is an **adapter filter-graph change** — the render
  layer is unfrozen (ADR-0043 D1/D3).
- **Timeline audio data exists (zero migration).** `list_tracks(timeline_id)` → tracks
  with `kind` (`video`/`audio`/`subtitle`/`effect`) + `muted`; `list_clips_for_timeline`
  → clips grouped by track, each with `volume`, `start_seconds`, `source_*`. All
  authorable today (α6.3a/b write paths). No schema change.
- **Transitions/effects are NOT authorable.** `transition_in_id` / `transition_out_id`
  / `effects` are read-only until α6.4 → out of α8.4e scope (Fork A).
- **`ProcessRenderJob._resolve_clips` reads only video clips today.** α8.4e extends the
  resolver to also read `track.kind`/`muted` (via `list_tracks`) and audio-kind clips —
  an **additive, non-frozen** read.
- **Neutral render DTOs are extensible.** `RenderInput` / `RenderSpec` are α8.4b DTOs on
  the unfrozen render seam; α8.4e may add audio fields (ADR-0043 D3).

---

## 4. Design forks (for sign-off)

- **Fork A — Scope (recommended: audio only).** α8.4e = audio mixing.
  Transitions/crossfades/effects/color-grading → **α8.4f**, explicitly gated on α6.4
  authoring write paths. *Alternative:* bundle transitions now (rejected — unauthorable,
  and it balloons the slice past α8.4a–d size).

- **Fork B — Audio composition model.**
  - **B1 (recommended): full mix.** (a) each video clip carries its **own** audio at
    `clip.volume`, muted if its track is `muted`; (b) **dedicated audio-track** clips
    (music / voiceover) are placed at their `start_seconds` and mixed (`amix`) over the
    composed timeline, at their `volume`, honoring track `muted`.
  - **B2: preserve-only.** Stop discarding video-clip audio (`a=1` through the concat);
    no separate audio tracks. Smaller, but not really "mixing".
  - *Leaning B1* (it is the actual feature); B2 is a valid smaller first step if B1
    feels too large for one slice. **Your call.**

- **Fork C — Neutral DTO shape.**
  - **C1 (recommended):** extend `RenderInput` with `volume: float` + `muted: bool`
    (video-clip audio) and add `RenderSpec.audio_inputs: tuple[AudioInput, …]` where
    `AudioInput = {path, source_start, source_end, start_seconds, volume}` for dedicated
    audio tracks. Explicit, neutral, minimal.
  - **C2:** a generic `tracks` structure in `RenderSpec`. Over-general for now.
  - *Leaning C1.*

- **Fork D — Timing model (recommended: keep α8.4b sequential-concat).** Video stays a
  chronological **concat** of video clips (α8.4b behaviour, unchanged); each video
  clip's audio travels with its segment; dedicated audio-track clips are overlaid via
  `adelay`+`amix` at their timeline offset. Reworking video into absolute-offset
  placement is a *separate* composition change — **out of α8.4e scope**. (RC-compliant
  either way; this is scope discipline.)

- **Fork E — Idempotency / immutability (recommended: unchanged).** Output key stays
  deterministic per render job; `ConflictError` → recover (RC5: rendered media is
  immutable — new audio behaviour applies to **new** render jobs; re-running an existing
  job recovers its existing asset). No "backfill" concept for renders (unlike
  enrichment) — a new composition is a new job (RC5).

- **Fork F — No-audio timelines (recommended: silent, not an error).** A timeline with
  no audio anywhere renders a silent video (today's behaviour). Not a failure. A clip
  whose source has no audio stream contributes silence to the mix.

---

## 5. Invariants

**New — W8.4e.1 (signed off):**

> **W8.4e.1 — Audio composition is a pure function of Timeline audio state.** The
> rendered audio is determined solely by the Timeline's audio state (tracks, clips,
> `muted` flags, `volume` values) and the `RenderSpec`. The renderer introduces **no**
> implicit gain staging, normalization, dynamic processing (ducking / side-chain /
> compression), fades, or hidden audio sources. The mix is a deterministic weighted sum
> of the authored inputs.

W8.4e.1 reinforces **RC3** (deterministic from Timeline + MediaAssets + config) and
**RC6** (renderer purity) and pre-empts future "helpful" FFmpeg defaults from silently
changing the output waveform.

α8.4e also remains covered by the α8.4b render invariants (now RC instances):

- **W8.4b.2 / RC2** — the audio path reads only Timeline data + `MediaAsset` bytes;
  never provider audio, URLs, checkpoints, or orchestration state.
- **W8.4b.1 / RC1** — a pure Timeline → Media transform; no orchestration reads/writes.

---

## 6. Migration verdict

**Zero migration.** Audio tracks, `clip.volume`, and `track.muted` already exist and
are authorable; the change is an FFmpeg filter-graph extension + additive Timeline
reads + additive neutral-DTO fields. No new columns, tables, or enum values.

---

## 7. Test plan

- **Unit** — `ProcessRenderJob` resolves audio-kind tracks + per-clip volume + muted
  correctly into the `RenderSpec` (fake `IRenderer` asserts the audio inputs it
  receives); muted track contributes no audio; no-audio timeline → silent (no audio
  inputs); RC5 immutability (re-run recovers existing asset). Fake-renderer assertions
  only — no real FFmpeg in unit tier.
- **Opt-in integration** — real `FfmpegRenderer` mixes a video clip's audio + a music
  track into one output with an audible/probeable audio stream; skipped without the
  binary (α8.4b pattern).
- **Full gate** — ruff, black, mypy, import-linter, unit suite; **freeze guard green,
  zero overrides** (Gate 1).

---

## 8. Versioning

Runtime capability change → `0.4.29-phase3-alpha8.4e` and tag
`v0.4.29-phase3-alpha8.4e`. Standard two-commit release ritual.

---

## 9. α8.4f — the next render slice (defined by this grounding)

The deferrals above naturally define α8.4f: **transitions · crossfades · color grading
· effects · subtitle burn-in**. These all require **Timeline authoring** (the α6.4 write
paths for `transition_in_id` / `transition_out_id` / `effects` / subtitles) rather than
merely consuming already-authored state — so α8.4f is gated behind α6.4 and stays
outside α8.4e. That boundary is cleaner than squeezing them into the audio slice.

## 10. Crisp definition (signed off)

> **α8.4e extends rendering from "video-only composition" to "video + deterministic
> audio composition" using already-authorable Timeline state, while remaining fully
> compliant with ADR-0042 and ADR-0043 RC1–RC6.**
