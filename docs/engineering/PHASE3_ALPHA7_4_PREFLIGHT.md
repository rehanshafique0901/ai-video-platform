# Phase 3 Slice α7.4 — Provider Skeleton (capability ports · registry · dispatcher · mock providers) — Pre-flight

> Status: **SIGNED OFF (2026-07-17)** — see §6. The provider-runtime architecture
> and its runtime decisions (D1–D14) were signed off in
> [`ADR-0041`](../decisions/ADR-0041-provider-runtime-contract.md) and
> [`docs/architecture/CONTENT_GENERATION_PIPELINE.md`](../architecture/CONTENT_GENERATION_PIPELINE.md)
> (2026-07-17, α8.0, docs-only). This doc resolves the **α7.4-specific** open
> questions (§4). Nothing is implemented yet.
>
> Mirrors the α5/α6/α7.1/α7.2/α7.3 discipline: ground in the existing contract +
> code → lock decisions → sign-off → branch → implement → CI → merge → tag.
> Read-only planning artefact.
>
> **Predecessor.** α7.3 (`v0.4.17`, tag `v0.4.17-phase3-alpha7.3`) — the outbox
> relay + distributed lock manager: the first two concrete implementations of the
> ADR-0041 execution substrate (relay publishes what α7.1/α7.2 produce; the lock
> manager gives single-writer leases). Those are the two *runtime prerequisites*.
> **What is still missing is the thing they exist to serve: a provider.** No
> `ProviderPort`, no registry, no dispatcher — the α7.2 runner still returns
> `StepResult.commands` that **nothing interprets** (ADR-0040 D4: "only the
> interpreter of `StepCommand` changes"; that interpreter does not exist yet).
>
> **This is the abstraction-layer slice — the provider seam, mock-only.** α7.4 adds
> no aggregate, no HTTP surface, no external I/O. It builds, per ADR-0041 D1–D4 /
> D11:
>
> 1. **Capability ports** — narrow, typed, `async` protocols for the four provider
>    kinds (`llm`, `image`, `video`, `voice`), each taking a typed request and
>    returning a typed `ProviderResult` or raising a typed provider error (D1).
> 2. **Provider registry** — a pure lookup that resolves `kind` → a provider by a
>    selection/fallback chain and raises `NoHealthyProvider` on exhaustion (D2).
> 3. **`StepCommandDispatcher`** — the imperative shell that maps a declarative
>    `StepCommand{kind, args}` (α7.2, already produced by pure handlers) to a
>    resolved capability call via the closed mapping table (D4).
> 4. **Typed provider exceptions** + a **health-check** interface (D3).
> 5. **One deterministic mock provider per capability** — fixed `ProviderResult`,
>    **no network, no broker** (D1/D11).
>
> Per the runner-before-worker discipline (blueprint §13; ADR-0041 D11), α7.4 ships
> these as **library seams driven by tests** — **no HTTP client, no external API,
> no Celery, no Redis, no broker, no completion service, no usage recording, no
> workflow-execution change.** Real adapters (Pollinations/Groq/HuggingFace/
> OpenRouter/Replicate, etc.) and the Celery+Redis worker are α8.1+; the usage
> recorder is α7.5; the runner is wired to drive the dispatcher end-to-end in α7.6.
>
> **Baseline versioning.** `main` is at `0.4.17` (tag `v0.4.17-phase3-alpha7.3`).
> First α7.4 commit bumps `backend/app/main.py` → `"0.4.18-phase3-alpha7.4-dev"`.
> **Zero migrations** (blueprint D7 / ADR-0041 D14) — the only table α7.4 reads
> (`provider_settings`, if §4 Q4 lands a read path) already exists in baseline
> `0001` (`schema.md` §27.3); its ORM (`ProviderSetting`) is already mapped.

---

## Section 1 — Scope

### 1.1 One-line thesis

α7.4 establishes the **provider abstraction layer**: four capability ports, a
registry that selects a provider behind a fallback chain, a dispatcher that turns
α7.2's inert `StepCommand`s into capability calls, a typed error hierarchy, a
health interface, and one deterministic mock per kind. It is **pure ports + an
imperative shell over mocks** — no external I/O, no aggregate, no broker. It is
the seam α7.5 (usage), α7.6 (first pipeline), and α8.1–α8.5 (real providers) plug
into; nothing above it (the runner, the aggregates, the API) changes in this slice.

### 1.2 What's in

1. **Capability ports** (ADR-0041 D1) — four narrow `async` protocols keyed by
   `plugin_kind ∈ {llm, image, video, voice}`, each: one generate method
   (`generate_text` / `generate_image` / `generate_video` / `synthesize_voice`)
   taking a typed request and returning a typed `ProviderResult`, plus
   `health() -> ProviderHealth`. Providers never leak SDK types upward.
2. **Shared provider DTOs** — `ProviderResult` (`request_id`, `model_id`,
   `provider`, `status ∈ {succeeded, in_progress, failed}`, `output`,
   `provider_job_id | None`, `usage`, `error | None`), the per-kind request
   objects, `ProviderUsage` (units for the α7.5 recorder — **defined here,
   consumed there**), and `ProviderHealth`.
3. **Typed exception hierarchy** (ADR-0041 D3) — a `ProviderError` base plus
   `ProviderTimeout`, `ProviderRateLimited`, `ProviderInvalidRequest`,
   `ProviderUnavailable` (transient vs terminal classification for the future
   retry policy D10), and `NoHealthyProvider` (registry exhaustion → the calling
   step fails, §11).
4. **Provider registry** (ADR-0041 D2) — registration + a resolution function
   `resolve(kind, …) -> Provider` implementing the selection/fallback chain, and
   raising `NoHealthyProvider` when the chain is exhausted. Pure, I/O-free,
   unit-testable with mock registrations.
5. **`StepCommandDispatcher`** (ADR-0041 D4) — the closed mapping table
   `StepCommand.kind` → capability call (`generate_text` / `generate_image` /
   `generate_video` / `synthesize_voice`; `start_render` handled per §4 Q6),
   returning the capability's `ProviderResult` for the caller to thread into a
   step's `output` / `checkpoint_state`. **Not wired into the α7.2 runner in this
   slice** (§4 Q6) — exercised directly by tests.
6. **One deterministic mock provider per capability** — returns a fixed
   `ProviderResult` derived only from the request (reproducible), registered under
   keys like `mock-llm` / `mock-image` / `mock-video` / `mock-voice`. **No
   network.** The async-kind mock (video) models `in_progress` + `provider_job_id`
   per §4 Q5.
7. **`provider_settings` read path** (ADR-0041 D2 config precedence) — a read-only
   port/repository resolving provider config from `provider_settings`
   (tenant row → global row), **scope pinned by §4 Q4** (full precedence vs a
   minimal read now). No writes, no secret decryption (KMS is later).
8. **DI wiring** — a process-wide `ProviderRegistry` singleton (mirroring
   `WORKFLOW_REGISTRY`), container factories for the dispatcher + registry, and (if
   Q4 lands a read path) the settings repo on the UoW. **Unit tests** (registry
   resolution/fallback, dispatcher mapping, each mock, typed-error classification,
   health) + minimal **integration test** only if the settings read path touches
   the DB. Docs (`CHANGELOG`, `ROADMAP`, architecture notes; `API_CONTRACT` only if
   a surface lands — none expected; **ADR only if a decision falls outside
   ADR-0041** — see §4 Q1).

### 1.3 What's out (deferred)

- **Any real provider / HTTP client / external API / SDK** (Pollinations, Groq,
  HuggingFace, OpenRouter, Replicate, …) — α8.1+ (image), α8.2 (video), behind
  these same ports.
- **Celery, Redis, a broker, any worker/daemon/loop** — α8.1 (runner-before-worker).
- **Completion service, polling, webhooks** — α8.3 (the async mock returns
  `in_progress` but nothing *completes* it in α7.4).
- **Usage recording + credit ledger** — α7.5 (α7.4 only *defines* `ProviderUsage`).
- **Wiring the dispatcher into `AdvanceWorkflowRun`** (the runner keeps ignoring
  `result.commands`) — α7.6 (the first end-to-end pipeline).
- **`media_assets` register-by-metadata on generation output** (D12) — α8.x.
- **Secret decryption / KMS for `is_secret` provider settings** — later; α7.4
  reads config but does not resolve ciphertext.
- **`provider_settings` write API / admin surface** — not in the runtime path.
- **Zero migrations.**

---

## Section 2 — Grounded facts (the contract, the code it plugs into, the layering)

### 2.1 ADR-0041 pins the provider-runtime contract (D1–D4, D10, D11)

- **D1 `ProviderPort`:** four capability protocols keyed by `plugin_kind`; each is
  a narrow, typed, **async** port; a provider is a **leaf** layer
  (`import-linter`-enforced) that never imports `application` / `workflows`. Sketch
  places them at `app/infrastructure/ai/providers/ports.py`. `ProviderResult`
  carries `request_id` (dedupes usage + completion), `model_id` (resolved
  `ai_models` row), `provider`, `status`, `output`, `provider_job_id`, `usage`,
  `error`.
- **D2 registry:** decorator registration (`@register_plugin(kind=…, key=…)`) into
  an in-process registry; resolution = **selection precedence** (per-request
  override → project default → user/tier → tenant/global → fallback chain) over a
  **config precedence** (`provider_settings`: tenant row → global row → env →
  built-in default); exhausting the chain raises **`NoHealthyProvider`**.
- **D3 adapter lifecycle:** construct-from-settings → `health()` → wrapped call →
  typed error; SDK faults map to `ProviderTimeout` / `ProviderRateLimited` /
  `ProviderInvalidRequest` / `ProviderUnavailable`; adapters are stateless.
- **D4 dispatcher:** the imperative interpreter of `StepCommand{kind, args}`; the
  **closed mapping table** is the only place `kind` strings become provider calls
  (`generate_text`→sync, `generate_image`→sync, `generate_video`→async→completion,
  `synthesize_voice`→sync/async, `start_render`→creates a `RenderJob`). Built by
  **α7.4 / α7.6**.
- **D11:** mock providers, in-process/synchronous through α7.6; Celery+Redis in α8.1.

### 2.2 What α7.4 plugs into (α7.2 pure runner + StepCommand — already shipped)

From `backend/app/domain/workflow/registry.py`:

- `StepCommand{kind: str, args: dict[str, Any]}` — **frozen, already defined.**
- `StepResult.commands: tuple[StepCommand, ...]` — pure handlers already return
  commands; today they are `()` for the deterministic workflows.
- `StepHandler` is pure `(StepContext) -> StepResult` — **stays pure** (D3.11);
  α7.4 does not touch it.

From `backend/app/application/use_cases/workflow/advance_workflow_run.py`:

- `_run_single_step(...)` calls `result = step_def.handler(ctx)` and interprets
  `outcome` / `output` / `checkpoint_state` — but **ignores `result.commands`**.
  This is the exact seam the dispatcher will occupy **in α7.6**; α7.4 leaves the
  runner untouched (§4 Q6). The runner is a **use case** (`app.application`), so it
  can only depend on `app.application.interfaces` + `app.domain` + `app.core`
  (§2.4) — a load-bearing constraint on where the dispatcher's *port* lives.

### 2.3 `provider_settings` — `ProviderSetting(UUIDPrimaryKeyMixin, TimestampMixin, VersionMixin, Base)`

From `backend/app/infrastructure/db/models/configuration.py` (schema §27.3):

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `provider` | Text NOT NULL | registry key, e.g. `mock-image`, `replicate` |
| `tenant_id` | UUID **nullable** | NULL = global row; set = tenant row |
| `key` | Text NOT NULL | config key |
| `value` | JSONB NOT NULL | config value |
| `is_secret` | Bool NOT NULL | server default `false`; KMS ciphertext at rest (decryption later) |
| `version` | (VersionMixin) | OCC token |
| `created_at` / `updated_at` | (TimestampMixin) | |

Split partial-unique indexes: `uq_provider_settings_global_provider_key`
(`provider, key` WHERE `tenant_id IS NULL`) and
`uq_provider_settings_tenant_provider_key` (`tenant_id, provider, key` WHERE
`tenant_id IS NOT NULL`), plus `ix_provider_settings_provider`. **Zero application
consumers today** — α7.4 would be the first reader (scope per §4 Q4).

### 2.4 Layering / import-linter (the constraint that shapes port placement)

From `backend/pyproject.toml [tool.importlinter]`:

- **Domain** imports nothing from infrastructure/application/api.
- **`app.application.use_cases` must NOT import `app.infrastructure` or `app.api`**
  (forbidden contract). The composition root (`app.core.container`) is the only
  bridge; use cases depend on **ports in `app.application.interfaces`** + domain +
  `app.core.errors`.
- ADR-0041 D1 places the capability protocols in **`app.infrastructure.ai.providers`**
  (a *leaf* — the ADR also wants a new leaf contract that providers never import
  `application`/`workflows`). **Tension:** the α7.6 runner (a use case) will call
  the dispatcher; if the dispatcher and its capability ports live wholly in
  infrastructure, the runner cannot depend on them directly. This is resolved by a
  **dispatcher port in `app.application.interfaces`** (like `PublisherPort` in
  α7.3) with the concrete dispatcher + capability protocols + registry + mocks in
  `app.infrastructure.ai.providers`. **§4 Q1 pins this precisely.**

### 2.5 Conventions to mirror (unchanged from α7.1–α7.3)

- **Ports** in `app/application/interfaces/` (ABCs / Protocols); **impls** in
  `app/infrastructure/`; DI via `app/core/container.py` factories + `deps.py`
  aliases; a module singleton for framework-free catalogues (precedent:
  `WORKFLOW_REGISTRY` in `registry.py`, injectable into use cases for tests).
- The α7.3 `PublisherPort` (port in `application/interfaces/publisher.py`; impl
  `InProcessPublisher` in `infrastructure/publisher/`) is the **direct precedent**
  for how a runtime seam is split so a use case can depend on it.
- **Tests:** `pytest -m unit` (fakes/mocks, no DB) is where α7.4 lives almost
  entirely; `-m integration` only if a `provider_settings` DB read path lands
  (§4 Q4). Full gate = `scripts/ci_gate.py`.
- `app/infrastructure/ai/` **does not exist yet** — α7.4 creates the `ai/providers`
  package (a new leaf).

---

## Section 3 — Decisions (recommended)

- **D3.1 — α7.4 is the provider abstraction, mock-only; no aggregate, no I/O.**
  Capability ports + registry + dispatcher + typed errors + health + one mock per
  kind. Extends ADR-0041's reuse ledger (the `ProviderPort`, mock-per-capability,
  and dispatcher `⛔→α7.4` rows).
- **D3.2 — Pure ports + imperative shell, mirroring α7.2/α7.3.** Capability
  protocols and the registry are pure/side-effect-free (the registry is a lookup
  over config); the dispatcher is the imperative shell (the only place a
  `StepCommand.kind` becomes a call). Mocks are deterministic leaves.
- **D3.3 — The runner stays pure and untouched (runner-before-worker).** α7.4 does
  **not** wire the dispatcher into `AdvanceWorkflowRun`; `result.commands` stays
  ignored until α7.6. This keeps the slice cohesive and the α7.2 state machine
  frozen.
- **D3.4 — Deterministic mocks, no network.** Each mock returns a fixed
  `ProviderResult` computed from the request only, so α7.4–α7.6 run with no broker
  and byte-reproducible tests. The video mock models the async `in_progress` +
  `provider_job_id` path so α8.3's completion service has a shape to resolve.
- **D3.5 — Typed error hierarchy classifies transient vs terminal (ADR-0041 D3/D10).**
  `ProviderTimeout` / `ProviderRateLimited` / `ProviderUnavailable` = transient
  (future retry/fallback); `ProviderInvalidRequest` = terminal; `NoHealthyProvider`
  = registry exhaustion → the calling step fails. α7.4 defines + classifies them;
  the retry *scheduler* remains a worker concern (α8.1), consistent with α7.2's
  "counter, no scheduler."
- **D3.6 — Registry raises `NoHealthyProvider` on chain exhaustion.** Resolution is
  a pure precedence + fallback walk; `health()` gates selection. No I/O in the
  registry itself; config is read through the settings port (D3.7).
- **D3.7 — Config read is a narrow read-only port.** Provider config comes from a
  `provider_settings` read seam (tenant row → global row → env → default), **not**
  by having providers touch the DB. Secret (`is_secret`) values are read opaque; no
  KMS decryption in α7.4.
- **D3.8 — Boundary invariant.** Providers are a leaf: they never import
  `application` / `workflows`, never touch the outbox or an aggregate, never mint
  events. The dispatcher (shell) owns the `kind`→call mapping; the completion
  service and aggregates (later slices) own all state writes — matching ADR-0041's
  "providers call nothing directly."
- **D3.9 — Zero migrations, zero new schema.** `provider_settings` is read exactly
  as baseline defines it; no new tables/columns/immutability.

---

## Section 4 — Open questions for sign-off

Only decisions **not** already pinned by ADR-0041 / the blueprint are raised.
(Already decided, not re-asked: four capability kinds, async return shape, typed
error names, `NoHealthyProvider` on exhaustion, decorator-vs-not aside, mocks
in-process, no broker/Celery/Redis, zero migrations, runner-before-worker.)

**Q1 — Port placement + the `import-linter` leaf contract (the load-bearing one).**
ADR-0041 D1 sketches the capability protocols at
`app/infrastructure/ai/providers/ports.py` (a leaf). But the α7.6 runner (a
`use_cases` module) must eventually call the dispatcher, and `use_cases` may not
import `infrastructure` (§2.4). **Recommend:** split like α7.3's publisher —
(a) a **dispatcher port** `IStepCommandDispatcher` (+ the neutral `ProviderResult`/
request/`ProviderUsage`/`ProviderHealth` DTOs and the typed error hierarchy) in
**`app/application/interfaces/providers.py`** so the runner can depend on the port
in α7.6; (b) the **capability protocols, registry, concrete dispatcher, and mocks**
in **`app/infrastructure/ai/providers/`** as a new leaf; (c) add an **`import-linter`
contract** making `app.infrastructure.ai.providers` forbid importing
`app.application` / `app.api` / the workflow domain (encodes ADR-0041 D1's "leaf").
*(Alternative, ADR-literal: keep everything under `infrastructure/ai/providers`
including the dispatcher, and in α7.6 expose it to the runner only through a thin
port added then. That defers the port but risks a bigger α7.6.)* **This is the one
place α7.4 might warrant a short ADR addendum** (port-placement refinement of
ADR-0041 D1/D4) — **confirm whether you want (a) with an ADR-0041 addendum, or the
alternative.**

**Q2 — DTO representation: structural `Protocol` vs concrete `@dataclass`.**
ADR-0041 D1 writes `ProviderResult` as a `Protocol`. But α7.2/α7.3 model their
value objects as **frozen dataclasses** (`Lease`, `OutboxEvent`, `RelayResult`,
`StepCommand`, `StepResult`). **Recommend:** make the **request/result/usage/health
DTOs frozen dataclasses** (concrete, constructible, matches the codebase's VO
style + gives mocks something to return), and keep the **capability *interfaces*
as `Protocol`s** (structural, so real adapters need no base class). *(Alternative:
keep `ProviderResult` a `Protocol` per the ADR letter — but then every mock/adapter
re-declares fields and tests can't build one trivially.)* **Confirm dataclass DTOs
+ Protocol capabilities.**

**Q3 — Registry registration mechanism: decorator (ADR D2) vs explicit builder
(α7.2 precedent).** ADR-0041 D2 says `@register_plugin(kind, key)`
(import-time side-effect). α7.2's `WorkflowRegistry` instead uses **explicit
`register()` + a `default_registry()` builder + an injectable instance**
(no import-time magic; trivially testable). **Recommend:** follow the **α7.2
precedent** — a `ProviderRegistry` class with explicit registration and a
`default_registry()` that wires the four mocks, exposed as a `PROVIDER_REGISTRY`
module singleton and injectable into the dispatcher/tests. A thin
`@register_plugin` decorator can be added later as sugar over `register()` without
changing the contract. *(Alternative: decorator-first per the ADR letter — more
"plugin-like" but couples registration to import order and complicates test
isolation.)* **Confirm explicit-registry (α7.2 style) now, decorator deferred.**

**Q4 — `provider_settings` read path scope in α7.4.** ADR-0041's reuse ledger puts
"`provider_settings` read path" in α7.4 (D1–D4). How much lands now, given mocks
need almost no config? Options: (a) **minimal** — a read-only
`IProviderSettingsRepository.get(provider, key, tenant_id) -> value | None` (tenant
row → global row) with one integration test, and the registry's *full* selection
precedence (project/user/tier defaults) deferred to α7.6/α8.1 when there is real
config to resolve; (b) **full precedence resolver** now (project/user/tier/tenant/
global + env + default), unit-tested against fakes. **Recommend (a)** — it
establishes the seam and the DB read without over-building a precedence engine no
mock exercises; the fallback *chain* (registry-level, config-free) still ships so
`NoHealthyProvider` is real. *(If you want the whole precedence engine pinned now,
(b) — larger slice, more speculative.)* **Confirm minimal read path + registry
fallback chain; full precedence deferred.**

**Q5 — Async mock modelling (video) + `provider_job_id`.** The video capability is
async (D4: `generate_video` → completion). With no completion service until α8.3,
what does the video mock return? **Recommend:** the video mock returns
`status="in_progress"` + a **deterministic `provider_job_id`** (e.g. derived from
`request_id`) so the shape α8.3 will resolve exists and is tested now; the other
three mocks return `status="succeeded"` inline. Nothing in α7.4 *completes* the
in-progress job (asserted by tests: dispatcher surfaces `in_progress` + a job id,
no state transition). *(Alternative: all mocks return `succeeded` for simplicity,
and the async path is first modelled in α8.2 — simpler α7.4, but leaves the
async-shape untested until then.)* **Confirm async video mock returns
`in_progress` + deterministic `provider_job_id`.**

**Q6 — Dispatcher coverage of `start_render` + non-wiring into the runner.**
The D4 table includes `start_render` → "creates a `RenderJob` (links
`workflow_run_id`)", which is an **aggregate write**, not a provider call — and
α7.4 explicitly builds no aggregate coupling. **Recommend:** α7.4's dispatcher
handles the **four provider-capability kinds only** (`generate_text` /
`generate_image` / `generate_video` / `synthesize_voice`); `start_render` is
recognised by the mapping table but raises a clear `NotImplementedError`-style
"deferred to α7.6" (or is simply absent from the α7.4 table with a documented
gap), since wiring it needs the RenderJob create use case + `workflow_run_id`
linkage that belongs with the first pipeline. And per D3.3, the dispatcher is
**not** injected into `AdvanceWorkflowRun` in α7.4 — it is exercised by direct
unit tests. **Confirm: four capability kinds dispatched; `start_render` deferred;
runner untouched this slice.**

**Q7 — Typed-error + `NoHealthyProvider` placement.** Provider errors are a new
hierarchy. Do they live with the provider DTOs (`app/application/interfaces/
providers.py`, so use cases can catch them) or in `app/core/errors.py` (alongside
`ConflictError` / `NotFoundError`)? **Recommend:** define `ProviderError` +
subclasses **beside the provider port DTOs in `app/application/interfaces`** (they
are part of the provider contract the shell reasons about), and **not** map them to
HTTP error codes in α7.4 (no surface). `NoHealthyProvider` lives there too and is
what the registry raises. *(Alternative: put them in `app/core/errors.py` for one
error home — but that coretizes a provider-specific concern with no HTTP mapping
yet.)* **Confirm provider errors beside the provider port; no HTTP mapping.**

**Version (not a question — confirm cadence).** Continue the `0.4.x` slice cadence
→ `0.4.18-phase3-alpha7.4-dev`, tag `v0.4.18-phase3-alpha7.4` on merge (still
Phase-3 runtime infrastructure, not a product milestone).

---

## Section 5 — Planned surface (pending §4)

**No HTTP surface.** The α7.4 surface is ports + DTOs + registry + dispatcher +
mocks, consumed by tests. Shapes (as signed off — see §6):

```python
# app/application/interfaces/providers.py — neutral contract (Q1/Q2/Q7 + additions)
class Capability(StrEnum):              # LLM / IMAGE / VIDEO / VOICE
    ...
class ProviderStatus(StrEnum):          # SUCCEEDED / IN_PROGRESS / FAILED
    ...

@dataclass(frozen=True, slots=True)
class ProviderUsage: ...                # units for the α7.5 recorder (defined here)
@dataclass(frozen=True, slots=True)
class ProviderHealth: ...               # healthy: bool + optional detail
@dataclass(frozen=True, slots=True)
class ProviderMetadata:                 # ADDITION — every provider exposes this
    id: str; name: str; capability: Capability
    supports_polling: bool; supports_webhooks: bool; version: str

# Per-capability immutable request/response pairs (Q2). Each *Response carries the
# common envelope (request_id, provider, status, provider_job_id, usage, error).
@dataclass(frozen=True, slots=True)
class GenerateImageRequest: ...
@dataclass(frozen=True, slots=True)
class GenerateImageResponse: ...
# GenerateText…, GenerateVideo…, GenerateSpeech… mirror this.

class ProviderError(Exception): ...
class ProviderUnavailable(ProviderError): ...        # transient
class ProviderRateLimited(ProviderError): ...        # transient
class ProviderTimeout(ProviderError): ...            # transient
class ProviderAuthenticationError(ProviderError): ...# terminal
class ProviderValidationError(ProviderError): ...    # terminal
class NoProviderAvailable(ProviderError): ...        # registry: no provider for capability
                                                     #   (plays ADR-0041's NoHealthyProvider role;
                                                     #    fallback/health-ordering deferred per Q4)

class ProviderDispatcherPort(ABC):      # the α7.6 runner depends on THIS
    async def dispatch(self, command: StepCommand) -> ProviderResponse: ...
    def supports(self, capability: Capability) -> bool: ...          # discovery (ADDITION)
    def list_capabilities(self) -> list[Capability]: ...             # discovery (ADDITION)

# app/infrastructure/ai/providers/ports.py — capability protocols (leaf)
class ImageProvider(Protocol):
    metadata: ProviderMetadata
    async def generate_image(self, req: GenerateImageRequest) -> GenerateImageResponse: ...
    async def health(self) -> ProviderHealth: ...
# LLMProvider / VideoProvider / VoiceProvider mirror this.

# app/infrastructure/ai/providers/registry.py — explicit registry (Q3) + discovery (ADDITION)
class ProviderRegistry:
    def register(self, *, provider, capabilities: list[Capability]) -> None: ...
    def resolve(self, capability: Capability) -> object: ...   # → NoProviderAvailable if none
    def list_capabilities(self) -> list[Capability]: ...
    def list_providers(self, capability: Capability) -> list[ProviderMetadata]: ...
    def has_provider(self, capability: Capability) -> bool: ...
    def supports(self, capability: Capability) -> bool: ...
def default_registry() -> ProviderRegistry: ...            # wires the four mocks
PROVIDER_REGISTRY = default_registry()

# app/infrastructure/ai/providers/dispatcher.py — closed mapping table (D4), 4 kinds only
class StepCommandDispatcher(ProviderDispatcherPort): ...   # kind → capability call

# app/infrastructure/ai/providers/mocks/ — one deterministic mock per kind (Q5)
#   video mock returns IN_PROGRESS + deterministic provider_job_id
```

Signed-off **implementation order** (layer-by-layer, dependency-first):

1. **Neutral contract** — `app/application/interfaces/providers.py`: `Capability`
   / `ProviderStatus` enums, DTOs (`ProviderUsage` / `ProviderHealth` /
   `ProviderMetadata` / `Generate*Request` / `Generate*Response` + the shared
   `ProviderResponse` envelope), the typed error hierarchy + `NoProviderAvailable`,
   and the `ProviderDispatcherPort` (with discovery). Unit tests (DTOs + errors).
2. **Capability ports** — `app/infrastructure/ai/providers/ports.py`: the four
   `Protocol`s (each with `metadata` + generate + `health`). Add the `import-linter`
   strict-leaf contract (Q1).
3. **Mock providers** — `…/providers/mocks/`: one deterministic provider per kind,
   each exposing `ProviderMetadata`; video models `IN_PROGRESS` + `provider_job_id`
   (Q5). Unit tests per mock.
4. **Registry** — `…/providers/registry.py`: explicit `register` +
   `resolve`/discovery (`list_capabilities` / `list_providers` / `has_provider` /
   `supports`), `NoProviderAvailable`, `default_registry()` + `PROVIDER_REGISTRY`
   (Q3 + discovery addition). Unit tests.
5. **Dispatcher** — `…/providers/dispatcher.py`: the closed `kind`→capability table
   for the **four capability kinds only** (`start_render` / render / export /
   storage explicitly excluded, Q6); discovery delegated to the registry. Unit
   tests (each kind maps, unknown kind errors, async surfaces `IN_PROGRESS`).
6. **`provider_settings` read path** — minimal read-only port + SQLAlchemy impl
   (Q4: tenant row → global row; no fallback/priority/weighting/health-ordering);
   one integration test. **Runner untouched (D3.3).**
7. **DI wiring** — container factories (`get_provider_registry`,
   `get_step_command_dispatcher`), the `PROVIDER_REGISTRY` singleton, the settings
   repo on the UoW; no `deps` aliases (no HTTP surface).
8. **Docs** — `CHANGELOG`, `ROADMAP`, architecture notes (mark α7.4 shipped),
   and an **ADR-0041 change-log line** recording the Q1 port-placement refinement.
   Then CI gate → merge → tag `v0.4.18-phase3-alpha7.4`.

---

## Section 6 — Reviewer sign-off

**SIGNED OFF (2026-07-17).** All seven §4 questions approved, with two additions
and minor naming refinements:

- **Q1 — Port placement:** ✅ Approve the recommendation. Strict boundary: the
  neutral **`ProviderDispatcherPort`** (+ DTOs + errors) lives in
  `app/application/interfaces/providers.py`; **all** provider-specific code
  (capability `ports.py`, `registry.py`, `dispatcher.py`, `mocks/`) lives under
  `app/infrastructure/ai/providers/`. The workflow runner knows **only**
  `ProviderDispatcherPort` (exactly like `PublisherPort` / lock manager /
  repository interfaces). Add **one `import-linter` rule** making
  `app.infrastructure.ai.providers` a **strict leaf** (forbid importing
  `app.application` / `app.api` / the workflow domain). Record the refinement as an
  ADR-0041 change-log line.
- **Q2 — DTOs:** ✅ Immutable dataclasses for the per-capability
  `GenerateImageRequest` / `GenerateImageResponse`, `GenerateVideoRequest` /
  `GenerateVideoResponse`, `GenerateTextRequest` / `GenerateTextResponse`,
  `GenerateSpeechRequest` / `GenerateSpeechResponse`. Capability **interfaces stay
  `Protocol`s**. Deterministic tests, simple serialization, transport independence.
- **Q3 — Registry:** ✅ Explicit registration —
  `registry.register(provider=MockImageProvider(), capabilities=[Capability.IMAGE])`.
  **No decorators** in α7.4 (decorators become sugar over `register()` in α8+).
- **Q4 — `provider_settings`:** ✅ Minimal read path only: **load enabled providers
  → select configured provider → construct adapter.** **Do NOT** implement
  fallback, weighting, priority, or health ordering — those land once multiple real
  providers exist. `NoProviderAvailable` is raised when no configured provider
  exists for a capability (it plays ADR-0041's `NoHealthyProvider` role; renamed
  since health-ordering is deferred).
- **Q5 — Async mock:** ✅ The **video** mock behaves like reality: `submit()` →
  `status = IN_PROGRESS` → `provider_job_id` → (later) completion. This exercises
  the completion *shape* before real providers; nothing completes it in α7.4. The
  other three mocks return `SUCCEEDED` inline.
- **Q6 — Dispatcher scope:** ✅ The dispatcher supports **only Image, Video, LLM,
  Voice**. **Explicitly exclude** Render, Export, Storage, and workflow
  orchestration (`start_render` is **not** in the α7.4 mapping table) — those are
  separate later slices. The dispatcher is **not** wired into `AdvanceWorkflowRun`
  this slice (D3.3).
- **Q7 — Errors:** ✅ Typed provider exceptions beside the dispatcher interface:
  `ProviderError`, `ProviderUnavailable`, `ProviderRateLimited`,
  `ProviderAuthenticationError`, `ProviderTimeout`, `ProviderValidationError`
  (+ `NoProviderAvailable`). **No HTTP mapping, no FastAPI concerns** — pure
  domain/application semantics.

**Addition 1 — capability discovery on the registry (for α7.6 generic pipelines).**
`registry.list_capabilities()`, `registry.list_providers(capability)`,
`registry.has_provider(capability)`, `registry.supports(Capability.IMAGE)`. Surfaced
on `ProviderDispatcherPort` (`supports` / `list_capabilities`) too, so the α7.6
pipeline (application layer) can ask `dispatcher.supports(Capability.IMAGE)` without
importing infrastructure — keeping orchestration generic (no `if image:` hardcoding).

**Addition 2 — immutable `ProviderMetadata` on every provider.**
`ProviderMetadata(id, name, capability, supports_polling, supports_webhooks,
version)` — every provider (mock now; OpenAI / Gemini / Runway / Fal / Ideogram /
Leonardo later) exposes it identically. Discovery returns metadata.

**Forbidden in α7.4 (verbatim).** ❌ HTTP clients · ❌ aiohttp · ❌ requests ·
❌ API keys · ❌ external calls · ❌ Redis · ❌ Celery · ❌ retries · ❌ provider
fallback · ❌ usage accounting · ❌ event publishing · ❌ polling loop · ❌ webhook
handlers. **This slice is almost entirely architecture.**

- **Version:** ✅ `0.4.18-phase3-alpha7.4-dev` → tag `v0.4.18-phase3-alpha7.4`.

**Roadmap unchanged:** α7.1 ✅ → α7.2 ✅ → α7.3 ✅ → **α7.4 (current)** → α7.5
usage recorder → α7.6 first mock pipeline → α8.1 image → α8.2 video → α8.3
completion (webhook + polling) → α8.4 FFmpeg render → α8.5 export.

Proceed: branch `phase3/alpha7.4-provider-skeleton`, bump `app/main.py` →
`0.4.18-phase3-alpha7.4-dev`, implement in the §5 order, full quality gate, then
pause for release approval before touching `main` (linear history preserved).
