# Phase 3 Slice α7.5 — Usage Recorder (priced `usage_records` writes · token/image/video accounting · idempotent-on-`request_id`) — Pre-flight

> Status: **SIGNED OFF (2026-07-18)** — see §6. All eight §4 questions approved
> with a richer `RecordUsageCommand` (Q3: adds `project_id` + `render_job_id`) and
> one added invariant (**W7.5.1 — the recorder is purely observational**). The
> provider-runtime architecture and its runtime decisions (D1–D14) were signed
> off in [`ADR-0041`](../decisions/ADR-0041-provider-runtime-contract.md); the
> usage/cost seam is **D13**. The DB-level idempotency guarantee this slice
> writes against was accepted in
> [`ADR-0033`](../decisions/ADR-0033-usage-records-request-id-unique.md). This
> doc resolves the **α7.5-specific** open questions (§4). Nothing is implemented
> yet.
>
> Mirrors the α5/α6/α7.1/α7.2/α7.3/α7.4 discipline: ground in the existing
> contract + code → lock decisions → sign-off → branch → implement → CI → merge →
> tag. Read-only planning artefact.
>
> **Predecessor.** α7.4 (`v0.4.18`, tag `v0.4.18-phase3-alpha7.4`) — the provider
> skeleton: capability ports, registry, dispatcher, deterministic mocks, and the
> neutral provider contract (`ProviderResponse` / `ProviderUsage`). α7.4 delivered
> the seam **but deliberately deferred usage accounting** — `ProviderUsage` is
> *defined there, consumed here* (α7.4 pre-flight §1.3, §5; the `ProviderUsage`
> docstring literally says "the seam the α7.5 Usage Recorder prices").
>
> **This is the accounting slice — it activates persistence that already exists.**
> Per ADR-0041 D13 and the user-approved scope, α7.5 turns each *terminal*
> provider call into **exactly one immutable, priced `usage_records` row**
> (ADR-0019), idempotent on `request_id` (ADR-0033), pricing it against
> `ai_model_pricing` (CR-11). It builds:
>
> 1. **`UsageRecorderPort`** — the application-layer seam the α7.6 pipeline calls
>    to record one call's usage (the future "decorator around the adapter",
>    D13/§8k, but shipped as an explicit seam per runner-before-worker — §4 Q2).
> 2. **`UsageRecorderService`** — the imperative shell: resolve price → compute
>    cost + accounting axes → idempotent insert → return the record. Mirrors the
>    α7.3 `RelayService` structure exactly.
> 3. **Cost calculation + token/image/video accounting** — map the neutral
>    `ProviderUsage` onto the real (leaner-than-§8k) `usage_records` columns and
>    price it against the effective `ai_model_pricing` rows.
> 4. **Idempotent writes** — one row per `request_id` (ADR-0033 per-partition
>    partial-unique); a duplicate is a no-op that returns the existing row.
> 5. **Repository implementations** — `IUsageRecordRepository` (insert +
>    get-by-`request_id`) and a read-only `IModelPricingRepository` (resolve the
>    effective price), plus UoW + DI wiring.
> 6. **Deterministic unit tests** (cost math, accounting, mapping, idempotency
>    logic against fakes) + **integration tests** (real partitioned
>    `usage_records`, real pricing resolution, real duplicate-`request_id`
>    idempotency).
>
> Per the runner-before-worker discipline (ADR-0041 D11), α7.5 ships these as
> **library seams driven by tests** — **no HTTP, no Celery, no Redis, no polling,
> no provider SDKs, no OpenAI/Gemini, no credit-ledger debit (§4 Q1), no wiring
> into the runner/dispatcher (§4 Q2).** The recorder is connected end-to-end in
> α7.6 (first pipeline); real providers arrive in α8.1+.
>
> **Baseline versioning.** `main` is at `0.4.18` (tag `v0.4.18-phase3-alpha7.4`).
> First α7.5 commit bumps `backend/app/main.py` → `"0.4.19-phase3-alpha7.5-dev"`.
> **Zero migrations** (ADR-0041 D14) — every table α7.5 touches (`usage_records`,
> `ai_model_pricing`) already exists in baseline `0001`, with the per-partition
> `request_id` unique added by `0007` (ADR-0033). Their ORMs (`UsageRecord`,
> `AIModelPricing`) are already mapped.

---

## Section 1 — Scope

### 1.1 One-line thesis

α7.5 establishes the **usage/cost accounting seam**: a port + service that takes
one terminal provider call (its resolved `model_id`, billing context, and the
α7.4 `ProviderUsage`) and writes **exactly one immutable, priced `usage_records`
row**, idempotent on `request_id`, with cost resolved from `ai_model_pricing`. It
is **pure cost math + an imperative persistence shell** — no external I/O beyond
the DB write, no aggregate, no broker, no credit ledger. It is the seam α7.6
(first pipeline) wires around the dispatch call; nothing above it (the runner,
the aggregates, the API) changes in this slice.

### 1.2 What's in

1. **`UsageRecorderPort`** (ADR-0041 D13) — a narrow application-layer port:
   `record(command: RecordUsageCommand) -> UsageRecordView` (async), the seam the
   α7.6 pipeline calls. Lives in `app/application/interfaces/` (mirrors
   `PublisherPort` / repository interfaces), so a use case can depend on it.
2. **`RecordUsageCommand`** (neutral input DTO) — carries the billing context the
   `ProviderResponse` alone lacks: `tenant_id`, `model_id`, `request_id`,
   `capability`, the α7.4 `ProviderUsage`, terminal `status`, optional
   `workflow_run_id` / `workflow_step_id` / `user_id` / `project_id` / `scene_id`
   / `prompt_id`, `latency_ms`, `error_code`. Frozen dataclass (α7.2/α7.3/α7.4 VO
   style). Scope of fields pinned by §4 Q3.
3. **`UsageRecorderService`** (the shell) — resolve effective price(s) → compute
   `estimated_cost` + fill the granular accounting axes (`tokens_prompt` /
   `tokens_completion` / `images_count` / `seconds_generated`) + pick the primary
   `unit`/`unit_count` → idempotent insert → return a view. One `record()` call is
   one transaction. Mirrors `RelayService` (α7.3) precisely.
4. **Cost calculation + accounting** — map `ProviderUsage` → the real
   `usage_records` columns; price each applicable `pricing_unit` against the
   effective `ai_model_pricing` row; sum into `estimated_cost`; record the
   per-unit breakdown in `extra` for auditability (§4 Q4).
5. **Idempotent writes** (ADR-0033) — insert; on the per-partition
   `uq_<child>_request_id` violation, no-op and return the existing row (§4 Q7).
   `request_id IS NULL` (system-initiated) always inserts.
6. **`IUsageRecordRepository`** — `insert(record) -> UsageRecord` and
   `get_by_request_id(request_id, occurred_month|window) -> UsageRecord | None`;
   SQLAlchemy impl over the partitioned parent.
7. **`IModelPricingRepository`** (read-only) — `get_effective(model_id, unit, at)
   -> AIModelPricing | None` resolving the pricing row effective at the call time
   (§4 Q5). No writes.
8. **DI + UoW wiring** — the two repos on `IUnitOfWork` +
   `SqlAlchemyUnitOfWork`, a `get_usage_recorder()` container factory, the fake
   UoW in `tests/integration/conftest.py` extended. **Unit tests** (cost math,
   multi-unit LLM pricing, image/video accounting, status mapping, idempotency
   branch, missing-price policy) + **integration tests** (real partitioned insert,
   real pricing resolution, real duplicate-`request_id` no-op). Docs
   (`CHANGELOG`, `ROADMAP`, architecture notes; **ADR only if a decision falls
   outside ADR-0041/0019/0033** — see §4).

### 1.3 What's out (deferred)

- **`credit_ledger` debit / credit reservation (pending→consumed) / balance
  math** — deferred to a dedicated credit slice (§4 Q1). `credits_consumed` is a
  NOT-NULL column defaulting to `0`; α7.5 leaves it `0` and documents the seam.
- **Wiring the recorder around the dispatcher/adapter (the "decorator")** — α7.6
  (first pipeline). α7.5 ships the seam; the runner/dispatcher are untouched (§4 Q2).
- **Recording async `IN_PROGRESS` (video) calls at submission** — the α8.3
  completion service records them on terminal resolution using the same
  `request_id` (idempotent). α7.5 records **terminal** calls only (§4 Q6).
- **Reconciliation (`actual_cost`, `cost_reconciliations`, nightly job)** — later
  (§8k.3 `reconcile_costs`); α7.5 writes `estimated_cost`, leaves `actual_cost` NULL.
- **Usage query / summary / export HTTP API** (§8k.4) — no surface this slice.
- **`UsageRecorded` event emission to the outbox** — deferred; α7.5 writes the row
  only (the relay/event seam is α7.3's, wired to usage later if needed).
- **`media_assets` register-by-metadata on generation output** (D12) — α8.x.
- **Historical/temporal pricing UI, multi-currency conversion, tiered/negotiated
  pricing** — out; α7.5 uses the effective row's `currency` as-is.
- **Zero migrations.**

---

## Section 2 — Grounded facts (the tables, the contract, the code it plugs into)

### 2.1 `usage_records` — the **real** columns (leaner than §8k's aspirational domain object)

From `backend/app/infrastructure/db/models/usage.py` (schema §18; ADR-0019). PK is
composite `(id, occurred_at)`; partitioned `RANGE (occurred_at)` monthly (26
children + DEFAULT today):

| Column | Type / null | Notes |
|---|---|---|
| `id` | UUID, server `gen_random_uuid()` | part of PK with `occurred_at` |
| `tenant_id` | UUID **NOT NULL**, FK RESTRICT | strong ref |
| `user_id` / `project_id` / `scene_id` / `prompt_id` | UUID nullable, FK SET NULL | weak refs |
| `workflow_run_id` / `workflow_step_id` | UUID nullable, FK SET NULL | workflow linkage |
| `model_id` | UUID **NOT NULL**, FK RESTRICT → `ai_models` | the priced model |
| `pricing_id` | UUID nullable, FK SET NULL → `ai_model_pricing` | the price row used |
| `request_id` | Text **nullable** | idempotency key (ADR-0033) |
| `unit` | `pricing_unit_enum` **NOT NULL** | the **single** primary billable unit |
| `unit_count` | Numeric(18,4) **NOT NULL** | count for `unit` |
| `tokens_prompt` / `tokens_completion` | Integer nullable | LLM axes |
| `images_count` | Integer nullable | image axis |
| `seconds_generated` | Numeric(10,3) nullable | video/audio axis |
| `credits_consumed` | Numeric(18,4) **NOT NULL**, default `0` | **`0` in α7.5** (§4 Q1) |
| `estimated_cost` | Numeric(18,8) **NOT NULL**, default `0` | computed from pricing |
| `actual_cost` | Numeric(18,8) nullable | reconciliation later (NULL) |
| `currency` | char(3) **NOT NULL** | from the pricing row |
| `status` | `usage_status_enum` **NOT NULL** | `success`/`failed`/`partial`/`timeout` — **no `in_progress`** |
| `latency_ms` | Integer nullable | |
| `error_code` | Text nullable | |
| `extra` | JSONB **NOT NULL**, default `{}` | per-unit pricing breakdown (§4 Q4) |
| `occurred_at` | timestamptz **NOT NULL**, default `now()` | partition key |
| `created_at` | timestamptz **NOT NULL**, default `now()` | |

CHECKs: `credits_consumed >= 0`, `estimated_cost >= 0`,
`actual_cost IS NULL OR actual_cost >= 0`.

**There is no `provider`, `billable`, `capability`, `vendor_model_id`,
`total_tokens`, `image_megapixels`, `audio_seconds`, or `embedding_count` column**
— the §8k domain object is broader than the shipped schema. α7.5 maps onto the
**actual** columns (provider identity is derivable via `model_id → ai_models`).

### 2.2 Idempotency already lives in the DB (ADR-0033)

Every child partition carries `uq_<child>_request_id ON <child> (request_id)
WHERE request_id IS NOT NULL`. So:

- A second insert with the same non-NULL `request_id` (in the same month) raises
  `IntegrityError` on that index → the recorder treats it as a no-op (§4 Q7).
- `request_id IS NULL` rows coexist freely (system-initiated calls without a
  vendor id). ADR-0033 explicitly allows this.
- ADR-0019: **"each call produces exactly one immutable `UsageRecord`."** One
  `record()` → one row (or the pre-existing one on replay).

ADR-0033 also notes **CR-12 (the Usage Recorder producer) has never been built —
α7.5 is that producer.** W1.4 promoted the invariant *ahead of* its producer;
α7.5 is the first writer held to it.

### 2.3 `ai_model_pricing` — effective-row resolution (CR-11)

From `backend/app/infrastructure/db/models/ai_models.py` (schema §15):

- Columns: `model_id` (FK RESTRICT), `effective_from` (NOT NULL),
  `effective_to` (nullable), `unit` (`pricing_unit_enum`), `price_per_unit`
  (Numeric(18,8) ≥ 0), `currency` (char(3)).
- **Current price = partial-unique** `uq_ai_model_pricing_model_id_unit` on
  `(model_id, unit) WHERE effective_to IS NULL` — at most one open row per
  `(model, unit)`. Plus `ix_ai_model_pricing_model_id_effective_from`.
- **Multi-unit tension:** an LLM call consumes **both** `prompt_token` and
  `completion_token` at *different* prices (two pricing rows), but `usage_records`
  has a **single** `unit`/`unit_count`. Resolving this is §4 Q4.

### 2.4 `pricing_unit_enum` vs the α7.4 `ProviderUsage.unit` (a mapping gap)

- `pricing_unit_enum` = `prompt_token`, `completion_token`, `image`, `megapixel`,
  `video_second`, `audio_second`, `embedding`.
- `ProviderUsage` (α7.4, `app/application/interfaces/providers.py`) =
  `unit: str` + `quantity: int` + `detail: Mapping` — the α7.4 mocks populate
  `unit` with free-form strings (`"tokens"` / `"images"` / `"seconds"` /
  `"characters"`). There is **no enforced 1:1** with `pricing_unit_enum`, and a
  single `ProviderUsage` cannot express the LLM prompt/completion split.
- α7.5 must **translate** `ProviderUsage` (+ its `detail`) into the typed
  `usage_records` axes and choose `pricing_unit`(s). The cleanest shape is a
  per-capability accounting policy (§3, §4 Q3/Q4).

### 2.5 The α7.4 provider contract this consumes

From `app/application/interfaces/providers.py`:

- `ProviderResponse{request_id, provider, status: ProviderStatus, output,
  provider_job_id, usage: ProviderUsage | None, error}`.
- `ProviderStatus ∈ {SUCCEEDED, IN_PROGRESS, FAILED}`. **`IN_PROGRESS` has no
  `usage_status_enum` counterpart** → §4 Q6 (record terminal states only).
- `Capability ∈ {LLM, IMAGE, VIDEO, VOICE}`.
- `ProviderResponse` **does not carry `tenant_id`, `model_id`, or workflow
  linkage** — those are the caller's billing context, so α7.5 needs an input DTO
  richer than `ProviderResponse` (§4 Q3).

### 2.6 Service + layering precedents to mirror (α7.3 relay is the direct one)

- **Service:** `app/application/use_cases/relay/relay_service.py` —
  `RelayService(uow, publisher, …)`; one `relay_once()` = one `async with uow`
  transaction; returns a frozen `RelayResult`; `structlog` structured logs;
  module-constant defaults. **α7.5's `UsageRecorderService` mirrors this** at
  `app/application/use_cases/usage/usage_recorder_service.py`.
- **Port:** `app/application/interfaces/publisher.py` (`PublisherPort`) → the
  precedent for `UsageRecorderPort` in `app/application/interfaces/usage_recorder.py`.
- **Repos:** interfaces in `app/application/interfaces/repositories.py` (ABCs,
  e.g. the freshly-added `IProviderSettingsRepository` at the tail); impls in
  `app/infrastructure/repositories/`; both surfaced on `IUnitOfWork` /
  `SqlAlchemyUnitOfWork.__aenter__` and the integration `conftest` fake UoW.
- **DI:** `app/core/container.py` factories (`get_usage_recorder()`).
- **Layering (`pyproject.toml [tool.importlinter]`):** `app.application.use_cases`
  must not import `app.infrastructure`/`app.api`; the recorder depends only on
  `app.application.interfaces` (ports + repo interfaces) + `app.domain` +
  `app.core`. No new leaf/contract needed (unlike α7.4) — the recorder is an
  ordinary use-case service over repository ports.
- **Tests:** `pytest -m unit` (fakes, cost math, idempotency branch) +
  `pytest -m integration` (real partitioned `usage_records`, real pricing,
  duplicate-`request_id`). Full gate = `scripts/ci_gate.py` (10 stages).

---

## Section 3 — Decisions (recommended)

- **D3.1 — α7.5 is the usage/cost accounting seam; one priced row per terminal
  call, mock-agnostic.** Port + service + two repos + cost policy. Realises
  ADR-0041 D13's `usage_records` half; the `credit_ledger` half is deferred (§4 Q1).
- **D3.2 — Pure cost math + imperative persistence shell, mirroring α7.3.** The
  pricing/accounting computation is pure and unit-testable; the service is the
  shell (resolve price → compute → insert → return), one transaction per `record()`.
- **D3.3 — The recorder is a standalone seam, NOT wired this slice.** Per
  runner-before-worker, α7.5 does **not** wrap the dispatcher/adapter or touch
  `AdvanceWorkflowRun`; the α7.6 pipeline calls `UsageRecorderPort.record(...)`
  around the dispatch. Keeps the slice cohesive and the α7.2/α7.4 seams frozen (§4 Q2).
- **D3.4 — Map the neutral `ProviderUsage` onto the real (leaner) columns via a
  per-capability accounting policy.** LLM → `tokens_prompt`/`tokens_completion`;
  image → `images_count`; video/voice → `seconds_generated`. The policy also
  picks the primary `unit`/`unit_count` and the set of `pricing_unit`s to price (§4 Q4).
- **D3.5 — Price against the effective `ai_model_pricing` row; missing price is
  non-fatal.** Resolve `(model_id, unit)` effective at `occurred_at`; sum
  contributions into `estimated_cost`; set `pricing_id` to the primary unit's
  row; take `currency` from pricing. A missing pricing row contributes `0`,
  leaves `pricing_id` NULL, and emits a `WARN` — **usage recording must never
  fail the call it accounts for** (§4 Q5).
- **D3.6 — Idempotent-on-`request_id` via insert-then-catch (ADR-0033).** Insert;
  on the per-partition unique violation, roll back the insert and return the
  existing row (`get_by_request_id`). `request_id IS NULL` always inserts. One
  call → one row (ADR-0019). Not `ON CONFLICT` (partition-child partial-index
  inference on the parent is unreliable) (§4 Q7).
- **D3.7 — Terminal-only recording.** `SUCCEEDED → success`, `FAILED → failed`.
  `IN_PROGRESS` is **not** recorded by α7.5; the α8.3 completion service records
  the terminal outcome later under the same `request_id` (§4 Q6). (`partial` /
  `timeout` remain valid enum values a caller may pass explicitly.)
- **D3.8 — `credits_consumed = 0` in α7.5.** The column is NOT-NULL default `0`;
  credit computation + `credit_ledger` posting is a separate slice (§4 Q1). The
  usage row still records the *cost* (`estimated_cost`), just not the *credit debit*.
- **D3.9 — Zero migrations, zero new schema.** `usage_records` and
  `ai_model_pricing` are read/written exactly as baseline + `0007` define them.
- **D3.10 (W7.5.1) — The recorder is purely observational (added at sign-off).**
  The Usage Recorder may **never** mutate `WorkflowRun`, `WorkflowStep`,
  `RenderJob`, `Media`, `Timeline`, `Project`, or `ProviderSetting`. Its **only**
  write is `usage_records` (and, in a later slice, `credit_ledger`). It resolves
  price by **reading** `ai_model_pricing`. This preserves the aggregate boundaries
  enforced since α5 and matches ADR-0041's runtime contract ("providers call
  nothing directly"; the recorder observes and records, it does not orchestrate).
  Enforced structurally: the service holds no aggregate repositories — its `uow`
  surface is limited to the usage + pricing repos — and asserted by test (no
  aggregate repo is touched during `record()`).

---

## Section 4 — Open questions for sign-off

Only decisions **not** already pinned by ADR-0041 / ADR-0019 / ADR-0033 are raised.
(Already decided, not re-asked: one immutable row per call, idempotent on
`request_id`, price from `ai_model_pricing`, no HTTP/Celery/Redis/SDKs, zero
migrations, runner-before-worker.)

**Q1 — `credit_ledger` scope in α7.5 (the load-bearing scope decision).** ADR-0041
D13 pairs the usage row with a `credit_ledger` debit "idempotent on `(tenant_id,
idempotency_key)`"; §8k.3 adds a reserve→settle (pending→consumed) flow. But
`credit_ledger` is append-only with an immutability trigger and a
`balance_after`-enforcing trigger (`credit_ledger_balance`), so a debit must read
the current balance and post a balance-correct entry — a stateful FSM distinct
from a single usage insert. Your α7.5 scope names **`usage_records` +
`ai_model_pricing`** only. **Recommend:** ship the **usage-row half now**
(`estimated_cost` priced, `credits_consumed = 0`), and **defer** the
`credit_ledger` debit + reserve/settle to a dedicated credit slice. *(Alternative:
include the debit now — larger, stateful, and beyond the tables you scoped.)*
**Confirm: usage row only; `credit_ledger` deferred; `credits_consumed = 0`.**

**Q2 — Recorder placement + wiring timing (seam now vs decorator now).** D13/§8k
describe the recorder as "a decorator around the adapter … so no provider forgets
to record." **Recommend:** ship `UsageRecorderPort` + `UsageRecorderService` as an
**explicit seam** and **do not** wrap the dispatcher/adapter or touch the runner
this slice — the α7.6 first pipeline calls `record(...)` around the dispatch
end-to-end (exactly how α7.3 shipped `relay_once()` as a primitive with no loop,
and α7.4 shipped the dispatcher without wiring it into `AdvanceWorkflowRun`).
*(Alternative: build the decorator wrapper now — but that coupling belongs with
the pipeline that first drives a provider call, α7.6.)* **Confirm: standalone seam;
no decorator/runner wiring this slice.**

**Q3 — Recorder input contract (`RecordUsageCommand`).** `ProviderResponse`
(α7.4) lacks `tenant_id`, `model_id`, and workflow linkage — the caller's billing
context. **Recommend:** a neutral frozen `RecordUsageCommand` in
`app/application/interfaces/` carrying: `tenant_id` (req), `model_id` (req),
`request_id | None`, `capability`, `usage: ProviderUsage | None`, terminal
`status`, `occurred_at | None` (default now), and optional `user_id` /
`project_id` / `scene_id` / `prompt_id` / `workflow_run_id` / `workflow_step_id` /
`latency_ms` / `error_code`. The service maps `ProviderUsage` → the typed axes via
the per-capability policy (D3.4). *(Alternative: pass `ProviderResponse` +
context separately — more args, same result.)* **Confirm the field set (esp.
whether `user_id`/`project_id`/`scene_id`/`prompt_id` are in the α7.5 input or
deferred to α7.6 when the pipeline actually has them).**

**Q4 — Multi-unit pricing into a single-`unit` row (the load-bearing pricing
decision).** `usage_records` has one `unit`/`unit_count`, but an LLM call prices
`prompt_token` **and** `completion_token` separately. **Recommend:**
`estimated_cost` = **Σ** over all applicable units
(`tokens_prompt·price(prompt_token) + tokens_completion·price(completion_token)`;
`images_count·price(image)`; `seconds_generated·price(video_second|audio_second)`);
the single `unit`/`unit_count` records the **primary** billable axis per
capability — **LLM → `completion_token`**, **image → `image`**, **video →
`video_second`**, **voice → `audio_second`**; `pricing_id` = the primary unit's
row; `extra` JSONB carries the full per-unit breakdown
(`{"pricing": [{"unit","count","price_per_unit","cost"}...]}`) for auditability.
*(Alternative: pick a different primary unit, e.g. LLM → `prompt_token`; or
collapse LLM to a single blended unit — less faithful.)* **Confirm the Σ-cost +
primary-unit choices per capability.**

**Q5 — Pricing temporal resolution + missing-price policy.** **Recommend:**
resolve the pricing row **effective at `occurred_at`** (`effective_from <=
occurred_at AND (effective_to IS NULL OR occurred_at < effective_to)`), preferring
the open (`effective_to IS NULL`) row — which is all that exists today. If **no**
pricing row exists for a `(model_id, unit)`: contribute `0` to `estimated_cost`,
leave `pricing_id` NULL, set `currency` from a signed-off default (**recommend
`"USD"`**), and emit a `WARN` — the usage row is **still written** (recording must
not break generation). *(Alternative: raise `NoPricingError` and fail the call —
rejected: accounting must be non-fatal.)* **Confirm temporal-effective resolution,
non-fatal missing-price (0 + `pricing_id` NULL + warn), and the default currency.**

**Q6 — Async (`IN_PROGRESS`) recording timing.** `usage_status_enum` has no
`in_progress`. **Recommend:** α7.5 records **terminal** calls only (`SUCCEEDED →
success`, `FAILED → failed`); an `IN_PROGRESS` submission (video mock) is **not**
recorded now — the α8.3 completion service records the terminal outcome under the
**same `request_id`** (idempotent via ADR-0033), so no double-count. α7.5 asserts
this by test (calling `record()` with `IN_PROGRESS` is rejected or a no-op —
**recommend a typed `ValueError`/`ProviderValidationError`-style rejection** so a
mis-wired caller fails loudly). *(Alternative: map `IN_PROGRESS → partial` and
record at submission — risks double-counting at completion.)* **Confirm
terminal-only, and how `IN_PROGRESS` input is handled (reject vs silent no-op).**

**Q7 — Idempotent-write mechanism.** `request_id` is nullable; the unique index is
per-child + partial. **Recommend:** attempt `INSERT`; on `IntegrityError` matching
the `uq_<child>_request_id` violation, roll back and **return the existing row**
via `get_by_request_id` (the recorder is idempotent, so replays are no-ops
returning the original). `request_id IS NULL` always inserts (ADR-0033 permits
coexistence). **Not** `INSERT … ON CONFLICT` — partial-index inference on a
partitioned parent's per-child indexes is unreliable. *(Alternative: pre-`SELECT`
then insert — racy under concurrency; the catch-on-violation path is correct.)*
**Confirm insert-then-catch-then-fetch; `request_id`-NULL always inserts.**

**Q8 — Does a `UsageRecorded` event get written to the outbox?** ADR-0041's relay
(α7.3) publishes outbox events; §8k.3 step 5 emits `usage.recorded`.
**Recommend:** **no** — α7.5 writes only the `usage_records` row; emitting a
`UsageRecorded` event (and any projection consuming it) is deferred until there is
a consumer, consistent with α7.3's "don't create event histories with no
consumer." *(Alternative: emit now — speculative, no consumer.)* **Confirm no
event emission in α7.5.**

**Version (not a question — confirm cadence).** Continue the `0.4.x` slice cadence
→ `0.4.19-phase3-alpha7.5-dev`, tag `v0.4.19-phase3-alpha7.5` on merge (still
Phase-3 runtime infrastructure).

---

## Section 5 — Planned surface (pending §4)

**No HTTP surface.** The α7.5 surface is a port + input DTO + service + two repos,
consumed by tests (and by the α7.6 pipeline later). Shapes (subject to §4 sign-off):

```python
# app/application/interfaces/usage_recorder.py — the seam the α7.6 pipeline calls
@dataclass(frozen=True, slots=True)
class RecordUsageCommand:                     # Q3 (signed off) — application contract, NOT the ORM.
    tenant_id: UUID                           # It carries enough context for future slices
    model_id: UUID                            # without breaking changes; not every field maps
    status: ProviderStatus                    # to a usage_records column (e.g. render_job_id).
    request_id: str | None = None             # terminal status only (Q6)
    capability: Capability | None = None
    usage: ProviderUsage | None = None
    occurred_at: datetime | None = None       # default now(UTC)
    project_id: UUID | None = None            # ← added at sign-off (Q3)
    workflow_run_id: UUID | None = None
    workflow_step_id: UUID | None = None
    render_job_id: UUID | None = None         # ← added at sign-off (Q3); contract-only, no DB column
    user_id: UUID | None = None
    scene_id: UUID | None = None
    prompt_id: UUID | None = None
    latency_ms: int | None = None
    error_code: str | None = None

@dataclass(frozen=True, slots=True)
class UsageRecordView:                         # what record() returns (id + priced summary)
    id: UUID
    occurred_at: datetime
    unit: str                                  # primary pricing_unit
    unit_count: float
    estimated_cost: float
    currency: str
    status: str
    idempotent_replay: bool                    # True if an existing row was returned

class UsageRecorderPort(ABC):
    async def record(self, command: RecordUsageCommand) -> UsageRecordView: ...

# app/application/interfaces/repositories.py — two new interfaces
class IUsageRecordRepository(ABC):
    async def insert(self, record: UsageRecord) -> UsageRecord: ...          # raises on dup request_id
    async def get_by_request_id(self, request_id: str) -> UsageRecord | None: ...

class IModelPricingRepository(ABC):            # read-only (CR-11)
    async def get_effective(self, *, model_id: UUID, unit: str,
                            at: datetime) -> AIModelPricing | None: ...

# app/application/use_cases/usage/usage_recorder_service.py — the shell (mirrors RelayService)
class UsageRecorderService(UsageRecorderPort):
    def __init__(self, *, uow: IUnitOfWork, default_currency: str = "USD") -> None: ...
    async def record(self, command: RecordUsageCommand) -> UsageRecordView:
        # 1. reject non-terminal status (Q6)
        # 2. map ProviderUsage -> typed axes via per-capability policy (D3.4/Q4)
        # 3. resolve effective price per applicable unit; sum estimated_cost (Q4/Q5)
        # 4. idempotent insert; on dup request_id -> return existing (Q7)
        ...

# cost/accounting policy — pure, unit-tested (module fn or small strategy per capability)
def account_and_price(command, pricing_lookup) -> _PricedRow: ...
```

Signed-off **implementation order** (layer-by-layer, dependency-first — pending §6):

1. **Port + DTOs** — `app/application/interfaces/usage_recorder.py`
   (`RecordUsageCommand`, `UsageRecordView`, `UsageRecorderPort`). Unit tests
   (DTO immutability).
2. **Repository interfaces** — `IUsageRecordRepository` +
   `IModelPricingRepository` in `interfaces/repositories.py`.
3. **Cost/accounting policy** — pure `account_and_price` (per-capability mapping +
   Σ-pricing, Q4/Q5). Unit tests: LLM two-unit sum, image, video, voice,
   missing-price → 0 + warn, currency selection.
4. **`UsageRecorderService`** — the shell (terminal-only guard, map, price,
   idempotent insert, return view). Unit tests against fakes incl. the
   duplicate-`request_id` no-op branch and the `IN_PROGRESS` rejection.
5. **SQLAlchemy repos** — `UsageRecordRepository` (insert over the partitioned
   parent; `get_by_request_id`) + `ModelPricingRepository` (`get_effective`).
6. **UoW + DI wiring** — repos on `IUnitOfWork` / `SqlAlchemyUnitOfWork` + the
   integration `conftest` fake; `get_usage_recorder()` container factory.
7. **Integration tests** — real partitioned insert + read-back; real effective
   pricing resolution; **duplicate `request_id` → single row / no-op**; NULL
   `request_id` coexistence. (uuid-suffixed models/tenants for isolation, per the
   α7.4 `provider_settings` integration-test pattern.)
8. **Docs** — `CHANGELOG`, `ROADMAP` (mark α7.5 shipped),
   `docs/architecture/CONTENT_GENERATION_PIPELINE.md` sequencing + decision log,
   and an **ADR-0041 change-log line** recording the α7.5 usage-recorder scope
   (usage row now; credit-ledger deferred; terminal-only; Σ-cost/primary-unit).
   Then CI gate → pause for release approval → merge → tag `v0.4.19-phase3-alpha7.5`.

---

## Section 6 — Reviewer sign-off

**SIGNED OFF (2026-07-18).** All eight §4 questions approved, with one Q3
enrichment and one added invariant:

- **Q1 — Credit ledger:** ✅ **Do not touch `credit_ledger` in α7.5.** It is an
  append-only financial ledger with transactional semantics beyond usage
  recording and deserves its own aggregate + tests. `credits_consumed = 0` is the
  correct placeholder for this slice.
- **Q2 — Wiring:** ✅ Exactly as α7.3/α7.4 — **build the seam now, wire it in α7.6**
  (`Runner → Dispatcher → Mock Provider → Usage Recorder`). No coupling to
  unfinished runtime code; the runner/dispatcher are untouched this slice.
- **Q3 — `RecordUsageCommand`:** ✅ **with one addition.** Carry `tenant_id`,
  `project_id`, `workflow_run_id` (nullable), `render_job_id` (nullable),
  `request_id`, `model_id`, `capability`, and the usage metrics — **even where a
  field does not map to a DB column** (e.g. `render_job_id`). The command is the
  **application contract, not the ORM**; it must carry enough context for future
  slices without breaking changes. (`user_id` / `scene_id` / `prompt_id` /
  `workflow_step_id` / `latency_ms` / `error_code` remain optional passthroughs.)
- **Q4 — Multi-unit pricing:** ✅ `estimated_cost = Σ(unit_price × quantity)`;
  persist `unit` = primary billing axis, `unit_count` = primary quantity, while
  storing the granular metrics already present. No migration.
- **Q5 — Missing pricing:** ✅ `estimated_cost = 0`, `pricing_id = NULL`, `WARN`
  log. **Never fail the workflow because pricing hasn't been configured** — usage
  recording must not block execution. Default currency `"USD"`.
- **Q6 — Async usage:** ✅ **Only terminal usage** in α7.5. The α8.3 completion
  service later reuses the same `request_id` (provider → completion service →
  usage recorder), idempotent via ADR-0033.
- **Q7 — Idempotency:** ✅ `INSERT` → `IntegrityError` → `SELECT existing` →
  return existing. Matches ADR-0033. `request_id IS NULL` always inserts.
- **Q8 — `UsageRecorded` event:** ✅ **No event** — no consumer exists. A future
  billing/analytics pipeline (α8 / Phase 4) can introduce it when needed.

**Added invariant — W7.5.1 (the recorder is purely observational).** The Usage
Recorder may **never** mutate `WorkflowRun`, `WorkflowStep`, `RenderJob`, `Media`,
`Timeline`, `Project`, or `ProviderSetting`. Its **only** writes are
`usage_records` (and eventually `credit_ledger`, in a later slice); it **reads**
`ai_model_pricing` to price. This preserves the aggregate boundaries enforced
since α5 and aligns with ADR-0041's runtime contract. Captured as **D3.10** and
enforced structurally (the service holds no aggregate repositories) + by test.

**Forbidden in α7.5 (verbatim).** ❌ `credit_ledger` writes · ❌ credit
reservation/settlement · ❌ HTTP · ❌ Celery · ❌ Redis · ❌ polling · ❌ provider
SDKs · ❌ OpenAI/Gemini · ❌ decorator/runner wiring · ❌ recording `IN_PROGRESS`
calls · ❌ `UsageRecorded` event · ❌ reconciliation (`actual_cost`) · ❌ usage
query/summary/export API · ❌ mutating any aggregate (W7.5.1). **This slice
activates existing persistence; it introduces no new architectural foundations.**

- **Version:** ✅ `0.4.19-phase3-alpha7.5-dev` → tag `v0.4.19-phase3-alpha7.5`.

**Roadmap unchanged:** α7.1 ✅ → α7.2 ✅ → α7.3 ✅ → α7.4 ✅ → **α7.5 (current)** →
α7.6 first mock pipeline → α8.1 image → α8.2 video → α8.3 completion → α8.4 FFmpeg
render → α8.5 export.

Proceed: branch `phase3/alpha7.5-usage-recorder`, bump `app/main.py` →
`0.4.19-phase3-alpha7.5-dev`, implement in the §5 order, full quality gate, then
pause for release approval before touching `main` (linear history preserved).
