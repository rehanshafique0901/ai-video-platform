# Phase 3 — α8.5c Pre-Flight: Capability Catalogue & Provider Registry (tooling-first)

> Status: **SIGNED OFF — all forks ruled; proceeding to implement.** A **capability-first**,
> tooling-only slice: a curated, human-authored **design-time spec** for the AI capability→provider
> graph plus an **offline CI validator**. No runtime change, no migration, no live API calls. The
> YAML is the *design-time* source of truth; the **database remains the runtime source of truth** (a
> later slice, α8.5d, seeds the DB from the validated spec).
>
> Baseline: `v0.4.34-phase3-alpha8.5b3r`. Predecessor tooling: the ci_gate validation-DB hardening
> chore.
>
> **Rulings (SIGNED OFF):** **S-A1** (pure tooling) · **S-B1** (no runtime version bump) ·
> **S-C1** (new fast no-DB stage *before* the DB stages) · **S-D** (`backend/providers/*.yaml` +
> Pydantic) · **S-E** (invariants **W8.5c.1–6**) · catalogue (§4) & validator (§5) accepted ·
> **capability-first inversion**, **two-level `kind`+`capability`**, **no integer priority**,
> **family inheritance (acyclic)**, **adapter-id primacy**, **free-provider sanity** all adopted.
>
> **Revision R2 (post-ship design review):** the single `registry.yaml` is split into **three
> focused manifests** — `capabilities.yaml` (vocabulary), `providers.yaml` (providers + adapters +
> families), `routing.yaml` (policy). The catalogue is **enriched** with typed I/O
> (`inputs`/`outputs`) + `requires`/`optional` params; providers carry a richer **`pricing`
> (`free`/`freemium`/`paid`) + `quota` + `requires_login`** model (replacing the flat `free` block);
> adapters carry capability-specific **`supports` constraints** (`commercial`/`nsfw`/`watermark`/
> `max_duration_seconds`/`max_resolution`/`queue`/`async`/`polling`/`webhook`). **Dynamic health/
> latency scoring stays out** (static routing only). New validator rules: capability-metadata
> integrity, constraint applicability, pricing/quota sanity. Still tooling-only, no version bump.

---

## 0. Why this exists (end-goal alignment)

North star: autonomous, **free-first** content generation — automatically choose the best available
(ideally free) provider for a task, fall back on failure, compose capabilities
(image → video → voice → subtitles), later admit commercial providers without touching business
logic, and keep **publishing** wholly separate. Orchestration must **never** name a provider — it
asks for a *capability* and the registry answers. A future planner asks:

```
Need: image_generation, video_generation, music_generation, text_to_speech,
      caption_generation, publishing.youtube
```

…and the resolver picks providers by the active strategy (`free_first` / `balanced` / …) with **no
change to orchestration logic**. This slice lays the **capability language** and the **validated
spec** for that future, additively, before α8.6 needs it — and builds **none** of the runtime
resolver yet.

---

## 1. Grounding — what already exists (reconcile, don't duplicate)

Two representations of provider truth already exist; an untethered third YAML would drift. This slice
ties them together.

### 1.1 Code registry (in-memory, mock-wired) — α7.4 / ADR-0041
- `providers/ports.py` — `Capability` StrEnum = **`llm | image | video | voice`** (4 kinds);
  capability `Protocol`s; `Provider` protocol = `metadata` + async `health()`.
- `application/interfaces/providers.py` — neutral DTOs + `ProviderMetadata`
  (`id/name/capability/supports_polling/supports_webhooks/version`); `ProviderError` hierarchy with
  `transient`.
- `providers/registry.py` — `ProviderRegistry` keyed by capability; `resolve()` is a **direct lookup
  today**, with the design note that precedence "is layered on here without changing callers"
  (fallback/priority/health-ordering **deferred**, α7.4 Q4). **This slice is that deferred future's
  ground truth.**
- import-linter (ci_gate stage 3): *"Provider capability leaf never imports orchestration, api, or
  the workflow domain."*

### 1.2 DB-seeded registry (real tables, migration `0002`)
- `ai_models` — `kind` (`plugin_kind_enum`), **`capabilities ARRAY(Text)`** (open!), pricing linkage,
  `status`, **`successor_model_id`** (a successor edge), `extra JSONB`.
- `ai_model_pricing` — temporal, immutable, versioned.
- `provider_plugin_registrations` — `kind`, **`capabilities ARRAY(Text)`**, `enabled`,
  **`last_health_status` / `last_health_at`** (operational state already), `extra`.

### 1.3 Vocabulary reconciliation (the two-level model)
- **`kind`** = native `plugin_kind_enum` (`llm|image|video|voice`) == code `Capability` enum — the
  **coarse routing bucket**. Costly to extend (native enum).
- **`capability`** = the fine-grained action, already stored in the open **`capabilities text[]`**.
- Output taxonomy for reference: `media_kind_enum = image|video|narration|subtitle|music|
  sound_effect|thumbnail`.

**Consequence:** the ~26-term catalogue lives in the open `text[]` space + the YAML catalogue; each
fine capability declares its coarse `kind`. The 4-value enum stays as-is and is extended
**additively, later, only when a runtime serves a new kind** — so α8.5c is **zero-migration,
zero-runtime**.

---

## 2. Rulings captured

| # | Ruling |
|---|--------|
| **R1** | **Capability-first inversion** — model is *capability → providers*; orchestration asks for a capability, never a provider. |
| **R2** | **Two levels:** `kind` (coarse, = existing 4-value enum) **and** `capability` (fine). Every adapter declares a `capability`; its `kind` is derived from the catalogue. |
| **R3** | **No integer priority — anywhere.** Providers declare **static scores** (`quality/cost/speed/reliability`, 0–100); a **routing strategy** (deferred resolver) computes selection. |
| **R4** | **Static (Git) vs operational (DB) split.** YAML: scores, license, free-tier, adapter, family, auth, webhook support. DB/runtime: health, latency, quota remaining, success rate, last error, consecutive failures. Never in the same object. |
| **R5** | **Families + variants**; **failover happens inside a family first**; cross-family fallback only if the routing policy explicitly allows it. Family inheritance (`parent`) is allowed but must be **acyclic**. |
| **R6** | **Adapter IDs are first-class** (`pollinations.image`, `fal.flux`). The **adapter is what runtime loads**; the provider is ≈ documentation. |
| **R7** | **AI Capability Registry ≠ Publishing Destination Registry** — two registries, **shared tooling**; publishing is excluded from the AI catalogue (α8.6 bounded context). |
| **R8** | **Free-tier intelligence** per provider: `available / signup_required / api_key_required / daily_limit / monthly_limit / watermark`. |
| **R9** | **YAML → validator → seed → DB → runtime.** Runtime **never** reads YAML; **DB is the runtime source of truth**. Seeder + migration = α8.5d. |
| **R10** | **Expanded offline validator** (§5); **no live API calls** by default (`--probe` opt-in, not implemented here). |

---

## 3. Manifest schema (design-time spec) — `backend/providers/` (three files, R2)

**Three focused manifests**, validated by a Pydantic v2 schema in `scripts/provider_manifest.py`
(tooling module — **not** under `app/`, so runtime can't import it; enforces W8.5c.2).

```yaml
# backend/providers/capabilities.yaml — the vocabulary (see §4)
capabilities:
  - id: image_generation
    kind: image                       # kind ∈ {llm, image, video, voice}
    inputs: [text]                    # io types: text|image|video|audio|subtitle|embedding
    outputs: [image]
    requires: [prompt]                # snake_case param names; disjoint from `optional`
    optional: [negative_prompt, reference_image, seed, steps]
```

```yaml
# backend/providers/providers.yaml — providers, adapters, families
providers:
  - id: pollinations
    name: Pollinations
    homepage: https://pollinations.ai
    license: open
    commercial: true
    authentication: none              # none | api_key | oauth | token (credential source of truth)
    requires_login: false             # portal signup needed even if no per-call key
    pricing: free                     # free | freemium | paid (free+freemium = "free-capable")
    quota: { daily: unlimited, monthly: unlimited }   # int | "unlimited" | null
    config_keys: []                   # KEY NAMES only, never secret values
    scores: { quality: 70, cost: 100, speed: 90, reliability: 75 }   # static, 0–100; NO priority
    adapters:
      - id: pollinations.image        # first-class, runtime-loadable unit: <provider>.<suffix>
        capability: image_generation
        status: planned               # planned | implemented (implemented ⇒ import_path exists)
        fallback: []                  # adapter ids, same capability, acyclic, no self
        supports:                     # capability-specific constraints the resolver matches on
          commercial: true            # commercial|nsfw|watermark|queue|async|polling|webhook (bool)
          watermark: false
          max_resolution: "1024x1024" # only meaningful for image/video kinds
          # max_duration_seconds: 30  # only meaningful for video/voice kinds (int > 0)

families:
  - id: flux
    parent: null                      # optional; inheritance must be acyclic
    variants:
      - { id: flux.dev, provider: fal }
```

```yaml
# backend/providers/routing.yaml — the policy (score + strategy; NO integer priority)
defaults:
  strategy: free_first                # free_first | lowest_cost | highest_quality | fastest |
                                      # balanced | offline_only | privacy_first | commercial_only | free_only
  fallback: automatic                 # automatic | none
  selection: best_available           # best_available | first_available
by_capability:
  video_generation: { strategy: highest_quality }
  text_to_speech: { strategy: lowest_cost }
# operational state (health/latency/success_rate/quota-remaining/429-freq) is ABSENT — DB only
```

Publishing destinations get a **parallel** trio (`backend/publishing/*.yaml`), validated by the
**same** tooling but a **separate** registry (R7) — designed here, authored in α8.6.

---

## 4. Canonical capability catalogue (R2) — fine `capability` grouped by coarse `kind`

`publishing` is **excluded** (separate registry). Additive; runtime enum unaffected.

- **kind `llm`:** `text_generation`, `embedding`, `moderation`, `translation`, `ocr`
- **kind `image`:** `image_generation`, `image_edit`, `image_upscale`, `background_removal`,
  `inpainting`, `outpainting`, `object_removal`, `face_swap`, `caption_generation`
- **kind `video`:** `video_generation`, `video_upscale`, `video_interpolation`, `lip_sync`,
  `video_edit`, `frame_extraction`, `subtitle_generation`
- **kind `voice`:** `text_to_speech`, `speech_to_text`, `voice_clone`, `voice_conversion`,
  `music_generation`, `sound_effect_generation`

> `kind` assignment groups a fine capability under one of the four coarse routing buckets so it maps
> cleanly onto `plugin_kind_enum` / `Capability`. (`ocr`/`caption`/`subtitle` are grouped by the
> pipeline stage that produces them; assignments are data and can be re-grouped without code change.)

**R2 enrichment:** each catalogue entry now also declares typed `inputs`/`outputs` (from
`text|image|video|audio|subtitle|embedding`) and `requires`/`optional` request-param names. This is
what lets a future planner **compose capability graphs** (prompt → LLM → image → upscale → video →
music → voice → subtitles → render → export, each node asking only for a *capability*) and lets a UI
auto-generate/validate request forms — with zero provider knowledge in either.

---

## 5. Validator (`scripts/validate_providers.py`) — offline, deterministic

Fails closed; writes `.validation/provider_validation_report.json`; non-zero exit on any error.
Warnings do not fail the gate.

1. **Uniqueness:** unique provider ids · adapter ids · variant/model ids · capability ids.
2. **Capability metadata (R2):** non-empty `inputs`/`outputs` (∈ io-type vocab, enforced by schema);
   `requires`/`optional` are unique, snake_case, and **disjoint**.
3. **Catalogue integrity:** every adapter `capability ∈ catalogue` (**error**); catalogue capability
   served by no adapter = **warning** (orphan).
4. **Unique (provider, capability):** a provider serves a given capability at most once (**error**).
5. **Adapter integrity:** id shape `^[a-z0-9_]+\.[a-z0-9_]+$`; `implemented` ⇒ `import_path` present,
   importable, a class, and **structurally implements** the `kind`'s protocol
   (`health` + kind verbs: image→`generate_image`, video→`submit`&`resolve`, llm→`generate_text`,
   voice→`synthesize_voice`); `planned` ⇒ shape + capability only.
6. **Adapter constraints (R2):** `max_duration_seconds` only meaningful for `video`/`voice` kinds,
   `max_resolution` only for `image`/`video` (**warning** otherwise); `max_duration_seconds > 0`
   (schema).
7. **Fallback graph:** every edge targets an existing adapter of the **same capability**; **no
   self-fallback**; **acyclic**.
8. **Families:** variant ids unique; every `variant.provider` exists; `family.parent` exists if set;
   **family inheritance acyclic** (reject circular inheritance).
9. **Pricing/quota (R2):** `pricing ∈ {free,freemium,paid}` (schema); numeric quota limits `> 0`
   (**error**); a `free`/`freemium` provider with no declared quota = **warning**.
10. **Auth & routing enums:** `authentication`, `routing.strategy/fallback/selection` ∈ recognised
    sets (schema); every `routing.by_capability` key ∈ catalogue.
11. **Scores:** `quality/cost/speed/reliability` present, integer, `0..100`; **no `priority` key**
    permitted (schema `extra=forbid`).
12. **Config keys:** KEY NAMES only — an entry containing `=`/whitespace is an **error** (guards
    against secrets in Git); non-`UPPER_SNAKE` = **warning**.
13. **Anti-drift:** every code `Capability` value maps to a catalogue `kind`; `plugin_kind_enum`
    matches the coarse vocabulary.
14. **Free-provider sanity:** for every capability whose **effective** strategy is `free_first` or
    `free_only`, at least one serving provider has `pricing ∈ {free,freemium}` (**error** otherwise).

**No network** in the standard gate; a future `--probe` (default off) would exercise health
endpoints — out of scope to implement in α8.5c. **Dynamic health/latency scoring is explicitly not
modelled** (static routing only; `final = score × health × latency × cost` is a later resolver).

---

## 6. Scope (S-A1 — pure tooling)

**Ships:** catalogue + AI registry manifest (seeded with a real free-first set, all adapters
`planned`) + Pydantic schema module + `scripts/validate_providers.py` + a **fast no-DB CI stage
before the DB stages** + publishing schema **designed** (not authored) + docs + invariants + unit
tests.

**Deferred (own slices):** **α8.5d** YAML→DB seeder + additive migration; **resolver**
(scoring/routing/fallback in `resolve`, reading operational state from DB); real adapter code.

---

## 7. Invariants (S-E — accepted)

- **W8.5c.1 — Capability-first selection.** Orchestration selects by *capability*; no caller names a
  provider id.
- **W8.5c.2 — YAML is design-time only.** Runtime never reads the manifest; the **DB is the runtime
  source of truth**.
- **W8.5c.3 — Static/operational separation.** Manifest holds only static metadata + declared
  scores; operational state lives in the DB, never in YAML.
- **W8.5c.4 — Registry separation.** AI capabilities and publishing destinations are distinct
  registries; the AI catalogue never contains a publishing capability.
- **W8.5c.5 — Offline validation.** Provider validation makes no network calls in the standard gate.
- **W8.5c.6 — Anti-drift.** The manifest is validated against the existing capability/kind
  vocabulary; divergence fails the gate.

---

## 8. CI stage (S-C1)

A new **fast, no-DB** stage driven by `scripts/validate_providers.py`, added as **Stage 0** — a
pre-flight check that runs *before* every other stage (therefore before the DB stages, satisfying
S-C1's "before DB-dependent stages"). Stage 0 is chosen over renumbering because it keeps the
existing **1–10 map stable**: the ci_gate restoration guard keys on "stage 6 = downgrade" and the
live-DB range 5–9, so renumbering would silently break that hardening. `_parse_stage_range` is
refactored to derive the valid set from the stage list (so Stage 0 is included by default). The
stage validates the manifest offline and skips gracefully (exit 0) when the manifest is absent.

---

## 9. Migration & versioning

- **Migration: ZERO** (S-A1). Seeder/migration is α8.5d.
- **Versioning: NO runtime bump** (S-B1) — tooling + design-time spec only (as with the ci_gate
  hardening chore). The bump arrives with α8.5d/resolver.

---

## 10. Freeze check (ADR-0042 / ADR-0041)

- **ADR-0042:** not touched — no frozen orchestration surface, no runtime path changes. Freeze guard
  expected green, zero override markers.
- **ADR-0041:** additive **addendum** only — the capability catalogue + manifest schema are new,
  static, tooling-side; the enum extension is explicitly deferred.

---

## 11. Deliverable & test plan

**Deliverable:** `backend/providers/{capabilities,registry}.yaml` · `scripts/provider_manifest.py` ·
`scripts/validate_providers.py` · ci_gate stage · docs (CI_QUALITY_GATE ×2, ADR-0041 addendum,
CHANGELOG tooling note, this pre-flight) · unit tests.

**Tests (offline, `unit`-marked):** valid manifest parses; **every** validator rule (§5) has a *red*
and a *green* fixture (duplicate id, unknown capability, dup provider+capability, cyclic fallback,
self-fallback, orphan-warning, planned-vs-implemented, interface mismatch, cyclic family
inheritance, secret-in-config, bad routing strategy, out-of-range score, `priority` present,
free-tier inconsistency, anti-drift, free-provider-sanity). The committed `registry.yaml` +
`capabilities.yaml` validate clean. New ci_gate stage runs in the no-DB group and skips cleanly when
the manifest is absent. Fast gate (1–4 + new stage) green; freeze guard green.

---

## 12. Sign-off checklist

- [x] **S-A** pure tooling (S-A1)
- [x] **S-B** no runtime version bump (S-B1)
- [x] **S-C** new fast CI stage before DB stages (S-C1)
- [x] **S-D** `backend/providers/*.yaml` + Pydantic
- [x] **S-E** invariants W8.5c.1–6
- [x] Capability catalogue (§4) accepted
- [x] Validator rule set (§5) accepted
- [x] Refinements: two-level kind/capability · no integer priority · family inheritance (acyclic) ·
      adapter-id primacy · free-provider sanity · rename to "Capability Catalogue & Provider Registry"
