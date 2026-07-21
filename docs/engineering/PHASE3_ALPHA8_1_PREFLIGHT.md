# Phase 3 Slice α8.1 — First Real Provider (OpenAI Images, synchronous) — the adapter slice — Pre-flight

> Status: **SIGNED OFF (2026-07-21)** — see §6. All §4 questions Q1–Q10 approved
> as recommended, with three refinements: **Q4 formalized** (*provider constructors
> receive secrets, never retrieve them*), **W8.1.1 strengthened** (*infrastructure
> adapters are completely configuration-blind — no env / DB / filesystem / vault
> lookups; everything arrives via DI*), and a **new invariant W8.1.3** (*the OpenAI
> adapter is observationally equivalent to `MockImageProvider` — the runner cannot
> tell which produced a `ProviderResponse` by type, fields, metadata, or behaviour;
> only the payload values differ*). This is the **first real
> provider** slice. It builds **one adapter** — a synchronous OpenAI Images
> provider implementing the existing `ImageProvider` protocol — and proves the
> α7.4 abstraction / α7.6 pipeline can drive an external system **without any
> orchestration-layer change**. That is exactly the property
> [`ADR-0041`](../decisions/ADR-0041-provider-runtime-contract.md) was designed
> to deliver: α8.x replaces the bottom box (the mock adapter) and **nothing
> above it moves**.
>
> Mirrors the α5/α6/α7.x discipline: ground in the existing contract + code →
> lock decisions → sign-off → branch → implement → CI → merge → tag. Read-only
> planning artefact. **Nothing is implemented yet.**
>
> **Predecessors (all released, `main`):**
> * α7.4 (`v0.4.18`) — the **provider contract + leaf**: the neutral DTOs / errors /
>   metadata (`app/application/interfaces/providers.py`), the four capability
>   `Protocol`s (`app/infrastructure/ai/providers/ports.py`), the `ProviderRegistry`
>   (direct lookup, no fallback), the `StepCommandDispatcher` (closed `kind →
>   capability` table), and the four deterministic mocks.
> * α7.5 (`v0.4.19`) — the **recorder**: idempotent-on-`request_id` `usage_records`
>   writes, priced from read-only `ai_model_pricing`.
> * α7.6 (`v0.4.20`) — the **first pipeline**: `AdvanceWorkflowRun` now interprets
>   `StepResult.commands` — mints a deterministic `request_id`
>   (`run_id:step_index:command_index`), dispatches exactly once (**W7.6.2**),
>   records usage in-transaction (`record_usage_in_uow`), checkpoints the **opaque**
>   provider envelope (**W7.6.1**), retries on transient `ProviderError`, fails on
>   terminal, pauses on `IN_PROGRESS`, records-failed-then-fails on `FAILED`.
>
> **What α8.1 changes vs α7.6.** Only the leaf's *contents* and one DI seam:
>
> ```
> WorkflowRun → Runner → StepCommand → Dispatcher → [ Provider Adapter ] → ProviderResponse
>   α7.2         α7.2       α7.4          α7.4              ▲                    α7.4
>  UNCHANGED    UNCHANGED  UNCHANGED    UNCHANGED           │               UNCHANGED
>                                                 the ONLY box that changes:
>                                          MockImageProvider → OpenAIImageProvider
> ```
>
> No HTTP touches the runner, dispatcher, recorder, relay, or lock manager. The
> real provider is a synchronous request/response call that returns `SUCCEEDED`
> inline — the **same shape the α7.6 image pipeline already drives end-to-end**.

---

## 1. Grounding — what already exists (verified against code)

### 1.1 The capability protocol the adapter must satisfy (`app/infrastructure/ai/providers/ports.py`)
`ImageProvider` is a **runtime-checkable async `Protocol`**: `metadata:
ProviderMetadata`, `async health() -> ProviderHealth`, and
`async generate_image(req: GenerateImageRequest) -> GenerateImageResponse`
(lines 36–53). The real adapter implements this **exactly** — no new method, no
new field. `MockImageProvider` (`mocks/mock_image.py`) is the shape to copy:
returns `status=SUCCEEDED`, `output={"image_ref": …, "size": …}`,
`usage=ProviderUsage(unit="images", quantity=1)`, `image_ref=…`.

### 1.2 The neutral DTOs (`app/application/interfaces/providers.py`)
* **Request** — `GenerateImageRequest{request_id, prompt, model?, size?, params}`
  (lines 135–141). The dispatcher already fills these from `command.args`
  (`dispatcher.py` lines 84–95).
* **Response** — `GenerateImageResponse(ProviderResponse)` adds `image_ref` on top
  of the common envelope `{request_id, provider, status, output, provider_job_id?,
  usage?, error?}` (lines 94–146).
* **Status** — `ProviderStatus.{SUCCEEDED, IN_PROGRESS, FAILED}` (lines 40–51).
  OpenAI Images is synchronous → **only `SUCCEEDED`**; failures are raised as typed
  errors, not returned as `FAILED` (see §3 Q7).
* **Usage** — `ProviderUsage{unit, quantity, detail}` (lines 54–65).
* **Errors** — the closed hierarchy (lines 184–222): transient
  (`ProviderUnavailable`, `ProviderRateLimited`, `ProviderTimeout`, all
  `transient=True`) vs terminal (`ProviderAuthenticationError`,
  `ProviderValidationError`). **These are exactly the buckets α8.1 needs** — no new
  error type is required.

### 1.3 The runner already handles every branch the real provider can produce (`advance_workflow_run.py`)
Verified (lines 561–686): the runner mints `request_id =
f"{run.id}:{step.step_index}:{command_index}"` (line 609), **dispatches exactly
once** (W7.6.2, line 615), and on the result:
* transient `ProviderError` → `mark_step_retrying` + retry with the **same**
  `request_id` (lines 449–452, 617–625),
* terminal `ProviderError` → fail (lines 564, 617–625),
* `SUCCEEDED` → record usage in-txn + checkpoint the **opaque** envelope,
* `FAILED` → record failed usage then fail the step (lines 642–654),
* `IN_PROGRESS` → pause (lines 629–642).

**Consequence:** a synchronous OpenAI adapter that returns `SUCCEEDED` on HTTP 200
and raises the right typed `ProviderError` otherwise needs **zero runner change**.

### 1.4 The dispatcher is capability-generic (`dispatcher.py`)
`_generate_image` (lines 84–95) does `registry.resolve(Capability.IMAGE)` and
calls `.generate_image(...)`. It does not know or care whether the resolved
provider is a mock or OpenAI. **No dispatcher change.**

### 1.5 The registry resolves by direct lookup (`registry.py`)
`resolve(capability)` returns `providers[0]` and raises `NoProviderAvailable` if
empty (lines 46–56) — **no fallback / weighting / health ordering** (α7.4 Q4).
`default_registry()` (lines 77–84) registers the four mocks; the module singleton
`PROVIDER_REGISTRY` (line 87) is what `get_provider_registry()` returns
(`container.py` line 249). **This is the one seam α8.1 touches** — see §3 Q5.

### 1.6 The config seam that already exists (`provider_settings`)
* **DB read seam** — `ProviderSettingsRepository.get_value(provider, key,
  tenant_id?)` (repo lines 30–58): read-only, **tenant-shadows-global**, backed by
  two partial unique indexes. The `ProviderSetting` model
  (`db/models/configuration.py` lines 68–101) has `provider`, `tenant_id?`, `key`,
  `value: JSONB`, `is_secret`. **This is request-scoped (needs an `AsyncSession`)**
  and lives behind `app.infrastructure.repositories` — a provider *leaf* cannot use
  it without threading a session (see §3 Q4).
* **Env config seam** — `Settings` (`core/config.py`), an `@lru_cache`
  `pydantic-settings` singleton already using `SecretStr` for secrets (e.g.
  `jwt_secret`, line 46). Adding `openai_api_key: SecretStr | None` here is the
  minimal, leaf-friendly way to inject a key.

### 1.7 The import-linter leaf contract (`pyproject.toml` lines 279–297)
`app.infrastructure.ai.providers` is a **strict leaf**: forbidden from importing
`app.application.use_cases`, `app.api`, `app.domain.workflow`. It **may** import
the neutral contract (`app.application.interfaces.providers`) and **any
third-party** package. So the OpenAI adapter may import `httpx` and the DTOs, but
**must not** reach up into orchestration — reinforcing that config/secrets are
**injected**, not fetched by the leaf (proposed invariant **W8.1.1**).

### 1.8 Dependencies (`pyproject.toml`)
Core `dependencies` (lines 7–24) has **no HTTP client**. `httpx>=0.27.0` exists only
as an **optional/test** dependency (lines 65–66, for the integration harness). α8.1
must **promote `httpx` to a core runtime dependency**. No `openai` SDK is present
(see §3 Q2 — we recommend keeping it that way).

---

## 2. Scope

### 2.1 α8.1 builds
1. **`OpenAIImageProvider`** in the provider leaf (`app/infrastructure/ai/providers/`)
   implementing `ImageProvider`: one synchronous `generate_image` HTTP call to the
   OpenAI image-generations endpoint, neutral-DTO in / neutral-DTO out, typed error
   mapping, per-attempt timeout. Static `metadata` + `health`.
2. **A small typed HTTP concern** inside the leaf: auth header, timeout,
   status→`ProviderError` mapping. **One request per call — no internal retry**
   (W7.6.2, §3 Q6).
3. **Config**: `openai_api_key: SecretStr | None` (+ optional `openai_base_url`,
   `openai_timeout_seconds`) on `Settings`, and matching lines in
   `.env.validation` / `.env.example`.
4. **Registry composition**: `get_provider_registry()` (container) becomes
   config-driven — register `OpenAIImageProvider` for `Capability.IMAGE` **iff** a
   key is configured, else keep `MockImageProvider`; **all other capabilities stay
   mock** (§3 Q5).
5. **Dependency**: promote `httpx` to core `dependencies`.
6. **Tests**: unit tests for the adapter (success → `SUCCEEDED`/`image_ref`/usage;
   each error class → correct `ProviderError`; **exactly one** HTTP call) against a
   **mocked transport** (no live network in CI); a registry-composition test
   (key present → OpenAI resolves for IMAGE; absent → mock). The α7.6 e2e stays on
   mocks — **CI never calls OpenAI**.
7. **Docs**: CHANGELOG entry, ADR-0041 change-log line, pipeline-doc note.

### 2.2 α8.1 explicitly does NOT build (forbidden this slice)
Celery · Redis · webhooks · polling · a completion service (α8.3) · FFmpeg · storage ·
export · **video / LLM / voice real providers** · Media-row creation (α7.6 Q7 holds —
checkpoint the `image_ref` only) · multi-provider fallback · a provider-selection
engine · health-ordering · **rate limiter** · **circuit breaker** · per-tenant key
resolution from `provider_settings` at runtime · **any change to**
`advance_workflow_run.py`, `dispatcher.py`,
`usage_recorder_service.py`, the relay, the lock manager, the `ProviderRegistry`
class, the neutral DTOs, or `ports.py`. **Zero Alembic migration.**

---

## 3. Recommended decisions (to be confirmed in §4)

**Q1 — Which provider?  → OpenAI Images (recommended).**
Its `images/generations` endpoint is **synchronous request/response**: it returns
the image inline, mapping cleanly to `ProviderStatus.SUCCEEDED` with no
`provider_job_id`, **no polling, no webhooks** (all forbidden this slice). This is
the *same* synchronous image path α7.6 already proved. Fal.ai's generate APIs are
**queue-based/async** (submit → poll/webhook), which would necessarily pull in the
α8.3 completion machinery — out of scope. So OpenAI is the correct first adapter
*because* it keeps α8.1 on the already-proven synchronous seam.

**Q2 — HTTP client: raw `httpx` (recommended) or the `openai` SDK?  → raw `httpx`.**
Reasons: (a) the leaf's stated invariant is *"SDK types never leak upward"* — raw
`httpx` keeps the boundary trivially clean; (b) full control over status→error
mapping and per-attempt timeout; (c) minimal, well-understood dependency (already
vendored for tests) vs a large SDK with its own retry/backoff we'd have to disable
to honour W7.6.2; (d) no SDK version churn in the leaf. Promote `httpx` to core deps.

**Q3 — Model + `image_ref` strategy under "no storage".  → `dall-e-3`,
`response_format="url"`; `image_ref` = the returned URL string.**
With storage forbidden and α7.6 checkpointing only the opaque envelope, we must not
put a multi-megabyte base64 blob into the checkpoint JSON. `dall-e-3` supports
`response_format="url"` (a short, ephemeral CDN URL) → `image_ref` stays a compact
string, checkpointed as-is, **no storage needed**. (`gpt-image-1` returns
**base64 only** — it is the right choice **once α8.4 storage exists**, not now.)
The workflow's `model` arg carries the literal model string; the adapter validates
it is a supported image model, else `ProviderValidationError`. α7.6's `model_id`
fail-fast + an `ai_model_pricing` row for that model still apply (data seed, **not**
a migration).

**Q4 — Where does the API key come from?  → env `Settings.openai_api_key`
(`SecretStr`), injected into the adapter at registry-build time.**
The provider leaf **must not** read the DB/secrets itself (import-linter + it is a
process-singleton with no session). `provider_settings` (the DB seam) is
request-scoped and aimed at **per-tenant** keys — wiring it now would require
threading a session/resolver through the dispatcher, which is orchestration surface
this slice forbids. So: read the key from env at composition time, inject it into
`OpenAIImageProvider(...)`. **`provider_settings` remains the documented future path
for per-tenant overrides** (α8.x), unchanged and unused this slice.

*Formalized principle (signed off):* **provider constructors receive secrets, they
never retrieve them.** The provider must never know where the key originated — env
today, `provider_settings` / Vault / AWS Secrets Manager tomorrow — and the provider
code does not change when the source does. *(Proposed invariant **W8.1.1**: the
provider leaf never reads config/DB/secrets — everything is injected at
construction.)*

**Q5 — Registry coexistence & selection (no selection engine allowed).  →
config-driven composition in `get_provider_registry()`.**
`resolve()` returns `providers[0]` and there is no fallback/selection — so
registering *both* mock and OpenAI for `IMAGE` would be ambiguous and the mock
(registered first) would silently win. Instead: **exactly one IMAGE provider per
process, chosen by config** — `OpenAIImageProvider` when `openai_api_key` is set,
else `MockImageProvider`; LLM/VIDEO/VOICE stay mock. Both classes **coexist in the
codebase** (satisfying "mock ↓ openai"), `resolve()` stays a pure direct lookup, and
the dispatcher/runner never learn which one they got. No selection engine, no
fallback, no health-ordering. *(Proposed invariant **W8.1.2**: α8.1 makes exactly
one capability — IMAGE — real; everything else stays mock.)*

**Q6 — Retry & timeout ownership (W7.6.2).  → provider does ONE HTTP call, never
retries; owns only the per-attempt timeout.**
The runner owns all re-dispatch/retry (verified §1.3). The adapter therefore makes
**one** request per `generate_image`, sets an `httpx` timeout, and on
timeout/transient failure raises a **transient** `ProviderError` so the runner
retries with the **same** deterministic `request_id`. Any SDK/client-side auto-retry
would violate W7.6.2 and is explicitly disabled (another reason to prefer raw
`httpx`, Q2).

**Q7 — Error classification.  → fixed HTTP-status → `ProviderError` map; failures
are *raised*, not returned as `FAILED`.**

| Condition | Mapped error | Class |
|---|---|---|
| 401 / 403 | `ProviderAuthenticationError` | terminal |
| 400 / content-policy rejection / unsupported param | `ProviderValidationError` | terminal |
| 429 | `ProviderRateLimited` | transient |
| 500 / 502 / 503 / 504, connection error | `ProviderUnavailable` | transient |
| `httpx.TimeoutException` | `ProviderTimeout` | transient |
| 200 | `GenerateImageResponse(status=SUCCEEDED, …)` | — |

No new error type; no `status=FAILED` is emitted (OpenAI signals failure via HTTP
status, which we translate to raised typed errors). The runner already handles both
the raised-`ProviderError` and the `FAILED`-status branches, so this is safe either
way.

**Q8 — Usage extraction.  → `ProviderUsage(unit="images", quantity=len(data))`,
mirroring the mock.**
`dall-e-3` bills per image by size/quality; the neutral unit is `"images"`. The
α7.5 recorder is unchanged; it prices this against an `ai_model_pricing` row for the
chosen `model_id` (operator-seeded data, **not** a migration). Optional `detail`
may carry `{"size": …, "quality": …}` for auditability.

**Q9 — HTTP client lifecycle.  → one shared `httpx.AsyncClient` per adapter
instance (pooling), created at construction with the configured timeout.**
Simplest correct choice for a process-singleton provider. A global ASGI-shutdown
`aclose()` hook is **not** wired this slice (the provider is exercised via the
in-process pipeline, not a long-lived server loop yet) and is noted as a
non-blocking follow-up.

**Q10 — `health()` implementation.  → static `ProviderHealth(healthy=True)` for
α8.1.**
The registry does **not** consult health yet (α7.4 Q4), so a live probe would add
network + auth surface for no behavioural gain. Static health matches the mock;
a real ping is deferred until health-ordering exists.

**W8.1.3 — observational equivalence (new invariant).** The OpenAI adapter must be
**observationally equivalent** to `MockImageProvider`: both return the *same DTO
type* (`GenerateImageResponse`) with the *same populated field-set* and the *same
status semantics* (`SUCCEEDED` inline, `image_ref` + `output{image_ref,size}` set,
`usage.unit="images"`). The runner cannot determine which provider served the call
by type, fields, metadata, or behaviour — its code path is identical; **only the
payload values differ** (the real image URL vs the `mock://` ref, and the provider
id string). This is enforced by a test that asserts the two responses have identical
shape. If the runner ever *could* tell them apart, the α7 abstraction would have
leaked — so W8.1.3 is the strongest single proof that α7.4/α7.6 got the boundary
right.

---

## 4. Open questions for sign-off

| # | Question | Recommendation |
|---|---|---|
| Q1 | First real provider | **OpenAI Images** (synchronous → fits α7.6 sync seam; Fal.ai is async → α8.3) |
| Q2 | HTTP client | **Raw `httpx`**, no `openai` SDK (leaf purity, error-map control, W7.6.2) |
| Q3 | Model + `image_ref` under no-storage | **`dall-e-3` + `response_format="url"`**; `image_ref` = URL string; `gpt-image-1`(b64) waits for α8.4 |
| Q4 | API-key source | **Env `Settings.openai_api_key` (`SecretStr`)**, injected at build time; `provider_settings` DB path stays future/unwired. **Refinement: provider constructors receive secrets, never retrieve them** |
| Q5 | Registry coexistence / selection | **Config-driven composition** in `get_provider_registry()` — one IMAGE provider per process; no selection engine |
| Q6 | Retry / timeout ownership | **One call, no internal retry**; provider owns per-attempt timeout only (W7.6.2) |
| Q7 | Error classification | **Fixed status→`ProviderError` map** (table above); failures raised, no new error types |
| Q8 | Usage extraction | **`unit="images", quantity=len(data)`**; recorder unchanged; pricing row is a data seed |
| Q9 | HTTP client lifecycle | **Shared `AsyncClient` per instance**; global shutdown hook deferred |
| Q10 | `health()` | **Static `healthy=True`** (no health-ordering yet) |
| — | **Invariant W8.1.1** (strengthened) | Infrastructure adapters are **completely configuration-blind** — no env / DB / filesystem / vault lookups; everything arrives via DI |
| — | **Invariant W8.1.2** | Exactly one real capability (IMAGE); LLM/VIDEO/VOICE stay mock; no selection/fallback |
| — | **Invariant W8.1.3** (new) | The OpenAI adapter is **observationally equivalent** to `MockImageProvider` — the runner cannot tell which produced a `ProviderResponse` by type, fields, metadata, or behaviour; only the payload values differ |

**Explicit no-change confirmation requested:** α8.1 touches only (a) the provider
leaf contents, (b) `Settings` + env files, (c) `get_provider_registry()`
composition, (d) `pyproject.toml` deps, (e) tests + docs. It does **not** change the
runner, dispatcher, recorder, relay, lock manager, `ProviderRegistry` class, neutral
DTOs, or `ports.py`, and introduces **no migration**.

---

## 5. Component / contract sketch (illustrative — not yet implemented)

### 5.1 Adapter (new file in the leaf, e.g. `providers/openai/image.py`)
```python
class OpenAIImageProvider:  # implements ImageProvider (structural)
    metadata = ProviderMetadata(
        id="openai-image", name="OpenAI Images", capability=Capability.IMAGE,
        supports_polling=False, supports_webhooks=False, version="1.0",
    )

    def __init__(self, *, api_key: str, base_url: str, timeout_s: float) -> None:
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout_s,
                                         headers={"Authorization": f"Bearer {api_key}"})

    async def generate_image(self, req: GenerateImageRequest) -> GenerateImageResponse:
        # ONE request (W7.6.2). Map non-2xx → typed ProviderError (Q7).
        # On 200: url = data[0]["url"]; usage = ProviderUsage("images", len(data))
        return GenerateImageResponse(
            request_id=req.request_id, provider=self.metadata.id,
            status=ProviderStatus.SUCCEEDED,
            output={"image_ref": url, "size": req.size},
            usage=ProviderUsage(unit="images", quantity=len(data)),
            image_ref=url,
        )

    async def health(self) -> ProviderHealth:
        return ProviderHealth(healthy=True, detail="static")  # Q10
```

### 5.2 Registry composition (the ONLY container change — `get_provider_registry`)
```python
def get_provider_registry() -> ProviderRegistry:
    registry = default_registry()  # four mocks
    key = get_settings().openai_api_key
    if key is not None:            # Q5: swap the IMAGE provider only
        registry.override(provider=OpenAIImageProvider(api_key=key.get_secret_value(),
                                                       base_url=..., timeout_s=...),
                          capabilities=[Capability.IMAGE])
    return registry
```
*(“override” = register-as-sole-provider for the capability; a 3-line helper on
`ProviderRegistry` **or** simply building a fresh registry that skips
`MockImageProvider` — final shape decided at implementation, both keep `resolve()`
a direct lookup and touch nothing above the leaf.)*

### 5.3 Config (`core/config.py`)
```python
openai_api_key: SecretStr | None = Field(default=None)
openai_base_url: str = Field(default="https://api.openai.com/v1")
openai_timeout_seconds: float = Field(default=60.0, gt=0)
```

### 5.4 Dependency (`pyproject.toml`)
Move `httpx>=0.27.0` from the optional/test extra into core `dependencies`.

---

## 6. Reviewer sign-off — **SIGNED OFF (2026-07-21)**

Q1–Q10 approved as recommended. Three refinements folded in:
* **Q4** — formalized: *provider constructors receive secrets, they never retrieve
  them* (source may move env → `provider_settings` → Vault → Secrets Manager with
  **no provider change**).
* **W8.1.1** — strengthened: *infrastructure adapters are completely
  configuration-blind* (no env / DB / filesystem / vault lookups; DI only).
* **W8.1.3** — added: the OpenAI adapter is *observationally equivalent* to
  `MockImageProvider`.

Explicitly forbidden this slice (confirmed): Celery · Redis · polling · webhooks ·
storage · media registration · export · provider selection · rate limiter · circuit
breaker · fallback provider. **The adapter is the only moving part** — well over 90%
of the diff should live in `app/infrastructure/ai/providers`, with only minimal DI
wiring elsewhere; if the runner, `WorkflowRegistry`, dispatcher, recorder, or relay
must change, α8.1 has violated the architecture.

Proceeding: branch `phase3/alpha8.1-openai-image`, bump `0.4.21-phase3-alpha8.1-dev`,
implement per §5, green the full CI gate (ruff · black · mypy · import-linter ·
unit — CI never calls OpenAI), then pause for release approval before
finalize/tag `v0.4.21-phase3-alpha8.1`.
