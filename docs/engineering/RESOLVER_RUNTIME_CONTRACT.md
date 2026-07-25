# Resolver Runtime Contract — α8.5e

> **Type:** Engineering design document (implementation contract). **Not an ADR.**
> The governing decisions live in **ADR-0041** (provider runtime contract),
> **ADR-0044** (α8.5x AI runtime architecture), the **α8.5d pre-flight**
> (`PHASE3_ALPHA8_5d_PREFLIGHT.md`) and the **Provider Runtime Data Model**
> (`PROVIDER_RUNTIME_DATA_MODEL.md`). This document is the *reference* engineers
> consult while writing the α8.5e resolver and its operational tables.
>
> **Status:** **SIGNED OFF** ✅ (2026-07-25) for α8.5e (pre-implementation), with
> amendments 1–6 folded in (invariants **W8.5e.6–8**, machine-readable explainability,
> expanded resolution ledger). No runtime change lands from this document. α8.5d
> (catalogue + seeder) is a completed, verified milestone; α8.5e is the point where the
> catalogue stops being passive data and starts influencing runtime behaviour.
>
> **One-line purpose:** define the resolver as a **pure, deterministic, explainable
> selection function** — never an executor — so selection, execution, and orchestration
> never fuse into one component.

---

## 0. Why this document exists

α8.5d made the catalogue *real* but *passive*: the DB now knows what can run, on what
hardware, at what cost, producing what outputs, depending on what. α8.5e is the first
consumer. The risk at this boundary is well-known: a "resolver" quietly grows to also
call providers, mutate health, and drive retries — and becomes an untestable knot of
selection + execution + orchestration.

This contract prevents that by fixing four things **before** any α8.5e code:

1. The resolver's **shape** — a pure function (§1).
2. Its **inputs**, split into three disjoint groups (§2).
3. Its **output** — an ordered, explainable candidate list, *not* an execution (§3, §4).
4. Its **invariants** — W8.5e.1–5 (§5) — and the **operational tables** it reads (§7).

Everything here upholds the α8.5c/α8.5d firewall: the runtime never reads YAML
(W8.5c.2 / W8.5d.1); the DB is the sole runtime source of truth; **catalogue is
immutable at runtime** and **operational state never lives in the catalogue** (W8.5d.10).

---

## 1. The resolver is a pure function

```
        Request                                     Ordered Candidate List
           +                                                  ▲
   Catalogue Snapshot   ───────►   resolve(...)   ────────────┘
           +                     (pure, no I/O side effects)
      Runtime State
```

**Not** `Request → resolve → Provider Execution`. The resolver returns a ranking; a
*separate* bounded context (Execution Runtime) decides whether to stop after the first
success or walk the list. That separation is what makes retries, fallback, and
reproducibility clean.

Conceptual signature (illustrative — final types land with α8.5e):

```
resolve(
    request: ResolveRequest,
    catalogue: CatalogueSnapshot,      # static, from §A.1 tables
    runtime: RuntimeState,             # operational, from §A.2 tables
) -> Resolution                        # ordered candidates + provenance
```

The three inputs are passed in; the resolver performs **no queries of its own** in its
core. A thin adapter loads the snapshot + runtime state and hands them to the pure core,
so the core is trivially unit-testable and satisfies determinism (W8.5e.4).

---

## 2. Resolver inputs — three disjoint groups

### 2.1 Static — from the catalogue (§A.1, seeder-owned, immutable)

| Input | Source table |
| --- | --- |
| capabilities (+ I/O types, params) | `capabilities` |
| capability dependencies | `capability_dependencies` |
| providers (identity, pricing model, declared quota ceilings, `scores`) | `providers` |
| adapters (capability, status, execution mode, features, outputs) | `provider_adapters` |
| routing policy (strategy / fallback / selection) | `routing_policies` |
| fallback graph | `adapter_fallbacks` |
| hardware requirements + resource estimates | `provider_adapters.runtime` (JSONB) |
| estimated cost (hint) / authoritative price | `provider_adapters.cost_*` / `ai_model_pricing` |
| output formats | `provider_adapters.outputs` |
| device profiles | `device_profiles` |

### 2.2 Operational — runtime only (§A.2, runtime-owned, live)

| Input | Source table (grounded in §7) |
| --- | --- |
| health score, last_success, 429 frequency | `provider_health` |
| average latency, success rate, current queue | `adapter_runtime_metrics` |
| quota **remaining**, window reset | `provider_quota_state` |
| rate-limit state | `provider_quota_state` / `provider_health` |
| GPU availability, local-model loaded status | `local_runtime_state` |

### 2.3 Request — the current generation

| Field | Meaning |
| --- | --- |
| `capability` | e.g. `image_generation` (required) |
| `prompt` | text (not scored; passed for feature gating only) |
| `duration_seconds` | for video/audio capabilities |
| `budget` | max spend for this request (or `0` ⇒ free-only) |
| `quality` | requested tier (maps to `generation_mode`) |
| `device` | current `device_profile` id (or detected specs) |
| `privacy_mode` | if set, disallow cloud egress of sensitive inputs |
| `local_only` | hard filter: only `execution.local` adapters |
| `commercial_allowed` | hard filter: only `supports.commercial` adapters |

> **Group discipline:** these three groups never bleed. Static is read-only catalogue;
> operational is live runtime; request is per-call. The resolver's job is to *join* them
> — it owns none of them.

---

## 3. Resolver output — ordered candidates, not a choice

The resolver returns an **ordered candidate list**, each entry explainable, plus
provenance (§6). It never returns "the chosen provider."

```
1.  pollinations.image   score 94   ← eligible, free, healthy
2.  comfyui.flux         score 91   ← eligible, local, heavier
3.  fal.image            score 83   ← eligible, paid, within budget
    huggingface.image    (filtered) ← ineligible: quota exhausted
```

Each candidate carries:

| Field | Purpose |
| --- | --- |
| `adapter_id` | the first-class runtime unit |
| `score` | final aggregate (0–100) |
| `breakdown` | per-component contribution (W8.5e.5) |
| `eligible` | bool — passed all hard filters (§4.1) |
| `ineligible_reason` | populated when `eligible=false` (e.g. `quota_exhausted`) |
| `fallbacks` | ordered `adapter_fallbacks` for this adapter (execution convenience) |

**Execution decides** whether to stop after the first success or continue down the
list. The resolver expresses preference; it does not enforce it.

---

## 4. Scoring model — explainable, never opaque

Selection is two stages: **hard eligibility filters** (binary, remove candidates) then
**soft scoring** (rank the survivors). No black-box AI ranking (W8.5e.5).

### 4.1 Hard filters (eligibility — a candidate is dropped, with a reason)

- capability mismatch (adapter does not serve `request.capability`)
- missing a **required** feature/input the request needs
- `local_only` and adapter is not `execution.local`
- `commercial_allowed=false`-clash (request needs commercial, adapter can't)
- `privacy_mode` and adapter is cloud (egress of sensitive input)
- device cannot meet adapter `runtime.hardware.minimum_ram_gb` / GPU backend
- `budget` insufficient for the adapter's minimum cost (unless free-capable)
- operational: quota exhausted, health = down, rate-limited right now

Filtered candidates are **kept in the output** with `eligible=false` + reason — never
silently dropped (aids debugging and the provenance ledger).

> **Eligibility is separate from scoring (Amendment 4).** A hard constraint makes a
> candidate **ineligible** — it never becomes a score *penalty*. If the request sets
> `local_only=true`, cloud adapters do not appear with a lower score; they are
> ineligible. Likewise `commercial_allowed=false` makes commercial adapters ineligible
> rather than penalised. Scoring (§4.2) ranks *only* the eligible survivors, so a
> filtered adapter can never out-rank — or under-rank — its way back into contention.

### 4.2 Soft score (rank the eligible)

A transparent weighted sum of normalised components (each 0–100), then a health
multiplier:

```
raw   = w_quality      · quality
      + w_cost         · cost_fit
      + w_speed        · speed
      + w_reliability  · reliability
      + w_hardware     · hardware_fit
score = raw · health_multiplier          # health ∈ [0,1], from §7
```

| Component | Derived from |
| --- | --- |
| `quality` | `providers.score_quality` |
| `cost_fit` | free ⇒ 100; else scaled by cost-vs-budget (cheaper ⇒ higher) |
| `speed` | `providers.score_speed`, adjusted by `adapter_runtime_metrics` latency |
| `reliability` | `providers.score_reliability` |
| `hardware_fit` | device_profile × adapter `runtime.hardware` (0 = would have been filtered) |
| `health_multiplier` | `provider_health` (recent success rate / 429 frequency) |

**Explainability is structural, not prose (Amendment 2 [W8.5e.5]).** Each candidate's
score is returned — and persisted — as a machine-readable breakdown, never a bare
number:

```json
{
  "adapter": "pollinations.image",
  "final_score": 91,
  "routing_strategy": "free_first",
  "components": {
    "quality": 30,
    "cost": 20,
    "speed": 15,
    "hardware": 12,
    "reliability": 10,
    "health_multiplier": 1.08
  }
}
```

Every weighted contribution and the health multiplier appears as a named field. This is
consumed directly by debugging, telemetry, UI explanations ("chosen because free +
healthy"), and future ML weight-tuning — with no re-derivation.

### 4.3 Routing strategy = the weight vector

`routing_policies` (per capability, else `default`) selects the weights — the strategy
is *only* a weight vector, keeping scoring uniform and explainable:

| Strategy | Emphasis (illustrative weights) |
| --- | --- |
| `free_first` | cost_fit dominant; free-capable providers rise to the top |
| `lowest_cost` | cost_fit dominant, quality/speed secondary |
| `highest_quality` | quality dominant |
| `balanced` | even weights |

### 4.4 Determinism & tie-break (W8.5e.4)

Given identical `(request, catalogue snapshot, runtime state)` the output is byte-for-byte
identical. Ties break by a **total order**: `score desc → reliability desc → adapter_id
asc`, applied by an **explicit comparator** — never SQL/result-set ordering (W8.5e.7). No
wall-clock, no RNG, no map-iteration-order dependence in the core. The snapshot the
scoring runs against is fixed for the whole invocation (W8.5e.6).

---

## 5. Resolver invariants (W8.5e.1–5)

> ### W8.5e.1 — The resolver never performs generation.
> It returns a ranking; the Execution Runtime calls adapters.

> ### W8.5e.2 — The resolver never mutates the catalogue.
> §A.1 tables are read-only to it (reinforces W8.5d.10).

> ### W8.5e.3 — Health is an input only; the resolver never mutates it.
> The resolver **may read** `provider_health` (and the other §A.2 tables) but **never
> writes** them. Health/metrics/quota updates belong to the Execution Runtime, the
> Health Worker, and telemetry — never the resolver. (Amendment 3.)

> ### W8.5e.4 — Same inputs produce identical ordered candidates.
> Determinism is a hard guarantee (total-order tie-break, no I/O in the core).

> ### W8.5e.5 — Scoring must be explainable, and machine-readable.
> Every score decomposes into named components serialised as a structured object (§4.2),
> not a bare number. The breakdown is returned with each candidate and persisted in the
> resolution ledger (§6). No opaque ranking. (Amendment 2.)

> ### W8.5e.6 — One immutable catalogue snapshot per invocation.
> Every resolution operates against a single, immutable catalogue snapshot: it reads one
> `catalogue_version`, scores entirely against it, and finishes against it even if a
> re-seed lands mid-request. The catalogue cannot change halfway through a resolution.
> (Amendment 1 — keeps routing reproducible.)

> ### W8.5e.7 — Candidate ordering is stable.
> Two candidates with equal score always appear in exactly the same order. Ordering is
> produced by an explicit deterministic comparator (`score desc → reliability desc →
> adapter_id asc`), **never** by SQL/result-set ordering. (Amendment 5.)

> ### W8.5e.8 — Capability-first; the resolver knows no provider implementations.
> The resolver contains no provider-specific branching — no `if provider == "pollinations"`
> or `if adapter == "fal.image"`. Every decision derives from catalogue metadata and
> operational state. Provider-specific behaviour lives in adapters (Execution Runtime),
> never in selection. (Amendment 6 — preserves the capability-first architecture.)

These sit alongside W8.5c.* and W8.5d.1–10 and are locked for α8.5e.

---

## 6. Provenance — reproduce *why* a provider was chosen

Every generation request records enough to reconstruct — and *replay* — the decision
months later, even after the catalogue changes:

| Field | Source |
| --- | --- |
| `generation_id` | the generation this resolution served |
| `catalogue_version` | `provider_registry_meta.catalogue_version` |
| `manifest_digest` | `provider_registry_meta.manifest_digest` |
| `resolver_version` | resolver code version (own constant) |
| `routing_strategy` | the strategy/weight vector applied (§4.3) |
| `candidate_list` | full ranked list + per-candidate structured breakdown (W8.5e.5) — *not only the chosen adapter* |
| `chosen_adapter` | the adapter Execution ultimately succeeded with |
| `start_time` / `end_time` | resolution + execution window |
| `execution_result` | success / failure / fell-through-to-fallback |

Persisted to the runtime-owned `generation_resolution_ledger` (§7) — an operational
table, never catalogue. Keeping the **full `candidate_list`** (not just the winner) gives
complete replay capability. This directly serves AR18 (provider transparency) and AR16
(project memory): "Planner Qwen3 · Images Pollinations · Voice Kokoro …" is a projection
of this ledger.

---

## 7. Grounding the operational tables

These are **operational** (§A.2 of the data model): runtime-owned, live, never in Git,
and — per W8.5d.10 — never merged into catalogue tables. They are grounded here (names,
columns, writer, cadence) so α8.5e builds them deliberately rather than bolting live
state onto the catalogue. **None of these ships in α8.5d.** Migration numbering (`0011+`)
is assigned at α8.5e implementation time.

| Table | Kind | Writer | Cadence | Key columns (indicative) |
| --- | --- | --- | --- | --- |
| `provider_health` | **observational** | Health Worker | periodic probe + on-failure | `provider_id`, `health_score`, `last_success_at`, `last_failure_at`, `error_rate`, `rate_limit_hits`, `updated_at` |
| `provider_quota_state` | **operational** | Execution Runtime | per call + window reset | `provider_id`, `window`, `used`, `remaining`, `resets_at`, `updated_at` |
| `adapter_runtime_metrics` | **historical** | Execution Runtime | rolling, per execution | `adapter_id`, `avg_latency_ms`, `p95_latency_ms`, `success_rate`, `current_queue_depth`, `sample_window`, `updated_at` |
| `local_runtime_state` | operational | Execution Runtime / device agent | on device/model change | `device_profile_id`, `gpu_available`, `loaded_models`, `free_vram_gb`, `updated_at` |
| `generation_resolution_ledger` | provenance | Resolver caller (Execution) | one row per request | `generation_id`, `capability`, `catalogue_version`, `manifest_digest`, `resolver_version`, `routing_strategy`, `candidate_list` (JSONB), `chosen_adapter`, `start_time`, `end_time`, `execution_result`, `created_at` |

> **Keep the three signal tables independent (operational-table clarification).**
> `provider_health` is **observational** (is the provider up / behaving?),
> `provider_quota_state` is **operational** (how much budget/quota is left right now?),
> and `adapter_runtime_metrics` is **historical** (rolling latency/success trends). They
> have different write cadences, retention, and consumers — do **not** merge them into a
> single "provider status" table later.

Existing, already-runtime-owned: `provider_plugin_registrations` (loaded-code plugins +
health surface), `usage_records` (authoritative spend — the Cost Estimator reads this;
adapter `cost_*` remain estimation-only hints, W8.5d.8).

> **Firewall check:** if a proposed field describes *what a provider is*, it belongs in
> the catalogue (§A.1). If it describes *how a provider is behaving right now*, it belongs
> here. When in doubt, it is operational.

---

## 8. What the resolver does NOT do (deferred to other contexts)

| Concern | Owning context |
| --- | --- |
| calling adapters, retry/fallback execution | **Execution Runtime** |
| writing health / metrics / quota | **Health Worker / Execution Runtime** |
| prompt → structured project, capability-graph, provider *requests* | **Planner** |
| CLIP/face/prompt verification + repair | **Verification Runtime** |
| character sheets / seeds / reference images | **Identity Runtime** |
| project memory, provider ledger projection | **Memory Runtime** |
| publishing | **Publishing** |

The resolver is the thin, pure seam between the catalogue and everything above it.

---

## 8a. Forward note — the three architectural planes

Stepping back from the individual slices, a larger pattern is emerging that these
boundaries should preserve. The platform is separating into **three planes**, each with a
*different mutability model and different invariants* — a stronger separation than a
typical layered architecture:

| Plane | Contains | Mutability | Example invariants |
| --- | --- | --- | --- |
| **Knowledge** | catalogue, manifests, capabilities, routing policy, provider metadata | static, design-time (seeded) | W8.5c.*, W8.5d.1–10 |
| **Decision** | resolver, planner, verifier | pure functions over knowledge + runtime state | W8.5e.1–8 (deterministic, explainable, no side effects) |
| **Execution** | provider adapters, local GPU, FFmpeg/ComfyUI/Ollama, uploads, publishing | stateful, side-effecting | writes §A.2 only; owns all I/O |

The resolver (this contract) is the first fully-realised member of the **Decision** plane,
and its invariants exist precisely to keep it from leaking into the Execution plane.
A dedicated architectural-overview document (engineering doc, **not** an ADR) capturing
these three planes and *why* the boundaries exist will be authored **once α8.5e is
complete** — recorded here so the intent is not lost.

---

## 9. Roadmap after this contract

```
α8.5d  →  Catalogue (done, verified on live PG)
α8.5e  →  Resolver (this contract: pure selection, explainable, deterministic)
          Planner (MRC: capability graph, provider requests)
          Execution Runtime (local-first; calls adapters; writes §A.2)
          Verification Runtime (similarity + repair)
α8.6   →  Publishing (separate registry)
```

---

## 10. Implementation order (α8.5e) — grounding → contract → tables → resolver

Disciplined, mirroring α8.5d:

1. **Grounding + contract** — this document (no code).
2. **Operational tables** — migration `0011+` for §7 (schema only; writers stubbed).
   Reinforce W8.5d.10 in review: no operational column ever added to §A.1.
3. **Pure resolver core** — `resolve(request, catalogue, runtime)`; hard filters (§4.1),
   scoring (§4.2), strategy weights (§4.3), deterministic tie-break (§4.4). Fully
   unit-testable with in-memory snapshots (no DB) — proves W8.5e.4/W8.5e.5.
4. **Snapshot + runtime loaders** — thin adapters that read §A.1 / §A.2 and feed the core.
5. **Provenance** — write `generation_resolution_ledger` (§6) from the Execution caller.
6. **Verification** — a resolver round-trip / golden-file suite (determinism +
   explainability), and only *then* wire the Planner/Execution consumers.

Runtime consumption begins **only after** steps 1–4 are green — the same
grounding → contract → implementation → verification cadence used throughout α8.x.

---

## Change log

| Date | Change |
| --- | --- |
| 2026-07-25 | **SIGNED OFF** with amendments 1–6. Added invariants **W8.5e.6** (single immutable catalogue snapshot per invocation), **W8.5e.7** (stable candidate ordering via explicit comparator, never SQL order), **W8.5e.8** (capability-first — no provider-specific branching); strengthened **W8.5e.3** (health is read-only input) and **W8.5e.5** (explainability is machine-readable — structured `components` object incl. `health_multiplier`). Made eligibility explicitly separate from scoring (`local_only`/`commercial_allowed` ⇒ ineligible, never a penalty). Clarified the three signal tables stay independent (`provider_health` observational / `provider_quota_state` operational / `adapter_runtime_metrics` historical — never merged). Expanded `generation_resolution_ledger` (routing_strategy, full candidate_list, start/end_time, execution_result) for complete replay. Added the forward note on the three architectural planes (§8a). |
| 2026-07-25 | Initial contract — resolver as a pure function (§1); three input groups (§2); ordered-candidate output (§3); explainable two-stage scoring with strategy-as-weight-vector + deterministic tie-break (§4); invariants **W8.5e.1–5** (§5); per-request provenance (§6); **grounded operational tables** `provider_health` / `adapter_runtime_metrics` / `provider_quota_state` / `local_runtime_state` / `generation_resolution_ledger` (§7); non-goals (§8); roadmap (§9); α8.5e implementation order (§10). Pairs with the completed α8.5d milestone; no runtime change. |
