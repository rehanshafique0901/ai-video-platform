# ADR-0046 — The Execution Runtime Is a Persistent Consumer; It Records What Happened, It Does Not Decide What Should

**Status:** Accepted. Unlike the governance-only **ADR-0045**, this ADR ships **with**
its implementation (Increment 4): migration `0012` (the `generations`,
`generation_shots`, `generation_assets`, `model_cache` tables), the raw-SQL Execution
Runtime repositories + store, and the persistent lifecycle wired into `GenerateVideo`.
It freezes the **Execution plane's** boundaries the way ADR-0045 froze the Decision
plane's. Every later slice (video-generation providers, local engines, repair strategies,
publishing) cites it and stays *additive* to the frozen surface.

**The inflection point.** ADR-0045 froze the Decision core: `Request → Resolver → Ordered
Candidates`. Increments 1–3 built the Execution plane as an in-memory pipeline; Increment 4
makes it **persistent** — every generation now records its full lifecycle, artefacts, and
provenance — without letting it absorb planning or selection.

```
Planner ── immutable GenerationPlan ──┐
                                       ▼
Resolver ── ordered candidates ──▶ Execution Runtime ──┬── Generation Ledger  (generations + generation_shots)
                                                        ├── Asset Store        (generation_assets, lineage graph)
                                                        ├── Resolution Ledger  (generation_resolution_ledger, reused)
                                                        ├── Model Cache        (model_cache, via IModelManager)
                                                        ├── Execution State     (generations.status machine)
                                                        └── Provenance + Events (component versions, outbox)
```

**Builds on:** **ADR-0044** (AI runtime & generation architecture), **ADR-0045** (AI
runtime core freeze; three planes, F1–F7), the α8.5e `RESOLVER_RUNTIME_CONTRACT.md`, and
the α8.6 `EXECUTION_RUNTIME_CONTRACT.md` (invariants W8.6.1–8, state machine, table
ownership).

---

## The frozen boundaries (X1–X8)

These restate `EXECUTION_RUNTIME_CONTRACT.md` §2 as governance a future slice may **not**
cross without its own ADR. They are the Execution-plane counterpart to ADR-0045's F1–F7.

- **X1 — Execution never chooses providers.** It consumes the resolver's ordered candidate
  list; it contains no scoring and no `if provider == "…"` logic (upholds ADR-0045 F2).
- **X2 — Execution never plans shots.** Shot decomposition is the Planner's; Execution
  consumes an immutable `GenerationPlan` (upholds ADR-0045 F3).
- **X3 — Execution consumes immutable plans + ordered candidates only** (read-only value
  objects).
- **X4 — All generated artefacts flow through the Asset Store.** Every frame / reference /
  mask / audio / video / thumbnail / metadata blob is a `generation_assets` row; nothing
  else in the Execution plane owns media.
- **X5 — Every execution produces provenance + ledger entries.** A `generations` row and a
  `generation_resolution_ledger` row are written per run, carrying the full component-version
  set and the ranked candidate list — reproducible after the catalogue changes (values, not
  FKs).
- **X6 — All model acquisition goes through the Model Manager** (`IModelManager` +
  `model_cache`); weights are never fetched ad-hoc.
- **X7 — All progress changes go through the Execution State machine** (`generations.status`);
  no ad-hoc status writes.
- **X8 — `generation_assets` is execution-owned.** Promotion into the platform's
  `media_assets` library is an **explicit** future use case (`PublishGenerationAssets`),
  never a direct write. The generation domain and the content/platform domain stay decoupled.

---

## Design rulings frozen by this ADR

Two forks were decided for Increment 4 and are now boundaries, not preferences:

- **Q1 — Asset persistence: a new execution-owned `generation_assets` table** (not reuse of
  `media_assets`). Generated artefacts have a different lifecycle (intermediate frames,
  repaired frames, masks, verification artefacts, pre-publication renders — many never
  become user media) and generation has no tenant/owner/project/publishing context yet.
  `parent_asset_id` makes repair a **lineage graph** rather than an overwrite. Promotion to
  `media_assets` is deferred to `PublishGenerationAssets` (X8).
- **Q2 — Persistence style: raw SQL + repositories + validator allowlist**, ORM-free, matching
  0010/0011 (ADR-0045 F4/F5). Introducing SQLAlchemy aggregates for only the Execution
  Runtime would create two persistence philosophies in one subsystem; a future whole-runtime
  ORM migration would be its own deliberate ADR.

---

## What this ADR is *not*

It freezes **boundaries**, not features. New generation capabilities, video/audio providers,
local engines, repair strategies, verification models, event consumers, and a publish/
promotion workflow are all expected and **additive** — they require no core change. Deferred
by design (not forbidden): distributed workers / queues (Redis/Celery/K8s), an actual model
downloader (no local adapter until Increment 6), resume/cancellation *behaviour* (the state
machine is persisted to make it possible later), and any external event bus (lifecycle events
use the existing transactional outbox with no external consumers yet).

---

## Consequences

- **Positive.** Every generation is now replayable and attributable (per-component version
  provenance + full ranked resolution). Artefacts have a durable, graph-structured registry
  ready for targeted verification/repair. The Execution plane stays a clean bounded context:
  it can gain providers/engines/publishing without reshaping the Decision or Knowledge planes,
  and it never couples prematurely to the content-management domain.
- **Cost.** More writes per run (state transitions, per-shot rows, asset rows, events) and a
  small indirection through the store port. Persistence is incremental (short transactions,
  never one long transaction across a multi-minute run), so a long generation never holds a DB
  transaction open — at the cost of the run not being a single atomic unit (intended: partial
  progress is observable and resumable-in-principle).

---

## Change log

| Date | Change |
| --- | --- |
| 2026-07-25 | Accepted. Freezes the Execution Runtime boundaries (X1–X8) with Increment 4; records the Q1 (new `generation_assets`) and Q2 (raw-SQL, ORM-free) rulings. Ships with migration `0012`, the Execution Runtime repositories/store, and the persistent lifecycle in `GenerateVideo`. Cites `EXECUTION_RUNTIME_CONTRACT.md`. |
