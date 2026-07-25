# AI Runtime — The Three Planes (Knowledge · Decision · Execution)

**Status:** Engineering overview (companion to **ADR-0045**, which freezes these
boundaries). Not an ADR — this document *explains* the architecture; ADR-0045 *enforces*
it. Written after α8.5e, once the Decision plane became real.

The platform separates into three planes with **different mutability models and different
invariants**. Understanding *why* the boundaries exist matters as much as *how* the code
is organised: each plane changes at a different cadence and must not absorb another's
responsibilities.

> For the end-to-end request flow — *how one prompt becomes one exported video*, with
> each stage linked to its governing ADR/contract — see the companion
> [`SYSTEM_MAP.md`](./SYSTEM_MAP.md). This document explains the planes; that one maps
> the pipeline through them.

```
        KNOWLEDGE                 DECISION                    EXECUTION
   (what CAN exist)          (what SHOULD happen)         (what DID happen)

  capabilities.yaml   ─┐
  providers.yaml       ├─►  Catalogue Reader ─┐
  routing.yaml         │                      ├─► Resolver ─► ordered ─► Generation Runtime
  devices.yaml        ─┘                      │   (pure)      candidates    (adapters, GPU,
        │                Runtime Reader ──────┘      │                       FFmpeg, uploads)
   Validator                (health/quota/metrics)   │                            │
        │                                            │                            ▼
   Seeder ─► Catalogue tables (0010)                 │                    operational state (0011)
                                                     └─► Resolution Ledger ◄──────┘
```

---

## 1. Knowledge plane — *what can exist*

- **Owns:** the capability catalogue, providers, adapters, routing policy, device
  profiles, and catalogue provenance (`catalogue_version`, `manifest_digest`).
- **Mutability:** design-time. Authored as YAML (`backend/providers/*.yaml`), validated
  offline, seeded into the `0010` catalogue tables. Changes land through review + reseed,
  never at request time.
- **Invariants:** no operational columns ever (W8.5d.10); runtime never reads YAML
  (W8.5c.2); the seeder is the only writer of catalogue tables.
- **Source of truth:** `docs/engineering/PROVIDER_RUNTIME_DATA_MODEL.md`.

## 2. Decision plane — *what should happen*

- **Owns:** the resolver (α8.5e) and, in future, the planner and verifier — **pure
  functions** that consume Knowledge + operational snapshots and produce deterministic,
  explainable decisions.
- **Mutability:** none. No side effects, no I/O beyond receiving immutable snapshots
  (`CatalogueSnapshot` W8.5e.6, `RuntimeSnapshot`). Same inputs ⇒ byte-identical output
  (W8.5e.4).
- **Invariants:** never executes (W8.5e.1), never mutates the catalogue (W8.5e.2) or
  health (W8.5e.3); scoring is machine-readable and versioned (`score_schema`, W8.5e.5);
  ordering is stable via an explicit comparator (W8.5e.7); no provider-specific branching
  (W8.5e.8).
- **Source of truth:** `docs/engineering/RESOLVER_RUNTIME_CONTRACT.md`.

## 3. Execution plane — *what did happen*

- **Owns:** provider adapters, local GPU engines (ComfyUI, Ollama, Stable Diffusion,
  Flux), FFmpeg, uploads and publishing — plus the operational tables (`provider_health`,
  `provider_quota_state`, `adapter_runtime_metrics`, `local_runtime_state`), the
  `generation_resolution_ledger`, and (α8.6 Increment 4) the **persistent Execution
  Runtime**: the generation ledger (`generations` + `generation_shots`), the execution
  artefact registry (`generation_assets`, with a `parent_asset_id` lineage graph), the
  model cache (`model_cache`), the `generations.status` state machine, and lifecycle
  events via the transactional outbox.
- **Mutability:** stateful, side-effecting. Writes operational state, artefacts, and
  provenance; consumes the resolver's ordered candidates (executes the first, falls back
  on failure). Persists incrementally in short transactions (a multi-minute run never
  holds one open).
- **Invariants:** never scores (F2) and never plans (F3); writes health/quota/metrics that
  the Decision plane only reads (F5); records the full ranked `candidate_list` for replay
  (AR18); artefacts are execution-owned and reach `media_assets` only via an explicit
  promotion use case. Frozen by **ADR-0046** (X1–X8, `EXECUTION_RUNTIME_CONTRACT.md`).

---

## Why the separation is worth it

- **Reproducibility.** The ledger stores the catalogue version, manifest digest, resolver
  version, and full candidate list, so any past decision can be replayed even after the
  catalogue changes.
- **Testability.** Decision = fast pure unit tests; readers/writers = repository tests;
  Execution = live integration. Each plane is verified in isolation.
- **Vendor neutrality & free-first.** Because Execution never scores and the planner never
  names providers, adding a free local engine or swapping a cloud provider is a Knowledge
  change (a manifest entry) — not a code change to the decision logic.
- **Consistency is a Verification concern, not a generator concern.** Character identity,
  lip-sync, temporal continuity, and drift checks belong to the (future) Verification
  plane + targeted Repair — never inside the generator, and never inside the resolver.

---

## Roadmap after the freeze (all additive)

1. **Generation Runtime (MRC)** — invoke adapters using the ordered candidates.
2. **Verification Runtime** — identity/prompt/temporal/watermark drift detection.
3. **Repair Runtime** — retry only the failed capability, not the whole project.
4. **Project Memory** — reuse prior assets, embeddings, scenes.
5. **Publishing** — YouTube / TikTok / Instagram adapters.

Each plugs into stable seams; none revisits the frozen boundaries (ADR-0045 F1–F7 for the
Decision plane, ADR-0046 X1–X8 for the Execution plane).
