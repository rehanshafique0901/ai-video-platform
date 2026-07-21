# CHANGELOG

> Keep-a-Changelog style. Each completed phase gets one entry. Pre-release work tracked under **[Unreleased]**.

---

## [Unreleased]

### Phase 3 Slice α8.1 — First Real Provider (OpenAI Images, synchronous) — the adapter slice (2026-07-21)

The **adapter** slice: it replaces the *one* mocked box at the bottom of the α7.6
pipeline — the image provider — with a **real** synchronous OpenAI Images adapter,
and proves the α7.4 abstraction / α7.6 orchestration can drive an external system
**without any orchestration-layer change**. The runner, `StepCommandDispatcher`,
`UsageRecorderService`, relay, lock manager, `ProviderRegistry` class, neutral DTOs,
and `ports.py` are all **byte-for-byte unchanged**; the entire behavioural diff lives
inside the provider leaf (`app/infrastructure/ai/providers/openai/`) plus minimal DI
wiring in the container. `OpenAIImageProvider` implements the existing `ImageProvider`
protocol over `POST /images/generations` (`dall-e-3`, `response_format="url"` — a
compact URL ref so **no storage layer** is needed; `gpt-image-1`/base64 waits for
α8.4), makes **exactly one** HTTP request per call (**W7.6.2** — all retry belongs to
the runner), and maps HTTP status → the existing typed `ProviderError` buckets
(401/403 → auth·terminal; other 4xx/policy → validation·terminal; 429 →
rate-limited·transient; 5xx/connection → unavailable·transient; timeout →
timeout·transient) so **nothing HTTP leaks upward** (Q7). The container composes the
registry by config: with `OPENAI_API_KEY` set, IMAGE resolves to the real provider;
without it, IMAGE stays on `MockImageProvider` — **exactly one provider per
capability, no selection engine, no fallback** (Q5). LLM/VIDEO/VOICE remain mock.
**Zero migration.** Three signed-off invariants govern the slice: **W8.1.1 —
adapters are completely configuration-blind** (the provider receives a
pre-authenticated `httpx.AsyncClient`; it performs no env/DB/filesystem/vault lookup
and never sees the raw key — *constructors receive secrets, they never retrieve
them*, Q4); **W8.1.2 — exactly one real capability** (IMAGE only); and **W8.1.3 —
observational equivalence**: the real adapter returns the *same* `GenerateImageResponse`
shape, populated field-set, and `SUCCEEDED` semantics as the mock, so the runner
cannot tell which produced a response — only the values (image URL, provider id)
differ. **Explicitly forbidden and absent:** Celery · Redis · webhooks · polling ·
storage · media registration · export · video/LLM/voice real providers ·
multi-provider fallback · provider selection · rate limiter · circuit breaker. See
`docs/engineering/PHASE3_ALPHA8_1_PREFLIGHT.md` and **ADR-0041** (D1/D4/D10).

#### Added
- **`OpenAIImageProvider`** (`app/infrastructure/ai/providers/openai/image.py`, new
  `openai/` subpackage in the strict provider leaf) — a synchronous adapter over the
  OpenAI image-generations endpoint implementing `ImageProvider`. Imports only
  `httpx` + the neutral provider DTOs/errors (no runner/dispatcher/recorder/workflow
  import — import-linter leaf contract still KEPT). Validates the requested model
  against a supported set (`dall-e-3`/`dall-e-2`) **before** any network call
  (unsupported → terminal `ProviderValidationError`, zero HTTP), performs one
  request, maps status → typed error, and returns `SUCCEEDED` with `image_ref` +
  `usage(unit="images")`. Static `health()` (Q10 — the registry does not consult
  health yet).
- **OpenAI settings** (`app/core/config.py`) — `openai_api_key: SecretStr | None`
  (default `None` → provider stays mock), `openai_base_url` (default
  `https://api.openai.com/v1`), and `openai_timeout_seconds` (default `60.0`, must be
  `> 0`). Mirrored in `backend/.env.example`.
- **Unit tests** — `tests/unit/infrastructure/ai/providers/test_openai_image.py`
  (success shape, request payload, one-request-per-call, the full status→error map,
  timeout/connection faults, empty/`url`-less 200 bodies, metadata, static health,
  and the **W8.1.3** observational-equivalence check against `MockImageProvider`, all
  through an in-memory `httpx.MockTransport` — CI never touches the network);
  `tests/unit/core/test_container_provider_registry.py` (Q5/W8.1.2 composition:
  key-present → real IMAGE provider, key-absent → mock, LLM/VIDEO/VOICE always mock,
  and the injected key baked into the shared client's `Authorization` header); plus
  new `Settings` cases in `tests/unit/core/test_config.py`.

#### Changed
- **DI container** (`app/core/container.py`) — the provider registry is now built by
  `init(settings)` (it joins the `init`/`shutdown`/`reset` lifecycle) via two new
  private helpers: `_build_openai_client(settings)` (a single shared,
  pre-authenticated `httpx.AsyncClient`, or `None` when no key is configured) and
  `_build_provider_registry(client)` (registers the real or mock IMAGE provider and
  the three mocks). `get_provider_registry()` returns that init-built singleton;
  `shutdown()` now `aclose()`s the shared client. `StepCommandDispatcher` and the
  runner factory are unchanged — they still receive a `ProviderRegistry` and never
  learn which concrete provider serves a capability (W8.1.3).
- **Dependencies** (`backend/pyproject.toml`) — `httpx>=0.27.0` promoted from the
  `dev` extra to a **core runtime** dependency (a real provider now calls it).

### Phase 3 Slice α7.6 — First Pipeline (mock) — runner ⇄ dispatcher ⇄ recorder ⇄ outbox, end-to-end (2026-07-19)

The **composition** slice: it introduces **almost no new infrastructure** and
instead wires the five seams already built (α7.2 runner · α7.4 dispatcher · α7.5
recorder · α7.3 outbox · checkpoints) into **one complete, deterministic,
in-process orchestration loop** — proving the entire stack end-to-end with **no
external provider dependency** (mocks stand in behind the dispatcher). The α7.2
`AdvanceWorkflowRun` is **extended, not forked** (Q6): after a pure step handler
succeeds, the runner now interprets its `StepResult.commands` — minting a
**deterministic** `request_id` (`run_id:step_index:command_index`, D5/Q3),
dispatching each command **exactly once** (W7.6.2 — retries are the runner's alone,
never the dispatcher's) via the injected `ProviderDispatcherPort`, recording
**terminal** usage in the **same** transaction (Q5), and either **pausing** on
`IN_PROGRESS` (Q2) or checkpointing the **opaque** provider envelope (W7.6.1). Two
pipelines ship: **`generate-image@1.0.0`** — fully executable (prepare-prompt →
mock image `SUCCEEDED` → priced `usage_records` row → checkpoint → `succeeded`) —
and **`generate-video@1.0.0`** — minimal pause seam (mock `IN_PROGRESS` +
`provider_job_id` → `running → paused`; **nothing** beyond pause: no completion, no
polling, no webhook — Q1). **Explicitly forbidden and absent:** real providers,
HTTP/SDKs, Redis/Celery/broker, polling loops, webhooks, and **`Media` rows** (Q7 —
generated media stays checkpointed; α8.4 owns registration). **Zero migration** —
reuses every existing table/enum. Two invariants govern the seam: **W7.6.1 — the
runner never interprets provider payloads** (it knows only `StepCommand` /
`ProviderResponse` / `ProviderStatus`; `image_ref` / prompt text / JSON payloads /
video metadata belong to the dispatcher + provider adapter) and **W7.6.2 — exactly
one dispatcher invocation per `StepCommand`.** See
`docs/engineering/PHASE3_ALPHA7_6_PREFLIGHT.md` and **ADR-0041** (D4/D11/D13).

#### Added
- **Provider-backed workflow pipelines** (`app/domain/workflow/registry.py`) — the
  `generate-image@1.0.0` (steps `prepare-prompt` → `generate-image`) and
  `generate-video@1.0.0` (step `generate-video`) definitions, registered in
  `default_registry()`, plus their **pure** handlers (`_prepare_image_prompt` /
  `_generate_image_step` / `_generate_video_step` and the `_generation_args`
  helper). Handlers only *emit* a `StepCommand` and thread `model` / `model_id` from
  the run input into its args — they never mint the `request_id`, never see a
  `ProviderResponse`, and do no I/O (provider-agnostic by construction).
- **Runner command execution** (`app/application/use_cases/workflow/advance_workflow_run.py`)
  — `AdvanceWorkflowRun` gains an optional `dispatcher: ProviderDispatcherPort` (+
  `default_currency`). After a step succeeds it runs `_execute_commands`: mints the
  deterministic `request_id`, injects it into a fresh `StepCommand`, dispatches
  **once** (W7.6.2), maps `ProviderError` → transient (runner-retry up to the step
  bound) / terminal (fail), handles `IN_PROGRESS` → pause and `FAILED` → record
  failed usage **then** fail (Q9), and records `SUCCEEDED` usage — all in the run's
  single transaction. The provider `output` is stored as an **opaque** checkpoint
  envelope via `_response_view` (W7.6.1). Fail-fast `MODEL_ID_MISSING` before
  dispatch (Q4). On pause the run settles `running → paused`, checkpoints the resume
  coordinates (`provider_job_id`, `pending_step_index`), and emits `WorkflowRunPaused`.
- **`record_usage_in_uow(...)`** (`app/application/use_cases/usage/usage_recorder_service.py`)
  — a **transaction-participating** helper that runs the account → price →
  idempotent-insert body on an **already-open** UoW **without committing**, so the
  runner records usage inside its own transaction (Q5). `UsageRecorderService.record`
  is refactored to wrap it (open → helper → commit) — the α7.5 public API is
  **unchanged**.
- **`WorkflowRunPaused` event** (`app/application/use_cases/workflow/_events.py`) —
  the single new event (Q8), carrying `step_index` + `provider_job_id` so the α8.3
  completion service can resume under the same `request_id`. No usage for a pause
  (Q6 — terminal-only).
- **`mark_run_paused` CAS** — `IWorkflowRunRepository.mark_run_paused` +
  its SQLAlchemy impl: a status-guarded `running → paused` that leaves `finished_at`
  **unset** (`paused` is not terminal). Mirrored in the test fakes.
- **DI wiring** (`app/core/container.py`) — `get_advance_workflow_run_use_case()`
  injects `dispatcher=get_step_command_dispatcher()`; the integration test UoW
  (`tests/integration/conftest.py`) wires `usage` + `model_pricing` for parity.
- **Docs** — this CHANGELOG, the α7.6 pre-flight sign-off, the content-generation
  pipeline note (§13 row + change log), and an ADR-0041 change-log line.
- **Tests** — unit (`test_advance_workflow_run_pipeline.py`, a `_ScriptedDispatcher`
  fake: image success + opaque checkpoint + priced usage; deterministic `request_id`
  + replay idempotency; video pause on `IN_PROGRESS` — `PAUSED` + event + no usage;
  provider `FAILED` — records usage then fails; transient retry with stable
  `request_id`; `model_id` fail-fast before dispatch; α7.2 backward-compat with a
  wired dispatcher) and integration (`test_first_pipeline_e2e.py`, the real runner +
  registry + `StepCommandDispatcher` + mocks + recorder on live SQL: image pipeline
  to `succeeded` with a priced `usage_records` row + verbatim opaque checkpoint +
  the started→completed×2→succeeded outbox chain; video pipeline to `paused` with
  the resume checkpoint, `WorkflowRunPaused`, and **no** usage).

#### Version
- App version bumped to **`0.4.20-phase3-alpha7.6`** (staying on `0.4.x`; still
  Phase-3 orchestration infrastructure).

### Phase 3 Slice α7.5 — Usage Recorder (priced, idempotent `usage_records` seam) (2026-07-18)

Activates the persistence that already exists (`usage_records`, `ai_model_pricing`)
by adding the **producer** ADR-0033 assumed but Phase 2 never built: a
`UsageRecorderService` that turns **one terminal provider call** into **exactly one**
immutable, priced `usage_records` row (ADR-0019, partitioned monthly by
`occurred_at`), **idempotent on `request_id`** (ADR-0033), priced against
`ai_model_pricing` (CR-11). This is **ADR-0041 D13**'s usage half. **Zero
migration** — every table, enum, and the per-partition `uq_<child>_request_id`
index (migration `0007`) already exist. Shipped as an explicit **seam** (Q2):
nothing is wired into the runner/dispatcher — the α7.6 pipeline calls
`record(...)` around the dispatch. **Explicitly forbidden and absent:** HTTP,
provider SDKs, Redis, Celery, polling, webhooks, event publishing (Q8 — no
`UsageRecorded` event, no consumer exists), and the `credit_ledger` debit (Q1 —
`credits_consumed` stays `0`; the append-only financial ledger is its own later
slice). **W7.5.1 — the recorder is purely observational:** its only write is
`usage_records`; it never mutates `WorkflowRun` / `WorkflowStep` / `RenderJob` /
`Media` / `Timeline` / `Project` / `ProviderSetting` (it holds only the `usage` +
`model_pricing` repos). See `docs/engineering/PHASE3_ALPHA7_5_PREFLIGHT.md`,
**ADR-0033**, and **ADR-0041**.

#### Added
- **Usage Recorder port + DTOs** (`app/application/interfaces/usage_recorder.py`) —
  the `UsageRecorderPort` (`record(RecordUsageCommand) -> UsageRecordView`), the
  `PricingUnit` / `UsageStatus` vocabularies (mirroring the DB enums without
  importing them), the **`RecordUsageCommand`** application contract (Q3 — richer
  than α7.4's `ProviderResponse`: `tenant_id` / `model_id` / `capability` / usage +
  workflow/render/project linkage, incl. `render_job_id` which has **no** column
  and rides in `extra`), the neutral `NewUsageRecord` (insert payload) /
  `UsageRecordRow` (read-model) / `EffectivePrice` (resolved price) DTOs, and the
  `DuplicateRequestIdError` replay signal. **No SQLAlchemy import** — neutral, like
  `OutboxEvent`.
- **Pure accounting/pricing policy** (`app/application/use_cases/usage/accounting.py`)
  — side-effect-free `account(command)` (maps `ProviderUsage` onto the typed
  `usage_records` axes **by capability** — D3.4 — and derives the **primary billing
  axis**: LLM→`completion_token`, image→`image`, video→`video_second`,
  voice→`audio_second`) + `price(accounting, prices)` (Q4 — `estimated_cost =
  Σ(unit_price × quantity)` over line items; a unit with no price contributes 0 and
  is reported). Tolerant of both the minimal α7.4 mock usage and a richer `detail`
  breakdown, so real α8.x providers need no recorder change.
- **`UsageRecorderService`** (`app/application/use_cases/usage/usage_recorder_service.py`)
  — account → price → assemble → **idempotent insert** in one transaction over one
  row. Terminal-only (Q6 — `IN_PROGRESS` is rejected with a `ValueError`; the α8.3
  completion service records the terminal outcome later under the same
  `request_id`). Missing pricing **never blocks** (Q5 — prices affected units at 0,
  leaves `pricing_id` NULL, emits a `WARN`). A colliding `request_id` (Q7) is
  recovered by returning the pre-existing row (`idempotent_replay=True`, `INFO`
  log). `credits_consumed` stays `0` (Q1).
- **Repository ports** (`IUsageRecordRepository` — `insert` (raises
  `DuplicateRequestIdError`) + `get_by_request_id`; `IModelPricingRepository` —
  read-only `get_effective(model_id, unit, at)`) and their SQLAlchemy impls
  (`app/infrastructure/repositories/usage_record_repository.py`,
  `model_pricing_repository.py`). The insert runs inside a **SAVEPOINT**
  (`begin_nested`) so an ADR-0033 unique-violation rolls back only the failed insert
  — the caller's transaction survives for the recovery SELECT. A `NULL` `request_id`
  never collides (the ADR-0033 index is partial: `WHERE request_id IS NOT NULL`).
  Pricing resolution is effective-at-time (`effective_from <= at < effective_to`,
  newest window wins).
- **DI wiring** — `IUnitOfWork` gains **`usage`** + **`model_pricing`**; the
  SQLAlchemy UoW instantiates both in `__aenter__`; `get_usage_recorder_service()`
  factory added to the container. The test UoW + fakes mirror it
  (`FakeUsageRecordRepository` — in-memory idempotent insert/replay;
  `FakeModelPricingRepository`).
- **Docs** — this CHANGELOG, the α7.5 pre-flight, the content-generation pipeline
  note, and an ADR-0041 change-log line recording that D13's usage half now has a
  concrete producer.
- **Tests** — unit (accounting per capability: LLM explicit-split / single-quantity
  fallback / image / video / voice / no-usage; pricing Σ + missing-price 0; service
  terminal success, `IN_PROGRESS` reject, failed-no-usage, missing-pricing WARN,
  idempotent replay, observational — no aggregate repo touched) and integration
  (partitioned insert + read-back, effective-at-time pricing resolution incl.
  unpriced→None, duplicate `request_id` → `DuplicateRequestIdError` with the original
  surviving, `NULL` `request_id` coexistence, and the full service pricing +
  idempotent-replay path on real rows).

#### Version
- App version bumped to **`0.4.19-phase3-alpha7.5-dev`** (staying on `0.4.x`; still
  Phase-3 orchestration infrastructure).

### Phase 3 Slice α7.4 — Provider Skeleton (capability ports · registry · dispatcher · mock providers) (2026-07-17)

Establishes the **provider abstraction layer** every real provider (α8.x) plugs
into — and **nothing else**: four async capability ports (LLM / Image / Video /
Voice), a framework-free **registry** with explicit registration + capability
discovery, a **`StepCommandDispatcher`** that turns α7.2's inert `StepCommand`s
into capability calls through a closed mapping table (ADR-0041 D4), a typed
provider-error hierarchy, immutable per-provider metadata, and **one deterministic
mock per capability**. It is **pure ports + an imperative shell over mocks** — the
slice is *almost entirely architecture*. **Explicitly forbidden and absent:** HTTP
clients (aiohttp/requests), API keys, external calls, Redis, Celery, retries,
provider fallback/weighting/priority/health-ordering, usage accounting, event
publishing, polling loops, webhook handlers. **Zero migration.** The α7.2 runner is
**untouched** (it still ignores `StepResult.commands`); wiring the dispatcher into
the runner is α7.6. See `docs/engineering/PHASE3_ALPHA7_4_PREFLIGHT.md` and
**ADR-0041**.

#### Added
- **Neutral provider contract** (`app/application/interfaces/providers.py`) — the
  `Capability` / `ProviderStatus` enums, the `ProviderUsage` (α7.5 seam) /
  `ProviderHealth` / **`ProviderMetadata`** DTOs, the shared `ProviderResponse`
  envelope + per-capability immutable request/response pairs
  (`GenerateText*` / `GenerateImage*` / `GenerateVideo*` / `GenerateSpeech*`), and
  the typed error hierarchy — `ProviderError` (base, `transient` classification) →
  `ProviderUnavailable` / `ProviderRateLimited` / `ProviderTimeout` (transient),
  `ProviderAuthenticationError` / `ProviderValidationError` (terminal), and
  `NoProviderAvailable` (registry exhaustion — plays ADR-0041's `NoHealthyProvider`
  role; fallback/health-ordering deferred). **No HTTP mapping.**
- **`ProviderDispatcherPort`** (`app/application/interfaces/provider_dispatcher.py`)
  — the runner-facing port (`dispatch(StepCommand) -> ProviderResponse` plus
  `supports` / `list_capabilities` discovery). Split from the DTO module so it can
  reference `StepCommand` without the provider leaf transitively importing the
  workflow domain.
- **Capability ports** (`app/infrastructure/ai/providers/ports.py`) — the
  `Provider` base + `LLMProvider` / `ImageProvider` / `VideoProvider` /
  `VoiceProvider` `Protocol`s (each `metadata` + async generate + `health`).
- **Deterministic mocks** (`app/infrastructure/ai/providers/mocks/`) — one per
  capability, byte-reproducible, no I/O. LLM/image/voice return `SUCCEEDED` inline;
  **video models the async path** (`IN_PROGRESS` + a deterministic `provider_job_id`)
  so the completion shape (α8.3) is exercised before any real async provider.
- **`ProviderRegistry`** (`app/infrastructure/ai/providers/registry.py`) — explicit
  `register(provider=…, capabilities=[…])` (no decorators), `resolve` →
  `NoProviderAvailable`, and capability discovery (`supports` / `has_provider` /
  `list_capabilities` / `list_providers`). `default_registry()` wires the four
  mocks; `PROVIDER_REGISTRY` is the process singleton.
- **`StepCommandDispatcher`** (`app/infrastructure/ai/dispatcher.py`) — the closed
  `kind` → capability table for the **four** provider capabilities only
  (`generate_text` / `generate_image` / `generate_video` / `synthesize_voice`);
  `start_render` and render/export/storage are excluded (`ProviderValidationError`).
  A missing `request_id` is a terminal `ProviderValidationError`. Discovery delegates
  to the registry. Lives **above** the leaf so the leaf stays orchestration-free.
- **`IProviderSettingsRepository`** (read-only) +
  **`ProviderSettingsRepository`** — minimal `get_value(provider, key, tenant_id)`
  with **tenant-shadows-global** precedence over `provider_settings` (the config
  read seam; no fallback/priority/weighting — Q4). Wired onto the UoW.
- **`import-linter` contract** — `app.infrastructure.ai.providers` is a **strict
  leaf**: forbidden from importing `app.application.use_cases`, `app.api`, or the
  workflow domain (it depends only on the neutral contract in
  `app.application.interfaces`, the same direction every repository uses).
- **DI wiring** — `get_provider_registry()` (the singleton) and
  `get_step_command_dispatcher()` factories; `IUnitOfWork` gains
  **`provider_settings`**; the test UoW + fakes mirror it
  (`FakeProviderSettingsRepository`).
- **Docs** — this CHANGELOG, the α7.4 pre-flight, ROADMAP, architecture notes, and
  an ADR-0041 change-log line recording the port-placement refinement.
- **Tests** — unit (neutral contract immutability + error taxonomy; each mock incl.
  the video async path + reproducibility; registry resolution/discovery/idempotence;
  dispatcher routing of all four kinds, excluded-kind + missing-`request_id` errors,
  `NoProviderAvailable` propagation, discovery delegation) and integration
  (`provider_settings` global read, tenant shadow, tenant→global fallback, per-key
  isolation).

#### Version
- App version bumped to **`0.4.18-phase3-alpha7.4-dev`** (staying on `0.4.x`; still
  Phase-3 orchestration infrastructure).

### Phase 3 Slice α7.3 — Outbox Relay + Distributed Lock Manager (pure infrastructure) (2026-07-17)

Adds the two pieces of **execution-substrate plumbing** every later slice depends
on, and nothing else: a **library-only outbox relay** that drains the
`event_outbox` (written transactionally by α7.1/α7.2) through a **`PublisherPort`**,
and a **distributed lock manager** over the baseline `distributed_locks` table
(**no migration** — the table, its `lock_key` PK, and the `lease_until > acquired_at`
CHECK all already exist). **No worker, no daemon, no CLI, no HTTP, no broker, no
Celery, no Redis** — the relay and the janitor are plain `async` methods a caller
invokes; the worker loop that calls them on a timer is α8.1. **No `event_log`
projection** (the default publisher is a synchronous in-process sink that fans out
to registered handlers; the immutable/partitioned `event_log` becomes an explicit
projection only when a consumer needs it). Poison events are **parked in-place**
(`attempts += 1`, `last_error`, `published_at` stays `NULL`) and the fetch query
ignores rows at/over `max_attempts` — **no DLQ table, no retry scheduler**.
Distributed locks are **owner-fenced** (`renew`/`release` require the owning
lease), `acquire` **steals expired leases and never active ones**, and correctness
comes from **steal-after-expiry**, not the explicit `reclaim_expired()`
maintenance sweep (ADR-0032). See `docs/engineering/PHASE3_ALPHA7_3_PREFLIGHT.md`
and **ADR-0032** (locks) / **ADR-0041** (the provider-runtime blueprint that will
consume both).

#### Added
- **`PublisherPort`** (`app/application/interfaces/publisher.py`) — the publish
  abstraction plus the immutable **`OutboxEvent`** DTO (id, aggregate, event type,
  version, payload, metadata, `occurred_at`, `attempts`) and the async
  **`EventHandler`** protocol. Default impl **`InProcessPublisher`**
  (`app/infrastructure/publisher/in_process_publisher.py`) — a synchronous
  in-process sink that awaits each registered handler in order; any handler raising
  fails the publish (the relay then parks the row).
- **`RelayService`** (`app/application/use_cases/relay/relay_service.py`) —
  `relay_once(batch_size=None) -> RelayResult` and `reclaim_expired(now=None) -> int`.
  One transaction per pass: `fetch_unpublished` → publish each → `mark_published`
  on success / `mark_failed` (park) on error → commit. Returns a **`RelayResult`**
  (`fetched`, `published`, `failed`, `parked`) for trivial testing/logging/metrics.
  Defaults `batch_size=100`, `max_attempts=10`. Every parked event emits an
  **`ERROR`** structured log (`outbox.publish_failed`) carrying event id, aggregate
  id/type, event type, `attempts`, `max_attempts`, `parked`, exception type +
  message; each pass emits an `INFO` `outbox.relay_pass` summary.
- **`IEventOutboxRepository`** extended with the relay read/mark surface —
  `fetch_unpublished(limit, max_attempts)` (`published_at IS NULL AND attempts <
  max_attempts`, ordered `occurred_at, id`, `FOR UPDATE SKIP LOCKED`),
  `mark_published(event_id, published_at)`, `mark_failed(event_id, error)`
  (`attempts += 1`, `last_error`). Implemented in `EventOutboxRepository` (the α7.1
  `add` producer path is unchanged).
- **`IDistributedLockManager`** + **`Lease`** VO
  (`app/application/interfaces/locks.py`) and **`SqlAlchemyDistributedLockManager`**
  (`app/infrastructure/repositories/distributed_lock_manager.py`) — `acquire`
  (atomic `INSERT … ON CONFLICT DO UPDATE … WHERE lease_until < now()` — free or
  expired only), owner-fenced `renew`/`release`, and `reclaim_expired(now=None)`
  (`DELETE … WHERE lease_until < now()`, returns the count). All clock arithmetic
  uses the DB `now()` so leases are wall-clock-agnostic.
- **DI wiring** — `IUnitOfWork` gains **`locks`** (and the extended `outbox`
  surface); `SqlAlchemyUnitOfWork` exposes `SqlAlchemyDistributedLockManager`; the
  container adds an `InProcessPublisher` singleton + a `RelayService` factory. The
  test UoW and fakes mirror both (`FakeDistributedLockManager`, the relay methods
  on `FakeEventOutboxRepository`).
- **Docs** — this CHANGELOG, the α7.3 pre-flight, ROADMAP, and architecture notes.
- **Tests** — unit (`InProcessPublisher` order/propagation; `RelayService` happy
  path, empty batch, transient failure, park-at-cap + log assertion, parked-row
  exclusion, `batch_size` override, chronological order) and integration
  (lock acquire/steal-expired/never-steal-active, owner-fenced renew/release,
  `reclaim_expired`, the `lease_until > acquired_at` CHECK; relay
  `fetch_unpublished`/`mark_published`/`mark_failed`, ordering, `max_attempts`
  exclusion, and `FOR UPDATE SKIP LOCKED` disjoint claims across two connections).

#### Version
- App version bumped to **`0.4.17-phase3-alpha7.3-dev`** (staying on `0.4.x`; still
  Phase-3 orchestration infrastructure — `0.5.0` reserved for a product-level
  milestone).

### Phase 3 Slice α7.2 — WorkflowRun Aggregate + Deterministic Runner (the first sequencing orchestration slice) (2026-07-16)

Introduces the **WorkflowRun aggregate** and a **synchronous, deterministic
runner** — the record of one workflow execution and the orchestration graph
beneath it. It is the project's **second** orchestration aggregate (after α7.1's
`RenderJob`) and the first that **sequences** work: it owns an ordered graph of
`WorkflowStep` children and **append-only** `WorkflowCheckpoint` children. Backed
by the existing baseline `workflow_runs` / `workflow_steps` / `workflow_checkpoints`
tables (**no migration** — the tables, the `workflow_status` / `step_status` ENUMs,
the per-project `idempotency_key` unique, the per-run `step_index` unique, and the
append-only checkpoint trigger all already exist). WorkflowRun uses **status-guarded
CAS** — `workflow_runs` / `workflow_steps` carry **no `version` column** (not in
`_VERSION_BUMP_TABLES`), a **deliberate divergence** from `RenderJob`'s self-versioned
OCC forced by the baseline schema (ADR-0040 D2). It owns **only orchestration/graph
state** and never mutates `projects.version` / `RenderJob` / `MediaAsset` / `Timeline`
(pre-flight D3.10); it coordinates **only** through domain events on the `event_outbox`
(D9). Step handlers are **pure, deterministic, side-effect-free** — a step returns a
command/result *describing* what should happen, and the runner (the imperative shell)
interprets it (D3.11), keeping the eventual move to an async worker an execution
concern, not a domain rewrite. **No worker, no providers, no scheduler in this
slice** — pause/resume (`paused`), `StepCommand` dispatch, render-producing steps
(`render_jobs.workflow_run_id`), backoff, and the `workflow_run:{id}` lock are α8.x.
See `docs/engineering/PHASE3_ALPHA7_2_PREFLIGHT.md`,
`docs/domain/WORKFLOW_RUN_AGGREGATE.md`, and **ADR-0040**.

#### Added
- **`POST /api/v1/projects/{project_id}/workflow-runs`** — queue a run. Body
  `{ workflow_key, workflow_version, input_snapshot?, idempotency_key? }`
  (`extra="forbid"`; `input_snapshot` defaults `{}`). `workflow_key@workflow_version`
  is resolved against the **in-code registry before any DB work** — an unknown pair →
  **`422`** (the project IS visible, so not `404`). Seeds ordered `pending` steps from
  the definition. Returns `201` + `WorkflowRunPublic` (`status='queued'`) and emits
  **`WorkflowRunCreated`** to the `event_outbox`. **Idempotent (Q7):** a repeat with
  the same `idempotency_key` for the project returns the **existing** run with **`200`**
  (no duplicate, no second event). Missing/foreign project → `404`; unauthenticated →
  `401`.
- **`GET  …/workflow-runs`** — the project's runs **newest-first** (`created_at` DESC,
  `id` DESC tiebreak) as summaries; optional **`?status=`** filters by one
  `workflow_status` (bad enum → `422`). Missing/foreign project → `404`.
- **`GET  …/workflow-runs/{workflow_run_id}`** — one run with its ordered `steps` and
  `latest_checkpoint`. Two-level gate (project → run); unknown, or under another
  owner's project → `404` (anti-enumeration).
- **`POST …/workflow-runs/{workflow_run_id}/advance`** — **no body**. Runs the
  deterministic runner to a terminal state (resume-safe: already-`succeeded`/`skipped`
  steps skipped, threading their checkpoint forward). `404` (project/run not visible);
  **`409`** if already terminal; otherwise **`200`** + `WorkflowRunPublic` (`succeeded`
  or `failed`). Emits **`WorkflowRunStarted`** (first `queued → running`), one
  **`WorkflowStepCompleted`** per step, and a terminal **`WorkflowRunSucceeded`** /
  **`WorkflowRunFailed`**.
- **`POST …/workflow-runs/{workflow_run_id}/cancel`** — **no body**. Status-guarded CAS
  (`status IN ('queued','running','paused')` in the WHERE), decided **404 → classify**:
  already `canceled` → **`200`** no-op (no event); `succeeded`/`failed` → **`409`**;
  cancelable → **`200`** + `WorkflowRunPublic` (`status='canceled'`) and emits
  **`WorkflowRunCanceled`**. **No `?version=`, no `412`, no `DELETE`** (no OCC token;
  runs are audit records — no `deleted_at`).
- **Domain** `app/domain/workflow/` — frozen `WorkflowRun` / `WorkflowStep` /
  `WorkflowCheckpoint`; the `WorkflowRunStatus` (`queued, running, paused, succeeded,
  failed, canceled`; `is_terminal` / `is_cancelable` / `is_advanceable`) and
  `WorkflowStepStatus` (`pending, running, succeeded, failed, skipped, retrying`;
  `is_terminal` / `is_done` / `is_runnable`) `StrEnum`s; and the **in-code registry**
  (`registry.py`) — the pure `StepHandler` protocol, `StepContext` / `StepResult` /
  `StepCommand` / `StepOutcome` contracts (D3.11), and four provider-free workflows
  (`noop-chain`, `retry-succeed`, `terminal-fail`, `retry-exhaust`).
- **`IWorkflowRunRepository`** + `WorkflowRunRepository` — `add` (idempotency
  pre-check + unique-violation backstop), `seed_steps`, `get_by_project_and_key`,
  `list_by_project` (status filter), `get_owned`, `list_steps`, `latest_checkpoint`,
  the status-guarded run transitions (`mark_run_running` / `mark_run_succeeded` /
  `mark_run_failed` / `cancel`), the step transitions (`mark_step_running` /
  `mark_step_succeeded` / `mark_step_retrying` / `mark_step_failed`, with a DB-side
  `retries` increment), and append-only `append_checkpoint`. Wired into `IUnitOfWork` /
  `SqlAlchemyUnitOfWork` and mirrored on the test UoW + fakes
  (`FakeWorkflowRunRepository`).
- **Use cases** `app/application/use_cases/workflow/` — `CreateWorkflowRun`,
  `ListWorkflowRuns`, `GetWorkflowRun`, `CancelWorkflowRun`, and the runner
  `AdvanceWorkflowRun`; `_view.py` (the shared `WorkflowRunView` read-model) and
  `_events.py` (emits the six `WorkflowRun*` events — orchestration-only payloads,
  `event_version="1.0"`). None call `IProjectRepository.touch_version`.
- **DTOs** `app/api/v1/schemas/workflow.py` — `WorkflowRunCreateRequest`
  (`extra="forbid"`), `WorkflowStepPublic`, `WorkflowCheckpointPublic`,
  `WorkflowRunSummary` (list), `WorkflowRunPublic` (detail; **no `version` field**).
- **Router** `app/api/v1/routers/workflow_runs.py` (registered in `main.py`); DI
  factories in `core/container.py` (wired with the `WORKFLOW_REGISTRY`) and `deps.py`
  aliases.
- **Docs** — `docs/domain/WORKFLOW_RUN_AGGREGATE.md`, **ADR-0040**, this CHANGELOG, the
  α7.2 pre-flight, `API_CONTRACT.md` §2 (Resource Map) + §3.2.6, and ROADMAP.
- **Tests** — unit (`create`/`list`/`get`/`cancel`/`advance`: happy paths, field +
  input-snapshot persistence, idempotent replay + distinct keys, status filter +
  cross-project isolation, cancel terminal/re-cancel, the runner's `noop-chain`
  success / `retry-succeed` retry accounting / `terminal-fail` / `retry-exhaust` /
  already-terminal `409` / resume of a partial run, event shapes) and integration
  (API happy/`401`/`404`/`422`/`409` + cross-owner isolation across all five verbs;
  repository `add`/dupe/`get_by_project_and_key`/`seed_steps`/`list_by_project`/
  `get_owned`/run + step CAS chains/retry accounting/append + latest checkpoint).

#### Version
- App version bumped to **`0.4.16-phase3-alpha7.2-dev`** (staying on `0.4.x`; still
  Phase-3 orchestration infrastructure — `0.5.0` reserved for a product-level
  milestone, per pre-flight Q9).

### Phase 3 Slice α7.1 — RenderJob Aggregate (the first orchestration slice) (2026-07-16)

Introduces the **RenderJob aggregate** — the request to render a project's
timeline and the record of that request's lifecycle. It is the project's first
**orchestration** aggregate (contrast the α5–α6 domain-model aggregates: Project,
Scene, Prompt, Media, Timeline). Backed by the existing baseline `render_jobs`
table (**no migration** — the table, its `version`/OCC trigger, `render_status`
ENUM, `queue`/`priority`/`progress` columns, and the per-project
`idempotency_key` unique all already exist). RenderJob is **self-versioned**
(`render_jobs.version` is its **own** OCC token — like `projects` / `media_assets`,
**not** the timeline's borrowed token; ADR-0039, adopts ADR-0037) and owns **only
orchestration metadata** — it does **not** own rendered/exported files, workflow
state, or timeline edits (pre-flight D3.10). It coordinates **only** through
domain events on the `event_outbox` (D9). **No render worker in this slice** —
`queued → running → {succeeded, failed}`, the distributed lock (`render_job:{id}`,
ADR-0032), and the worker-owned fields (`output_media_asset_id`, `started_at`,
`finished_at`, `error`, `progress` beyond `'0.00'`) are α8.x. Release/Draft
binding is the **worker's** decision (α7.1 persists no `mode`/`project_version_id`
— pre-flight Q1). See `docs/engineering/PHASE3_ALPHA7_1_PREFLIGHT.md`,
`docs/domain/RENDER_JOB_AGGREGATE.md`, and **ADR-0039**.

#### Added
- **`POST /api/v1/projects/{project_id}/render-jobs`** — enqueue a render. Body
  `{ pipeline?, pipeline_version?, queue?, priority?, idempotency_key? }`; defaults
  `pipeline='ffmpeg'`, `pipeline_version='0.0.0'` (Q2), `queue='normal'`,
  `priority=0` (clamped `0–1000`). The **timeline is resolved server-side** (1:1
  with the project) — a project with **no timeline → `422`** (visible but not
  fulfillable). Returns `201` + `RenderJobPublic` (`version=1`, `status='queued'`,
  `progress='0.00'`) and emits **`RenderJobCreated`** to the `event_outbox`.
  **Idempotent (Q4):** a repeat with the same `idempotency_key` for the project
  returns the **existing** job with **`200`** (no duplicate, no second event).
  Missing/foreign project → `404`; unauthenticated → `401`.
- **`GET  …/render-jobs`** — the project's jobs **newest-first** (`created_at`
  DESC, `id` DESC tiebreak); optional **`?status=`** filters by one `render_status`
  (bad enum → `422`). Missing/foreign project → `404`.
- **`GET  …/render-jobs/{render_job_id}`** — one job. Two-level gate (project →
  render-job); unknown, or under another owner's project → `404` (anti-enumeration).
- **`POST …/render-jobs/{render_job_id}/cancel`** — required `{ version }` (the
  job's own token). Version-fenced CAS with a **race-safe terminal guard**
  (`status IN ('queued','running')` in the WHERE), decided **404 → classify →
  412**: already `canceled` → **`200`** no-op (no event); `succeeded`/`failed` →
  **`409`**; cancelable but stale → **`412`**; success → **`200`** +
  `RenderJobPublic` (`status='canceled'`, `version` +1) and emits
  **`RenderJobCanceled`**. **No `DELETE` verb** (jobs are audit records — no
  `deleted_at`).
- **Domain** `app/domain/render/` — frozen `RenderJob` and the `RenderStatus`
  `StrEnum` (`queued, running, succeeded, failed, canceled`; `is_terminal` /
  `is_cancelable`).
- **`IRenderJobRepository`** + `RenderJobRepository` — `add` (idempotency
  pre-check + unique-violation backstop), `get_by_project_and_key`,
  `list_by_project` (status filter), `get_owned`, `cancel` (version-fenced CAS).
  **`IEventOutboxRepository`** + `EventOutboxRepository` — `add` (append to the
  outbox in the same UoW txn). Both wired into `IUnitOfWork` /
  `SqlAlchemyUnitOfWork` and mirrored on the test UoW + fakes
  (`FakeRenderJobRepository`, `FakeEventOutboxRepository`).
- **Use cases** `app/application/use_cases/render/` — `CreateRenderJob`,
  `ListRenderJobs`, `GetRenderJob`, `CancelRenderJob`; `_events.py` emits
  `RenderJobCreated` / `RenderJobCanceled` (orchestration-only payloads,
  `event_version="1.0"`). None call `IProjectRepository.touch_version`.
- **DTOs** `app/api/v1/schemas/render.py` — `RenderJobCreateRequest`,
  `RenderJobCancelRequest` (`extra="forbid"`, `priority` clamp), `RenderJobPublic`.
- **Router** `app/api/v1/routers/render_jobs.py` (registered in `main.py`); DI
  factories in `core/container.py` and `deps.py` aliases.
- **Docs** — `docs/domain/RENDER_JOB_AGGREGATE.md`, **ADR-0039**, this CHANGELOG,
  the α7.1 pre-flight, `API_CONTRACT.md` §2 (Resource Map) + §3.2.5, and ROADMAP.
- **Tests** — unit (`create`/`list`/`get`/`cancel`: happy paths, field
  persistence, idempotent replay + distinct keys, status filter + isolation,
  cancel OCC/terminal/re-cancel/stale, event shapes) and integration
  (API happy/`401`/`404`/`422`/`409`/`412` + cross-owner isolation; repository
  `add`/dupe/`get_by_project_and_key`/`list_by_project`/`get_owned`/`cancel` +
  outbox persistence).

#### Version
- App version bumped to **`0.4.15-phase3-alpha7.1-dev`** (staying on `0.4.x`;
  `0.5.0` reserved for a product-level milestone — end-to-end render/export — per
  pre-flight Q7).

### Phase 3 Slice α6.3b — Timeline Aggregate (clips) (2026-07-14)

Completes the **Timeline aggregate** (Timeline → Tracks → **Clips**) by placing
registered media (α6.2) onto tracks (α6.3a) as time-bounded **clips**, backed by
the existing baseline `clips` table (no migration — the table, its FKs, and the
`start_seconds` / `end_seconds` / `volume` CHECKs already exist). Clips are pure
**children of the Timeline aggregate** (α6.3 pre-flight Q13, **ADR-0038**): they
carry **no `version`** column, so **`timelines.version` remains the single OCC
token** for the whole tree. A clip write fences on / bumps `timelines.version`
and never touches `projects.version` (adopts ADR-0035). `track_id` is
**immutable** in this slice (α6.3b pre-flight Q4 — a cross-track move is a
delete + recreate); `effects` is **read-only** (write path deferred to α6.4,
Q1); clip **overlaps are allowed** (α6.3 Q6); timeline `duration_seconds` stays
**client-controlled** (no auto-growth from clips, Q5). See
`docs/engineering/PHASE3_ALPHA6_3B_PREFLIGHT.md`.

#### Added
- **`POST /api/v1/projects/{project_id}/timeline/tracks/{track_id}/clips`** —
  append a clip. Body `{ media_asset_id?, start_seconds, end_seconds,
  source_start_seconds?, source_end_seconds?, volume?, locked?, version? }`;
  `end_seconds > start_seconds` and `source_end_seconds ≥ source_start_seconds`
  (else `422`); `volume` `0–4`. `media_asset_id`, when set, must reference a
  **live media asset you own** (else `422`, Q… link validation). `version` is
  **optional** (a child create cannot be harmfully stale): omitted → bumps
  `timelines.version` unconditionally; supplied → fence (stale → `412`). Returns
  `201` + `ClipPublic` (**no `version`**) with the token in `meta.timeline_version`.
  Unknown project / timeline / track → `404`.
- **`GET  …/tracks/{track_id}/clips`** — the track's live clips ordered by
  `start_seconds` ASC (`id` ASC tiebreak); token in `meta.timeline_version`.
- **`GET  …/tracks/{track_id}/clips/{clip_id}`** — one live clip. Four-level gate
  (project → timeline → track → clip); any miss → `404`; cross-track → `404`.
- **`PATCH …/tracks/{track_id}/clips/{clip_id}`** — required `version` (the
  **timeline's**); body any subset of `{ media_asset_id, start_seconds,
  end_seconds, source_start_seconds, source_end_seconds, volume, locked }`.
  `media_asset_id` re-validated when present (explicit `null` unlinks); the
  **merged** time range is validated against stored values (else `422`) so an
  invalid state never reaches the DB CHECK. Bumps the token; `412` on stale;
  `200` no-op on same-value; empty patch → `422`. 404-before-412.
- **`DELETE …/tracks/{track_id}/clips/{clip_id}?version=<n>`** — required
  `?version=`; soft-deletes, bumps the token; `204`. **Idempotent-by-404**
  (repeat delete → `404`, not `412`, Q3).
- **Domain** `app/domain/timeline/clip.py` (frozen `Clip`, **no** `version`;
  `effects: list[Any]` read-only).
- **`ITimelineRepository`** + `TimelineRepository` — `add_clip`, `list_clips`,
  `list_clips_for_timeline` (grouped by `track_id` for composition reads),
  `get_clip`, `update_clip`, `soft_delete_clip`. Mirrored on `FakeTimelineRepository`.
- **Use cases** `app/application/use_cases/timeline/` — `CreateClip`, `ListClips`,
  `GetClip`, `UpdateClip`, `DeleteClip` (+ `ClipResult` / `ClipListResult`, and
  `TimelineResult.clips_by_track`). `_links.validate_clip_media_link` re-uses the
  media aggregate's `get_owned`. None call `IProjectRepository.touch_version`.
- **DTOs** `app/api/v1/schemas/timeline.py` — `ClipCreateRequest`,
  `ClipUpdateRequest`, `ClipPublic` (no `version`; `extra="forbid"`; cross-field
  range checks); `TrackPublic.clips[]` now embeds `ClipPublic` (ordered) in
  composition reads (`GET …/timeline`, `GET …/tracks`); container factories +
  `deps` aliases + 5 nested routes on `routers/timeline.py`.
- **Tests** — unit matrix for the 5 clip use cases (create with optional-fence /
  valid+unknown media / stale→412 / 404s; list ordering + isolation; get 4-level
  gate + cross-track→404; update incl. relink / unlink / merged-range→422 /
  stale→412; delete idempotent-by-404 + 404-before-412); repository integration
  (R11–R15: ordered listing excl. soft-deleted, `id` tiebreak, track isolation,
  real-change update, idempotent soft delete, grouped `list_clips_for_timeline`);
  HTTP integration `test_timeline.py` (A17–A26: 201/200/204/404/412/422,
  media validation, stale fence, composition-tree embedding).

#### Documentation
- `docs/engineering/PHASE3_ALPHA6_3B_PREFLIGHT.md` (new) — the five resolved
  open questions and the approved slice scope.
- `docs/domain/TIMELINE_AGGREGATE.md` — clips documented as the third tier of the
  aggregate (children, no `version`, `media_asset_id` validation, immutable
  `track_id`, read-only `effects`).
- `API_CONTRACT.md` §3.2.4 — the five clip endpoints + `TrackPublic.clips[]`.

#### Version
- `0.4.14-phase3-alpha6.3b-dev`.

### Phase 3 Slice α6.3a — Timeline Aggregate (root + tracks) (2026-07-13)

Introduces the **Timeline aggregate** — the *composition layer* that places
registered media (α6.2) onto ordered **tracks** (α6.3b adds clips) — backed by the
existing baseline `timelines` / `tracks` tables (no migration — tables, the
`uq_timelines_project_id` / `uq_tracks_timeline_id_z_index` partial uniques, and
the `frame_rate` CHECK already exist). The Timeline is a **self-contained
optimistic-concurrency aggregate** (α6.3 pre-flight Q1, **ADR-0038**) — a **third**
posture, distinct from projects+scenes (aggregate OCC *in* the version ledger) and
prompts+media (last-writer-wins, no OCC): the root carries `version` (baseline
`VersionMixin` + guarded bump trigger), its children do **not**, so
**`timelines.version` is the single OCC token for the whole tree** (root + tracks +
clips, Q13). A timeline edit is a composition change — it fences on / bumps
`timelines.version` but does **NOT** bump `projects.version` and is **excluded**
from `project_versions` snapshots / restore / diff (**adopts ADR-0035**).
Endpoints are **project-nested** (Q4); the timeline is created **explicitly** (Q3,
one per project — second → `409`); `z_index` is **client-assigned** and unique per
live timeline (Q5, collision → `409`). See `docs/domain/TIMELINE_AGGREGATE.md`,
**ADR-0038**, and `docs/engineering/PHASE3_ALPHA6_3_PREFLIGHT.md`.

#### Added
- **`POST /api/v1/projects/{project_id}/timeline`** (`CurrentUserDep`) — provision
  the single timeline (explicit, non-lazy). Body `{ aspect_ratio?, frame_rate?,
  background_color? }`; `aspect_ratio` defaults from the project orientation
  (`horizontal→16:9`, `vertical→9:16`, `square→1:1`) when omitted; `frame_rate`
  `1–240`; `background_color` hex. Returns `201` + `TimelinePublic` (`version = 1`,
  `tracks = []`). Second provision → `409 CONFLICT` (`uq_timelines_project_id`
  backstop). Missing/foreign project → `404`.
- **`GET /api/v1/projects/{project_id}/timeline`** — the timeline root + its live
  tracks ordered by `z_index` ASC. Un-provisioned timeline → `404`.
- **`PATCH /api/v1/projects/{project_id}/timeline`** — version-fenced root update.
  Body `{ version, aspect_ratio?, frame_rate?, background_color?,
  duration_seconds? }`; net **+1** on a real change; `412` on stale; `200` no-op on
  same-value; empty patch → `422`. No `projects.version` bump.
- **`POST /api/v1/projects/{project_id}/timeline/tracks`** — append a track. Body
  `{ kind, z_index, name, locked?, muted?, version? }`; `kind` a `track_kind` enum
  (`video/audio/subtitle/effect`); `z_index ≥ 0`, unique per live timeline
  (collision → `409`). `version` is **optional** (a child create cannot be
  harmfully stale — Q13): omitted → bumps `timelines.version` unconditionally;
  supplied → fence (stale → `412`). Returns `201` + `TrackPublic` (**no
  `version`**) with the token in `meta.timeline_version`.
- **`GET /api/v1/projects/{project_id}/timeline/tracks`** — the live tracks
  (`z_index` ASC); token in `meta.timeline_version`.
- **`PATCH /api/v1/projects/{project_id}/timeline/tracks/{track_id}`** — required
  `version` (the **timeline's**); body any subset of `{ kind, z_index, name,
  locked, muted }`; z_index collision → `409`; bumps the token; `412` on stale;
  `200` no-op on same-value; empty patch → `422`. 404-before-412.
- **`DELETE /api/v1/projects/{project_id}/timeline/tracks/{track_id}?version=<n>`**
  — required `?version=`; soft-deletes (frees the `z_index`), bumps the token;
  `204`. **Idempotent-by-404** (repeat delete → `404`, not `412`).
- **Domain** `app/domain/timeline/timeline.py` (frozen `Timeline`, **with**
  `version`), `app/domain/timeline/track.py` (frozen `Track`, **no** `version`).
- **`ITimelineRepository`** + `TimelineRepository` (`add` [unique→`ConflictError`],
  `get_by_project`, `update_owned` [version-fenced CAS, net +1], `bump_version`
  [fenced vs unconditional aggregate roll-up], `add_track` / `list_tracks` /
  `get_track` / `update_track` [z_index→`ConflictError`] / `soft_delete_track`).
  Wired onto the real `UnitOfWork`, the integration `_TestUnitOfWork`, and
  `FakeUnitOfWork` (+ `FakeTimelineRepository`).
- **Use cases** `app/application/use_cases/timeline/` — `ProvisionTimeline`,
  `GetTimeline`, `UpdateTimeline`, `CreateTrack`, `ListTracks`, `UpdateTrack`,
  `DeleteTrack` (+ `TimelineResult` / `TrackResult`). None call
  `IProjectRepository.touch_version`.
- **DTOs** `app/api/v1/schemas/timeline.py` — `TimelineProvisionRequest`,
  `TimelineUpdateRequest`, `TrackCreateRequest`, `TrackUpdateRequest`,
  `TimelinePublic`, `TrackPublic` (no `version`; `extra="forbid"`; empty PATCH →
  `422`); container factories + `deps` aliases + `routers/timeline.py`, mounted in
  `app/main.py`.
- **Tests** — unit matrix for the 7 use cases (provision happy / aspect default /
  second→409 / 404; fenced root PATCH incl. same-value no-op / stale→412; track
  create with optional-fence / z_index→409 / stale→412; update fenced incl.
  404-before-412; delete incl. idempotent-by-404 + z_index slot freeing);
  repository integration (`add`→409, fenced CAS net +1, `bump_version` fenced vs
  unconditional, z_index uniqueness, ordered listing, soft-delete slot reuse); HTTP
  integration `test_timeline.py` (A1–A16 end-to-end: 201/200/204/404/409/412/422/401,
  cross-owner isolation, `meta.timeline_version`, `projects.version` untouched).

#### Documentation
- `API_CONTRACT.md` §2 resource map + new §3.2.4 — timeline documented as shipped
  (project-nested, self-contained OCC via `timelines.version`, `meta.timeline_version`).
- `docs/domain/TIMELINE_AGGREGATE.md` (new) + **ADR-0038** (new, adopts ADR-0035) —
  the composition-layer identity, self-contained OCC aggregate model, explicit
  provision, client-assigned `z_index`, and the exclusion from the project version
  ledger.
- `docs/domain/PROJECT_AGGREGATE.md` §6/§8 — timeline noted as **outside** the
  versioned snapshot boundary; §8 map updated with the α6.3a Timeline composition.

#### Version
- `0.4.13-phase3-alpha6.3a-dev`.

### Phase 3 Slice α6.2 — Media Asset Aggregate (generation-output CRUD) (2026-07-13)

Introduces the **Media Asset aggregate** — the first *generation-output* content
— as a **register-by-metadata** CRUD surface backed by the existing baseline
`media_assets` table (no migration — table + indexes + the `(storage_backend,
storage_bucket, storage_key)` unique constraint already exist). Unlike prompts /
scenes, `media_assets` carries its **own `tenant_id` + `owner_user_id`** (direct
ownership) and only a **nullable `project_id`**, so the endpoints are **top-level
and owner-scoped** (α6.2 pre-flight Q1), not project-nested. α6.2 **registers**
an object the client already holds (`source ∈ {uploaded, stock}`) — it makes
**no** provider call, byte upload, presigned URL, or checksum fetch; AI
generation (`source = generated`) and object storage are later slices (Q2). The
concurrency posture **adopts ADR-0036** (Q3, **ADR-0037**): no `version` column,
no per-row OCC, a `PATCH` is **last-writer-wins** (no `412`), mutations do **not**
bump `projects.version`, and media is **excluded** from `project_versions`
snapshots / restore / diff. Duplicate storage coordinates → `409`; foreign /
unknown optional links → `422`. See `docs/domain/MEDIA_AGGREGATE.md`,
**ADR-0037**, and `docs/engineering/PHASE3_ALPHA6_2_PREFLIGHT.md`.

#### Added
- **`POST /api/v1/media`** (`CurrentUserDep`) — register a media asset
  (metadata only). Body `{ kind, source, storage_backend, storage_bucket,
  storage_key, mime_type, size_bytes, checksum_sha256, project_id?, scene_id?,
  prompt_id?, model_id?, provider?, width?, height?, duration_seconds?,
  source_metadata? }`. `kind` / `storage_backend` validated enums; `source`
  restricted to `uploaded` / `stock` (`generated` → `422`); `checksum_sha256` a
  64-char hex string (→ 32 bytes); `size_bytes ≥ 0`. Each present optional link
  validated for the caller (foreign/unknown `project_id`, `scene_id`/`prompt_id`
  without or outside that project, unknown/retired `model_id` → `422
  VALIDATION_FAILED`, not `404`). Duplicate `(storage_backend, storage_bucket,
  storage_key)` → `409 CONFLICT` (unique-constraint backstop behind a
  pre-check). Identity + ownership server-owned (`extra="forbid"` → `422`).
  Returns `201` + `MediaPublic`.
- **`GET /api/v1/media`** — list the caller's live assets newest-first
  (`created_at` desc, `id` desc) with optional `?kind=<enum>`, `?source=<str>`,
  `?project_id=<uuid>`, `?scene_id=<uuid>` filters (combined = AND; bad enum /
  non-UUID → `422`). Owner-scoped; not paginated. Empty → `200 []`.
- **`GET /api/v1/media/{media_id}`** — one asset (`200`) or the uniform owner
  `404` (unknown / other owner / soft-deleted).
- **`PATCH /api/v1/media/{media_id}`** — narrow, partial, **no version fence**.
  Body = any subset of `{ project_id, scene_id, prompt_id, model_id, provider,
  source_metadata }`; tri-state via `exclude_unset` (explicit `null` clears a
  nullable link, re-validated when non-null → `422`; `source_metadata`
  non-nullable). Physical-object fields immutable (`extra="forbid"` → `422`);
  empty patch → `422`; same-value patch is a `200` no-op. No `projects.version`
  bump.
- **`DELETE /api/v1/media/{media_id}`** — owner-scoped soft delete (`204`), no
  version fence, idempotent-by-404.
- **Domain** `app/domain/media/media_asset.py` — frozen `MediaAsset` entity
  (slim view of the physical row; **no `version` field** by design;
  `checksum_sha256` as `bytes`).
- **`IMediaRepository`** + `MediaRepository` (`add` [unique→`ConflictError`],
  `list_owned` + `kind`/`source`/`project_id`/`scene_id` filters, `get_owned`,
  `update_owned` [no OCC fence], `soft_delete_owned`, `model_is_linkable`) — all
  owner-scoped (tenant + owner_user) + soft-delete excluded. Wired onto the real
  `UnitOfWork`, the integration `_TestUnitOfWork`, and `FakeUnitOfWork`
  (+ `FakeMediaRepository`).
- **Use cases** `app/application/use_cases/media/` — `RegisterMedia`,
  `ListMedia`, `GetMedia`, `UpdateMedia`, `DeleteMedia`, plus a shared
  `_links.validate_media_links` helper (project/scene/prompt/model consistency →
  `422`). Structured logs never carry `storage_key` / `checksum` /
  `source_metadata` values.
- **DTOs** `app/api/v1/schemas/media.py` — `MediaRegisterRequest`,
  `MediaUpdateRequest` (tri-state, `extra="forbid"`, empty-patch → `422`),
  `MediaPublic` (no `version`; `owner_user_id`/`tenant_id`/`deleted_at` omitted;
  `checksum_sha256` emitted as hex); container factories + `deps` aliases +
  `routers/media.py`, mounted in `app/main.py`.
- **Tests** — unit matrix for the 5 use cases (happy / each-link-foreign→422 /
  scene-without-project→422 / unknown-model→422 / duplicate→409 / same-value
  no-op / explicit-null clears / idempotent-by-404); repository integration incl.
  the load-bearing **F5** test (a media asset's `project_id`/`scene_id`/
  `prompt_id` links **survive** a parent *soft-delete* — `ON DELETE SET NULL`
  fires only on a hard delete) + the unique-conflict path; HTTP integration
  `test_media.py` (A1–A15 end-to-end: 201/200/204/404/422/409/401, owner
  isolation, filters, tri-state PATCH, immutable-field rejection,
  idempotent-by-404).

#### Documentation
- `API_CONTRACT.md` §2 resource map + new §3.2.3 — media documented as shipped
  (top-level, owner-scoped, register-by-metadata, no `version`).
- `docs/domain/MEDIA_AGGREGATE.md` (new) + **ADR-0037** (new, adopts ADR-0036) —
  the generation-output identity, direct owner-level ownership, register-by-
  metadata boundary, storage-identity uniqueness, and the no-OCC / no-snapshot
  rationale.
- `docs/domain/PROJECT_AGGREGATE.md` §6/§8 — media noted as **outside** the
  versioned snapshot boundary; §8 map updated with the α6.2 Media output.

#### Version
- `0.4.12-phase3-alpha6.2-dev`.

### Phase 3 Slice α6.1 — Prompt Aggregate (generation-input CRUD) (2026-07-12)

Introduces the **Prompt aggregate** — the first *generation-input* content — as
an owner-scoped CRUD surface nested under a project
(`/projects/{id}/prompts`), backed by the existing baseline `prompts` table
(no migration — table + all three indexes already exist). A prompt is authored
text (`kind` + `text_content`) with an **optional** live-scene link and an
**optional** validated `ai_models` link. The load-bearing decision (α6.1
pre-flight Q1/Q8, **ADR-0036**): the baseline gave `prompts` **no `version`
column** on purpose — prompts are **generation inputs, not versioned editorial
content**. So they take **no per-row OCC**, a `PATCH` is **last-writer-wins**
(no `version` on the wire, no `412`), mutations do **not** bump
`projects.version`, and prompts are **excluded** from `project_versions`
snapshots / restore / diff. The versioned aggregate stays {project root +
scenes}; generated media (α6.2) may later retain the prompt used for provenance
independently of the current prompt record. All endpoints reuse the α5c
patterns: `CurrentUserDep`, owner+tenant scoping via the project gate,
two-level `404`-anti-enumeration, soft-delete idempotent-by-404. See
`docs/domain/PROMPT_AGGREGATE.md`, **ADR-0036**, and
`docs/engineering/PHASE3_ALPHA6_1_PREFLIGHT.md`.

#### Added
- **`POST /api/v1/projects/{project_id}/prompts`** (`CurrentUserDep`) — create
  a prompt. Body `{ kind, text_content, scene_id?, model_id?, extra? }`;
  `kind` validated against the `prompt_kind` enum (8 modality kinds), 
  `text_content` `1 ≤ len ≤ 10000` (stripped). A non-null `scene_id` must be a
  **live scene in the same project** (else `422 VALIDATION_FAILED`, not `404`);
  a non-null `model_id` must be a live, non-`retired` `ai_models` row (else
  `422`). Identity + `generated_by_agent` are server-owned (`extra="forbid"` →
  `422`). `404` if the project is missing / not the caller's. Returns `201` +
  `PromptPublic`.
- **`GET /api/v1/projects/{project_id}/prompts`** — list the project's live
  prompts newest-first (`created_at` desc, `id` desc) with optional
  `?kind=<enum>` and `?scene_id=<uuid>` filters (combined = AND; bad enum /
  non-UUID → `422`). Not paginated. Empty → `200 []`. `404` on unowned project.
- **`GET /api/v1/projects/{project_id}/prompts/{prompt_id}`** — one prompt
  (`200`) or the uniform two-level `404`.
- **`PATCH /api/v1/projects/{project_id}/prompts/{prompt_id}`** — partial,
  content-only, **no version fence**. Body = any subset of `{ text_content,
  kind, model_id, extra }`; tri-state via `exclude_unset` (explicit
  `model_id: null` clears the link; a non-null `model_id` is re-validated →
  `422`; `text_content`/`kind` non-nullable). `scene_id` immutable (not
  accepted, `extra="forbid"` → `422`); empty patch → `422`; same-value patch is
  a `200` no-op. Returns `200` + `PromptPublic`. No `projects.version` bump.
- **`DELETE /api/v1/projects/{project_id}/prompts/{prompt_id}`** — owner-scoped
  soft delete (`204`), no version fence, idempotent-by-404.
- **Domain** `app/domain/prompts/prompt.py` — frozen `Prompt` entity (slim view
  of the physical row; **no `version` field** by design).
- **`IPromptRepository`** + `SqlAlchemyPromptRepository` (`add`, `list_owned`
  + `kind`/`scene_id` filters, `get_owned`, `update_owned`,
  `soft_delete_owned`, `model_is_linkable`) — all project-scoped + soft-delete
  excluded. Wired onto the real `UnitOfWork`, the integration `_TestUnitOfWork`,
  and `FakeUnitOfWork` (+ `FakePromptRepository`).
- **Use cases** `app/application/use_cases/prompts/` — `CreatePrompt`,
  `ListPrompts`, `GetPrompt`, `UpdatePrompt`, `DeletePrompt` (two-level gate,
  scene/model link validation, same-value no-op detection, structured logs
  that never carry `text_content`/`extra` values).
- **DTOs** `app/api/v1/schemas/prompts.py` — `PromptCreateRequest`,
  `PromptUpdateRequest` (tri-state, `extra="forbid"`), `PromptPublic` (no
  `version`; `generated_by_agent`/`deleted_at` omitted); container factories +
  `deps` aliases + `routers/prompts.py`, mounted in `app/main.py`.
- **Tests** — unit matrix for the 5 use cases (happy / scene-link-foreign→422 /
  model-link-unknown→422 / not-owned→404 / filters / same-value no-op /
  explicit-null clears / idempotent-by-404); repository integration incl. the
  load-bearing **F6** test (a prompt's `scene_id` link **survives** a scene
  *soft-delete* — `ON DELETE SET NULL` fires only on a hard delete); HTTP
  integration `test_prompts.py` (A1–A15 end-to-end: 201/200/204/404/422/401,
  two-level 404, filters, tri-state PATCH, idempotent-by-404).

#### Documentation
- `API_CONTRACT.md` §2 + new §3.2.2 — prompts documented as shipped; the
  `/prompts/{id}` stub reconciled to the nested `/projects/{id}/prompts/{id}`
  shape (α6.1 pre-flight Q2).
- `docs/domain/PROMPT_AGGREGATE.md` (new) + **ADR-0036** (new) — the
  generation-input identity, the no-OCC / no-snapshot rationale, and the
  governing principle recorded verbatim.
- `docs/domain/PROJECT_AGGREGATE.md` §6/§8 — prompts noted as **outside** the
  versioned snapshot boundary; §8 map updated with the α6.1 Prompt child.

#### Version
- `0.4.11-phase3-alpha6.1-dev`.

### Phase 3 Slice α5d.3 — Project Version Branch (fork to a new project) (2026-07-12)

Completes the versioning story: a historical snapshot can now be **branched** —
forked into a **new, independently-editable project** (α5d.3 pre-flight Q1
Option A). Unlike restore (which rewinds *this* project onto an old snapshot),
branch leaves the source untouched and spins up a fresh aggregate seeded from
the chosen version's content — the "fork this save into a new project"
operation. This is the only migration-free reading of "branch" that is
genuinely distinct from restore: the schema has a single `current_version_id`
per project and per-project-unique `version_number`, so true in-project
multi-head branches would need a new table (deferred). Provenance is preserved
by a structured `branched_from` block (`{ project_id, version_id,
version_number }` of the source) embedded in the new project's `v1` snapshot
and echoed in the response `meta` — a one-way historical link, not a live
coupling. No migration — `reason=branch` already exists in the enum and the fork
reuses the α5d restore scene-materialization helpers and the guarded version-bump
trigger. See `docs/domain/PROJECT_AGGREGATE.md` §6, **ADR-0035** (D12), and
`docs/engineering/PHASE3_ALPHA5D3_PREFLIGHT.md`.

#### Added
- **`POST /api/v1/projects/{project_id}/versions/{version_id}/branch`**
  (`CurrentUserDep`) — fork a snapshot into a new project. Body is `{ name }`
  (the new project's name; every other root field, including the immutable
  `aspect_ratio`, is inherited from the snapshot; `extra="forbid"` → `422`).
  Two-level `404` gate (source project owned → version belongs to it) runs
  **before** any write (anti-enumeration); a duplicate live project name for the
  caller → `409 CONFLICT`. On success, creates a new caller-owned project,
  materializes the snapshot scenes with **fresh** ids (ordered by
  `scene_number`, full fat columns), captures the new project's `reason=branch`
  `v1` (`parent_version_id` NULL) with a `branched_from` provenance block, and
  advances the new project's `current_version_id` — all in **one transaction**.
  There is **no OCC fence** and **no source `projects.version` bump** (the source
  is not mutated). Returns `201` with the **new project** as `ProjectPublic`
  (its `version` = 2, i.e. created + first capture) plus `meta.branched_from`.
- **`IProjectVersionRepository.branch`** (+ real repo and fake) — inserts the new
  project row (name-collision → `ConflictError` before any child write),
  materializes scenes via the shared restore writer (`_scene_write_values`) with
  fresh ids, captures `v1` via the canonical snapshot builder with an embedded
  `branched_from` block, and advances the new project's pointer (guarded trigger
  bumps its `version` to 2). The source aggregate is provably untouched.
- **`BranchProjectVersion` use case** — runs the source project + version gates,
  delegates to `versions.branch`, and re-raises `ConflictError` (→ `409`).
  DTO `ProjectVersionBranchRequest` (`{ name }`, 1..200 chars, `extra="forbid"`);
  reuses `ProjectPublic` for the response; provenance echoed via a new
  `envelope(..., extra_meta=...)` helper param; container factory + `deps` alias
  + router endpoint.
- **Tests** — 5 branch use-case unit tests (happy fork + provenance, fresh scene
  ids in order, source untouched, duplicate-name `409`, unowned/unknown `404`);
  4 `ProjectVersionRepository.branch` integration tests (fork fidelity incl. fat
  columns + decimal strings + fresh ids, source untouched, one-transaction
  rollback on injected failure, duplicate-name `ConflictError`); 4 HTTP
  integration tests (branch happy — asserting the new project is a first-class
  project via follow-up GET project/scenes/`v1` — plus `422` / `404` / `409`).

#### Documentation
- `API_CONTRACT.md` §3.3 — branch documented as shipped; autosave + field-level
  diff deferred to α5d.4+.
- `PROJECT_AGGREGATE.md` §6/§8 + **ADR-0035** (D12) — branch = fork-to-new-project,
  the `branched_from` lineage/provenance model, and the rejected alternatives
  (in-project multi-head, restore-alias) recorded.

#### Version
- `0.4.10-phase3-alpha5d3-dev`.

### Phase 3 Slice α5d.2 — Project Version Restore + Diff (2026-07-12)

Makes the version ledger *actionable*: a historical snapshot can be **restored**
into live state, and two versions can be **diffed**. Restore never rewrites
history (**ADR-0035** D2) — it appends a new `reason=restore` version parented on
the source and repoints `current_version_id`. The load-bearing decision is the
**Aggregate OCC Rule**: `projects.version` is promoted to the concurrency token
for the *entire* Project aggregate, so a scene mutation now also bumps
`projects.version`. This gives restore a single, honest fence: the token the
caller last observed is invalidated by *any* observable aggregate change
(project column **or** scene edit) since their read. No migration — restore
reuses the α5c project lock and the existing guarded version-bump trigger. See
`docs/domain/PROJECT_AGGREGATE.md` §6, **ADR-0035**, and
`docs/engineering/PHASE3_ALPHA5D2_PREFLIGHT.md`.

#### Added
- **`POST /api/v1/projects/{project_id}/versions/{version_id}/restore`**
  (`CurrentUserDep`) — restore a snapshot into live content. Body is the
  aggregate OCC token `{ version }` (required; `extra="forbid"` → `422`).
  Two-level `404` gate (project owned → version belongs to it) runs **before**
  the fence (anti-enumeration); a stale `projects.version` → `412
  VERSION_CONFLICT` with **zero writes**. On success, appends a
  `reason=restore` version (`parent_version_id` = source), reconciles the live
  scene set to the snapshot (upsert by `id`, soft-delete removed, insert added),
  rewrites the mutable project root, advances `current_version_id`, and bumps
  `projects.version` by **exactly one** — all in **one transaction**. Returns
  `200` with the new head `ProjectVersionDetail`.
- **`GET …/versions/{version_id}/diff?against={base_version_id}`** — a coarse,
  **on-demand** change summary between the `against` base and the `{version_id}`
  target, computed from the two stored snapshots (nothing persisted). Uniform
  `404` if either version is missing / not the caller's; `against` required
  (missing/malformed → `422`). Returns `200` with `ProjectVersionDiff`
  (`base_version_number`, `target_version_number`, `project_changed`,
  `scene_changes` = `added` / `removed` / `modified`).
- **Aggregate OCC Rule** — `IProjectRepository.touch_version` (+ real repo and
  fake) bumps `projects.version` explicitly; wired into all four scene use cases
  (`create` / `update` / `move` / `delete`), guarded so **no-op** edits (an
  update to an identical value, a move that doesn't change order) do **not** bump.
- **`IProjectVersionRepository.restore`** (+ real repo and fake) — project-row
  lock, aggregate OCC fence, source-snapshot load, `aspect_ratio` immutability
  assert, default-storyboard rehome, scene reconcile (blanket soft-delete →
  upsert-by-`id`, reviving soft-deleted rows in place), trailing capture, and a
  single project UPDATE that rewrites the root + advances the pointer + bumps the
  version in one statement.
- **`RestoreProjectVersion` / `DiffProjectVersions` use cases** — restore runs
  the project + source-version gates then delegates to `versions.restore`
  (`None` → `412`); diff is a pure function over the two snapshots (no repo
  method). DTOs `ProjectVersionRestoreRequest` (`{ version }`, `extra="forbid"`)
  and `ProjectVersionDiff` (+ `SceneChangeCounts`); container factories + `deps`
  aliases; two new router endpoints.
- **Tests** — 8 restore/diff use-case unit tests + 6 Aggregate-OCC-bump
  regression tests (`tests/unit/.../versions/`, `tests/unit/.../scenes/`);
  5 `ProjectVersionRepository.restore` integration tests (round-trip fidelity
  incl. fat columns + decimal strings, revive-soft-deleted, stale-fence
  no-write, one-transaction rollback on injected failure, history immutability);
  8 HTTP integration tests (restore happy / 412 / 404 / 422; diff happy / 404 /
  422).

#### Documentation
- `API_CONTRACT.md` §3.3 — restore + diff documented as shipped; branching /
  autosave deferred to α5d.3+.
- `PROJECT_AGGREGATE.md` §6 + **ADR-0035** — Aggregate OCC Rule invariant,
  restore algorithm, and on-demand diff recorded.

#### Version
- `0.4.9-phase3-alpha5d2-dev`.

### Phase 3 Slice α5d.1 — Project Versions (capture / list / get) (2026-07-12)

Establishes the **Project Version** ledger — immutable, append-only content
snapshots of a project plus its ordered scenes. This is the *product* "version
history" feature and the foundation for restore/branch (α5d.2). It is
deliberately distinct from the row-OCC `projects.version` concurrency counter:
the ledger is a user-facing history, the row `version` is a write guard
(**ADR-0035** D1). A capture serializes on the project row (reusing the α5c
lock), assigns a monotonic `version_number`, links a `parent_version_id`
lineage chain, stores a canonical JSONB snapshot, and advances
`projects.current_version_id`. No migration — `project_versions`, its
immutability trigger, and the current pointer all exist in the α1 baseline.
See `docs/domain/PROJECT_AGGREGATE.md` §6, **ADR-0035**, and
`docs/engineering/PHASE3_ALPHA5D_PREFLIGHT.md`.

#### Added
- **`POST /api/v1/projects/{project_id}/versions`** (`CurrentUserDep`) —
  capture an immutable snapshot. `reason` is server-set to `manual_save`
  (α5d.1 takes **no** client input; `extra="forbid"` → `422`). Assigns the
  next `version_number` (1, 2, 3 …) under the project-row lock, links
  `parent_version_id` to the previous current version, advances
  `current_version_id` (bumps the row `version` by one), returns `201` with
  the full `ProjectVersionDetail` (metadata + `snapshot`). An empty project
  is valid (`snapshot.scenes == []`). `404` if the project is missing / not
  the caller's.
- **`GET …/versions`** — the project's version history as **metadata-only**
  `ProjectVersionPublic`, newest-first by `version_number`, un-paginated
  (bounded editorial history). No snapshot bodies. `404` on an unowned
  project.
- **`GET …/versions/{version_id}`** — one version WITH its immutable
  `snapshot` (`ProjectVersionDetail`), addressed by UUID `id` (α5d Q3), or
  the uniform `404` (two-level gate: project owned → version belongs to it).
- **Snapshot boundary** (ADR-0035 D3) — project root fields + capture-time
  `version` + the default storyboard identity + all live scenes ordered by
  `scene_number`, each as its **full physical row** (restore-ready). Canonical
  serialization: leading `schema_version`, `Numeric` durations as lossless
  decimal strings, scene `id`s preserved (α5c Q8). Excludes prompts / media /
  render / timeline / tags / folder (not yet API-managed).
- **`ProjectVersion` + `ProjectVersionSummary` domain entities**
  (`app/domain/versions/`) — frozen; the summary is a metadata-only read model
  so the list never drags snapshots off the DB.
- **`IProjectVersionRepository` + `ProjectVersionRepository`** —
  `create_snapshot` (project-row-locked numbering + lineage + snapshot
  assembly + current-pointer advance), `list_by_project` (metadata columns
  only), `get_owned` (UUID-scoped). Extended on the unit-test
  `FakeProjectVersionRepository` and the integration `_TestUnitOfWork`; `.versions`
  added to the UoW.
- **Three use cases** (`CreateProjectVersion` / `ListProjectVersions` /
  `GetProjectVersion`) — all run the project ownership gate first;
  each pairs its payload with the project's `current_version_id`
  (`VersionResult` / `VersionListResult`) so the router derives the
  `is_current` DTO flag without a second query.
- **DTOs** `ProjectVersionCreateRequest` (empty, `extra="forbid"`) /
  `ProjectVersionPublic` (metadata + derived `is_current`) /
  `ProjectVersionDetail` (+ `snapshot`, `diff_summary`); router
  `app/api/v1/routers/versions.py` mounted at `/api/v1`; container factories +
  `deps` aliases.
- **Tests** — 12 use-case unit tests (`tests/unit/.../versions/`),
  `ProjectVersionRepository` integration tests (numbering + lineage, snapshot
  fidelity incl. ordering / fat fields / decimal strings, DB-enforced
  immutability — direct UPDATE rejected), and 13 HTTP integration tests
  (`tests/integration/api/test_versions.py`).

#### Documentation
- `API_CONTRACT.md` §3.3 (Project Versions) filled in with the shipped
  capture + browse surface.
- New `docs/decisions/ADR-0035-project-version-snapshots.md` (immutable
  ledger, restore-by-new-version, identity preservation, hard-delete
  constraint); `PROJECT_AGGREGATE.md` §6 updated to mark the ledger shipped.

#### Version
- `0.4.8-phase3-alpha5d-dev`.

### Phase 3 Slice α5c — Scenes (create / list / get / patch / move / soft-delete) (2026-07-11)

Establishes the **Scene** aggregate — the first child aggregate under a
project and the first real content-editing workflow. Keeps the α1 baseline
`Project → Storyboard → Scene` schema but hides the intermediary: a single
**implicit default storyboard** is auto-created on the first scene, so the
public API is a flat `…/projects/{id}/scenes` surface (storyboard never on
the wire). Introduces two patterns reused by every future nested resource:
the **two-level visibility gate** (project ownership → scene visibility,
both → uniform `404`) and **project-row-locked ordering** (sparse gap-based
`scene_number` with a transparent full rebalance). See
`docs/domain/SCENE_AGGREGATE.md` and
`docs/engineering/PHASE3_ALPHA5C_PREFLIGHT.md`.

#### Added
- **`POST /api/v1/projects/{project_id}/scenes`** (`CurrentUserDep`) —
  append a scene (`201`, `version=1`, `position`=last). Body `{ title,
  duration_seconds, narration?, subtitle? }` (`extra="forbid"`;
  `duration_seconds > 0`). Auto-creates the default storyboard on the first
  scene (emits `storyboard.default_created`). `404` if the project is
  missing / not the caller's.
- **`GET /api/v1/projects/{project_id}/scenes`** — the project's live
  scenes ordered by `position` ascending, **un-paginated** (bounded
  editorial list). Read-only: no storyboard is created for an empty project
  (`data: []`). `404` on an unowned project.
- **`GET …/scenes/{scene_id}`** — one scene (`200`) or the uniform `404`
  (two-level gate). `ScenePublic` exposes a dense 1-based `position` and
  omits `storyboard_id` + the raw `scene_number`.
- **`PATCH …/scenes/{scene_id}`** — partial, version-fenced, **content
  only** (`title` / `duration_seconds` / `narration` / `subtitle`).
  Tri-state via `SceneUpdateRequest` (`extra="forbid"`, required
  `version`). `200` (real change → `version` +1; same-value → no-op);
  `404` (404-before-412); `412 VERSION_CONFLICT`; `422` (empty patch,
  forbidden `position`, non-nullable `null`, missing version).
- **`POST …/scenes/{scene_id}/move`** — dedicated version-fenced reorder.
  Body `{ version, position }` (1-based, clamped to `[1, N]`; current-slot
  = `200` no-op). `412` on stale version / concurrent content bump.
- **`DELETE …/scenes/{scene_id}`** — owner-scoped soft delete (`204`, no
  version fence), idempotent-by-404.
- **`Scene` domain entity** (`app/domain/scenes/scene.py`) — frozen slim
  projection of the fat `scenes` table (defers cinematography columns).
- **`ISceneRepository` + `SceneRepository`** — `ensure_default_storyboard`
  (get-or-create under a `SELECT … FOR UPDATE` project-row lock),
  gap-based `add` (`max + 1000`), `list_by_project`, `get_owned_scene`,
  version-fenced `update_owned` CAS (hand-set `+1` over the guarded
  trigger → net +1), `soft_delete_owned`, and `reorder_owned` (gap
  midpoint with a two-phase full rebalance when a gap is exhausted).
  Extended on the unit-test `FakeSceneRepository` and the integration
  `_TestUnitOfWork`.
- **Six use cases** (`CreateScene` / `ListScenes` / `GetScene` /
  `UpdateScene` / `MoveScene` / `DeleteScene`) — all run the two-level gate;
  `UpdateScene` is fetch-then-fence with same-value no-op detection.
- **DTOs** `SceneCreateRequest` / `ScenePublic` / `SceneUpdateRequest` /
  `SceneMoveRequest`; router `app/api/v1/routers/scenes.py` mounted at
  `/api/v1`; container factories + `deps` aliases; `.scenes` on the UoW.
- **Tests** — 23 use-case unit tests (`tests/unit/.../scenes/`), 11
  `SceneRepository` integration tests (incl. the load-bearing +1
  anti-double-bump and the reorder rebalance path), and 22 HTTP
  integration tests (`tests/integration/api/test_scenes.py`).

#### Documentation
- `API_CONTRACT.md` §3.2.1 (Scenes) + Resource Map updated to the nested
  scene routes.
- New `docs/domain/SCENE_AGGREGATE.md` and
  `docs/engineering/PHASE3_ALPHA5C_PREFLIGHT.md`; `PROJECT_AGGREGATE.md`
  corrected to `Project owns Storyboards, Storyboard owns Scenes`.

#### Version
- `0.4.7-phase3-alpha5c-dev`.

### Phase 3 Slice α5b — Projects update + soft-delete (`PATCH`/`DELETE /projects/{id}`) (2026-07-11)

Completes the Project CRUD lifecycle (`create → read → update →
soft-delete`). Brings the α4 optimistic-concurrency CAS to a
**path-addressed** resource and establishes the **404-before-412**
pattern — a path-addressed authenticated mutation decides *visibility*
(missing / out-of-scope / soft-deleted → `404`, exactly like the read)
**before** the version fence (`412`), so a caller can never learn a
resource exists via a `412`. Ships the M3 composite pagination index
(deferred from α5a) in the same slice. See
`docs/engineering/PHASE3_ALPHA5B_PREFLIGHT.md`.

#### Added
- **`PATCH /api/v1/projects/{id}`** (`CurrentUserDep`) — partial,
  version-fenced update. Mutable surface: `name` / `description` /
  `language` / `style` / `settings`. Tri-state semantics via
  `ProjectUpdateRequest` (`extra="forbid"`, required `version`): absent =
  unchanged, explicit `null` clears a nullable field, value sets it;
  `settings` is whole-object replace. `200` on success (real change →
  `version` +1 and `updated_at` advances; same-value → `200` no-op);
  `404` (missing/not-yours/soft-deleted); `412 VERSION_CONFLICT` (stale
  version / concurrent-bump race); `409 CONFLICT` (rename collision);
  `422` (empty patch, forbidden/mis-typed field, missing version,
  `null` for a non-nullable field).
- **`DELETE /api/v1/projects/{id}`** (`CurrentUserDep`) — owner-scoped
  soft delete → `204 No Content`, no version fence. Idempotent-by-404
  (repeat delete, and GET/PATCH after delete → `404`); frees the project
  `name` for re-use (uniqueness index excludes soft-deleted rows).
- **`IProjectRepository.update_owned` / `soft_delete_owned`** + their
  `SqlAlchemy` implementations. `update_owned` is a `UPDATE ... WHERE
  version = :expected` CAS (mirrors `UserRepository.update_profile`),
  mapping a rename `IntegrityError` → `ConflictError`; `soft_delete_owned`
  sets `deleted_at` and reports whether a live owned row was marked.
  Extended on the unit-test `FakeProjectRepository` too.
- **`UpdateProject` / `DeleteProject` use cases** — `UpdateProject` is
  fetch-then-fence (404-before-412) with same-value no-op detection;
  `DeleteProject` maps a `False` soft-delete to `404`. Container
  factories + `UpdateProjectDep` / `DeleteProjectDep` aliases wire them.
- **Migration `0008`** — composite partial index
  `ix_projects_owner_created_id`
  `(tenant_id, owner_user_id, created_at DESC, id DESC) WHERE deleted_at
  IS NULL` (M3), declared on `Project.__table_args__` and created/dropped
  by the migration. Serves `list_owned`'s keyset scan; the older
  `ix_projects_tenant_id_owner_user_id` is kept.
- **Tests** — unit: `UpdateProject` (9, U1–U9), `DeleteProject` (4,
  U10–U13); integration: `ProjectRepository` (6, R8–R13 incl. the
  guarded-trigger version-bump check) and HTTP `test_projects.py` (16,
  H17–H32 covering 200/204/404/409/412/422, 404-before-412, tri-state
  PATCH, and idempotent-by-404 delete).

#### Changed
- **`API_CONTRACT.md`** — §3.2 annotated with the α5b PATCH (tri-state,
  404-before-412) and DELETE (soft, idempotent-by-404) semantics.
- App version → `0.4.6-phase3-alpha5b-dev`.

#### Fixed
- **`validation_error_handler`** (`app/core/errors.py`) now runs
  `exc.errors()` through `jsonable_encoder`. A Pydantic `model_validator`
  that raises `ValueError` (first exercised by the α5b empty-PATCH guard)
  embeds the raw exception object in `ctx["error"]`, which the plain
  `json.dumps` render path could not serialise — turning an intended
  `422 VALIDATION_FAILED` into a `500`. The encoder coerces such objects
  recursively while preserving the human-readable `msg`, hardening the
  422 path for every field- and model-level validator.

### Phase 3 Slice α5a — Projects create + read (`POST`/`GET /projects`) (2026-07-11)

The first resource beyond identity, and the first **collection** endpoint.
Establishes the owner-and-tenant scoping pattern and the cursor
(keyset) pagination primitives that every future list endpoint reuses.
A thin, additive slice: create + read only (`PATCH` / `DELETE` /
`duplicate` deferred to α5b+). **No migration** — the `projects` table
and its `version` / soft-delete / uniqueness constraints already exist
from α1. See `docs/domain/PROJECT_AGGREGATE.md` (aggregate model) and
`docs/engineering/PHASE3_ALPHA5_PREFLIGHT.md` (slice scope + decisions).

#### Added
- **`Project` domain entity** (`app/domain/projects/project.py`) — a
  frozen dataclass mirroring the `projects` row. `current_version_id` /
  `duration_seconds` are modelled but unset in α5a (managed by later
  slices).
- **Cursor pagination primitives** (`app/application/pagination.py`) —
  `Cursor` / `Page` dataclasses + opaque, versioned, URL-safe base64
  `encode_cursor` / `decode_cursor`. A malformed token is a
  `ValidationFailedError` (422), never a 500. Placed in the application
  layer so both use cases and the API import it without an import-linter
  violation.
- **`IProjectRepository`** (`add` / `get_owned` / `list_owned`) + a
  `SqlAlchemy` `ProjectRepository`. `get_owned` / `list_owned` filter on
  `(tenant_id, owner_user_id, deleted_at IS NULL)`; `list_owned`
  paginates by keyset `(created_at, id) DESC` with a deterministic
  `id` tie-break (D14); `add` maps a duplicate-name `IntegrityError` to
  `ConflictError`. `IUnitOfWork` gains a `projects` attribute wired
  through the SQLAlchemy UoW, the unit-test fakes, and the integration
  conftest.
- **`CreateProject` / `GetProject` / `ListProjects` use cases**
  (`app/application/use_cases/projects/`) — ownership + tenancy come
  from the authenticated caller; a cross-owner / cross-tenant / missing
  project collapses to `NotFoundError` (anti-enumeration).
- **DTOs** (`app/api/v1/schemas/projects.py`) — `ProjectCreateRequest`
  (`extra="forbid"`; `aspect_ratio` constrained to
  `horizontal|vertical|square`; ownership/tenancy not accepted from the
  body) and `ProjectPublic` (omits `current_version_id` /
  `duration_seconds`, exposes `version` as the α5b PATCH handle).
- **Endpoints** (`app/api/v1/routers/projects.py`, all `CurrentUserDep`):
  `POST /api/v1/projects` (201, `version=1`; 409 on duplicate name),
  `GET /api/v1/projects` (owner-scoped, newest-first, `?limit=` 1–100
  default 20 + opaque `?cursor=`; `meta.next_cursor` present iff a
  further page exists), and `GET /api/v1/projects/{project_id}` (200 or
  a uniform 404). Container factories + `*Dep` aliases wire them through
  the composition root.
- **Tests** — unit: `CreateProject` (6), `GetProject` (4),
  `ListProjects` (5, incl. a multi-page keyset walk), pagination (4);
  integration: `ProjectRepository` (7, R1–R7) and HTTP `test_projects.py`
  (16, H1–H16 covering 201/401/404/409/422, owner scoping, and cursor
  pagination).

#### Changed
- **`API_CONTRACT.md`** — §1.2 error example corrected from
  `PROJECT_NOT_FOUND` to the canonical `NOT_FOUND`; §3.2 annotated with
  the α5a-shipped subset and the deferred surface.
- App version → `0.4.5-phase3-alpha5a-dev`.

### Phase 3 Slice α4 — Authenticated profile update (`PATCH /users/me`) (2026-07-10)

The first authenticated **mutation**, and the write-path counterpart to
α3's read-path `CurrentUserDep` pattern. Establishes the canonical
authenticated mutation flow — `CurrentUserDep` → DTO validation →
optimistic-concurrency check → domain mutation → versioned repository
CAS → updated representation — that every future write endpoint copies.
**No migration** (the `users.version` column and its `bump_version` /
`touch_updated_at` triggers already exist from α1). In α4 the only
mutable field is `display_name`.

#### Added
- **`IUserRepository.update_profile(user_id, expected_version, display_name)`**
  — a targeted, version-fenced mutation. The concrete `UserRepository`
  implementation runs a SQL compare-and-swap
  (`UPDATE … WHERE id = ? AND version = ? AND deleted_at IS NULL …
  RETURNING`) so optimistic-concurrency violations are detected
  atomically at the DB layer (no TOCTOU). A same-value target
  short-circuits before any `UPDATE` — no write, no `version` bump, no
  `updated_at` bump. Returns the updated entity on change, the unchanged
  entity on a no-op, and `None` on a version mismatch or a soft-deleted
  row (the two collapse deliberately — anti-enumeration).
- **`UpdateUserProfile` use case** (`app/application/use_cases/users/`,
  a new user-management package distinct from `auth/`) — orchestrates
  the update, distinguishes real change from same-value no-op by
  comparing the returned version, raises `VersionConflictError` when the
  repository returns `None`, and emits the audit log. Field **names**
  only in logs, never submitted values.
- **`VersionConflictError`** (`app/core/errors.py`) — `ApplicationError`
  subclass, `code = "VERSION_CONFLICT"`, HTTP 412. Rendered by the
  existing centralized exception handler.
- **`UpdateUserProfileRequest` DTO** (`app/api/v1/schemas/users.py`) —
  `extra="forbid"`, required `display_name` (1–200, whitespace-stripped,
  non-null), required `version` (≥ 1). This is the 422 rejection
  surface.
- **`PATCH /api/v1/users/me`** endpoint (`app/api/v1/routers/users.py`)
  — the canonical mutation reference. 200 with the updated `UserPublic`
  on success (never 204); 412 `VERSION_CONFLICT` on a stale fence; 422
  on body validation; 401 on any α3 auth-rejection branch. Container
  factory `get_update_user_profile_use_case` + `UpdateUserProfileDep`
  wire it through the composition root; a shared `_to_public` helper
  projects the domain `User` for both `GET` and `PATCH`.
- **Structured-log events** — `user.profile.updated` (INFO:
  `changed_fields`, `previous_version`, `new_version`) and
  `user.profile.update_rejected` (WARN for `version_mismatch`, INFO for
  `same_value_noop`).
- **Tests** — 8 unit tests for `UpdateUserProfile`
  (`tests/unit/application/use_cases/users/test_update_profile.py`),
  10 HTTP integration tests (H15–H24 in `test_users_me.py`) covering the
  happy path, 401/412/422 surfaces, same-value no-op, PATCH→GET
  round-trip, and a sequential-CAS race, and 3 repository integration
  tests (R1–R3 appended to `test_user_repository.py`) for the CAS happy
  path, version mismatch, and soft-deleted-row guard.
- **`docs/api/AUTH_ENDPOINTS.md`** — new §7.1 (`PATCH /users/me`) and §9
  (Canonical Authenticated Mutation Flow); §7 `UserPublic` example
  updated to show `version` + `updated_at`.

#### Changed
- **`UserPublic`** (`app/api/v1/schemas/users.py`) gains `version: int`
  and `updated_at: datetime`. Additive — every response returning a
  `UserPublic` (register, login, refresh, `GET /me`, `PATCH /me`) now
  carries both. `version` is the optimistic-concurrency fence clients
  round-trip with `PATCH /me`; `updated_at` supports "last modified" UX.
  `routers/auth.py::_to_payload` and `routers/users.py::get_me` updated
  to populate them.
- Version bumped to `0.4.4-phase3-alpha4-dev` in `app/main.py`.
- **Version-increment invariant** established project-wide:
  `users.version` moves only when a persisted field actually changes —
  never on auth, reads, identical PATCHes, or failed mutations.

### Phase 3 Slice α3 — Authenticated-request seam + `GET /users/me` (2026-07-09)

The read-path foundation for every authenticated endpoint, and the
predecessor to α4's write path. Introduces the `get_current_user`
dependency that resolves a bearer access token into a live `User` domain
entity, and proves the seam end-to-end with the first authenticated
business endpoint, `GET /api/v1/users/me`. **No migration** —
application-layer only.

#### Added
- **`get_current_user` dependency + `CurrentUserDep` alias**
  (`app/api/v1/deps.py`) — resolves `Authorization: Bearer <access>` →
  `User`. Strict verification (`allow_expired=False`), sid-driven
  session-liveness check, soft-delete-aware user lookup. Emits an
  anti-enumeration 401 with a single generic message for every failure
  mode; the server-side structured log carries the specific reason.
- **`GET /api/v1/users/me`** (`app/api/v1/routers/users.py`, a new router
  registered under the `/api/v1` prefix) — returns `UserPublic` for the
  authenticated caller. The first endpoint to consume `CurrentUserDep`.
- **`ISessionRepository.get_by_id`** — sid → session read used by the
  dependency for the session-liveness check (the one port-surface
  addition α3 introduces).
- **`app/api/v1/schemas/users.py`** — new schema module re-exporting
  `UserPublic` so the users router does not import an auth-named module.
- **Structured-log events** — `auth.request.authenticated` (INFO, happy
  path) and `auth.request.rejected` (WARN, `reason=` field, with the
  `security_event` flag on tamper-flavoured reasons).
- **Tests** — unit coverage for `get_current_user` (every rejection
  branch) and HTTP integration for `GET /users/me` (200 happy path + the
  401 surfaces).
- **`docs/engineering/AUTH_TOKEN_LIFECYCLE.md` §3.5** — authenticated-request
  path appendix (`bearer → verify_access → session-liveness →
  user-liveness → User`).

#### Changed
- Version bumped to `0.4.3-phase3-alpha3` in `app/main.py`.
- **`docs/engineering/RUNBOOK_WAVE.md` §7.5** — "no file-sync-hosted
  repositories" codified after the OneDrive → `C:\dev\ai-video-platform`
  migration (2026-07-09).
- **`ROADMAP.md`** — Phase 3 row updated with the α3 status line.

#### Fixed
- **`render_jobs.progress` type-hint drift** (`chore(orm)`, PR #12, merge
  `d30fb3a`) — the ORM annotation was `Mapped[float]` while the column is
  `text`; corrected to `Mapped[str]` to match what SQLAlchemy actually
  loads. Carried as a debt from the α2 trilogy and closed here in its own
  dedicated PR. No migration.

### Phase 3 Slice α2b — Auth (refresh + logout) (2026-07-01)

Completes the authentication lifecycle started in α2a. Adds refresh
token rotation with family-level reuse detection, session revocation
via logout, and the `IClock` port used by both new use cases.
Delivered as two internal checkpoints (α2b.1: `ISessionRepository`
extensions + `RefreshSession`; α2b.2: `verify_access(allow_expired)` +
`LogoutSession` + router wiring). **No migration, no ADR** — a direct
application of the α2a auth foundation.

#### Added
- **`IClock` port** (`app/application/interfaces/clock.py`) and
  `SystemClock` implementation (`app/infrastructure/clock.py`). All
  auth use cases (`RegisterUser`, `LoginUser`, `RefreshSession`,
  `LogoutSession`) now take an injected clock instead of calling
  `datetime.now(UTC)` inline. `FakeClock` in the unit fakes supports
  a frozen `fixed_at` + `tick(seconds)` for deterministic time-based
  tests.
- **`ISessionRepository.get_by_hash / revoke / list_family`** —
  three new methods on the α2a port. `revoke` uses a compare-and-swap
  clause (`WHERE revoked_at IS NULL`) so the first revoker wins and
  the original `revoked_at` timestamp is preserved for audit through
  all subsequent no-op calls. `list_family` powers the family sweep on
  reuse detection. `get_by_hash` returns revoked rows too, matching
  the use case's need to inspect `revoked_at` as the reuse signal.
- **`RefreshSession` use case** (`app/application/use_cases/auth/`) —
  orchestrates the full rotation flow: JWT verify → SHA-256 hash lookup
  → sid consistency check (A12) → reuse detection with full-family
  revocation → user liveness check → CAS-revoke old row → mint fresh
  tokens preserving `family_id` → insert new row. Every failure mode
  raises the same client-facing `InvalidRefreshTokenError` for
  anti-enumeration; server-side logs carry the specific reason.
- **`LogoutSession` use case** — CAS-revokes the session identified by
  the access token's `sid` claim. **Accepts expired access tokens**
  (documented prominently in the class docstring and in
  `docs/engineering/AUTH_TOKEN_LIFECYCLE.md`): forcing a refresh before
  logout would defeat the "I am done" intent. Signature and `kind` are
  still strictly enforced. Idempotent: second logout returns 204 and
  preserves the original `revoked_at`.
- **`verify_access(allow_expired: bool = False)`** — new kwarg on
  `ITokenIssuer`, threaded through `JWTService.verify` via PyJWT's
  `options={"verify_exp": False}`. Only `LogoutSession` sets it; every
  other consumer keeps the strict default.
- **`InvalidRefreshTokenError`** (`app/application/use_cases/auth/errors.py`) —
  subclass of `UnauthorizedError` used by both `RefreshSession` and
  `LogoutSession` for uniform 401 envelopes on every non-happy path.
- **`RefreshRequest` DTO** + `BearerAccessTokenDep` (in `app/api/v1/`) —
  the FastAPI dependency parses `Authorization: Bearer <token>`,
  raising 401 for missing / malformed headers.
- **Two new endpoints** — `POST /api/v1/auth/refresh` (200 with the
  rotated pair) and `POST /api/v1/auth/logout` (204 No Content).
- **`docs/engineering/AUTH_TOKEN_LIFECYCLE.md`** — operational spec
  covering the session state machine, endpoint sequence diagrams, the
  Refresh Family Example (visualising why reuse detection nukes the
  whole family), invariants, and the structured-log event catalogue
  including which events carry `security_event=True` for SIEM alerting.
- **Extended tests** — `test_token_issuer.py` +2 (`allow_expired`
  accepts stale / still rejects tampered), `test_refresh_session.py`
  (13 unit tests), `test_logout_session.py` (8 unit tests),
  `test_clock.py` (1 unit test), `test_session_repository.py` +5
  integration tests (get_by_hash / revoke CAS / list_family),
  `test_auth.py` +9 integration tests (refresh happy path, reuse
  detection, garbage token, access-token-as-refresh, sid mismatch,
  logout happy path, logout idempotent, missing header, malformed
  header, refresh-token-as-logout).

#### Changed
- `RegisterUser` and `LoginUser` constructors now take an `IClock`
  parameter. All timestamp assignments (`created_at`, `updated_at`,
  `last_login_at`, `issued_at`, `last_used_at`) go through the clock.
  `FakeTokenIssuer` and `FakeSessionRepository` extended for the new
  port surface.
- Version bumped to `0.4.2-phase3-alpha2b-dev` in `app/main.py`.

### Phase 3 Slice α2a — Auth (register + login) (2026-07-01)

First real business capability shipped on top of the α1 architecture
scaffold. Delivers the password-auth happy path — `POST /api/v1/auth/register`
and `POST /api/v1/auth/login` — end-to-end through the layered
architecture (domain → application → infrastructure → API). Split from
the original combined α2 plan into α2a (register + login) + α2b
(refresh + logout) per the pre-flight review for reviewability. **No
migration, no ADR** (no new architectural trade-off — the plan is a
direct application of ADR-0008 + the α1 DI pattern).

#### Added
- **Domain layer** — `app/domain/identity/{user,tenant,session}.py`.
  Frozen dataclasses with `slots=True`, zero ORM inheritance, zero
  framework dependencies. Enforced by import-linter contract #1.
- **Two new application ports** (`app/application/interfaces/security.py`):
  `IPasswordHasher`, `ITokenIssuer`, plus the `IssuedTokens` and
  `TokenClaims` value objects. Existed to keep unit tests fast (Argon2id
  fake substitution) and to lock the seam for future token-scheme
  swaps (PASETO, opaque tokens).
- **Extended `IUserRepository`** — new methods `get_by_email`,
  `get_by_id`, `add`, `update_last_login`. α1 methods
  (`count`, `exists_by_id`) preserved per the pre-flight review.
- **Three new repository ports** — `ITenantRepository` (add /
  get_by_id / exists_by_slug), `ISessionRepository` (add only in α2a;
  extended in α2b), `IRoleRepository` (assign_role_by_code, idempotent
  via ON CONFLICT DO NOTHING).
- **UoW attribute-style repos** — `IUnitOfWork` now exposes
  `.users`, `.tenants`, `.sessions`, `.roles` populated by the
  concrete UoW on `__aenter__`, so use cases call
  `await uow.users.add(...)` without ever seeing SQLAlchemy classes.
- **Two application use cases** — `RegisterUser` (application-level
  global email-uniqueness pre-check → auto-creates a self-service
  tenant per signup with slug-collision retry → inserts the user →
  assigns the `owner` role → issues tokens → persists the initial
  session), `LoginUser` (get-by-email → constant-time Argon2 verify
  → issue tokens with fresh family/session ids → persist session →
  bump `last_login_at`).

  *Note on the email-uniqueness pre-check:* the auto-tenant-per-signup
  design (Decision 1A) defeats the DB per-tenant unique constraint on
  `(tenant_id, email)` for the "same email registered twice"
  scenario — each signup arrives at a different `tenant_id` so the
  constraint always sees a distinct pair. Without an application-layer
  pre-check, re-registration would silently create a second orphan
  tenant under the same email. `RegisterUser` therefore calls
  `users.get_by_email(email)` inside the same UoW before creating the
  tenant and raises `EmailAlreadyRegisteredError` on a hit. Race
  window between the pre-check and insert is acceptable for α2a; a
  later hardening pass may add an application-level lock table or
  rate-limit if this proves exploitable in practice.

  *Note on the role assignment:* the pre-flight originally called for
  `user + owner`. On implementation this proved to conflate two
  orthogonal concepts — the `roles` table (workspace permissions,
  seeded with `owner, admin, editor, viewer, billing, support`) vs
  the `auth_role` ENUM in `schema.md` §0.1 (plan tiers). `user` lives
  on the ENUM, not the table. Assigning `owner` alone captures the
  intended semantics ("creator owns the tenant they just created");
  "any authenticated user" is enforced by JWT validity, not by a
  role row. Documented in the `RegisterUser` class docstring.
- **Anti-enumeration login path** — `LoginUser` burns one Argon2
  verify against a startup-computed dummy hash when the email is
  unknown or the account is OAuth-only, so wall-time is
  indistinguishable from the wrong-password branch (OWASP ASVS L2 §2.6.3).
- **`AuthTokenIssuer`** (`app/infrastructure/security/token_issuer.py`) —
  wraps the α1 `JWTService` + SHA-256 + up-front `session_id` /
  `family_id` generation into one call. Emits `sid` (session id) and
  `fam` (family id) claims on **both** access and refresh tokens so
  α2b `LogoutSession` can revoke a precise session row from the access
  token alone (no need to accept the refresh token in the logout body).
- **DTOs** (`app/api/v1/schemas/auth.py`) — Pydantic v2 request /
  response models. Request DTOs strip whitespace and lowercase the
  email before it ever reaches the use case (canonical `CITEXT` values).
  `UserPublic` explicitly enumerates public fields — `password_hash`
  cannot leak through DTO drift because it isn't declared.
- **Router** (`app/api/v1/routers/auth.py`) — two POST endpoints,
  mounted under `/api/v1`. Envelope response per API_CONTRACT §1.1.
  Zero try/except — errors surface via the α1 exception-handler chain.
- **DI wiring** — `app.core.container` grows a
  `get_token_issuer` singleton, a pre-computed
  `get_dummy_password_hash` (Argon2 cost paid once at process start,
  not per request), and two use-case factories
  (`get_register_user_use_case`, `get_login_user_use_case`).
- **New unit tests** (~19 across three files):
  `test_register_user.py`, `test_login_user.py`,
  `test_token_issuer.py`. All auth use-case tests use in-memory fakes
  (`tests/unit/application/use_cases/auth/_fakes.py`) — total unit-suite
  runtime stays sub-second because Argon2id verify is stubbed with a
  string comparison.
- **New integration tests** — `test_tenant_repository.py`,
  `test_session_repository.py`, `test_role_repository.py`; extended
  `test_user_repository.py` with α2a method coverage; new
  `tests/integration/api/test_auth.py` (9 scenarios covering register /
  login happy paths, duplicate email → 409, short password → 422,
  email lowercasing, `sid`/`fam` claim presence in JWT, anti-enumeration
  message equality, distinct families per device).
- **Integration test client fixture rebind** —
  `tests/integration/conftest.py::client` now overrides
  `container.get_session` and `container.get_unit_of_work` so mutation
  handlers run inside the test's SAVEPOINT connection. Nothing persists
  across tests; the shared Supabase instance stays clean.
- **New runtime dependency** — `email-validator>=2.2,<3` (required by
  Pydantic `EmailStr` at DTO parse time).
- **Fifth `import-linter` contract** — "Application use_cases never
  import infrastructure or api". Locks the layered boundary the
  moment the layer is introduced.

#### Changed
- **`app/main.py`** — imports and mounts `auth.router` under
  `/api/v1`; health router stays at the root path (API_CONTRACT §2
  designates `/healthz` + `/readyz` as public, versionless). App
  version bumped `0.4.0-phase3-alpha1-dev → 0.4.1-phase3-alpha2a-dev`.
- **`app/infrastructure/security/password_hasher.py`** — now declares
  `class PasswordHasher(IPasswordHasher)` (implements the new port).
  No runtime behaviour change.
- **`app/infrastructure/uow/sqlalchemy_unit_of_work.py`** — `__aenter__`
  populates the four repository attributes from the session it owns.

#### Deferred (Slice α2b)
- `POST /api/v1/auth/refresh` — token rotation with reuse detection.
- `POST /api/v1/auth/logout` — precise per-`sid` revocation using the
  claim shipped in α2a.
- `ISessionRepository` extensions: `get_by_hash`, `revoke`,
  `list_family`. Kept out of α2a intentionally per the pre-flight
  review — repositories in α2a cover only the α2a use cases.
- `IClock` port — introduced in α2b where `RefreshSession` needs it
  for the session-row `expires_at` computation.

#### Deferred (Slice α3+)
- Email verification (`/auth/email/verify`, `/auth/email/resend`) — α3.
- Password reset (`/auth/password/forgot`, `/auth/password/reset`) — α4.
- Google OAuth (PKCE) — α5.
- RBAC enforcement at endpoint boundaries — α6.
- OCC retry on `LoginUser.update_last_login` — retained as a deferred
  optimisation; add only if concurrent-login contention becomes
  observable.

---

### Phase 3 Wave 1.4 — `usage_records` per-partition `(request_id)` uniqueness (ADR-0033) (2026-06-30)

Wave-closing item for Phase 3 Wave 1: promotes a per-partition
partial-unique `(request_id) WHERE request_id IS NOT NULL` index to
every child partition of `usage_records`, resolving `schema.md` §37 q6.
First migration-coupled ADR to reference `docs/engineering/RUNBOOK_WAVE.md`
in place of inlining operational steps (per `CONTRIBUTING.md` §6,
established at `v0.3.3-infra`). **Wave 1 of Phase 3 closes with this
release (`v0.3.4-phase3-w1.4`).**

#### Added
- **`backend/alembic/versions/0007_usage_records_request_id_unique.py`** —
  hand-written single revision (`revision = "0007_usage_records_request_id_unique"`,
  `down_revision = "0006_widen_alembic_version_num"`). Upgrade body
  iterates `pg_inherits` for all current children of `usage_records`
  (26 monthly + 1 DEFAULT today) and creates one partial-unique index
  per child named `uq_<child>_request_id` (e.g.
  `uq_usage_records_y2025m12_request_id`,
  `uq_usage_records_default_request_id`) with predicate
  `(request_id) WHERE request_id IS NOT NULL`. Idempotent via
  `IF NOT EXISTS`. Downgrade mirrors with `DROP INDEX IF EXISTS`.
  Hand-written rather than via `alembic revision --autogenerate`
  because autogenerate cannot express per-child partition-level DDL
  and would not preserve the partial predicate. The per-child
  mechanic is PostgreSQL's standard and correct pattern for
  unique-on-non-partition-key constraints (the parent-level form
  `CREATE UNIQUE INDEX ON usage_records (request_id)` is rejected
  because the unique key omits the `occurred_at` partition key) —
  not a workaround. The 35-char revision ID fits the `VARCHAR(255)`
  ceiling established by `0006_widen_alembic_version_num` (v0.3.3-infra).
- **`docs/decisions/ADR-0033-usage-records-request-id-unique.md`** — new
  ADR (fourth file-per-ADR adopter; first to reference
  `RUNBOOK_WAVE.md` in §Migration Plan rather than inlining operational
  steps). Documents the architectural-review process that preceded the
  ADR, the rejected alternatives (`(provider, request_id)` scope
  expansion deferred to a future separate decision; `(model_id,
  request_id)` invention with no repository support; documentation-only
  closure inconsistent with wave-era planning artifacts; top-level
  parent index too weak; `ON ONLY` + `ATTACH PARTITION` rejected by
  the same partition-key rule), the per-child mechanic, the deliberate
  ORM-absence, the validator-extension rationale, the future-partition
  contract, and a Future Considerations section preserving the broader
  architectural pattern for a separate later decision.
- **`backend/scripts/validate_schema.py::check_usage_records_per_partition_unique_indexes`** —
  new ~120-LoC check function and `run_all_checks` wiring. Scans
  `pg_inherits` for all `usage_records` children and asserts each
  carries `uq_<child>_request_id` with `indisunique = true` and the
  expected `WHERE (request_id IS NOT NULL)` partial predicate. This
  is a CI-visibility addition compensating for the
  `load_snapshot()` bulk-index query that deliberately excludes
  partition children (`NOT EXISTS (SELECT 1 FROM pg_inherits ...)`
  for performance — Supabase round-trip count would otherwise scale
  with partition count). Not a workaround for a PostgreSQL
  limitation; not a substitute for ORM declaration (which is
  impossible by PostgreSQL design). The check passes when
  27/27 partition children carry the expected index after
  `alembic upgrade head`, fails on missing children, missing
  indexes, or wrong predicate.

#### Changed
- **`docs/database/schema.md`** §18 reconciliation note: amended to
  record the §37 q6 resolution with the architectural-review
  conservative wording — "The Phase 3 wave-planning artifacts
  consistently anticipate a `request_id`-based W1.4 implementation.
  Earlier architectural documents describe provider-scoped idempotency
  at the application level. W1.4 implements the scope reflected in
  the Phase 3 planning artifacts without attempting to reconcile that
  broader architectural question." The Step-A `(provider, request_id)`
  design is explicitly described as neither implemented nor
  superseded by W1.4; any future move is reserved for a separate
  decision informed by CR-12 implementation evidence (ADR-0033
  §Future Considerations). The §18 schema box, the §18 indexes line,
  and the §31 CR-12 use-case table row remain unchanged — column
  shape and broader app-layer idempotency are not altered by this
  wave.
- **`docs/database/schema.md`** §37 Q6 row: flipped from `rely on
  idempotency_keys` to **Resolved (Phase 3 W1.4, 2026-06-30)** with
  full constraint details, mirroring the Q8/Q9/Q10 resolved-row
  shape established by W1.1/W1.2/W1.3.
- **`docs/database/schema.md`** §37 Wave 1 epilogue: §18 q6 bullet
  marked **✅ Done — Phase 3 W1.4**, closing the Wave 1 quartet.
- **`docs/database/INDEX_STRATEGY.md`** line 147: status flipped from
  **Deferred (Phase 3)** to **Implemented (Phase 3 W1.4)**; rationale
  expanded to document the per-child mechanic, the PostgreSQL
  partition-key rule, the future-partition contract enforced by the
  validator check, and the explicit non-supersession of broader
  `(provider, request_id)` architectural semantics.
- **`ROADMAP.md`** Wave 1 row: W1.4 annotated **✅ Complete** with
  full ADR + migration cross-reference; "**Wave 1 closes with this
  tag (`v0.3.4-phase3-w1.4`).**" sentence appended.
- **`DECISIONS.md`**: one-line cross-link entry for ADR-0033 appended
  after the ADR-0032 entry, sorted by ADR number. Status initially
  `Proposed`; flipped to `Accepted` on the pre-merge status-flip
  commit.
- **`backend/app/infrastructure/db/models/usage.py`** —
  `UsageRecord.__table_args__` gains a multi-line inline comment near
  the existing `Index("ix_usage_records_request_id", "request_id")`
  declaration documenting that the per-child unique indexes are added
  by migration `0007` and intentionally have no ORM counterpart
  (PostgreSQL's partition-key rule makes a parent-level
  `Index(unique=True, postgresql_where=...)` impossible for
  `(request_id)` because the key omits the `occurred_at` partition
  key, and the children themselves are not ORM-modelled). The
  comment points at ADR-0033 §Implementation Notes and at the
  validator check. No `Index` or `CheckConstraint` declaration is
  added to the ORM.

#### Validated
- **Pre-upgrade safety SELECT** against live Supabase: `SELECT
  request_id, count(*) FROM usage_records WHERE request_id IS NOT NULL
  GROUP BY request_id HAVING count(*) > 1` returned zero rows
  (expected — the table is empty in every current environment; run
  for audit-trail completeness and to prove the production-rollback
  variant is not required).
- `alembic upgrade head` from `0006_widen_alembic_version_num` →
  `0007_usage_records_request_id_unique` applied cleanly; `pg_indexes`
  shows 27 new unique partial indexes (one per child) named per the
  `uq_<child>_request_id` pattern with `indexdef` containing the
  expected `WHERE (request_id IS NOT NULL)` predicate.
- `alembic downgrade -1` reverted cleanly; `pg_indexes` shows the 27
  indexes removed; `ix_usage_records_request_id` (the parent's
  non-unique propagating index) unaffected.
- `alembic upgrade head` re-applied cleanly (idempotency proven via
  `IF NOT EXISTS` guards).
- `python scripts/validate_schema.py` reported **all checks PASS**
  with the new `check_usage_records_per_partition_unique_indexes`
  reporting `27/27 usage_records partition(s) carry uq_<child>_request_id`.
- `python scripts/regenerate_erd.py` + `python scripts/compare_erd.py`
  reported 0 drift (per-child unique indexes are invisible to the ERD
  by design — it tracks entities and FKs, not indexes).
- `python scripts/ci_gate.py` reported **10/10 PASSED** locally
  against Supabase from a cold shell — `RUNBOOK_WAVE.md` referenced
  per ADR-0033 §Migration Plan, success-metric satisfied: W1.4
  required fewer manual steps than W1.3 (no env-load preamble, no
  migration-ID length gymnastics, no `.cursor/` accidents, no
  inline operational-steps duplication in the ADR).
- GitHub Actions on the PR: 10/10 green.

#### Not modified
- `backend/alembic/versions/0001_baseline.py` (no in-place edits to
  merged migrations; the new indexes are owned entirely by `0007`).
- `backend/alembic/versions/0003_export_jobs_partial_unique.py` (W1.1
  territory).
- `backend/alembic/versions/0004_idempotency_keys_invariants.py` (W1.2
  territory).
- `backend/alembic/versions/0005_distributed_locks_lease.py` (W1.3
  territory).
- `backend/alembic/versions/0006_widen_alembic_version_num.py`
  (`v0.3.3-infra` territory).
- `docs/database/ERD.md` (per-child unique indexes are invisible to
  ERD by design — entities + FKs only).
- `docs/database/schema.md` §18 schema box (lines 638–668), §18
  indexes line (line 673), §31 use-case table row (line 1175) —
  column shape and broader app-layer idempotency are unchanged by
  this wave.
- `ARCHITECTURE.md` §8k.1 (CR-12 domain spec), `API_CONTRACT.md`
  line 233 (webhook handlers) — broader architectural pattern
  remains documented; W1.4 neither implements nor supersedes it.
- `backend/app/application/`, `backend/app/api/`,
  `backend/app/infrastructure/ai/` — CR-12 (Usage Recorder
  middleware named in `schema.md` §31 / `ARCHITECTURE.md` §8k.1) is
  not built; W1.4 establishes the DB-level invariant in advance of
  the producer and does not anticipate the producer's design.
- `CONTRIBUTING.md` (file-per-ADR + ADRs-vs-Runbooks conventions
  established in earlier releases; W1.4 is the first migration-coupled
  ADR to exercise the runbook reference convention).
- `pyproject.toml`, dependency manifests.

#### Scope discipline
- One PR, one branch (`phase3/wave1.4-usage-records-request-id-unique`),
  one Alembic revision (`0007`), one ADR (ADR-0033), one validator
  check function. `git diff main...HEAD` touches **only** the files
  enumerated above. No opportunistic refactors, no unrelated cleanup,
  no W2 work; the `v0.3.3-infra` discipline rule held.
- ADR-0033 deliberately reserves provider-scoped DB-level enforcement
  for a separate later decision rather than expanding W1.4 scope —
  preserves the wave-era documented implementation shape exactly,
  preserves the architectural pattern documented elsewhere unchanged,
  and preserves the historical record's accuracy by neither
  rewriting earlier documents nor inventing supersession claims.

---

### Phase 3 Engineering Checkpoint — `v0.3.3-infra` workflow cleanup (2026-06-30)

Non-feature release between W1.3 and W1.4 removing three recurring
engineering friction points discovered while shipping W1.1–W1.3, plus
the first engineering runbook. **Success metric:** W1.4 must require
fewer manual steps than W1.3.

#### Added
- **`backend/alembic/versions/0006_widen_alembic_version_num.py`** —
  Alembic migration widening `alembic_version.version_num` from the
  default `VARCHAR(32)` to `VARCHAR(255)`. The 32-char ceiling was hit
  by W1.3's natural revision ID `0005_distributed_locks_lease_check`
  (34 chars), which had to be renamed in-place to
  `0005_distributed_locks_lease` (28 chars) to fit. The widen removes
  the ceiling globally so W1.4's natural slug
  `0007_usage_records_request_id_unique` (35 chars) and every future
  Wave migration can use descriptive names without abbreviation
  gymnastics. The migration's own revision ID
  (`0006_widen_alembic_version_num`, 30 chars) fits the pre-existing
  limit, so it applies cleanly; the widen DDL and the row insert
  happen in the same `upgrade()` transaction, so no chicken-and-egg.
  Hand-written rather than via `alembic revision --autogenerate`
  because autogenerate does not emit DDL against system tables like
  `alembic_version`.
- **`docs/engineering/RUNBOOK_WAVE.md`** — first entry in a new
  `docs/engineering/` directory for repeatable engineering procedures.
  Six sections: Pre-flight, Development, Verification, Release,
  Recovery, Lessons Learned. Documents the Phase 3 Wave process that
  W1.1–W1.3 executed by hand (with minor per-Wave variation) so W1.4
  onwards can simply reference the runbook rather than have its ADR
  re-describe operational steps. Per `CONTRIBUTING.md` §6, ADRs
  answer WHY a decision was made; runbooks answer HOW it is executed.

#### Changed
- **`backend/scripts/ci_gate.py`** — stages 5–9 now load
  `DATABASE_URL` from `backend/.env.validation` automatically. The
  previous code path checked the FILE'S existence for the
  `db_available` flag but never actually loaded variables from it, so
  the `alembic`, `validate_schema`, and `regenerate_erd` subprocesses
  inherited an empty env and silently fell back to `alembic.ini`'s
  localhost URL — failing to reach Supabase. The 6-line fix imports
  the existing `_load_env.load()` function (already in
  `backend/scripts/_load_env.py` and used by every Step B validation
  script since Phase 2) and calls it when `db_available` but
  `DATABASE_URL` is not yet exported. Idempotent; safe to re-run; no
  behavioral change in GitHub Actions CI (where `DATABASE_URL` is set
  by the service container, so the conditional short-circuits).
- **`backend/scripts/run_ci_gate.ps1`** — header comment near the
  Python invocation clarifies that env loading happens inside
  `ci_gate.py` (no PowerShell-level `.env.validation` sourcing
  required). No behavioral change; the comment exists at the
  call-site so future contributors don't add redundant PowerShell
  env-load logic. Single source of truth for env loading is Python.
- **`.gitignore`** — replaced the partial Cursor ignore
  (`.cursor/state/` + `.cursor/cache/`, lines 114–116) with `.cursor/`
  (whole directory, single rule). The partial ignore left
  `.cursor/rules/`, `.cursor/automations/`, and any future
  Cursor-managed subdirectory exposed to `git add -A` sweeps, which
  caused a pre-commit incident during W1.3's amend cycle. No
  `.cursor/` content has ever been intentionally tracked in practice;
  if a specific rule ever needs sharing,
  `git add -f .cursor/rules/<file>` works for the deliberate case.
- **`CONTRIBUTING.md`** — §6 Documentation Policy extended with an
  "ADRs vs Runbooks (v0.3.3-infra)" paragraph codifying the
  convention: ADRs are for WHY (context, alternatives, consequences);
  runbooks are for HOW (step lists, commands, recovery actions).
  Cross-references `docs/engineering/RUNBOOK_WAVE.md` and notes that
  the W1.4 ADR (ADR-0033) will be the first to reference the runbook
  in place of inlining operational steps.
- **`ROADMAP.md`** — small engineering-checkpoint annotation between
  the Phase 3 wave table's W1 row and the surrounding "each wave
  produces its own ADR(s)" sentence, recording the `v0.3.3-infra`
  release and pointing at the new runbook. The Wave table itself is
  unchanged (the checkpoint is not a Wave).

#### Validated (live, 2026-06-30)
- **Pre-fix reproduction** — confirmed that the v0.3.2 `ci_gate.py`,
  when run from a shell where `DATABASE_URL` is unset, fails stage 5
  (`alembic upgrade head`) with `psycopg.OperationalError` despite
  `backend/.env.validation` being present. This was the original
  W1.2 symptom that required a manual PowerShell env-load workaround.
- **Post-fix reproduction** — same shell, no env vars set, no manual
  PowerShell loader: `scripts/ci_gate.py` reaches 10/10 stages green
  with `DATABASE_URL` loaded from `backend/.env.validation`
  automatically. The success metric — *will W1.4 require fewer manual
  steps than W1.3?* — is satisfied: zero manual steps for env
  loading.
- **Alembic round-trip** — `alembic upgrade head` (applies 0006,
  widens column); inspection of `\d alembic_version` confirms
  `version_num` is now `character varying(255)`; `alembic downgrade -1`
  returns the column to `character varying(32)`; `alembic upgrade head`
  re-applies cleanly (idempotency proven).
- **`.gitignore` enforcement** — `git status` after the new ignore is
  in place no longer lists `.cursor/` as untracked; `git add -A` no
  longer stages anything under `.cursor/`.
- **Full CI gate** — `scripts/ci_gate.py` 10/10 local; matching
  GitHub Actions run on the PR also 10/10 against the pgvector
  service container.

#### Not modified (scope discipline)
- **`backend/app/infrastructure/db/models/*.py`** — no ORM changes.
  `alembic_version` is Alembic's own bookkeeping table and is not
  modelled in the ORM (it is explicitly whitelisted out of the
  schema validator's table-parity check).
- **`docs/database/schema.md`** — no changes (`alembic_version` is
  intentionally not documented there; it is build infrastructure,
  not application data).
- **`docs/database/INDEX_STRATEGY.md`** — no changes (no new indexes
  or unique constraints; the column-type widen does not affect index
  counts).
- **`docs/database/ERD.md`** — no changes (ERD does not include
  `alembic_version`).
- **`DECISIONS.md`** — no new ADR. This is a workflow cleanup, not
  an architectural decision; the rationale lives in this CHANGELOG
  entry and in `docs/engineering/RUNBOOK_WAVE.md` §6 Lessons Learned.
- **`backend/alembic/versions/0001_baseline.py` through
  `0005_distributed_locks_lease.py`** — none amended in place; the
  column widen is added entirely by `0006` and reverted on its
  `downgrade()`.

#### Scope discipline (per the v0.3.3-infra PR scope rule)
- Every changed file either implements one of the five engineering
  improvements (env-load fix, alembic widen, gitignore, runbook,
  ADRs-vs-runbooks convention) or documents the release (this
  CHANGELOG entry, the ROADMAP annotation).
- No feature work. No schema changes other than the
  `alembic_version` column widen. No API changes. No refactors. No
  opportunistic cleanup. No "while we're here" edits.

### Phase 3 Wave 1.3 — `distributed_locks` lease CHECK (2026-06-29, ADR-0032)

#### Added
- **`backend/alembic/versions/0005_distributed_locks_lease.py`** —
  Alembic migration adding a single CHECK constraint
  `chk_distributed_locks_lease_until_after_acquired_at` enforcing
  `lease_until > acquired_at`. Strict greater-than (`>`, not `>=`)
  rejects the degenerate zero-second lease that a buggy `$lease = 0` or
  negative-`$lease` call site would produce. Hand-written rather than via
  `alembic revision --autogenerate` because autogenerate does not
  reliably preserve the exact text of CHECK expressions. Smallest W1.x
  migration to date: one `ALTER TABLE … ADD CONSTRAINT` in `upgrade()`,
  one `ALTER TABLE … DROP CONSTRAINT` in `downgrade()`. Forward + reverse
  + idempotency round-trip validated against Supabase Postgres 17.6 +
  pgvector 0.8.0 via `backend/.env.validation`.
- **`docs/decisions/ADR-0032-distributed-locks-lease-check.md`** — third
  file-per-ADR under `docs/decisions/` (ADR-0030 was the first,
  ADR-0031 the second). Records the promotion of the §37 Q10 invariant
  verbatim — no bundling with `lease_until >= heartbeat_at` or other
  temporal-anchor invariants (those remain future-ADR territory). 7
  alternatives considered, 3-tier rollback plan, 19-item acceptance
  criteria including an explicit pre-upgrade safety SELECT.
- **`DECISIONS.md`** — one-line cross-link entry for ADR-0032 pointing
  at the new file (status flips from `Proposed` → `Accepted` in the
  pre-merge `docs(adr): mark ADR-0032 Accepted` commit).

#### Changed
- **`backend/app/infrastructure/db/models/operations.py`** —
  `DistributedLock.__table_args__` extended with the matching
  `CheckConstraint("lease_until > acquired_at", name="chk_distributed_locks_lease_until_after_acquired_at")`
  declaration so the ORM mirrors the migration exactly. No import changes
  needed; `CheckConstraint` was already imported during W1.2 for
  `IdempotencyKey`. The existing `Index("ix_distributed_locks_lease_until", "lease_until")`
  is preserved unchanged; the new `CheckConstraint` is placed
  immediately before it inside the `__table_args__` tuple per the
  W1.2 ordering precedent (constraint before index).
- **`docs/database/schema.md`** — §32 column block now lists the CHECK
  constraint inline; new **Lease validity invariant (DB-enforced,
  Phase 3 W1.3)** paragraph mirrors §31's W1.2 FSM-invariant paragraph
  and explains the single-predicate scoping decision (and why
  `lease_until >= heartbeat_at` is intentionally deferred); §32
  reconciliation note revised to acknowledge that W1.3 reverses the 2D
  deferral with stated reasoning (the original "harder to diagnose"
  argument inverts in practice once the CHECK has a descriptive name);
  §37 Q10 row marked **Resolved (Phase 3 W1.3, 2026-06-29)** with full
  constraint details; §37 epilogue Wave 1 bullet for §32 q10 marked
  ✅ Done.
- **`ROADMAP.md`** — Phase 3 wave table W1 row annotated with W1.3
  ✅ Complete alongside W1.1 and W1.2; remaining W1.4 split out.

#### Validated (live, 2026-06-29)
- **Pre-upgrade safety check** — `SELECT COUNT(*) FROM
  distributed_locks WHERE NOT (lease_until > acquired_at)` against
  Supabase returned `0`, clearing the gate for `alembic upgrade head`.
  Zero existing rows in the live target, so the gate is trivially
  satisfied — but the SELECT is run for audit-trail completeness and
  to verify the production-rollback variant (`ADD CONSTRAINT … NOT
  VALID` + later `VALIDATE CONSTRAINT`) is not required.
- **Alembic round-trip** — `upgrade head → downgrade -1 → upgrade head`
  clean; `pg_constraint` diff = exactly one CHECK added on forward,
  exactly one removed on reverse; the constraint's `consrc` predicate
  reads exactly `lease_until > acquired_at`.
- **Schema validator** — `scripts/validate_schema.py` 9/9 (no validator
  code change; none of the 9 structural checks inspect CHECK
  constraints by name — the table-parity check passes by construction
  because the ORM and DB agree on the column shape, which is unchanged
  by W1.3).
- **ERD round-trip** — `regenerate_erd.py` + `compare_erd.py` 0 drift
  (ERD ignores CHECK constraints).
- **Full CI gate** — `scripts/ci_gate.py` 10/10 local; matching GitHub
  Actions run on the PR also 10/10 against the pgvector service
  container.

#### Not modified (scope discipline)
- **`docs/database/INDEX_STRATEGY.md`** — no changes (W1.3 adds zero
  indexes and zero unique constraints; only a CHECK constraint, which
  `INDEX_STRATEGY.md` does not track; the 87-index count stays at 87).
- **`docs/database/ERD.md`** — no changes (ERD tracks entities and FKs
  only; CHECK constraints are invisible to it; the 51-entity / 60-edge
  count stays unchanged).
- **`CONTRIBUTING.md`** — no changes (the file-per-ADR convention was
  already documented in W1.1; ADR-0032 is the third adopter, not the
  convention-establisher).
- **`backend/alembic/versions/0001_baseline.py`** — baseline migrations
  are historical and never amended in place; the new CHECK is added
  entirely by migration `0005` and dropped on its downgrade.

#### Scope discipline (per Wave 1 isolation constraint)
- No changes to `export_jobs` (W1.1 territory), `idempotency_keys`
  (W1.2), or `usage_records` (W1.4). W1.4 gets its own branch + ADR.

### Phase 3 Wave 1.2 — `idempotency_keys` mutability + status↔response invariant (2026-06-29, ADR-0031)

#### Added
- **`backend/alembic/versions/0004_idempotency_keys_invariants.py`** — Alembic
  migration applying three coordinated changes to `idempotency_keys` in a
  single transaction: (1) `ADD COLUMN updated_at timestamptz NOT NULL
  DEFAULT now()`, (2) `CREATE TRIGGER tg_idempotency_keys_biu_touch_updated_at`
  bound to the shared `touch_updated_at()` function (already defined in
  the baseline, already wired to 30+ other tables), and (3) `ADD
  CONSTRAINT chk_idempotency_keys_response_hash_matches_status CHECK
  ((status = 'in_flight') = (response_hash IS NULL))`. Hand-written
  rather than via `alembic revision --autogenerate` because autogenerate
  does not emit `CREATE TRIGGER` statements and would not preserve the
  exact text of the CHECK expression or the explicit sequencing of the
  three ops. Forward + reverse + idempotency round-trip validated
  against Supabase Postgres 17.6 + pgvector 0.8.0 via
  `backend/.env.validation`.
- **`docs/decisions/ADR-0031-idempotency-keys-invariants.md`** — second
  file-per-ADR under `docs/decisions/` (ADR-0030 was the first). Records
  the promotion of two long-standing application-layer assumptions to
  the DB: the mixin misclassification that left `idempotency_keys` in a
  "mutable-but-untracked" state, and the unprotected status↔response
  FSM invariant. 8 alternatives considered, 3-tier rollback plan, 17-item
  acceptance criteria including an explicit pre-upgrade safety SELECT.
- **`DECISIONS.md`** — one-line cross-link entry for ADR-0031 pointing
  at the new file (status flips from `Proposed` → `Accepted` in the
  pre-merge `docs(adr): mark ADR-0031 Accepted` commit).

#### Changed
- **`backend/app/infrastructure/db/models/operations.py`** — `IdempotencyKey`
  switched from `CreatedAtOnlyMixin` to `TimestampMixin` (the original
  mixin choice was a Phase 2 Step-A misclassification — `CreatedAtOnlyMixin`
  is documented as "for immutable / append-only tables" but the row IS
  mutated `in_flight → succeeded`/`failed`). `__table_args__` extended
  with the matching `CheckConstraint(..., name="chk_idempotency_keys_response_hash_matches_status")`
  declaration so the ORM mirrors the migration exactly. `CheckConstraint`
  added to the SQLAlchemy import line; `CreatedAtOnlyMixin` removed
  from the mixins import.
- **`docs/database/schema.md`** — §31 column block now lists
  `updated_at`; new **FSM invariant (DB-enforced, Phase 3 W1.2)**
  paragraph explains the CHECK's scope decision (`response_hash` only,
  not `response_payload` or `http_status`); §31 reconciliation note
  updated to acknowledge that W1.2 reverses the 2D `updated_at`
  omission with stated reasoning (the original "audit event covers it"
  rationale conflated audit replay with operational observability);
  §37 Q9 row marked **Resolved (Phase 3 W1.2, 2026-06-29)**; Wave 1
  bullet for §31 q9 marked ✅ Done.
- **`ROADMAP.md`** — Phase 3 wave table W1 row annotated with
  W1.2 ✅ Complete alongside W1.1; remaining W1.3 / W1.4 split out.

#### Validated (live, 2026-06-29)
- **Pre-upgrade safety check** — `SELECT COUNT(*) FROM idempotency_keys
  WHERE (status = 'in_flight') <> (response_hash IS NULL)` against
  Supabase returned `0`, clearing the gate for `alembic upgrade head`.
- **Alembic round-trip** — `upgrade head → downgrade -1 → upgrade head`
  clean; `pg_constraint` diff = exactly one CHECK added on forward,
  exactly one removed on reverse; `pg_trigger` diff = exactly one BIU
  trigger added on forward, exactly one removed on reverse;
  `information_schema.columns` confirms `updated_at` is
  `timestamp with time zone NOT NULL` after upgrade and gone after
  downgrade.
- **Schema validator** — `scripts/validate_schema.py` 9/9 (no validator
  code change; `check_table_parity` picked up the new `updated_at`
  column automatically from ORM metadata; no validator check covers
  CHECK constraints or `_UPDATED_AT_TABLES` membership, so those
  remain green by construction).
- **ERD round-trip** — `regenerate_erd.py` + `compare_erd.py` 0 drift
  (ERD ignores CHECK constraints, triggers, and per-column shape; only
  entity-level changes show up there).
- **Full CI gate** — `scripts/ci_gate.py` 10/10 local; matching GitHub
  Actions run on the PR also 10/10 against the pgvector service container.

#### Not modified (scope discipline)
- **`docs/database/INDEX_STRATEGY.md`** — no changes (W1.2 adds zero
  indexes and zero unique constraints; only a column, a trigger, and a
  CHECK constraint — none of which `INDEX_STRATEGY.md` tracks).
- **`CONTRIBUTING.md`** — no changes (the file-per-ADR convention was
  already documented in W1.1; ADR-0031 is the second adopter, not the
  convention-establisher).
- **`backend/alembic/versions/0001_baseline.py`** — baseline migrations
  are historical and never amended in place; the new trigger is added
  entirely by migration `0004` and dropped on its downgrade.

#### Scope discipline (per Wave 1 isolation constraint)
- No changes to `export_jobs` (W1.1 territory), `distributed_locks`
  (W1.3), or `usage_records` (W1.4). W1.3 / W1.4 each get their own
  branch + ADR.

### Phase 3 Wave 1.1 — `export_jobs` partial-unique constraint (2026-06-29, ADR-0030)

#### Added
- **`backend/alembic/versions/0003_export_jobs_partial_unique.py`** — Alembic
  migration creating the partial-unique index
  `uq_export_jobs_render_job_id_format_quality_orientation` on
  `export_jobs (render_job_id, format, quality, orientation)` with
  `WHERE status IN ('queued','running','succeeded')`. Hand-written rather
  than via `alembic revision --autogenerate` because autogenerate does not
  reliably emit partial-unique indexes via `postgresql_where` (it produces
  a vanilla unique constraint instead). Forward + reverse + idempotency
  round-trip validated against Supabase Postgres 17.6 + pgvector 0.8.0
  via `backend/.env.validation`.
- **`docs/decisions/ADR-0030-export-jobs-partial-unique.md`** — first
  file-per-ADR under the new `docs/decisions/` directory. Records the
  promotion of the `(render_job_id, format, quality, orientation)`
  uniqueness invariant from the use-case layer (where it had no consumer
  yet) directly to the database, with full rationale, 7 rejected
  alternatives, 3-tier rollback plan, and 15-item acceptance criteria.
  ADRs 0001–0029 remain inline in `DECISIONS.md`; all Phase-3-and-later
  ADRs use the file-per-ADR convention.
- **`DECISIONS.md`** — one-line cross-link entry for ADR-0030 pointing
  at the new file (status flips from `Proposed` → `Accepted` in the
  pre-merge `docs(adr): mark ADR-0030 Accepted` commit).

#### Changed
- **`backend/app/infrastructure/db/models/jobs.py`** — `ExportJob.__table_args__`
  extended with the matching `Index(..., unique=True, postgresql_where=text(...))`
  declaration so the ORM mirrors the migration exactly. Same shape as
  the existing partial-unique pattern used across the model layer.
- **`docs/database/schema.md`** — §17 reconciliation note for `export_jobs`
  flipped from "Phase-3 decision" to "Implemented via ADR-0030 / migration
  `0003`"; §37 Q8 row marked **Resolved (Phase 3 W1.1, 2026-06-29)**;
  Wave 1 bullet for §17 q8 marked ✅ Done.
- **`docs/database/INDEX_STRATEGY.md`** — §8 `export_jobs` row moved
  **Deferred (Phase 3)** → **Implemented** with full predicate spelled out;
  §18 reconciliation summary counts updated (indexes 81 → 82,
  unique constraints 23 → 24, Implemented rows 73 → 74,
  Deferred (Phase 3) 21 → 20).
- **`ROADMAP.md`** — Phase 3 wave table W1 row annotated with
  W1.1 ✅ Complete and the remaining W1.2 / W1.3 / W1.4 split out.
- **`CONTRIBUTING.md`** — §1 ground rule 2 and §6 documentation policy
  updated to acknowledge the new `docs/decisions/` file-per-ADR
  convention (introduced by ADR-0030) while preserving compatibility
  with the inline ADRs 0001–0029 in `DECISIONS.md`.

#### Validated (live, 2026-06-29)
- **Pre-upgrade safety check** — `SELECT COUNT(*) FROM export_jobs WHERE
  status IN ('queued','running','succeeded')` against Supabase returned
  `0`, clearing the gate for the in-development upgrade path.
- **Alembic round-trip** — `upgrade head → downgrade -1 → upgrade head`
  clean; `pg_indexes` diff = exactly one row added on forward, exactly
  one row removed on reverse; `indexdef` contains the expected
  `WHERE … status = ANY` predicate.
- **Schema validator** — `scripts/validate_schema.py` 9/9 (no validator
  code change; `check_unique_constraints` and `check_indexes` picked up
  the new `Index(unique=True, postgresql_where=…)` automatically from
  ORM metadata).
- **ERD round-trip** — `regenerate_erd.py` + `compare_erd.py` 0 drift
  (ERD ignores non-FK indexes).
- **Full CI gate** — `scripts/ci_gate.py` 10/10 local; matching GitHub
  Actions run on the PR also 10/10 against the pgvector service container.

#### Scope discipline (per Wave 1 isolation constraint)
- No changes to `idempotency_keys`, `distributed_locks`, or
  `usage_records`. W1.2 / W1.3 / W1.4 each get their own branch + ADR.

### Phase 2D — Documentation Reconciliation (2026-06-29, approved by reviewer; no code changes)

#### Verification
- **Manual spot-check** (8/8 MATCH) — `PHASE2D_SPOT_CHECK.md`. Eight
  models (`tenants`, `projects`, `project_tags`, `workflow_runs`,
  `usage_records`, `credit_ledger`, `audit_log`, `provider_settings`)
  compared by hand against ORM, baseline migration, `schema.md`,
  `ERD.md`, and `INDEX_STRATEGY.md`. Zero semantic mismatches.
- **CI quality gate** — re-run with no code changes; 10/10 stages
  green (5 non-DB + 5 DB; oracle migration round-trip clean; schema
  validator 9/9; ERD compare 0 drift; coverage 100% over Phase 2 scope).
- **Phase 3 wave sequencing** recorded in `ROADMAP.md` and
  `schema.md` §37 (Waves 1–4).
- **Baseline tag (pre-flight)** — deferred to the user. Workspace is
  not yet a git repository; exact `git init`/commit/tag command
  sequence recorded in `ROADMAP.md` Phase 3 Pre-flight section.


#### Changed (docs only)
- **`docs/database/schema.md`** — added a top-of-doc audit-of-truth rule
  ("implementation is the source of truth"); reconciled §16 (workflow
  runs/steps/checkpoints), §17 (render/export jobs), §18 (usage records),
  §19 (cost reconciliations), §20 (plans/subscriptions/invoices), §22
  (feature flags), §25 (event outbox), §26 (event log), §27 (system /
  tenant / provider settings), §31 (idempotency keys), §32 (distributed
  locks), and §33 (audit log) to match the validated ORM column shapes,
  FK shapes, and indexes. Each section carries an inline
  "Reconciled in 2D" note documenting what changed and why. Added §37
  cataloguing the 13 questions deferred to Phase 3 entry (relationship()
  pattern, deferred indexes, `cost_reconciliations` immutability,
  `auth_role` enum retention, ERD cross-cluster elision policy, …).
- **`docs/database/ERD.md`** — added a top-of-doc reconciliation note;
  rewrote the column shapes in Cluster 6 (workflows / render / export),
  Cluster 7 (usage records / cost reconciliations), Cluster 8 (billing),
  Cluster 9 (feature flags / event outbox / event log), and Cluster 10
  (config / operations / audit) to match the ORM. Cross-cluster FK
  elision policy made explicit so `compare_erd.py` continues to report
  zero design-edge drift.
- **`docs/database/INDEX_STRATEGY.md`** — full rewrite. Every row is now
  labeled `Implemented` (matches an ORM index by name), `Renamed` (the
  design name differed; row updated to the actual ORM name), or
  `Deferred (Phase N)` with a Phase-3 entry decision attached. Added
  §16 (Phase 3 index decisions) and §18 (reconciliation summary:
  81 implemented indexes + 23 unique constraints).
- **`docs/database/BACKUP_RESTORE.md`** — `_backup_sentinel` column
  shape updated from the draft `(taken_at, marker)` to the shipped
  `(inserted_at, label, notes)`.
- **`DECISIONS.md`** — renumbered the second ADR-0028 to **ADR-0029**
  ("CI Quality Gate Operational Contract — Phase 2C Ratification") to
  resolve the duplicate ADR id surfaced by the architectural audit.
  ADR-0028 retains its original content. ADR-0029's Context paragraph
  notes the renumber explicitly.

#### Not changed (deferred to Phase 3 entry by reviewer rule)
- ORM models / Alembic migrations / database schema / seed data / CI
  gate remained untouched. The validation harness (`validate_schema.py`)
  and ERD round-trip continue to pass with the same 81 indexes,
  95 FKs, 52 base tables. The architectural audit's recommendations on
  `relationship()` adoption, additional indexes, `cost_reconciliations`
  immutability, `auth_role` retention, and cross-cluster ERD edges
  were deliberately left as Phase-3-entry questions per the reviewer's
  guidance.

### Phase 2C — CI Quality Gate (implementation complete, awaiting reviewer)

#### Added
- **`backend/scripts/ci_gate.py`** — cross-platform 10-stage runner
  (ruff → black → mypy + import-linter → pytest+cov → alembic up → down
  → up → validator → ERD diff → coverage threshold). Stages 5–9 are
  skipped (not failed) when `DATABASE_URL` is absent so the
  laptop-no-Postgres path still works.
- **`backend/scripts/run_ci_gate.ps1`** — PowerShell wrapper for Windows
  developers; thin convenience layer over `ci_gate.py` with stage-range
  pass-through and credential redaction in the banner.
- **`.github/workflows/ci.yml`** — GitHub Actions wiring: triggers on
  PRs and pushes to `main`, runs against a `pgvector/pgvector:pg16`
  service container, uploads validator + ERD + coverage artefacts, and
  appends the coverage report to the job summary.
- **`backend/tests/`** — Phase 2C smoke suite (24 tests, **100 % branch
  coverage** on `app/` for Phase 2C scope):
  - `test_models_import.py` — every model module imports; metadata
    contains the expected aggregate-root subset; `Base` is declarative
    and shares the canonical metadata.
  - `test_metadata.py` — partitioned parents declare
    `postgresql_partition_by`; every FK declares an explicit
    `ON DELETE`; immutable tables have no `updated_at`/`deleted_at`;
    pgvector is scoped to the two approved columns; naming convention is
    populated; no naive `DateTime` columns.
  - `test_mixins.py` — UUID PK, timestamp, soft-delete, version, and
    created-at-only mixins all expose the documented column shapes; the
    UUID PK Python default is the `uuid.uuid4` factory (verified by
    `__module__` + `__qualname__` to survive import-system reloads).
  - `test_enums.py` — enum count pinned at 26, all `native_enum=True`,
    all values lowercase snake_case, no duplicate values, no PG type
    name collisions.
- **`backend/pyproject.toml`** — `black`, `pytest`, `pytest-cov`,
  `pytest-asyncio`, `types-PyYAML`, `import-linter` added to
  `[project.optional-dependencies.dev]`; configs added for
  `[tool.black]`, `[tool.pytest.ini_options]`, `[tool.coverage.*]`,
  `[tool.importlinter]`; existing `[tool.ruff]` extended with
  `SIM/C4/RUF` rule sets and per-file ignores for migrations / tests /
  scripts; `[tool.mypy]` narrowed to `app/` only with strict mode
  preserved.
- **`CI_QUALITY_GATE.md`** — stage map, runtime budgets, local
  invocation contract, failure runbook, and coverage threshold roadmap
  (60 % → 80 % → 85 % across phases).
- **`DECISIONS.md` ADR-0028** — "Mandatory CI Quality Gate Before
  Phase 3" (ratified at close of Phase 2 Step B).
- **Architectural fitness contracts** (`import-linter`, 4 contracts):
  domain layer has no infra / app / api deps; DB models cannot import
  app / api; api layer cannot import infra directly; application layer
  never imports api.
- **`backend/app/{domain,application,api}/__init__.py`** — empty
  package skeletons created at the close of Phase 2 so the
  architectural contracts are live the moment any Phase 3 code lands.

#### Changed
- **`backend/app/infrastructure/db/models/*.py`** — 39 `Mapped[dict]` /
  `Mapped[list]` annotations parameterised to `Mapped[dict[str, Any]]`
  / `Mapped[list[Any]]` (resolved 39 of 44 mypy `--strict` errors);
  three unused `# type: ignore[assignment]` comments removed from
  pgvector fallback branches.
- **`backend/scripts/ci_gate.py`** stage 3 — now invokes both `mypy`
  and `lint-imports` (previously only `mypy` despite the title); the
  `lint-imports` entrypoint is resolved relative to the active venv to
  avoid PATH surprises.

#### Self-tested (local, 2026-06-29)
- Stages 1–4 (lint / format / static analysis / tests + coverage):
  **green** — 24 tests pass, mypy 0 errors, lint-imports 0 violations.
- Stages 8–10 (live schema validator / ERD diff / coverage threshold):
  **green** against Supabase Postgres 17.6 + pgvector 0.8.0 — 9/9
  structural checks pass, 51/51 entities + 58/58 design edges in ERD
  round-trip, coverage 100 % over the 22 `app/` modules currently in
  scope (well above the 60 % Phase 2C threshold).
- Stages 5–7 (alembic up/down/up): deliberately not re-exercised in the
  self-test to avoid re-running migrations against the live target;
  wired identically to the proven Step B validation path and will
  execute against the pgvector service container in CI.

#### Pending (Phase 2C exit criteria)
- Reviewer sign-off on `CI_QUALITY_GATE.md` + ADR-0028 → unlocks
  Phase 3.

---

### Phase 2 — Database, Step B: SQLAlchemy + Alembic — ✅ APPROVED 2026-06-28

#### Added
- `backend/pyproject.toml`, `backend/alembic.ini`, `backend/alembic/env.py`,
  `backend/alembic/script.py.mako`.
- Declarative base + naming convention (`app/infrastructure/db/base.py`).
- Reusable mixins: `UUIDPrimaryKeyMixin`, `TimestampMixin`,
  `SoftDeleteMixin`, `VersionMixin`, `CreatedAtOnlyMixin`.
- Central ENUM registry (`app/infrastructure/db/enums.py`).
- 23 ORM model files (`app/infrastructure/db/models/*.py`) covering every
  table in `docs/database/schema.md`.
- Alembic baseline migration `0001_baseline.py` — extensions, ENUMs,
  helper PL/pgSQL functions, all tables, indexes (incl. imperative
  GIN / HNSW), triggers (`touch_updated_at`, `bump_version`,
  `reject_mutation`, `enforce_credit_ledger_balance`), partition
  bootstrap (current month + 24 forward months + default partitions),
  and the deferred `projects.current_version_id` FK.
- Alembic seed migration `0002_seed_system_data.py` — plans, feature
  flags, provider plugins, AI model catalogue, RBAC roles, and the
  initial system settings rows. Idempotent via `ON CONFLICT DO NOTHING`.
- Schema validator (`backend/scripts/validate_schema.py`) — 9 automated
  checks covering extensions, tables, partitions, FKs, unique
  constraints, indexes, immutability triggers, pgvector column scope,
  and the credit_ledger balance trigger.
- ERD regenerator (`backend/scripts/regenerate_erd.py`) — Mermaid output
  for stable diffs against `docs/database/ERD.md`.
- One-command orchestrator (`backend/scripts/run_validation.py` and
  PowerShell wrapper `run_validation.ps1`) implementing the
  upgrade → downgrade → re-upgrade → introspect → ERD-regenerate cycle.
- `backend/docker-compose.db.yml` — local pgvector Postgres 16.
- `SCHEMA_VALIDATION.md` — methodology, checks, run instructions,
  pending live-run section.
- `PROJECT_STATUS.md` — living project status with version, milestones,
  debt, risks, open questions, and step-level checklist.
- ADR-0027 — Tenant-Scoped Billing Aggregates (`DECISIONS.md`).

#### Changed (during live validation)
- Validator rewritten against `pg_catalog`: a single `load_snapshot(engine)`
  pulls every base table, FK, and index in three bulk queries; per-check
  functions consume the cached snapshot instead of issuing ~400 per-table
  `inspect()` round-trips. Validator runtime against the Supabase pooler:
  **263 s → 17 s.**
- ERD regenerator rewritten against `pg_catalog`; partition children
  excluded at the SQL level so the FK query no longer hits Supabase's
  2-minute `statement_timeout`. ERD generation: **>120 s timeout → 13 s.**
- `alembic_version` whitelisted in `validate_schema.py`'s table-parity check
  (it's Alembic's own bookkeeping; not in the ORM `metadata`).
- `validate_schema.py` redacts the password from the connection URI in
  `schema_validation_report.json`.
- `alembic/env.py` doubles `%` in URL-encoded passwords before handing the
  URI to ConfigParser (fixes `%40` → `@` round-tripping for Supabase URIs).
- Credentials now loaded via `_load_env.py` from `backend/.env.validation`
  (git-ignored); never appear on the shell command line.
- `docs/database/ERD.md` Cluster 8 (Billing) corrected: subscriptions are
  tenant-scoped (not user-scoped); invoices are subscription-scoped;
  `users → credit_ledger` is nullable (SET NULL).
- `docs/database/ERD.md` Cluster 7 (Media/Library): direction of the
  `media_assets ↔ library_assets` edge corrected (library_assets has the
  FK, not the other way around).
- `docs/database/ERD.md` Clusters 5/9: `provider_plugin_registrations →
  ai_models` and `event_outbox → event_log` converted to Mermaid comments
  (logical references — no DB FK).
- `docs/database/schema.md` §20–§21 corrected to match the implementation
  (subscriptions/invoices have no `user_id` column; credit_ledger.user_id
  is nullable with SET NULL).

#### Validated (live, 2026-06-28)
- Target: Supabase managed PostgreSQL 17.6 + pgvector 0.8.0
  (ap-northeast-2 session pooler, IPv4).
- `alembic upgrade head` ✅; `alembic downgrade base` ✅
  (only `alembic_version` retained); `alembic upgrade head` again ✅
  (idempotency proven).
- All 9 structural checks pass: 5 required extensions, 52 ORM tables,
  4 partitioned parents (27 children each), 95 FKs, all declared
  unique indexes, 86 indexes including 5 imperative GIN/HNSW,
  8 immutable-trigger-protected tables, exactly 2 pgvector columns,
  `credit_ledger` balance trigger present.
- ERD round-trip: 51/51 entities match; 58/58 design-declared edges
  present in implementation; 0 design edges missing.

#### Pending
- Reviewer sign-off on `SCHEMA_VALIDATION.md` §6.

### Phase 2 — Database, Step A: Design Documents (APPROVED 2026-06-28, revision 2)

#### Added (initial)
- `docs/database/NAMING_CONVENTIONS.md`
- `docs/database/ERD.md` (Mermaid ER diagram covering every aggregate root)
- `docs/database/schema.md` (full table-by-table schema with FKs / ON DELETE / uniqueness / checks)
- `docs/database/INDEX_STRATEGY.md`
- `docs/database/RETENTION_POLICY.md`
- `docs/database/BACKUP_RESTORE.md`

#### Added (revision 2 — final design CRs)
- **CR-DB-1** First-class Idempotency Framework — `idempotency_keys` table (ADR-0021).
- **CR-DB-2** Database-backed Distributed Locks — `distributed_locks` table with lease + heartbeat (ADR-0022).
- **CR-DB-3** Audit Log — partitioned, immutable `audit_log` table separate from `event_log`, Class C retention (ADR-0023).
- **CR-DB-4** Explicit Configuration Tables — `system_settings`, `tenant_settings`, `provider_settings`; generic `settings` table removed (ADR-0024).
- ADR-0025 — defer `user_preferences` to `users.extra` JSONB.
- ERD cluster 10 (Configuration & Operations) added.
- Index strategy §14a/§14b/§14c added.
- Retention policy updated: `audit_log` → Class C (7 years); `idempotency_keys` / `distributed_locks` → TTL classes.
- Immutability verification job now also covers `audit_log` and `cost_reconciliations`.

#### Pending
- Step A review and approval → unlocks Step B (SQLAlchemy models + Alembic baseline) following the execution order recorded in `ROADMAP.md` Phase 2 Step B.

---

## [Phase 1 — 2026-06-28] — Architecture & Folder Structure (Rev 3, APPROVED)

#### Added
- `rule.md` — governing requirements document with anti-hallucination guardrails.
- `ARCHITECTURE.md` — full system architecture, folder structure, and tech decisions (rev 3).
- `ROADMAP.md` — phased delivery plan with explicit exit criteria.
- `DECISIONS.md` — twenty ADRs (ADR-0001 … ADR-0020).
- `CONTRIBUTING.md` — coding standards and contribution workflow.
- `API_CONTRACT.md` — API surface designed before implementation.
- **CR-1** AI Provider Plugin System (`BasePlugin` + capability ABCs + `@register_plugin`).
- **CR-2** Multiple Rendering Pipelines (Pipeline A stock-footage, B AI-images-motion, C AI-video-clips).
- **CR-3** Split AI orchestration into seven subpackages: `agents`, `providers`, `prompts`, `memory`, `tools`, `chains`, `workflows`.
- **CR-4** Event Bus (Redis Streams default, NATS/Kafka pluggable) with canonical topic registry and transactional outbox.
- **CR-5** Multi-storage Provider plugins (Local / S3 / R2 / Azure Blob / GCS).
- **CR-6** Versioned Projects — immutable `ProjectVersion` snapshots, branching, restore.
- **CR-7** Resumable Workflow Engine with Postgres checkpointer.
- **CR-8** Asset Library — auto-persist every generated artefact.
- **CR-9** Feature Flags — pluggable provider, default DB-backed, optional Unleash.
- **CR-10** Explicit Domain Layer — framework-free `app/domain/` with named aggregate roots.
- **CR-11** AI Model Registry — model catalogue, deprecation lifecycle, default-selection chain.
- **CR-12** AI Cost Tracking — single recorder middleware producing immutable `UsageRecord` per call.
- **CR-13** Five-tier Priority Queues — `critical / high / normal / low / background` with tenant fairness.

#### Approved
- 2026-06-28 — User approved Phase 1 Rev 3; Phase 2 unlocked.

---

## How to Update This Changelog

When a phase is accepted:

1. Move the **Unreleased** section into a new dated entry: `## [Phase N — YYYY-MM-DD]`.
2. Group changes under: **Added**, **Changed**, **Deprecated**, **Removed**, **Fixed**, **Security**.
3. Reference ADRs and CRs by ID.
4. Start a fresh **[Unreleased]** block.

Format example:

```
## [Phase 2 — 2026-MM-DD] — Database

### Added
- Alembic baseline migration.
- ORM models for every aggregate root listed in `ARCHITECTURE.md` §6.
- pgvector extension.

### Security
- Per-row `tenant_id` enforced via DB-level row-level security policies.
```
