# ADR-0041 — The Provider Runtime Is a Set of Pure Ports Driven by an Imperative Shell Over Existing Infrastructure

**Status:** Proposed (a **docs-only** planning blueprint — the "α8.0 Provider
Runtime Blueprint" — that locks the runtime *contract* every α7.3→α8.x slice
implements against). **No code, no branch, no migration, no version bump** ship
with this ADR. Flips to Accepted on merge of this ADR PR; each downstream slice
cites it and implements one seam.

**The runtime era.** α7.1 (`RenderJob`, ADR-0039) and α7.2 (`WorkflowRun` + the
synchronous deterministic runner, ADR-0040) modelled orchestration *state* and a
*pure* runner. What is still missing is **execution**: the layer that turns the
runner's declarative `StepCommand`s and the queued `RenderJob`s into real work —
provider calls, background workers, completion, and the atomic publication of the
events those aggregates already write. This ADR is the contract for that layer.

**Reuses the substrate already in baseline `0001`; builds nothing new to persist.**
Every table the runtime needs already exists (`distributed_locks`, `event_outbox`,
`usage_records`, `credit_ledger`, `provider_settings`, `ai_models` /
`ai_model_pricing`, `media_assets`, `webhook_deliveries`, `idempotency_keys`), and
carries zero application consumers today. The runtime is **application +
infrastructure code over an unchanged schema** — the D7 zero-migration discipline
holds through α8.

**Refines / documents:** `docs/architecture/CONTENT_GENERATION_PIPELINE.md` (§5
provider abstraction, §6 workers, §7 concurrency, §8 generation flow, §9 render
flow, §11 retries, §13 sequencing, §14 D1–D9). Builds on **ADR-0031**
(idempotency-keys FSM), **ADR-0032** (distributed-locks lease CHECK), **ADR-0033**
(usage-records `request_id` unique), **ADR-0037** (media register-by-metadata),
**ADR-0039** (RenderJob), **ADR-0040** (WorkflowRun + pure `StepCommand`).

**Wave:** Phase 3, planning slice **α8.0**. The contract is exercised by α7.3
(outbox relay + lock manager), α7.4 (provider skeleton + mock providers), α7.5
(usage recorder), α7.6 (first pipeline, mock providers), then α8.1–α8.5 (real
image/video providers, webhook/polling completion, FFmpeg render, export).

---

## Context

The pipeline blueprint's runtime decisions were signed off at D1–D9 (§14,
2026-07-16): Celery + Redis as the broker (D2), an in-house runner adaptable to
LangGraph (D3), **poll-first with a single completion service** (D4), and
event-only cross-aggregate coordination (D9). Two orchestration slices then
shipped:

- **α7.1 `RenderJob`** — `create/list/get/cancel`; a `queued` job whose
  `running/succeeded/failed` transitions and worker-owned fields
  (`output_media_asset_id`, `started_at`, `finished_at`, `error`, `progress`
  beyond `'0.00'`) are explicitly deferred to "the render worker (α8.x)."
- **α7.2 `WorkflowRun`** — a synchronous, deterministic runner over **pure** step
  handlers that return a `StepResult` carrying declarative
  `StepCommand`s. Nothing consumes `StepCommand`s yet (ADR-0040 D4:
  "only the interpreter of `StepCommand` changes" in α8.x).

Both aggregates already **produce** domain events (`RenderJobCreated/Canceled`,
six `WorkflowRun*`) into `event_outbox`, but **no relay publishes them** —
`published_at` stays `NULL` forever. And `distributed_locks` exists with the
ADR-0032 lease CHECK but has **zero acquire/heartbeat/release code**. A provider
worker built before these two prerequisites would emit events into a void and run
without a single-writer guarantee.

This ADR resolves that by **locking the interfaces** for the whole runtime *before*
any provider is wired, so each later slice implements a stable seam rather than
re-deciding the shape. It follows the "runner before worker" discipline (Q2,
signed off): the driver interface is defined now and exercised **in-process /
synchronously** with mock providers through α7.6; the real Celery + Redis broker
and its config land only in α8.1, when there is an actual external provider to
execute.

### What exists vs. what each slice builds (the reuse ledger)

| Seam | Exists in baseline / α7.x | Built by |
|---|---|---|
| `event_outbox` producers | ✅ α7.1 / α7.2 | — |
| **Outbox relay** (publish, mark `published_at`) | ⛔ | **α7.3** |
| `distributed_locks` table + lease CHECK | ✅ baseline | — |
| **Lock manager** (acquire/heartbeat/release/janitor) | ⛔ | **α7.3** |
| `ProviderPort` + registry + adapter lifecycle | ⛔ (docs only) | **α7.4** |
| Mock provider per capability | ⛔ | **α7.4** |
| `StepCommand` dispatcher (runner → provider) | ⛔ | **α7.4 / α7.6** |
| `usage_records` / `ai_model_pricing` | ✅ baseline | recorder in **α7.5** |
| First end-to-end pipeline (mock) | ⛔ | **α7.6** |
| Real image / video providers | ⛔ | **α8.1 / α8.2** |
| Completion service + poll / webhook | ⛔ (contract here) | **α8.3** |
| RenderJob worker transitions + FFmpeg | ⛔ | **α8.4** |
| Export engine + storage providers | ⛔ | **α8.5** |
| Celery + Redis broker + config | ⛔ | **α8.1** (first real provider) |

---

## Decision

The provider runtime is **pure ports + an imperative shell**, mirroring the α7.2
runner: the *ports* (providers, storage, clock, lock, completion) are
side-effecting seams behind narrow interfaces; the *shell* (workers, dispatcher,
relay) is the only place effects are ordered. Everything below is a **contract**,
not an implementation — signatures pin the shape; bodies land in later slices.

### D1 — `ProviderPort`: capability interfaces, provider is a leaf layer

A provider is addressed only through one of four **capability protocols**, keyed by
`plugin_kind ∈ {llm, image, video, voice}` (blueprint §5). Each is a narrow,
typed, **async** port that takes a typed request and returns a typed result or
raises a typed provider error — never leaking SDK types upward. The `providers`
package is a **leaf** (`import-linter`-enforced): it never imports `agents` /
`workflows` / `application`.

```python
# app/infrastructure/ai/providers/ports.py  (contract — α7.4 implements)
class ProviderResult(Protocol):
    request_id: str            # client-minted, dedupes usage + completion
    model_id: UUID             # resolved ai_models row (RESTRICT on media_assets)
    provider: str              # registry key, e.g. "mock-image", "replicate"
    status: Literal["succeeded", "in_progress", "failed"]
    output: Mapping[str, Any]  # sync result payload (bytes ref / structured)
    provider_job_id: str | None  # set when status == in_progress (async → completion)
    usage: ProviderUsage       # units for the Usage Recorder (D12)
    error: ProviderError | None

class ImageProvider(Protocol):
    plugin_kind: ClassVar[Literal["image"]]
    async def generate_image(self, req: ImageRequest) -> ProviderResult: ...
    async def health(self) -> ProviderHealth: ...

# LLMProvider / VideoProvider / VoiceProvider follow the same shape.
```

An async provider (e.g. video) returns `status="in_progress"` + a
`provider_job_id`; the completion service (D5) later resolves it. A synchronous
provider returns `succeeded`/`failed` inline. This one return shape lets a mock
provider be **fully deterministic** (Q2): it returns a fixed `ProviderResult`, so
α7.4–α7.6 run with no network and no broker.

### D2 — Provider registry: decorator registration, precedence selection, fallback chain

Providers self-register via a single `@register_plugin(kind=..., key=...)`
decorator into an in-process registry (blueprint §5 / `ARCHITECTURE.md`
§8.1–§8.3). Resolution is a **precedence chain**, then a **fallback chain**:

- **Selection precedence:** per-request override → project default → user/tier
  default → tenant/global default → configured fallback chain.
- **Config precedence** (`provider_settings`, schema §27.3): tenant row → global
  row → env var → built-in default; `is_secret` values are KMS ciphertext at rest.
- Exhausting the fallback chain raises **`NoHealthyProvider`** (→ the calling step
  fails per §11).

The registry is a pure lookup over configuration; it performs no I/O and is
trivially unit-testable with mock registrations.

### D3 — Adapter lifecycle: construct → health → wrapped call → typed error

An adapter is constructed from resolved `provider_settings`, exposes `health()`
for the selection chain, and every capability call is **wrapped by the Usage
Recorder** (D12). The adapter owns provider-SDK translation and maps SDK faults to
a small typed hierarchy — `ProviderTimeout`, `ProviderRateLimited`,
`ProviderInvalidRequest`, `ProviderUnavailable` — which the retry policy (D10)
classifies as transient vs terminal. Adapters hold no cross-request state
(stateless, horizontally scalable, blueprint §6).

### D4 — Command dispatcher: the imperative interpreter of `StepCommand` (ADR-0040 D4)

The α7.2 runner stays pure. A **`StepCommandDispatcher`** is the imperative shell
that maps a declarative `StepCommand{kind, args}` to a resolved provider capability
call, threads the result back as the step's `output` / `checkpoint_state`, and (for
async commands) records the `provider_job_id` for completion. The mapping is an
explicit, closed table — the only place `kind` strings become provider calls:

| `StepCommand.kind` | Capability | Sync? |
|---|---|---|
| `generate_text` | `LLMProvider.generate_text` | sync |
| `generate_image` | `ImageProvider.generate_image` | sync |
| `generate_video` | `VideoProvider.generate_video` | **async** → completion |
| `synthesize_voice` | `VoiceProvider.synthesize_voice` | sync/async |
| `start_render` | creates a `RenderJob` (links `workflow_run_id`) | async → event |

The dispatcher is what turns α7.2's `advance` from a pure loop into a
provider-driving driver in α7.6 — **without changing the pure handler contract**.
Until α7.4 it does not exist; the runner keeps ignoring `result.commands`.

### D5 — One completion service, poll-first (blueprint D4)

Async provider jobs (and `RenderJob`s) converge on a **single completion service**
— the sole writer that turns an external job's terminal outcome into aggregate
state + a `*Finished`/`*Failed` event + (for generation) a `media_assets` row.
Both the **polling worker** (D6, built first) and the **webhook ingress** (D7,
later) call the *same* `complete(provider_job_id, outcome)` entrypoint, which is
**idempotent** on the provider job id: a poll and a webhook for the same job
produce exactly one state transition. This is the "single completion service"
D4 mandates and the reason poll-vs-webhook is an ingress detail, not two code
paths.

### D6 — Polling lifecycle

The polling worker periodically queries in-progress provider jobs' status and
calls the completion service on a terminal result. It holds the per-job lock
(D8), applies capped backoff between polls, and treats a stuck job (lease expiry)
as a janitor-reclaimable failure (§11). Poll cadence and cap come from
configuration, not code constants.

### D7 — Webhook lifecycle (contract now, implementation deferred)

Inbound provider callbacks (`POST /webhooks/providers/{name}`) are **idempotent on
`idempotency_keys(resource_type='webhook')`** (blueprint §6/§7), verified for
authenticity, then funnel into the *same* completion service (D5). Outbound
notifications reuse the existing `webhook_deliveries` table (unique
`(tenant_id, source_event_id)`). The inbound router and verification are **α8.3**;
this ADR only fixes that webhook and poll are two ingresses to one idempotent
completion, never divergent logic.

### D8 — Distributed lock manager: single-writer leases over `distributed_locks` (ADR-0032)

An **`IDistributedLockManager`** gives every worker a single-writer guarantee over
the existing table (columns `lock_key, owner, lease_until, heartbeat_at,
acquired_at, metadata`; CHECK `lease_until > acquired_at`):

```python
# app/application/interfaces/locks.py  (contract — α7.3 implements)
class IDistributedLockManager(ABC):
    async def acquire(self, key: str, owner: str, lease: timedelta) -> Lease | None: ...
    #   atomic upsert; succeeds on a free key OR one whose lease_until < now()
    #   (steal-after-expiry) in one round trip; returns None if held & live.
    async def heartbeat(self, lease: Lease) -> bool: ...   # extend lease_until
    async def release(self, lease: Lease) -> None: ...     # owner-fenced delete
```

Canonical keys (blueprint §7): `render_job:<id>`, `workflow_run:<id>`,
`project_publish:<id>`, `timeline_edit:<id>`. A **lock janitor** worker reclaims
`lease_until < now()` rows. Acquire and steal-after-expiry are one atomic
statement (no read-then-write race). This is the first application consumer of a
table that has existed, unused, since baseline.

### D9 — Event ordering: the outbox relay (α7.3) publishes atomically-written events

State and its announcing event already commit in one transaction (D9 of the
blueprint; α7.1/α7.2 producers). The missing half is the **relay**: a worker that
selects unpublished rows, publishes them, and stamps `published_at`:

```sql
SELECT * FROM event_outbox
WHERE published_at IS NULL
ORDER BY occurred_at
FOR UPDATE SKIP LOCKED
LIMIT :batch;      -- publish → UPDATE published_at = now(); on failure bump attempts,last_error
```

Delivery is **at-least-once**; every consumer is therefore **idempotent** (keyed on
`aggregate_id + event_type` or the completion job id, D5). Ordering is best-effort
by `occurred_at` per aggregate, never a total order. The relay is the seam that
finally makes the eight events α7.1/α7.2 already write observable to downstream
consumers.

### D10 — Retry semantics: layered, each owner keeps its own policy (blueprint §11)

| Layer | Mechanism | Terminal |
|---|---|---|
| Provider call | typed-error classification → fallback chain (D2/D3) | `NoHealthyProvider` → step fails |
| Workflow step | `workflow_steps.retries` counter (α7.2, deterministic) up to cap; `retrying → running` | step `failed` → run `failed` unless optional (`skipped`) |
| Workflow run | resume from last `workflow_checkpoint`; `workflow_retry` idempotency key | manual retry = fenced re-run |
| Render / export | structured `error {code,message,trace_id,retries}` | `failed`; re-queue = new job row |
| Poison / stuck | lease expiry → janitor reclaim; DLQ for repeat failures | surfaced as run/job `failed` + trace id |

Only transient typed errors are retried; terminal ones fail fast. Backoff is
**represented** (config) but the scheduler is a worker concern — consistent with
α7.2's "retry counter, no scheduler."

### D11 — Worker responsibilities; runner-before-worker (Q2)

Five stateless, horizontally-scalable roles (blueprint §6): **workflow worker**
(one runner tick under `workflow_run:<id>`), **render worker** (`render_job:<id>`),
**outbox relay** (D9), **lock janitor** (D8), **webhook/ingress worker** (D7).
Per Q2, the *driver* that invokes a worker's body is defined behind an interface
now and runs **in-process/synchronously** with mock providers through α7.6 — a
`POST …/advance`-style call or a test loop drives it, exactly as α7.2's runner
does today. **Celery + Redis + `REDIS_URL`/broker config are introduced in α8.1**,
the first slice with a real external provider to execute. Until then: deterministic
tests, no infrastructure, faster CI, zero broker races.

### D12 — Media persistence seam: worker output reuses the α6.2 register path

A generation worker writes bytes via a **Storage Provider** (CR-5:
`local/s3/r2/azure_blob/gcs` behind the same plugin discipline), then registers a
`media_assets` row with `source=generated`, the resolved `model_id` (RESTRICT), the
`provider`, storage coords, checksum, and dims/duration — **reusing ADR-0037's
register-by-metadata contract**, not a parallel writer. This is what unblocks the
`source=generated` value currently rejected at the α6.2 DTO. The completion service
(D5) is the sole caller.

### D13 — Usage/cost seam: every call wrapped, idempotent on `request_id`

Every provider call is wrapped by the **Usage Recorder** (α7.5), which writes one
`usage_records` row priced against `ai_model_pricing`, **idempotent on the
per-partition `request_id` unique** (ADR-0033). Credit debits post to the
append-only `credit_ledger`, **idempotent on `(tenant_id, idempotency_key)`**. The
recorder is a decorator around the adapter (D3), so no provider forgets to record
and a retried call never double-charges.

### D14 — Sequencing honours §13 as-is; zero migrations through α8

No renumbering (Q1). The implementation order, dependency-first:

```
α7.3  Outbox relay + Lock manager      (unblocks events + single-writer)
α7.4  Provider skeleton                (ProviderPort, registry, dispatcher, 1 mock/kind)
α7.5  Usage recorder                   (usage_records + credit_ledger seams)
α7.6  First pipeline (mock provider)   (runner drives dispatcher end-to-end, in-process)
α8.1  Image provider     ── introduces Celery + Redis + broker config
α8.2  Video provider     ── first async provider → exercises completion service
α8.3  Webhook / polling completion
α8.4  FFmpeg render      (RenderJob worker transitions + compose)
α8.5  Export engine      (export_jobs + storage providers)
```

Each slice keeps the α-series discipline (pre-flight → sign-off → branch →
implement → CI → merge → tag) and ships **zero migrations** — the schema is
already complete.

---

## Alternatives Considered

- **Jump straight to an image/video worker.** Rejected: the events those workers
  emit would never publish (no relay) and they would run without a single-writer
  lease (no lock manager). The two prerequisites (α7.3) must come first.
- **Introduce Celery + Redis now (Q2=B).** Rejected: it adds broker infrastructure,
  non-deterministic tests, and CI slowness before there is any real provider to
  execute. Runner-before-worker deferred exactly this in α7.1/α7.2 and it paid off.
- **Renumber the roadmap under an "α8" umbrella (Q1=B).** Rejected: churns ADRs,
  ROADMAP, CHANGELOG, and tag history for zero architectural gain. §13 already
  sequences the runtime; honour it.
- **Two completion code paths (poll and webhook separately).** Rejected: violates
  D4's single completion service and invites divergent, double-effect logic. One
  idempotent `complete()` with two ingresses.
- **Providers call the outbox / mutate aggregates directly.** Rejected: breaks D9
  no-cross-mutation. Providers are a leaf; the completion service and dispatcher
  (the shell) own all state writes and event emission.

## Consequences

**Positive.** Every later slice implements against a frozen contract, so the phase
becomes "almost mechanical." The pure-port / imperative-shell split keeps mock-
driven, deterministic tests through α7.6. The runtime reuses nine existing tables
(zero migrations). The relay + lock manager, once built, are consumed uniformly by
all five worker roles. Poll-first with one completion service means webhooks are a
later ingress, not a rewrite.

**Negative / accepted.** Contract-first means some interfaces (webhook verification,
storage-provider config, DLQ shape) are only sketched here and finalised in their
slice; minor drift is expected and resolved in each pre-flight. At-least-once
relay delivery pushes idempotency onto every consumer (accepted; the tables already
support it). The mock-driven phase (α7.4–α7.6) does not exercise real network
failure modes — those first appear in α8.1 and may surface adapter-level fixes.

**Neutral.** This ADR ships no code; it is a planning artefact. It does not change
runtime behaviour until α7.3 begins implementing it.

## Pattern Reference (contract sketches, not implementations)

The signatures in D1 (`ProviderPort`), D8 (`IDistributedLockManager`), and the D9
relay query are the load-bearing contract sketches. Concrete implementations,
their unit/integration tests, and the mock providers land in α7.3 (relay + locks),
α7.4 (ports + registry + dispatcher + mocks), and α7.5 (recorder). No bodies are
shipped with this ADR.

## Future Extensions

- **LangGraph adapter** (D3 of the blueprint): the dispatcher (D4) is the seam a
  LangGraph runtime plugs into without touching the pure handler contract.
- **DLQ + poison handling** (D10): concrete dead-letter shape decided in the worker
  slices.
- **`(provider, request_id)` composite idempotency** (ADR-0033 defers this): revisit
  once multiple providers can share a `request_id` space.
- **Storage-provider plugin contract** (CR-5): fully specified in α8.5 (export).

---

## References

- `docs/architecture/CONTENT_GENERATION_PIPELINE.md` — §5 provider abstraction, §6
  workers, §7 concurrency, §8 generation, §9 render, §11 retries, §13 sequencing,
  §14 D1–D9 (runtime decisions signed off 2026-07-16).
- **ADR-0031** idempotency-keys FSM · **ADR-0032** distributed-locks lease CHECK ·
  **ADR-0033** usage-records `request_id` unique · **ADR-0037** media
  register-by-metadata · **ADR-0039** RenderJob · **ADR-0040** WorkflowRun + pure
  `StepCommand`.
- `docs/domain/RENDER_JOB_AGGREGATE.md`, `docs/domain/WORKFLOW_RUN_AGGREGATE.md`,
  `docs/domain/MEDIA_AGGREGATE.md`.

## Change log

| Date | Change |
|---|---|
| 2026-07-17 | Initial authoring — the **α8.0 Provider Runtime Blueprint** (docs-only). Locks the runtime contract: `ProviderPort` capability protocols + leaf layer (D1), decorator registry with precedence + fallback (D2), adapter lifecycle with typed errors (D3), the `StepCommand` dispatcher as the imperative interpreter of α7.2's pure runner (D4), one poll-first completion service (D5) with polling (D6) and webhook (D7) ingresses, the `IDistributedLockManager` over `distributed_locks` (D8), the outbox relay `FOR UPDATE SKIP LOCKED` (D9), layered retry semantics (D10), five stateless worker roles + runner-before-worker deferral of Celery/Redis to α8.1 (D11), the media register-by-metadata seam unblocking `source=generated` (D12), the Usage Recorder / credit-ledger idempotent cost seam (D13), and §13-honouring, zero-migration sequencing (D14). No code, no branch, no migration, no version bump. Adopts Q1=A / Q2=A / Q3=A. |
| 2026-07-17 | **α7.4 port-placement refinement of D1/D4** (implementing slice, `v0.4.18`). D1 sketched the capability protocols at `app/infrastructure/ai/providers/ports.py` (a leaf). To let the α7.6 runner (a `use_cases` module, which may not import `infrastructure`) depend on the dispatcher, α7.4 splits the contract: the **neutral DTOs / enums / metadata / errors** live in `app.application.interfaces.providers`, the runner-facing **`ProviderDispatcherPort`** in the sibling `app.application.interfaces.provider_dispatcher` (it references `StepCommand`), and the **capability `Protocol`s + registry + mocks** stay in the `app.infrastructure.ai.providers` **strict `import-linter` leaf** (forbids `app.application.use_cases` / `app.api` / the workflow domain — the neutral contract in `app.application.interfaces` remains an allowed dependency, matching the repository precedent). The concrete **`StepCommandDispatcher`** (D4) lives at `app.infrastructure.ai.dispatcher`, one level **above** the leaf, so it can bridge `StepCommand` ↔ capability calls without weakening the leaf. Registry `NoHealthyProvider` (D2) ships as **`NoProviderAvailable`** in α7.4 since fallback/health-ordering are deferred until multiple real providers exist. Errors renamed per α7.4 sign-off Q7 (`ProviderInvalidRequest` → `ProviderValidationError`; adds `ProviderAuthenticationError`). |
| 2026-07-19 | **α7.6 composes D4 + D13 into the first end-to-end pipeline** (implementing slice, `v0.4.20`). Wires the dispatcher (D4) into the α7.2 runner: `AdvanceWorkflowRun` now interprets a succeeded step's `StepCommand`s, minting a **deterministic** `request_id` = `run_id:step_index:command_index` (owned by the runner, never the provider) and dispatching each **exactly once** (**W7.6.2** — no dispatcher-side retry; retries stay the runner's). Terminal usage (D13) is recorded in the runner's **own** transaction through the new **`record_usage_in_uow(...)`** helper (α7.5's `record()` public API is unchanged), so the `usage_records` row commits/rolls-back atomically with the step. **W7.6.1 — the runner never interprets provider payloads:** it forwards `resp.usage`/`resp.status` to the recorder (capability + `model_id` come from the command it minted) and stores `resp.output` as an **opaque** checkpoint envelope, so it stays provider-agnostic. `IN_PROGRESS` (D5's async shape) takes the run `running → paused` via a new `mark_run_paused` CAS (`paused` is **not** terminal — `finished_at` unset), checkpoints `provider_job_id`, and emits the single new **`WorkflowRunPaused`** event (α8.3 owns resumption). Three-bucket error mapping (transient→retry / terminal→fail / provider-`FAILED`→record-then-fail). Fail-fast `MODEL_ID_MISSING` before dispatch. Ships two registry pipelines (`generate-image` full success; `generate-video` pause-only). **No media rows** (D12 stays α8.4's — checkpoint only), no broker/HTTP/real providers/polling/webhooks. **Zero migration.** |
| 2026-07-21 | **α8.1 ships the first real adapter behind D1/D4/D10** (implementing slice, `v0.4.21`). Replaces the **one** mocked box below the dispatcher — the image provider — with a synchronous **`OpenAIImageProvider`** (`app/infrastructure/ai/providers/openai/image.py`) implementing D1's `ImageProvider` protocol over `POST /images/generations` (`dall-e-3`, `response_format="url"` → compact URL ref, **no storage** until α8.4). D10's transient/terminal split is realized as an explicit HTTP-status → typed-`ProviderError` map (401/403→auth·terminal; other 4xx/policy→validation·terminal; 429→rate-limited·transient; 5xx/connection→unavailable·transient; timeout→timeout·transient); the adapter makes **exactly one** request per call — **W7.6.2** holds, all retry stays the runner's. The registry (D2) is composed by config in the DI container: `OPENAI_API_KEY` present → IMAGE resolves to the real provider, absent → mock; **one provider per capability, no fallback / precedence / health-ordering** (deferred until multiple real providers exist). **Nothing above the leaf changed** — runner, `StepCommandDispatcher`, `UsageRecorderService`, relay, lock manager, `ProviderRegistry` class, neutral DTOs, and `ports.py` are untouched (the whole diff is the leaf + container wiring + `httpx` promoted to a core dep). Three invariants: **W8.1.1 — adapters are configuration-blind** (the provider receives a pre-authenticated `httpx.AsyncClient`; it never reads env/DB/secrets — *constructors receive secrets, they never retrieve them*), **W8.1.2 — exactly one real capability** (IMAGE; LLM/VIDEO/VOICE stay mock), **W8.1.3 — observational equivalence** (the real `GenerateImageResponse` is indistinguishable from the mock's by type/fields/status/shape; only values differ). No Celery/Redis/webhooks/polling/storage/media-registration/selection/rate-limiter/circuit-breaker. **Zero migration.** |
| 2026-07-21 | **α8.2 ships the first real *async* adapter behind D1/D4/D5/D10** (implementing slice, `v0.4.22`). Replaces the remaining async-shaped mock — the video provider — with a submit-only **`FalVideoProvider`** (`app/infrastructure/ai/providers/fal/video.py`) implementing D1's `VideoProvider` protocol over the Fal.ai queue endpoint. It exercises D5's async shape for real: **exactly one** HTTP request (**W7.6.2**) *submits* the job and returns `IN_PROGRESS` + `provider_job_id` (= the Fal `request_id`, the runner's resume coordinate) — it never polls/waits/resolves; completion (poll/webhook/resume/terminal usage) stays D5's **α8.3**. The completion URLs ride a **versioned opaque `output` envelope** (`schema_version: 1`) the runner checkpoints verbatim (**W7.6.1**), giving α8.3 a stable payload contract. D10's transient/terminal split reuses α8.1's exact HTTP-status → typed-`ProviderError` map (401/403→auth·terminal; 4xx→validation·terminal; 429→rate-limited·transient; 5xx/connection→unavailable·transient; timeout→timeout·transient). **No usage on submit** — the runner already discards usage on the `IN_PROGRESS` pause (α7.6); α8.3 records the priced terminal row under the same `request_id`. The registry (D2) composes VIDEO by config **independently of IMAGE**: `FAL_API_KEY` present → real VIDEO, absent → mock; **one provider per capability, no fallback / precedence / health-ordering**. **Nothing above the leaf changed** — runner, `StepCommandDispatcher`, `UsageRecorderService`, relay, lock manager, `ProviderRegistry` class, neutral DTOs, `ports.py`, and the `generate-video` pipeline are untouched (the whole diff is the new `fal/` leaf + one container branch). Four invariants: **W8.1.1 — configuration-blind** (a pre-authenticated `httpx.AsyncClient` with the `Authorization: Key …` header; the provider never reads the raw key), **W8.2.1 — observational equivalence** with `MockVideoProvider` on the `IN_PROGRESS` path (the runner pauses identically; only values + `usage=None` differ), **W8.2.2 — the run stops at the pause boundary** (the adapter only ever returns `IN_PROGRESS`), **W8.2.3 — the adapter never mutates orchestration state** (no resume/complete/checkpoint/event/usage — a pure request→response leaf). No polling/webhooks/completion-service/Celery/Redis/storage/media-registration/`video_ref`/export/selection/rate-limiter/circuit-breaker. **Zero migration.** |
| 2026-07-22 | **α8.3 implements D5's completion service (poll-first) behind D6/D8/D11** (implementing slice, `v0.4.23`). Closes the async loop α8.2 opened: the single, idempotent **completion engine** (`CompletionEngine.complete()` — one public method every ingress converges on; `poll_once()` is the D6 polling ingress) turns an in-flight provider job's terminal outcome into aggregate state. Grounding confirmed the runner **already resumes** (re-advances a `running` run, skips `succeeded` steps), so completion **never re-implements step execution and never re-dispatches** (**W8.3.3**): it resolves the job, records the deferred terminal usage under the **checkpointed** `request_id`, marks the paused step `succeeded`, flips `paused → running`, and hands continuation to the **unchanged** runner. Resume is split across two public seams so no service touches runner internals — `CompletionEngine` (resolve under the per-run lease) → **`ResumeWorkflowRun`** (atomic *resume + terminal usage + step-succeeded + delegate continuation* in one txn) → `AdvanceWorkflowRun` (step-execution semantics untouched; entered via a new **public** `continue_paused_run_in_uow` that drives + settles on the caller's open UoW). The async capability becomes a **lifecycle** (Q3): `VideoProvider.submit()` (renamed from `generate_video`) + new `resolve()` (terminal, or `IN_PROGRESS` if still running); Fal `resolve` GETs the α8.2 opaque envelope's `status_url`/`response_url`, mock resolves deterministically; `ProviderDispatcherPort.resolve_job` routes VIDEO (sync capabilities → terminal `ProviderValidationError`). **Exactly-once resume** (**W8.3.2**) via the `workflow_run:<id>` lease (D8) + the `paused → running` CAS backstop; usage idempotent on `request_id`. New `WorkflowRunResumed` event (Q9). Ingress = **polling only** (D6 poll-first); webhook is a thin second ingress to the same `complete()` → **α8.3b**. Library-only, synchronous (D11 — `complete()`/`poll_once()` driven by a test loop; no Celery/Redis/daemon); lease owner + duration are config. **Zero migration** — the only new persistence surface is two repo methods on existing tables (`resume_run` CAS, `list_paused` scan) + additive `_paused` handoff fields (`command_index`/`capability`/`model_id`/`tenant_id`/opaque `envelope`, Fork 1A). Unchanged: the pure step handlers, the dispatch `kind` contract, neutral DTOs, the `generate-video` pipeline, the `ProviderRegistry` class, the relay, the lock-manager impl, and the recorder public API. Four invariants **W8.3.1–W8.3.4** adopted. No media rows / `video_ref` (→ α8.4). |
| 2026-07-18 | **α7.5 gives D13's usage half a concrete producer** (implementing slice, `v0.4.19`). D13 specified the usage/cost seam; α7.5 ships the **`UsageRecorderService`** (`app.application.use_cases.usage`) that turns one **terminal** provider call into exactly one immutable, priced `usage_records` row — the producer ADR-0033 assumed but Phase 2 never built. Shipped as a **seam** (sign-off Q2): `record(RecordUsageCommand)` is called by the α7.6 pipeline around the dispatch, not wired into the runner this slice. Idempotency uses the ADR-0033 per-partition `uq_<child>_request_id` index via an insert-inside-SAVEPOINT + recover-on-collision (`DuplicateRequestIdError` → return existing, `idempotent_replay=True`). Pricing sums `Σ(unit_price × quantity)` over per-capability line items against `ai_model_pricing` (CR-11); the single `usage_records.unit`/`unit_count` records the **primary billing axis** (Q4). **Scoped down from D13 for this slice:** the **`credit_ledger` debit is deferred** (Q1 — `credits_consumed = 0`; the append-only ledger is its own aggregate + slice), missing pricing **never blocks** execution (Q5 — cost 0, `pricing_id` NULL, WARN), only **terminal** outcomes are recorded (Q6 — `IN_PROGRESS` rejected; the α8.3 completion service records the terminal row later under the same `request_id`), and **no `UsageRecorded` event** is emitted (Q8 — no consumer). **W7.5.1 — the recorder is purely observational** (its only write is `usage_records`; it never mutates an aggregate). **Zero migration** — reuses existing tables + migration `0007`. |
