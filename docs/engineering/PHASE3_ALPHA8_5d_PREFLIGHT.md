# Phase 3 — α8.5d Pre-Flight: Seed the Capability Registry (YAML → DB, the runtime source of truth)

> Status: **SIGNED OFF** (2026-07-25 — forks **D-A…D-I** all approved with the
> recommended direction + refinements; four additional metadata categories ruled
> in). The first **runtime + migration** slice of the α8.5x program (ADR-0044). It
> turns the validated α8.5c design-time spec into **populated database tables** so
> the DB becomes the *runtime source of truth* (upholding **W8.5c.2**: the runtime
> never reads YAML). Everything before this was tooling/governance; α8.5d is where
> data lands.
>
> **Ruling refinements (2026-07-25):**
> - **D-A** approve — new **catalogue tables alongside** the legacy tables (legacy =
>   compatibility/runtime; catalogue = the rich design-time-seeded model). Flow:
>   `YAML → Validator → Seeder → Provider Catalogue Tables → Resolver → Runtime`.
> - **D-A2** approve — **fallback join table** with `(adapter_id,
>   fallback_adapter_id, reason, ordinal)` (FK integrity, cycle validation, SQL
>   traversal, analytics, future weighted routing). No JSON fallback arrays.
> - **D-B** refined into **three storage tiers**: **stable ⇒ typed columns**
>   (`execution_mode`, `provider_status`/`enabled`/`implemented`, `pricing_model`,
>   `authentication`); **semi-structured ⇒ JSONB** (hardware, runtime estimates,
>   adapter constraints/`supports`, supported resolutions, queue limits, features,
>   output formats); **highly-variable ⇒ separate tables** (pricing history,
>   fallback graph, capabilities, provider variants).
> - **D-C / D-F / D-I** approve as recommended (DDL+seeder split; flatten variants
>   at seed; amend α8.5c + ADR-0044 before the held push).
> - **D-G** expanded — besides authoritative `ai_model_pricing`, store a **derived**
>   estimation view (`estimated_generation_cost`, `estimated_download_cost`,
>   `estimated_gpu_minutes`) for planners (derived, non-authoritative — W8.5d.8).
> - **Ruled-in additive metadata (§4.5):** capability **dependencies**, provider/
>   adapter **feature matrix**, **resource estimation**, **output characteristics**.
> - **Scope guard (§2.1):** α8.5d owns **catalogue metadata only** — the resolver
>   (α8.5e) owns scoring/routing/health/selection/balancing/fallback-execution; the
>   planner owns workflow construction/dependency resolution/provider requests.
>
> Baseline: `v0.4.34-phase3-alpha8.5b3r` (+ merged-but-unpushed: CI hardening
> chore, α8.5c registry, ADR-0044). Proposed version: **`0.4.35-phase3-alpha8.5d-dev`**
> (runtime + additive migration → a `-dev` bump, unlike α8.5c).
>
> **Inherited rulings (ADR-0044):** **X-F** MRC-first staging · **X-G** execution/
> hardware/mode metadata is stored in *this* slice's provider/runtime schema (not
> scattered) · additive/zero-freeze-override posture · AR7/AR8 (local-/free-first,
> scoring) + AR9/AR10 (hardware) + AR15 (cost) + AR18 (provenance) are the ARs this
> slice lays **data** for. α8.5d is **MRC-2/-3 foundation**; the *resolver logic* is
> α8.5e, hardware *detection* is α8.5x-exec, and the *planner/estimator use case* is
> the MRC slice.
>
> **New in this pre-flight (user direction, 2026-07-25), all additive:** per-adapter
> **runtime requirements** (execution + hardware + estimated timings), curated
> **device profiles**, **cost estimation** data, and treating **local models as
> ordinary providers/adapters**.

---

## 0. Why this exists

α8.5c produced a *validated, human-authored* capability→provider graph as three
YAML manifests, deliberately **never read at runtime**. α8.5d makes that graph
*queryable by the runtime* by seeding it into Postgres. After this slice:

```
capabilities.yaml + providers.yaml + routing.yaml (+ devices.yaml)   [design-time]
        │  validate (Stage 0)
        ▼
  seed_providers.py  (idempotent upsert)                             [build/deploy]
        ▼
  DB: capabilities / providers / provider_adapters / routing / …     [runtime truth]
        ▼
  α8.5e resolver reads the DB  ·  planner estimates cost  ·  runtime never sees YAML
```

This is the last checkpoint before the platform shifts from *framework
construction* to *AI generation features*: once the registry is in the DB and the
resolver (α8.5e) reads it, adding a provider is data + an adapter, not a redesign.

---

## 1. Grounding — what already exists (reconcile, don't duplicate)

**α8.5c manifests** (`backend/providers/`, `backend/scripts/provider_manifest.py`):
- `capabilities.yaml` — 27 capabilities: `id`, `kind∈{llm,image,video,voice}`,
  `inputs`/`outputs` (typed I/O), `requires`/`optional` params.
- `providers.yaml` — 4 providers (pollinations, huggingface, fal, kokoro), each
  with `pricing`/`quota`/`authentication`/`requires_login`/`config_keys`/`scores`
  and **adapters** (`provider.suffix`, one capability each, `status`, `fallback`,
  `supports`); plus **families/variants** (flux → dev/schnell/kontext).
- `routing.yaml` — `defaults` (strategy/fallback/selection) + `by_capability`.

**Existing DB tables** (`ai_models.py`, migration `0002`):
- `ai_models` — *model*-level catalogue: `model_key` (unique), `provider` (text),
  `kind` (`plugin_kind_enum`), **`capabilities text[]`** (open vocabulary),
  pricing linkage, `status`, `successor_model_id`, `extra jsonb`.
- `ai_model_pricing` — temporal, immutable, per-model, per-unit pricing (**the
  authoritative cost source**).
- `provider_plugin_registrations` — *loaded-plugin* level: `name`+`version`,
  `kind`, `capabilities text[]`, `enabled`, **`last_health_status`/`last_health_at`**
  (operational state already), `extra jsonb`.

**Migration precedent:** `0002_seed_system_data` seeds *required system data* with
`ON CONFLICT DO NOTHING` (idempotent); sample data lives in `scripts/seed_demo.py`
and is never migrated (CONTRIBUTING §Migrations). Next revision id: **`0010`**.

**CI hardening (merged chore):** `ci_gate.py` can run destructive DB stages against
an **ephemeral** (`--ephemeral-db`) or **`VALIDATION_DATABASE_URL`** database with
guaranteed restore-to-`head`. α8.5d's seed round-trip test rides that seam.

**The key reconciliation (static vs operational, per W8.5c.3):**
| Concern | Home | Nature |
| --- | --- | --- |
| Capability vocabulary, providers, adapters, routing, scores, runtime reqs, cost hints, device profiles | **new α8.5d catalogue tables** (seeded from YAML) | **static / declared** |
| Model catalogue + authoritative pricing | **existing `ai_models` / `ai_model_pricing`** | static, but versioned/temporal |
| Loaded-plugin health, latency, quota-remaining, success rate | **existing `provider_plugin_registrations`** (+ future ops tables) | **operational / live** |

α8.5d seeds only the **static catalogue**; it never writes operational state. The
α8.5e resolver *joins* the static catalogue to operational state at query time.

---

## 2. Scope & non-goals

**In scope (α8.5d):**
1. **Manifest evolution** (design-time, additive to α8.5c — amend cleanly *before*
   the held push): adapter `runtime` block, adapter `cost` block, new
   `devices.yaml`; matching Pydantic schema + validator rules (Stage 0).
2. **Schema:** additive migration `0010` creating the catalogue tables (DDL only).
3. **Seeder:** `scripts/seed_providers.py` — idempotent YAML→DB sync.
4. **CI:** a seed round-trip stage (validate → migrate → seed → assert DB == manifest)
   on the ephemeral/validation DB.
5. **Docs + version bump** (`-dev`), invariants **W8.5d.1–9**.

**Explicitly out (later slices):**
- **Resolver logic** (capability + strategy → provider) → **α8.5e** (MRC-2/-3).
- **Hardware detection** / `IExecutionEnvironment` / device matching → **α8.5x-exec** (AR9/AR10).
- **Planner + cost-estimator use case** (project-level estimate) → **MRC-1** (α8.5x-mrc).
  α8.5d only *stores the data* an estimator needs, plus a pure preview helper.
- Any change to `ai_models`/pricing semantics, or to the frozen orchestration
  surface (ADR-0042). No provider *code* adapters are implemented here (all remain
  `status: planned`).

### 2.1 Scope guard — α8.5d is *catalogue metadata*, not "the provider system"

The temptation is to grow α8.5d into the whole runtime. It must not. Ownership:

| Concern | Owner | Slice |
| --- | --- | --- |
| Rich, validated, **seeded** catalogue metadata | **α8.5d** | this slice |
| Scoring · routing · health · selection · balancing · **fallback execution** | **Resolver** | α8.5e |
| Workflow construction · dependency resolution · provider **requests** | **Planner** | α8.5x-mrc |

α8.5d only makes the future resolver/planner *possible* by ensuring the catalogue
is rich enough that neither has to hardcode provider knowledge.

---

## 3. The nine design questions (answered)

**Q1 — How does validated YAML seed the DB?**
Two artifacts, cleanly separated (Fork **D-C**): migration `0010` creates the
**tables** (reproducible, static DDL); `scripts/seed_providers.py` loads the
**already-validated** manifests (reusing `provider_manifest.py`) and **upserts** the
**data** in one transaction. Data is *not* embedded in the migration because it is
derived from frequently-revised, git-versioned YAML — coupling it to a migration
would force a new migration per catalogue edit. The seeder runs in CI and on deploy.

**Q2 — How are providers versioned?**
The manifest set carries a **content digest** (sha256 over the three/four files) +
an optional top-level `revision:` int. The seeder records `{digest, revision,
seeded_at}` in a singleton **`provider_registry_meta`** row and no-ops when the
digest is unchanged (fast idempotency + observability). Per-adapter *semantic*
versioning is deferred until adapters are *implemented* (they're all `planned`
now); `provider_plugin_registrations.version` continues to version loaded **code**.

**Q3 — How are capability changes handled?**
The catalogue is an **open text vocabulary** (already `capabilities text[]` in the
legacy tables). Adding a capability = a manifest row + a re-seed (no migration);
`kind` stays the 4-value `plugin_kind_enum` and is extended only when a runtime
serves a genuinely new coarse kind (rare, migration-bearing). Removed capabilities
are **deprecated, not deleted** (W8.5d.3) so historical references survive.

**Q4 — How do seed updates stay idempotent?**
Upsert on natural keys (`capability.id`, `provider.id`, `adapter.id`,
`device_profile.id`) via `INSERT … ON CONFLICT (id) DO UPDATE`. A second run with
the same digest is a **no-op** (W8.5d.2); a changed manifest converges the DB to
the manifest exactly (W8.5d.5, asserted by the CI round-trip). Ordering is
deterministic; the whole sync is one transaction (all-or-nothing).

**Q5 — Planned vs implemented adapters?**
`provider_adapters.status ∈ {planned, implemented}` (already in the manifest).
`planned` adapters are **catalogue/estimation-only** — visible to the planner for
graph/cost planning but **never runtime-selectable**. The α8.5e resolver filters to
`status='implemented' AND enabled` (W8.5d.4). This lets the whole graph exist as
data long before any adapter code is written.

**Q6 — How are execution preferences (local/cloud) stored?**
On each adapter's **`runtime.execution`** (`local`/`cloud` booleans), seeded to
`provider_adapters` (Fork **D-B**: `runtime jsonb` vs typed columns). The resolver's
local-first ordering (AR7) reads these; local adapters are ordinary rows with
`execution.local=true` (Q from the "Local Model Registry" direction).

**Q7 — How are hardware requirements represented?**
On each adapter's **`runtime.hardware`** — `minimum_ram_gb`, `recommended_ram_gb`,
and a **`gpu`** backend map (`metal`/`cuda`/`rocm`/`cpu`) — plus **`runtime.estimated`**
timings. Curated **`device_profiles`** (a separate reference table) describe known
machine classes. α8.5d **stores** both; the *matching* (detected device → compatible
adapters) is α8.5x-exec (W8.5d.7).

**Q8 — How do provider variants inherit defaults?**
Inheritance is **resolved at seed time** (Fork **D-F**): a variant row = family
defaults ⊕ provider defaults ⊕ variant overrides, flattened so the runtime reads
**fully-resolved rows with zero runtime inheritance logic** (W8.5d.6). Family/parent
lineage is retained for provenance. Recommendation: variants seed into the existing
`ai_models` catalogue (reconciling the two representations), linked to their provider.

**Q9 — How is backward compatibility maintained?**
Migration `0010` is **purely additive** (new tables only; no change to `ai_models`/
pricing/`provider_plugin_registrations` or any frozen surface). The seeder is
**non-destructive**: entries dropped from the manifest are **disabled**
(`enabled=false`)/deprecated, never hard-deleted (W8.5d.3), preserving referential
integrity with future usage/history. Downgrade drops only the new tables.

---

## 4. Manifest evolution (design-time additions, additive to α8.5c)

All additions are **optional** (absent ⇒ "unspecified"), so existing manifests stay
valid. Amended into α8.5c cleanly *before* the held push.

### 4.1 Adapter `runtime` block (Provider Requirements — AR7/AR9)
```yaml
adapters:
  - id: comfyui.flux_schnell
    capability: image_generation
    status: planned
    runtime:
      execution: { local: true, cloud: false }
      hardware:
        minimum_ram_gb: 16
        recommended_ram_gb: 32
        gpu: { metal: true, cuda: false, rocm: false, cpu: true }
      estimated:
        startup_seconds: 8
        unit_seconds: 6          # per output unit of THIS adapter's capability
```
*Refinement (Fork **D-E-timings**):* because an adapter serves exactly one
capability, `estimated` is a single `unit_seconds` + `startup_seconds` (not the
per-capability `image_/video_generation_seconds` of the illustrative example).

### 4.2 Adapter `cost` block (Cost Prediction — AR15/AR18)
```yaml
    cost:
      unit: image              # image | second | minute | token | character | request
      amount: 0.0              # per unit in `currency`; 0 when free within quota
      currency: GBP
      source: declared         # declared | derived | unknown
```
Reconciliation with `ai_model_pricing` (Fork **D-G**): where an adapter maps to a
priced `ai_models` row, `source: derived` and the authoritative figure is the
temporal pricing table; free/freemium-within-quota ⇒ `0`; otherwise `declared` hint
for planning only. **Cost data is never a billing source** (W8.5d.8) — billing stays
`ai_model_pricing` + `usage_records`.

### 4.3 `devices.yaml` (Device Profiles — AR9/AR10)
```yaml
device_profiles:
  - id: intel_macbook_2019
    ram_gb: 16
    gpu: amd
    backend: metal            # metal | cuda | rocm | cpu
    unified_memory: false
    preferred_mode: balanced  # quick | balanced | quality | ultra  (AR11)
  - id: macbook_m1
    ram_gb: 16
    gpu: apple
    backend: metal
    unified_memory: true
    preferred_mode: quality
  - id: windows_cuda
    gpu: nvidia
    backend: cuda
    preferred_mode: quality
```
Curated **design-time reference data** (Fork **D-H**). Runtime *detection* matches a
live machine to the nearest profile (or computes compatibility directly from
`runtime.hardware`) — **α8.5x-exec**, not here.

### 4.4 Validator additions (Stage 0, offline)
- `runtime.execution` has ≥1 of `local|cloud` true; `gpu` has ≥1 backend true.
- `recommended_ram_gb ≥ minimum_ram_gb`; timings ≥ 0.
- `cost.source=derived` ⇒ adapter resolves to an `ai_models`/pricing link (checked
  at seed, warned at validate); `pricing:free` provider ⇒ adapter `cost.amount==0`
  (free-provider sanity, extends the α8.5c rule).
- `device_profiles`: unique ids; `backend`/`preferred_mode` in-enum; `ram_gb>0`.
- Local adapters (`execution.local=true`) with a cloud-only provider `authentication`
  ⇒ warning (a local adapter shouldn't need a cloud key).

### 4.5 Additional metadata categories (ruled in 2026-07-25)

All additive/optional; they enrich the catalogue so the planner/resolver never
hardcode provider knowledge. Each is validated at Stage 0 and seeded by α8.5d.

**(a) Capability dependencies** (`capabilities.yaml`) — capability→capability, a
`dependencies` block **distinct from** the existing param-level `requires`/`optional`:
```yaml
- id: video_generation
  kind: video
  inputs: [text]
  outputs: [video]
  requires: [prompt]            # existing: request PARAMETERS
  optional: [reference_image, duration_seconds, resolution, fps, seed]
  dependencies:                 # NEW: prerequisite CAPABILITIES
    requires: [image_generation]
    optional: [music_generation, text_to_speech, subtitle_generation]
```
*Validator:* referenced ids exist; `requires`/`optional` disjoint; no self-dep; the
`dependencies.requires` graph is **acyclic** (new cycle check). *Seeded* to a
`capability_dependencies` table (highly-variable ⇒ own table, D-B). Invaluable for
the planner's graph construction.

**(b) Feature matrix** (adapter, `providers.yaml`) — fine *features* (not
capabilities) from a controlled vocabulary, so we don't spawn dozens of tiny
capabilities:
```yaml
    features: [txt2img, img2img, negative_prompt, seed_control, reference_image,
               consistent_character, lora, inpainting, outpainting, motion_control,
               face_reference, depth_control, pose_control]
```
Adapter-level (features vary per adapter/model, not per provider). *Validator:*
in-vocabulary, no dups, applicable to the adapter's capability `kind` (e.g.
`motion_control` only on `video`). Seeded as **JSONB** (semi-structured, D-B).

**(c) Resource estimation** (adapter `runtime.estimated`) — supersedes §4.1's coarse
timings with the full planning/scheduling set:
```yaml
    runtime:
      estimated:
        cold_start_seconds, warm_start_seconds,
        image_seconds, video_seconds, audio_seconds,
        peak_ram_gb, peak_vram_gb, disk_gb
```
All optional, ≥0. Seeded as **JSONB**. Powers scheduling, local execution,
batching, progress estimates, and device selection (α8.5x-exec) — **stored** here.

**(d) Output characteristics** (adapter `outputs`) — concrete container/codec
formats, so downstream planning needs no adapter-specific logic:
```yaml
    outputs:
      image: [png, jpg, webp]
      video: [mp4, webm, gif]
      audio: [wav, mp3, opus]
```
*Validator:* format tokens in a controlled vocab; output **io-type keys must be a
subset of the adapter's capability `outputs`** (e.g. an `image_generation` adapter
may only declare an `image:` block). Seeded as **JSONB**.

**(e) Derived cost estimates** (D-G, seeder-computed — *not* manifest input) —
`estimated_generation_cost`, `estimated_download_cost`, `estimated_gpu_minutes`
columns computed from `cost` + `runtime.estimated` + `ai_model_pricing`; **derived,
non-authoritative** (W8.5d.8).

---

## 5. Target DB schema (additive migration `0010`, DDL only)

New catalogue tables (all seeded; none touch existing tables):

| Table | Key columns | Notes |
| --- | --- | --- |
| `capabilities` | `id` PK, `kind` (`plugin_kind_enum`), `inputs text[]`, `outputs text[]`, `requires text[]`, `optional text[]` | the vocabulary |
| `capability_dependencies` | (`capability_id` FK, `depends_on_id` FK, `kind` `'requires'`\|`'optional'`) | §4.5(a); highly-variable ⇒ own table; acyclic on `requires` |
| `providers` | `id` PK, `name`, `homepage`, `license`, `commercial`, `authentication`, `requires_login`, `pricing`, `quota_daily`/`quota_monthly` (`int`/null; null≡unlimited), `config_keys text[]`, `score_quality/cost/speed/reliability`, `enabled`, `extra jsonb` | one row per provider |
| `provider_adapters` | **stable typed:** `id` PK (`provider.suffix`), `provider_id` FK, `capability_id` FK, `status`, `execution_mode`, `implemented`, `enabled`; **cost typed:** `cost_unit`/`cost_amount numeric`/`cost_currency`/`cost_source` + derived `estimated_generation_cost`/`estimated_download_cost`/`estimated_gpu_minutes`; **semi-structured JSONB:** `supports`, `runtime` (hardware+estimated), `features`, `outputs`, `extra` | the runtime-loadable unit (D-B tiers) |
| `adapter_fallbacks` | (`adapter_id` FK, `fallback_adapter_id` FK, `reason`, `ordinal`) | join table ⇒ FK integrity + cycle checks + weighted routing (Fork **D-A2**) |
| `routing_policies` | `scope` (`'default'` \| capability id) PK, `strategy`, `fallback`, `selection` | one default + per-capability overrides |
| `device_profiles` | `id` PK, `ram_gb`, `gpu`, `backend`, `unified_memory`, `preferred_mode`, `extra jsonb` | curated reference data |
| `provider_registry_meta` | singleton: `digest`, `revision`, `seeded_at` | idempotency + version |

New Postgres enums (mirroring the Pydantic StrEnums): `provider_pricing_enum`,
`adapter_status_enum`, `routing_strategy_enum`, `fallback_mode_enum`,
`selection_enum`, `gpu_backend_enum`, `generation_mode_enum`. `kind` reuses the
existing `plugin_kind_enum`.

**Coexistence (not duplication):** `providers`/`provider_adapters` = the *static
catalogue* (what *could* run); `provider_plugin_registrations` = *loaded code
plugins* + health (what *is* running). `ai_models`/`ai_model_pricing` = the *model*
catalogue + authoritative price. Variants (§Q8) flatten into `ai_models`. The α8.5e
resolver joins catalogue × plugin-health × pricing.

---

## 6. Seeder design (`scripts/seed_providers.py`)

- **Validate-then-load:** refuses to run unless all manifests validate (Stage 0
  logic reused); loads via `provider_manifest.py` (no second parser).
- **Digest short-circuit:** compute the manifest digest; if it equals
  `provider_registry_meta.digest`, **no-op** (W8.5d.2).
- **Upsert, single transaction:** `INSERT … ON CONFLICT (id) DO UPDATE` for
  capabilities → providers → adapters → fallbacks → routing → devices, in
  dependency order; variants flattened into `ai_models` (W8.5d.6).
- **Non-destructive convergence:** ids present in DB but absent from the manifest
  are set `enabled=false` (adapters/providers) / deprecated (capabilities) — never
  `DELETE` (W8.5d.3). A guarded `--prune` (default off) is available for local dev.
- **Idempotent + reproducible:** re-running converges to a byte-identical registry
  for a given digest (W8.5d.5).
- **Flags:** `--database-url`, `--dry-run` (print diff, touch nothing), `--prune`.

---

## 7. CI integration (rides the hardening seam)

Add a **seed round-trip** to the DB-dependent stages (runs against the ephemeral /
`VALIDATION_DATABASE_URL` database, auto-restored to `head`):

```
Stage 0  validate_providers                (offline, existing)
   …     migrate to head                   (existing DB stages)
   +     seed_providers.py --database-url $VALIDATION_DATABASE_URL
   +     assert DB == manifest             (round-trip: W8.5d.5)
   +     re-run seeder ⇒ 0 rows changed    (idempotency: W8.5d.2)
```
Placed inside the destructive/isolated region so a failed seed can never degrade a
shared DB (the whole reason for the hardening chore). Stage numbering avoids
renumbering 1–10 (same rationale as α8.5c's Stage 0).

---

## 8. Invariants (proposed)

- **W8.5d.1** — the DB is the sole runtime source of truth for the registry; the
  runtime never reads the YAML (extends W8.5c.2).
- **W8.5d.2** — seeding is idempotent: an unchanged digest is a no-op; a second run
  changes zero rows.
- **W8.5d.3** — seeding is non-destructive: manifest-removed entries are
  disabled/deprecated, never hard-deleted.
- **W8.5d.4** — only `implemented AND enabled` adapters are runtime-selectable;
  `planned` adapters are catalogue/estimation-only and never dispatched.
- **W8.5d.5** — the seeded registry is a faithful projection of the validated
  manifest (DB == manifest for the current digest), asserted in CI.
- **W8.5d.6** — variant/family defaults are fully resolved at seed time; the runtime
  reads flattened rows (no runtime inheritance).
- **W8.5d.7** — device profiles + adapter runtime/hardware requirements are
  declarative data only; α8.5d performs no hardware detection or selection.
- **W8.5d.8** — cost metadata is estimation-only; authoritative cost remains
  `ai_model_pricing` + `usage_records`.
- **W8.5d.9** — α8.5d changes no ADR-0042 frozen surface (Gate 1 = No) and no render
  composition (Gate 2 = N/A).

---

## 9. Freeze / boundary gates (ADR-0044 D6)

- **Gate 1 (ADR-0042 freeze): No.** New additive tables + a seeder script + manifest
  additions. No runner/dispatcher/registry-code/usage/relay/lock surface touched.
  Zero freeze overrides.
- **Gate 2 (ADR-0043 render boundary): N/A.** Nothing in composition/render/export.

---

## 10. Version & migration

- **Version:** `0.4.35-phase3-alpha8.5d-dev` → finalized on sign-off + green gate.
  (First α8.5x runtime slice ⇒ the first `-dev` bump since α8.5c's docs-only work.)
- **Migration:** `0010_provider_registry` — additive DDL only (new enums + tables);
  downgrade drops them. Idempotent **data** lands via the seeder, not the migration.

---

## 11. Forks needing a ruling

- **D-A — Schema strategy.** *(Rec)* **New catalogue tables** coexisting with
  `ai_models`/`provider_plugin_registrations`, vs overloading the legacy tables.
  - **D-A2 — Fallbacks:** a `adapter_fallbacks` **join table** (FK integrity + cycle
    checks) *(Rec)* vs a `fallback text[]` column.
- **D-B — Runtime/supports storage.** `runtime`/`supports` as **`jsonb`** columns
  *(Rec — flexible, additive)* vs fully typed columns (rigid, migration-per-field).
  *(cost is proposed as typed columns for indexed estimation.)*
- **D-C — Seeder mechanism.** **DDL migration + idempotent seeder script** *(Rec)*
  vs data-in-migration (like `0002`). Rec keeps catalogue edits migration-free.
- **D-D — Versioning.** **Manifest digest + optional `revision:` in meta** *(Rec)*;
  per-adapter semver deferred until adapters are implemented.
- **D-E — Planned/implemented + timings.** Confirm resolver filters to
  `implemented AND enabled` (W8.5d.4); adopt single per-adapter
  `estimated.unit_seconds`+`startup_seconds` (D-E-timings).
- **D-F — Variant inheritance.** **Flatten at seed into `ai_models`** *(Rec)* vs a
  new `model_variants` table vs runtime inheritance.
- **D-G — Cost source.** **Derive from `ai_model_pricing` where linked; else
  declared hint; else unknown; free⇒0** *(Rec)*, cost is estimation-only (W8.5d.8).
- **D-H — Device profiles.** **Curated design-time `devices.yaml` seeded to
  `device_profiles`** *(Rec)*; runtime detection/matching is α8.5x-exec.
- **D-I — ADR-0044 addendum + amend-before-push.** Since the push is held: fold the
  §4 manifest additions into the α8.5c files/schema/validator and add a **one-row
  ADR-0044 change-log addendum** (α8.5d introduces the concrete runtime/device/cost
  schema realizing AR7/AR9/AR10/AR15/AR18) — **amended cleanly**, not a follow-up
  governance commit. *(Rec: yes.)*

## 12. Sign-off checklist

- [x] **D-A / D-A2** new catalogue tables alongside legacy; fallback **join table** (`+reason,+ordinal`)
- [x] **D-B** three tiers — stable⇒typed, semi-structured⇒JSONB, highly-variable⇒tables
- [x] **D-C** seeder mechanism (migration DDL + seeder script)
- [x] **D-D** versioning (digest + revision)
- [x] **D-E** planned/implemented filter + per-adapter resource estimation (§4.5c)
- [x] **D-F** variant inheritance (flatten at seed → `ai_models`)
- [x] **D-G** cost source `ai_model_pricing` + derived estimation view
- [x] **D-H** device profiles as curated design-time data
- [x] **D-I** amend α8.5c manifests/schema + ADR-0044 addendum before the held push
- [x] **Ruled-in metadata (§4.5):** capability dependencies · feature matrix · resource estimation · output characteristics
- [x] **Scope guard (§2.1):** α8.5d = catalogue metadata only (resolver α8.5e / planner own logic)
- [x] Invariants **W8.5d.1–9** accepted
- [x] Version `0.4.35-phase3-alpha8.5d-dev` + migration `0010` (additive) accepted
- [x] Scope/non-goals (resolver→α8.5e, hardware detection→α8.5x-exec, estimator→MRC) accepted

---

## Change log

| Date | Change |
|---|---|
| 2026-07-25 | **SIGNED OFF** — D-A…D-I approved with refinements: catalogue tables alongside legacy (D-A); fallback **join table** with `reason`+`ordinal` (D-A2); **three storage tiers** stable⇒typed / semi-structured⇒JSONB / highly-variable⇒tables (D-B); DDL+seeder split (D-C); flatten variants at seed (D-F); derived cost-estimation view atop `ai_model_pricing` (D-G); amend α8.5c + ADR-0044 before the held push (D-I). Ruled in four additive metadata categories (§4.5): **capability dependencies**, **feature matrix**, **resource estimation**, **output characteristics**. Added a **scope guard** (§2.1): α8.5d owns catalogue metadata only — resolver (α8.5e) owns scoring/routing/health/selection/fallback-execution; planner owns workflow/dependency/requests. |
| 2026-07-25 | Initial **DRAFT** — α8.5d seeds the α8.5c capability registry into the DB (the runtime source of truth). Answers the nine design questions (seed mechanism, versioning, capability changes, idempotency, planned-vs-implemented, execution prefs, hardware reqs, variant inheritance, backward compat); adds the user-directed **runtime requirements**, **device profiles**, **cost estimation**, and **local-model-as-provider** treatment (all additive to α8.5c). Proposes additive migration `0010` (catalogue tables, DDL only) + an idempotent `seed_providers.py` + a CI seed round-trip on the ephemeral/validation DB, invariants **W8.5d.1–9**, version `0.4.35-phase3-alpha8.5d-dev`. Freeze Gate 1 = No, Gate 2 = N/A. Forks **D-A…D-I** raised for sign-off. |
