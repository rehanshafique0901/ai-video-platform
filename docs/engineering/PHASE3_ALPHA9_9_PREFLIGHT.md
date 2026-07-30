# Phase 3 — α9.9 Pre-flight: Execution Adapter Dispatch

> **Status:** **Approved** 2026-07-30 — PF1–PF7 signed off; architecture phase closed.
> **Baseline (frozen):** `v0.4.51-phase3-alpha9.8` (branched from its descendant `main`; docs-only delta)
> **Target version:** `0.4.52-phase3-alpha9.9-dev` → `0.4.52-phase3-alpha9.9`
> **Governed by:** [`ADR-0054`](../decisions/ADR-0054-execution-adapter-dispatch.md) (Accepted 2026-07-30)
>
> Design blueprint only. No code, no migration, no fixture accompanies this document.

---

## 1. What the ADR fixed, and what this document adds

ADR-0054 settled four decisions and froze seventeen consequences of them: **executability authority**
lives in deployed code, expressed to the Decision plane as a deployment-scoped input (D1);
**provenance semantics** are defined per field, separating what was *selected* from what *produced*
bytes (D2); the **ordered candidate list** is the canonical fallback representation and authored
chains are metadata (D3); and **Execution owns invocation while Decision owns ordering**, with no
walking in this slice (D4).

What the ADR deliberately left open: the concrete type of the executable-set input and where it is
assembled, the registry's shape and failure semantics, the container's lifecycle for N adapters, the
exact write source for every provenance field, and how Stage 13 stops asserting the defect. This
document fixes those, and nothing else.

**The ADR is frozen.** Where this pre-flight and ADR-0054 appear to disagree, the ADR wins and the
disagreement is a defect in this document. Section 12 records the check for contradictions; none was
found.

---

## 2. Pre-flight rulings that need your explicit sign-off

### PF1 — the executable set is a domain-owned frozen set, injected as a port at the application seam

A new frozen dataclass in `app/domain/resolver/models.py`:

```python
@dataclass(frozen=True, slots=True)
class ExecutableAdapters:
    adapter_ids: frozenset[str]
```

It is a **fourth argument** to `resolve_candidates(request, catalogue, runtime, executable, *, resolver_version)`
and a parameter threaded into `_ineligibility_reason`. The domain therefore receives *data*, never a
registry, and ADR-0045 F7 and the domain import contract are untouched.

At the application seam, `ResolverCapabilityResolver` takes the **registry port** —
`IImageAdapterRegistry` from `app/application/interfaces/` — and builds `ExecutableAdapters` from
`supported_adapters()` per resolution. Injecting the port rather than a pre-computed set keeps one
source of truth; the registry is immutable after composition, so the two are equivalent in value and
the port is cheaper to reason about.

**Why not `CatalogueSnapshot`.** ADR-0054 D1 forecloses it: the object is identified by
`catalogue_version` + `manifest_digest`, and two deployments of one manifest can construct different
adapters, so its identity fields would stop identifying its contents.

**Why not `RuntimeSnapshot`.** Not forbidden by the ADR, but rejected here on construction: every
member is a fleet-wide measurement and `RuntimeStateReader.load_snapshot()` is defined as exactly
three SQL reads (`_HEALTH_SQL`, `_QUOTA_SQL`, `_METRICS_SQL`). A build fact that lives in no table
would make the object heterogeneous in origin and force the reader to take a non-DB dependency.

### PF2 — `not_executable` is the first check in `_ineligibility_reason`, ahead of `provider_disabled`

Position is load-bearing, not stylistic. `_ineligibility_reason` returns the **first** failing
constraint, so checking executability first is what makes the recorded reason a complete account of
executability — every candidate is then either labelled `not_executable` or known to have passed the
check. This is the mechanism ADR-0054 relies on when it declines to persist the executable set.

**Accept this visible consequence:** an adapter that is both disabled and unconstructible now reports
`not_executable` where it previously reported `adapter_disabled`. The ledger's `candidate_list` is
API-visible, so this changes observable output for such rows.

### PF3 — a keyed registry on the `DestinationRegistry` pattern, port in application, impl in infrastructure

Port `IImageAdapterRegistry` beside `IImageGenerator` in
`app/application/interfaces/image_generator.py`, mirroring how `destination_publisher.py` holds both
`IDestinationPublisher` and `IDestinationRegistry`:

```python
class IImageAdapterRegistry(ABC):
    @abstractmethod
    def for_adapter(self, adapter_id: str) -> IImageGenerator: ...
    @abstractmethod
    def supported_adapters(self) -> frozenset[str]: ...
```

Implementation `ImageAdapterRegistry` in `app/infrastructure/generation/registry.py`: an immutable
`dict[str, IImageGenerator]` copied at construction, exactly as `DestinationRegistry` does. This
satisfies open question 1 in the ADR — the import-linter contract *"Application use_cases never
import infrastructure or api"* forecloses a use-case-level factory, and the composition root is the
legitimate bridge.

### PF4 — an unknown or unconstructible `adapter_id` is a permanent, terminal failure

`for_adapter` raises `AdapterNotRegisteredError` (new, non-retryable, `code="unknown_adapter"`),
following `DestinationError(retryable=False)`. The generation fails closed. Per ADR-0054 this path
writes **no shot row and no `adapter_used`**, and the decision fields stay populated — the
construction-failure row of D2's population table, which is the row the ADR exists to protect.

Under PF2 this should be unreachable in a conformant deployment, because DISP-1 guarantees the
resolver cannot return a non-executable adapter. It is retained as a fail-closed assertion for
non-conformant wiring, and its unreachability is exactly what the negative test in PF9 proves.

### PF5 — producer identity is captured at invocation, not recovered from the artefact

`_ShotOutcome` gains `produced_by: str | None`, set to the **registry key passed to `for_adapter`**
immediately after `generate()` returns bytes, before verification runs. `_shot_record` then writes
`adapter_used=outcome.produced_by` instead of today's `adapter_used=chosen.adapter_id`.

This is what makes D2's verification-failure row satisfiable. Today the rejected image is discarded
(`image=None` on the non-accepted path), so identity recovered from the artefact would vanish exactly
when D2 requires it to persist. Capturing at invocation also survives exceptions after production,
retries, and partial success without depending on the artefact outliving verification.

`GeneratedImage.provider_id` and `.model` may be recorded as corroboration in future work; they are
**not** the source. `GeneratedImage.adapter_id` is never a source — today's sole adapter echoes the
requested id back, so a misbinding yields an artefact whose two identity fields contradict each other.

### PF6 — `execution_result` keeps its name, its write site, and its value

No migration, no re-sourcing, no new write point. ADR-0054 ruled it a **decision** field: the outcome
of resolution, not of provider execution. Today's `ExecutionOutcome.SUCCESS if chosen else NONE`,
written before the shot loop, is already correct under that ruling. What changes is that it is now
deliberate rather than accidental, and that `FAILURE`/`FALLBACK` are documented as unreachable on
this column by construction.

### PF7 — Stage 13 is rewritten, not adjusted

Its current assertion `all(r["adapter_used"] == _ADAPTER_ID for r in shots)` passes while
`OfflineDeterministicImageGenerator` produces the bytes — it asserts the defect. The synthetic
adapter is also seeded with only `(id, provider_id, capability_id, execution_mode, enabled)`, so it
is `implemented = false` with no code and DISP-1 would filter it. Both must change together; see
section 9.

---

## 3. The executability filter

| Concern | Ruling |
|---|---|
| Type | `ExecutableAdapters(frozenset[str])`, `app/domain/resolver/models.py` |
| Assembled | Composition root builds the registry; `ResolverCapabilityResolver` derives the set from the injected port |
| Reaches the domain | As a positional argument to `resolve_candidates` / `_ineligibility_reason` |
| Rejection reason | `not_executable`, added to the rejection vocabulary |
| Position | First, ahead of `provider_disabled` |
| Recorded | Per candidate, via the existing `candidate_list` payload (`eligible`, `ineligible_reason`, `breakdown`) |

The filter lands inside the **pure resolver's** eligibility pass, which means every `not_executable`
verdict appears in `Resolution.candidates` and therefore in the persisted `candidate_list` — the
ledger already serialises "winners *and* filtered ones". No new provenance surface is created, which
is what ADR-0054 requires.

`AdapterInfo.implemented` remains loaded and unread. This slice does **not** start consulting it;
D1's authority is code, and the manifest's `implemented` flag is reconciled by CI, not by the
resolver.

---

## 4. The adapter registry

| Concern | Ruling |
|---|---|
| Port | `IImageAdapterRegistry`, `app/application/interfaces/image_generator.py` |
| Implementation | `ImageAdapterRegistry`, `app/infrastructure/generation/registry.py` |
| Population | `{"pollinations.image": PollinationsImageGenerator(...)}` at the composition root |
| Unknown key | `AdapterNotRegisteredError`, permanent, terminal |
| Dynamic loading | **None.** No runtime `importlib`; ADR-0054 rejects `import_path` loading |
| Frozen registry reuse | **None.** `ProviderRegistry`/`StepCommandDispatcher` need an ADR-0042 override, cannot express an `adapter_id`, and speak the wrong protocol (F5) |

**The registered key must equal the catalogue's adapter id.** `pollinations.image` is the id the
manifest already uses. Confirming that equality is the staged CI reconciliation check's first job —
assert every registry key exists in the catalogue, deferring the `implemented` / `import_path`
direction until F5's protocol mismatch is settled on its own terms.

### Resource ownership and shutdown for N adapters

Today `_get_image_generator()` memoises one generator and one `_image_client`, and `shutdown()`
closes that client by name. That does not scale to N adapters. Ruling: introduce
`_image_adapter_clients: list[httpx.AsyncClient]`, appended to as each adapter is constructed, closed
in a loop in `shutdown()`, and reset to `[]` alongside the other globals. This is the smallest change
that preserves the existing explicit-close discipline; a generic closeable registry is not warranted
for one adapter.

---

## 5. Dispatch semantics

One call site changes. `GenerateVideo._render_shot` currently calls the single injected
`IImageGenerator`; it will resolve the adapter through the registry and invoke it:

- Execute **only** `resolution.candidates[0]` — `chosen`, after the executability filter and the tier
  cascade have both run.
- **No walking.** `candidates[1:]` is never invoked. `ExecutionOutcome.FALLBACK` stays unwritten.
- **No authored fallback-chain execution.** `AdapterInfo.fallbacks` / `Candidate.fallbacks` remain
  loaded, unread by Execution, and available as future resolver input (D3).
- **No provider-name branching** anywhere in Execution.
- **No re-scoring.** Execution consumes the order it is given (ADR-0045 F2, ADR-0046 X1).

`_ensure_model` keeps its current position and semantics: it is driven by `chosen.execution_tier` and
`chosen.model_ref`, both decision facts, and is unaffected by dispatch.

---

## 6. Provenance: every touched field and its write source

| Field | Kind (ADR-0054) | Write source after α9.9 |
|---|---|---|
| `generations.chosen_adapter` | decision | `resolution.top.adapter_id` — unchanged |
| `generations.chosen_provider` | decision | `resolution.top.provider_id` — unchanged |
| ledger `candidate_list` | decision | pure `Resolution.candidates`, now including `not_executable` verdicts |
| ledger `chosen_adapter` | decision | `resolution.top.adapter_id` — unchanged |
| ledger `execution_result` | decision | `SUCCESS if chosen else NONE` — unchanged (PF6) |
| `generation_shots.adapter_used` | **execution** | **`_ShotOutcome.produced_by`, the dispatch binding** — changed |
| `generation_shots.attempts` JSON | lifecycle | per-attempt seed/verification/action — unchanged, carries no identity |
| `GeneratedImage.adapter_id` | echoed input | **never a write source**; may not be persisted as identity |
| `GeneratedImage.provider_id` | execution (self-reported) | not persisted in α9.9; corroboration only |
| `GeneratedImage.model` | execution (self-reported) | not persisted in α9.9 |
| `media_assets.provider` | decision-derived | `video.chosen_provider` — unchanged, and explicitly not execution provenance |
| promoted `source_metadata.chosen_*` | decision | unchanged; the `chosen_` prefix correctly preserves the framing |

Checks this table must satisfy, all from ADR-0054:

- Decision fields come from the Decision plane. ✅ every decision row reads `resolution`.
- Production facts come from the invocation path. ✅ `adapter_used` is the only execution field and
  reads the binding.
- No execution field is copied from a decision. ✅ the single copy — `adapter_used=chosen.adapter_id`
  — is removed.
- Verification failure still records the producer. ✅ `produced_by` is set before verification (PF5).
- Construction failure records no producer. ✅ terminal before invocation, no shot row (PF4).
- Legacy rows are neither inferred nor backfilled. ✅ no migration, no backfill; pre-α9.9 rows are
  legacy artefacts.

---

## 7. Ledger consistency

**`execution_result` as a resolution outcome.** Resolved in implementation without a migration: the
column keeps its name and its value, and the ADR's classification table is the normative statement of
what it means. Nothing is renamed, nothing is re-sourced.

**Unconditional ledger rows.** ADR-0046 X5 wants a ledger row per run.
`ExecutionRuntimeStore.record_resolution` returns early when `CapabilityResolution.resolution is
None`, so no row is written in that case. With the real resolver the field is always populated, so
the gap is not reachable in production. Ruling: **leave as is**, and record the condition here rather
than tightening it — tightening would mean synthesising a `Resolution` the resolver never produced.

**Optional / fake resolver behaviour.** `CapabilityResolution.resolution` is `| None` precisely so
unit tests can inject `FakeCapabilityResolver`. Those fakes write no ledger row and bypass the
executability filter, so **unit tests cannot cover dispatch**. Coverage must sit at the integration
layer (section 9). This is ADR-0054's open question 6 and is discharged here, not deferred.

**Pre-constraint versus post-constraint explanation — known limitation, not fixed in α9.9.** The
ledger serialises the *pure* resolver's `candidates` while `chosen_adapter` is the winner *after*
`_apply_constraints` applies the tier cascade in the application layer. Under `AUTO` the cascade can
select a lower-scored candidate from a preferred tier, so a row can record a `chosen_adapter` that is
not the top of its own `candidate_list`, with nothing in the row explaining the gap.

α9.9 **improves** this without closing it: because the executability filter runs inside the pure
resolver, every `not_executable` verdict *is* in the recorded list. What remains unexplained is the
tier cascade. Closing it means either recording the constrained list — which loses the filtered
candidates the ledger exists to keep — or adding a second column, which is a migration. Both exceed
this slice. Recorded as a residual in section 11.

---

## 8. Gate impact

No new stage. Stage 13 (`test_generation_end_to_end.py`) is rewritten in place and keeps its position
and isolation rules. Stage 26 (worker runtime) is untouched. The staged catalogue/registry
reconciliation check is a **new assertion inside the existing provider-validation stage**, not a new
stage, and begins in its weakest form: every registry key exists in the catalogue.

---

## 9. Test plan

**Stage 13, rewritten.** The fixture becomes dispatch-aware:

1. Make the registry injectable in the e2e harness and register
   `OfflineDeterministicImageGenerator` under the id the resolver will actually select.
2. Make **selected identity and producing implementation independently distinguishable** — the
   offline generator reports a `provider_id` of its own, so an assertion that `adapter_used` equals
   the binding cannot be satisfied by accident from either the decision or the artefact.
3. **Prove a wrong binding fails.** Register the offline generator under a key the resolver will not
   select and assert the run fails closed with `AdapterNotRegisteredError` rather than silently
   succeeding with false provenance. This is the test that would have caught today's defect.
4. **Exercise `AUTO` against the seeded catalogue.** With the executability filter in place, `AUTO`
   drops `comfyui.flux_schnell` as `not_executable`, the LOCAL tier empties, the cascade reaches
   FREE_REMOTE, and `pollinations.image` is selected — which the harness serves from the offline
   generator. This is the first integration coverage the default path has ever had.
5. **Delete the "ignore `adapter_id`" shortcut** and the docstring that blesses it.

**Domain unit tests.** `not_executable` fires first; an executable-but-disabled adapter reports
`not_executable`; an empty executable set yields zero eligible candidates with reasons recorded.

**Registry unit tests.** Unknown key raises permanently; `supported_adapters()` reflects construction.

**Provenance tests.** Verification failure writes `adapter_used` and `accepted=false`; construction
failure writes no shot row; no path copies a decision into `adapter_used`.

---

## 10. Scope decisions

| Item | In α9.9? | Reason |
|---|---|---|
| Executability filter + `not_executable` | **In** | D1; the slice's core |
| Keyed adapter registry + dispatch | **In** | D4; the slice's core |
| `adapter_used` from the dispatch binding | **In** | D2/DISP-2; the correctness defect |
| Stage 13 rewrite | **In** | The current test asserts the defect |
| Staged catalogue/code reconciliation (weak form) | **In** | D1; assert registry keys exist in the catalogue |
| `provider` / `model` columns on `generation_shots` | **Deferred** | Migration; `adapter_used` alone discharges DISP-2 |
| Media-context provenance cleanup | **Deferred** | Media owns its model; ADR-0054 only classifies `media_assets.provider` |
| Runtime measurement replay | **Deferred** | ADR-0054 withdraws the replay guarantee; restoring it is its own decision |
| Request fingerprint persistence | **Deferred** | Not required by any accepted invariant |
| Model-manager wiring | **Deferred** | `_ensure_model` already drives the seam; no local adapter exists to exercise it |
| Strict catalogue/code reconciliation | **Deferred** | Blocked on F5's `generate` vs `generate_image` protocol mismatch |
| Fallback walking | **Deferred** | D4 explicitly: no walking ships in α9.9 |
| Usage metering | **Deferred** | The generation path writes no `usage_records`; GEN-2 unresolved for second-provider spend |
| Whole-run wall-clock cap | **Deferred** | Prerequisite for walking, not for single dispatch |
| ComfyUI adapter | **Deferred** | `status: planned`; would need the local tier and the model manager |
| Additional real adapters | **Deferred** | One adapter is enough to prove dispatch; a second is a later slice |

---

## 11. Risks and residuals

1. **Tier-cascade opacity persists** (section 7). The ledger can still show a `chosen_adapter` that
   is not the top of its `candidate_list`. Unchanged by this slice, now documented.
2. **`not_executable` masks other reasons.** First-position checking is deliberate and required, but
   it reorders observable rejection reasons for adapters failing several constraints.
3. **`UNIQUE (provider_id, capability_id)` on `provider_adapters`** permits one adapter per provider
   per capability, and this slice fixes adapter identity as the dispatch key. Flagged in ADR-0054 as
   a future hazard; unchanged here.
4. **Unit tests remain blind to dispatch** because fakes bypass the resolver. Mitigated by moving
   coverage to Stage 13, not by weakening the fakes.
5. **One adapter means the registry is barely exercised.** The wrong-binding negative test is the
   main defence against the abstraction rotting before a second adapter arrives.

---

## 12. Architectural decision check

**No new ADR is required.** Every ruling here is an implementation of an ADR-0054 decision or an
explicitly delegated open question:

| Ruling | Authority |
|---|---|
| PF1 executable-set type and seam | ADR-0054 D1 + open question 4 |
| PF2 first-position ordering | ADR-0054 D1, frozen decision 2 |
| PF3 registry shape | ADR-0054 open question 1 |
| PF4 unknown-adapter semantics | ADR-0054 D1 consequences, open question 2 |
| PF5 capture at invocation | ADR-0054 D2/DISP-2, frozen decisions 8–10 |
| PF6 `execution_result` unchanged | ADR-0054 D2 ruling, frozen decision 7 |
| PF7 Stage 13 rewrite | ADR-0054 Consequences item 1 |

**Contradiction scan — none found.** Checked against ADR-0054 (all four decisions, DISP-1/2/3, the
seventeen frozen decisions), ADR-0045 F1/F2/F4/F5/F7, ADR-0046 X1/X5/X8, ADR-0042's frozen-path
checker, and ADR-0052 GEN-2. The two places worth recording as *checked and clear*: passing
`ExecutableAdapters` into the pure resolver does not breach the domain import contract because it is
a domain-owned value type; and adding `not_executable` inside eligibility keeps the executability
verdict in the recorded `candidate_list`, which is what lets ADR-0054 decline to persist the set.

---

## 13. Implementation order

1. `ExecutableAdapters` + `not_executable` in the domain, first position, with unit tests.
2. `IImageAdapterRegistry` port + `ImageAdapterRegistry` implementation, with unit tests.
3. Composition root: build the registry, thread the port into `ResolverCapabilityResolver`, extend
   `shutdown()` for N clients.
4. `GenerateVideo`: dispatch through the registry, add `produced_by`, write `adapter_used` from the
   binding.
5. Stage 13 rewrite, including the wrong-binding negative test and the `AUTO` path.
6. Weak reconciliation assertion in provider validation.
7. Docs sync: `PLATFORM_STATUS`, `SYSTEM_MAP`, `CHANGELOG`.

---

## 14. Implementation record — where the build diverged from this plan

Three departures, all discovered while implementing and none reopening an ADR-0054 decision.

**A second Decision-plane entry point.** The plan named `ResolverCapabilityResolver` as the only
consumer of the pure resolver. `ResolverService` — which serves the resolver API's preview/explain
path — is a second one. Left alone it would have kept recommending adapters this deployment cannot
construct, so it takes the executable set the same way. DISP-1 is a property of resolution, not of
one caller.

**The wrong-binding test splits in two.** Section 9 item 3 expected a misbinding to surface as
`AdapterNotRegisteredError`. It cannot, and the reason is the invariant working: a wrong binding
means the adapter is not executable, so DISP-1 filters it during resolution and the run fails with
*no eligible provider* before dispatch is reached. The negative test now asserts that, and a second
test deliberately desynchronises the two views — resolver told the adapter is executable, Execution
handed a registry without it — to prove the dispatch-time failure still fails closed and still
claims no producer. That path is unreachable in a conformant deployment, which is precisely why it
needs a test that constructs the non-conformance by hand.

**The `AUTO` test seeds its own unconstructible adapter.** Item 4 leaned on `comfyui.flux_schnell`
from the real manifest, which makes the test depend on ambient database state — it passes against a
seeded catalogue and fails silently-differently against a bare one. It now seeds a top-scored local
adapter of its own with no implementation behind it, so the cascade is proven against data the test
owns.
