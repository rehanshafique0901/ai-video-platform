# ADR-0045 — The AI Runtime Core Is Frozen; Providers & Engines Plug In, They Do Not Reshape the Decision Boundaries

**Status:** Accepted (a **governance** decision — it ships documentation and the
boundary contract, but **no application code, no schema migration, and no runtime
behaviour change**). This is the AI-runtime analogue of **ADR-0042**, which froze the
orchestration platform. Every subsequent slice (Generation Runtime, Verification,
Repair, Project Memory, Publishing, new providers, local engines) cites it and must stay
*additive* to the frozen surface.

**The inflection point.** α8.5c built the design-time capability catalogue; α8.5d seeded
and versioned it into the database; α8.5e turned it into a runtime **decision engine**:
a pure, deterministic, explainable resolver that maps a capability request to an ordered
candidate list. With `feat/alpha8.5e-resolver-runtime` the decision loop closed:

```
Design-time YAML → Validator → Seeder → Runtime Catalogue
                                              │
                          Catalogue Reader ───┤
                                              ▼
Request ─────────────────────────────────► Resolver ─► Ordered Candidates ─► Execution
                          Runtime Reader ───▲                                    │
                                            └──────── Resolution Ledger ◄────────┘
```

That was the **last architectural milestone** for AI runtime decision-making. The
remaining work — actually invoking adapters (Generation Runtime / MRC), checking outputs
(Verification), retrying the failed capability (Repair), reusing assets (Project Memory),
and Publishing — is **integration built on top of stable seams**, not decision-engine
design. This ADR makes that boundary explicit and treats it as the contract future
contributors work within.

**Builds on:** **ADR-0041** (provider runtime contract), **ADR-0042** (orchestration
freeze), **ADR-0044** (AI runtime & generation architecture; AR1–AR18, MRC), the α8.5d
`PROVIDER_RUNTIME_DATA_MODEL.md`, and the α8.5e `RESOLVER_RUNTIME_CONTRACT.md`.

---

## The three planes

The platform has settled into three planes with **different mutability models and
different invariants**. This is stronger than a layered architecture because each plane
answers a different question and is allowed to change at a different cadence.

| Plane | Owns | Mutability | Examples |
| --- | --- | --- | --- |
| **Knowledge** | what *can* exist | design-time, seeded | capabilities, providers, adapters, routing policy, device profiles, catalogue metadata |
| **Decision** | what *should* happen | pure functions over snapshots | resolver, planner, verifier — deterministic, no side effects |
| **Execution** | what *did* happen | stateful, side-effecting | provider adapters, local GPU (ComfyUI/Ollama/Flux/SD), FFmpeg, uploads, publishing, operational state, ledger |

See `docs/engineering/AI_RUNTIME_PLANES.md` for the full overview.

---

## The frozen boundaries (F1–F7)

These are the rules a future slice may **not** cross without its own ADR:

- **F1 — The resolver never executes.** It returns an ordered candidate list; it never
  invokes an adapter, generates media, or performs I/O beyond reading snapshots.
- **F2 — Execution never scores.** Ranking lives only in the Decision plane. Execution
  consumes the ordered list and records outcomes; it does not re-order on quality/cost.
- **F3 — The planner never chooses providers directly.** It asks for capabilities; the
  resolver selects. No `if provider == "…"` in the planner (mirrors W8.5e.8).
- **F4 — The catalogue is design-time + seeded runtime metadata only.** It never gains
  operational columns (health, latency, quota-remaining, queue depth) — W8.5d.10.
- **F5 — Runtime (operational) state stays operational.** `provider_health`,
  `provider_quota_state`, `adapter_runtime_metrics`, `local_runtime_state` are written by
  Execution/Health workers and only *read* by the Decision plane (W8.5e.3).
- **F6 — Provider adapters remain capability-driven.** A new provider is a manifest entry
  + adapter registration; it never edits the resolver, planner, or catalogue schema.
- **F7 — No provider-specific logic in the planner or resolver.** Every decision derives
  from catalogue metadata + operational state, never a hard-coded provider identity.

---

## What this ADR is *not*

It does not freeze features — it freezes **boundaries**. New capabilities, providers,
local engines, verification models, routing strategies, and publishing destinations are
all expected and welcome; they are **additive** and require no core change. If a slice
*appears* to need a boundary change, that is a signal to surface a genuine architectural
gap that earns its own ADR — not something that slips into a feature branch.

---

## Consequences

- **Positive.** Contributors add providers/engines without re-reading the whole system;
  decisions stay reproducible (provenance ledger); each plane is independently testable
  (pure unit tests for Decision, integration for readers/writers, live for Execution).
- **Cost.** A little indirection: capability requests route through the resolver rather
  than calling a provider directly. That indirection is the point — it is what keeps the
  system free-first, fallback-capable, and vendor-neutral.

---

## Change log

| Date | Change |
| --- | --- |
| 2026-07-25 | Accepted. Freezes the AI runtime core (F1–F7) after α8.5e; names the three planes (Knowledge / Decision / Execution) and points to `AI_RUNTIME_PLANES.md`. Governance-only: no code, schema, or behaviour change; no version bump. |
