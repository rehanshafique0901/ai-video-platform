# Phase 3 Slice α8.2 — First Real Async Provider (Fal.ai Video, submit-only) — the pause-proving slice — Pre-flight

> Status: **SIGNED OFF (2026-07-21)** — Q1–Q10 + W8.2.1 / W8.2.2 approved as
> recommended, plus two additions folded in below: **W8.2.3** (the adapter never
> mutates orchestration state) and a **versioned opaque checkpoint envelope**
> (`schema_version`) giving α8.3 a stable payload contract. This is the **first
> real *async* provider** slice. It replaces the **one** remaining async-shaped mock —
> the video provider — with a real **Fal.ai** adapter that **submits** a
> queue-based video job and returns `IN_PROGRESS` + a `provider_job_id`, driving
> the **pause seam built in α7.6** with real bytes. It is the first slice that
> exercises *new orchestration behaviour* (the async/pause branch) rather than
> just swapping a synchronous mock. Per the roadmap and
> [`ADR-0041`](../decisions/ADR-0041-provider-runtime-contract.md) (D1/D4/**D5**/D10),
> **the run intentionally stops at the pause boundary** — completion (polling +
> webhooks + resume + terminal usage) is **α8.3**, not this slice.
>
> Mirrors the α5–α8.1 discipline: ground in the existing contract + code → lock
> decisions → sign-off → branch → implement → CI → merge → tag. Read-only
> planning artefact. **Nothing is implemented yet.**
>
> **Predecessors (all released, `main`):**
> * α7.6 (`v0.4.20`) — the **pause seam**: on `IN_PROGRESS` the runner returns
>   `paused`, `mark_run_paused` CAS (`running → paused`, `finished_at` unset),
>   checkpoints `provider_job_id` + the **opaque** `output` envelope, emits
>   **`WorkflowRunPaused`**, and records **no usage**. Proven end-to-end against
>   `MockVideoProvider`.
> * α8.1 (`v0.4.21`) — the **first real (sync) provider**: `OpenAIImageProvider`
>   established the adapter pattern — raw `httpx`, one HTTP call per dispatch
>   (W7.6.2), HTTP-status → typed `ProviderError`, config-blind construction
>   (W8.1.1), config-driven registry composition, observational equivalence with
>   the mock (W8.1.3). α8.2 reuses every one of those patterns for the async path.
>
> **What α8.2 changes.** Only the leaf's *contents* and one DI seam — identical
> footprint to α8.1:
>
> ```
> WorkflowRun → Runner → StepCommand → Dispatcher → [ Provider Adapter ] → ProviderResponse(IN_PROGRESS)
>   α7.2         α7.2       α7.4          α7.4              ▲                         │
>  UNCHANGED    UNCHANGED  UNCHANGED    UNCHANGED           │                    pause seam (α7.6)
>                                                the ONLY box that changes:      UNCHANGED
>                                          MockVideoProvider → FalVideoProvider
> ```

---

## 1. Grounding — what already exists (verified against code)

### 1.1 The runner's async/pause branch is already complete (`advance_workflow_run.py`)
Verified: `_execute_commands` (line 629) — on `resp.status is IN_PROGRESS` it returns
`_CommandsResult(kind="paused", pause=_PauseInfo(..., provider_job_id=resp.provider_job_id))`
and **does not call `_record_usage`** (usage is recorded only on `SUCCEEDED`/`FAILED`,
lines 645/659). The pause is then persisted (lines 308–321): `mark_run_paused` CAS,
`emit_workflow_run_paused(..., provider_job_id=...)`, and the step's opaque output is
checkpointed. **So "pause + checkpoint job id + emit event + record no usage" is the
runner's existing behaviour — α8.2 writes no runner code.**

### 1.2 The mock that models the shape (`mocks/mock_video.py`)
`MockVideoProvider.generate_video` returns `status=IN_PROGRESS`,
`provider_job_id="mock-video-job:<request_id>"`, `output={"provider_job_id": …}`,
`supports_polling=True`, `supports_webhooks=True` (lines 25–46). It also returns a
`ProviderUsage(unit="seconds", …)` which **the runner discards on pause** (§1.1) — so
whether the real adapter returns usage or `None` on `IN_PROGRESS` is behaviourally
invisible to the runner (see §3 Q5). This is the exact shape the Fal adapter must
reproduce (W8.2.1).

### 1.3 The capability protocol + DTOs (unchanged)
`VideoProvider.generate_video(GenerateVideoRequest) -> GenerateVideoResponse`
(`ports.py`). `GenerateVideoRequest{request_id, prompt, model?, duration_seconds?,
params}`; `GenerateVideoResponse(ProviderResponse)` adds `video_ref` (unused on the
async path — it is populated at completion in α8.3). The common envelope already
carries `provider_job_id` (set **iff** `IN_PROGRESS`) and the opaque `output` bag.

### 1.4 The dispatcher is capability-generic (`dispatcher.py` lines 97–108)
`_generate_video` does `registry.resolve(Capability.VIDEO)` and calls
`.generate_video(GenerateVideoRequest(request_id=…, prompt=…, model=args.get("model"),
duration_seconds=args.get("duration_seconds"), params=…))`. It neither knows nor cares
whether the resolved provider is the mock or Fal. **No dispatcher change.**

### 1.5 The pipeline already emits the command (`registry.py` lines 317–328)
`_generate_video_step` emits `StepCommand(kind="generate_video", args={prompt,
duration_seconds, model, model_id})` via `_generation_args` (threads `model`/`model_id`
from the run input; runner injects `request_id`). **No pipeline change** — the same
`generate-video@1.0.0` definition now drives a real provider.

### 1.6 The α8.1 composition seam to extend (`core/container.py`)
`init()` builds the registry via `_build_provider_registry(openai_client)` and
`_build_openai_client(settings)` (added in α8.1). IMAGE resolves to the real provider
iff an OpenAI key is set, else mock; LLM/VIDEO/VOICE are mock. α8.2 adds the symmetric
VIDEO path: a `_build_fal_client(settings)` and one extra branch in
`_build_provider_registry` — **exactly the α8.1 shape, one capability over**.

### 1.7 The import-linter leaf contract (`pyproject.toml`) + deps
The Fal adapter lives in the strict leaf (`app.infrastructure.ai.providers`), importing
only `httpx` + the neutral DTOs (contract already KEPT for the OpenAI adapter). `httpx`
is already a core dependency as of α8.1 — **no new dependency**.

---

## 2. Scope

### 2.1 α8.2 builds
1. **`FalVideoProvider`** in the leaf (`app/infrastructure/ai/providers/fal/video.py`)
   implementing `VideoProvider`: **one** HTTP POST that *submits* a job to the Fal
   queue and returns `IN_PROGRESS` + `provider_job_id` (the Fal `request_id`), with the
   completion coordinates (`status_url` / `response_url`) in the **opaque** `output`
   envelope for α8.3. Typed error mapping, per-attempt timeout, static `health`.
2. **Config**: `fal_api_key: SecretStr | None` (+ `fal_base_url`,
   `fal_timeout_seconds`) on `Settings`; mirrored in `.env.example`.
3. **Registry composition**: `_build_fal_client` + one VIDEO branch in
   `_build_provider_registry` — real VIDEO iff a Fal key is configured, else mock.
4. **Tests**: adapter unit tests (submit → `IN_PROGRESS`/`provider_job_id`/opaque
   `output`; each error class; **exactly one** HTTP call; **no** usage; W8.2.1
   equivalence with `MockVideoProvider`) against `httpx.MockTransport`; a
   registry-composition test (Fal key → real VIDEO; absent → mock). CI never calls Fal.
5. **Docs**: CHANGELOG, ADR-0041 change-log line, pipeline §13 note.

### 2.2 α8.2 explicitly does NOT build (forbidden this slice)
Polling · webhook receiver · **completion service (α8.3)** · resume/`advance`-after-pause
logic · Celery · Redis · broker · **usage recording for the async job** (α8.3 records the
terminal row under the same `request_id`) · storage · media registration (α8.4) · export ·
`video_ref` population · image-path changes · multi-provider fallback · provider selection ·
health-ordering · rate limiter · circuit breaker · **any change to** the runner,
dispatcher, recorder, relay, lock manager, `ProviderRegistry` class, neutral DTOs,
`ports.py`, or the `generate-video` pipeline. **Zero Alembic migration.**

---

## 3. Recommended decisions (to be confirmed in §4)

**Q1 — Which async provider?  → Fal.ai (recommended).**
Its queue API is submit → `{request_id, status_url, response_url}` → resolve-later, which
is *exactly* the `IN_PROGRESS` + `provider_job_id` shape the pause seam expects; mature
docs, stable REST, both polling and webhooks available (for α8.3). Runway/others are
viable later; Fal is the cleanest first async adapter.

**Q2 — HTTP client: raw `httpx`.**
Same rationale as α8.1 (leaf purity, error-map control, W7.6.2 — no hidden SDK retries).
Reuse the established adapter pattern verbatim.

**Q3 — Submit-only semantics.  → one POST to the Fal queue submit endpoint; return
`IN_PROGRESS`; do NOT wait/poll.**
The adapter makes **exactly one** HTTP call (W7.6.2): it submits the job and immediately
returns `IN_PROGRESS`. It never polls the `status_url`, never blocks, never resolves the
result. The run stops at the pause boundary (W8.2.2). Completion is α8.3.

**Q4 — `provider_job_id` vs the opaque `output`.  → `provider_job_id` = the Fal
`request_id`; the completion coordinates go into a *versioned* opaque `output`.**
`provider_job_id` is the single resume coordinate the runner checkpoints + emits in
`WorkflowRunPaused` (§1.1) — it must be the Fal `request_id`. The completion URLs Fal
returns are put in the **opaque** `output` envelope, which the runner checkpoints
verbatim without inspecting (W7.6.1); α8.3 reads them from the checkpoint to poll/resume.
The envelope carries a **`schema_version`** so α8.3 has a stable payload contract that can
evolve without breaking older checkpoints:

```json
{ "schema_version": 1, "provider": "fal", "provider_job_id": "...",
  "status_url": "...", "response_url": "..." }
```

This keeps everything α8.3 needs persisted **now**, with zero runner change.

**Q5 — Usage on `IN_PROGRESS`.  → return `usage=None`.**
The runner discards usage on pause (§1.1), so this is behaviourally invisible; `None` is
the honest value (no billable terminal outcome yet). α8.3 records the priced terminal row
under the same deterministic `request_id`. (The mock returns an *ignored* `seconds`
usage; the real adapter returning `None` is observationally equivalent on every field the
runner reads — W8.2.1.)

**Q6 — Auth.  → `Authorization: Key <FAL_KEY>` header, injected via a pre-authenticated
shared client (W8.1.1).**
Fal uses the `Key` scheme (not `Bearer`). As in α8.1 the container builds a single shared
`httpx.AsyncClient` with the header + base URL + timeout baked in and injects it; the
adapter is configuration-blind and never sees the raw key (Q4/W8.1.1 from α8.1 —
*constructors receive secrets, they never retrieve them*).

**Q7 — Model / endpoint.  → the workflow `model` arg carries the Fal route (e.g.
`fal-ai/ltx-video`); the adapter validates it against a supported set and threads
`duration_seconds` into the Fal input.**
Submit target is `POST {base_url}/{model}` (Fal queue). The adapter validates the model
is a supported video route (unsupported → terminal `ProviderValidationError`, **no** HTTP
call — mirrors α8.1's model guard) and maps `prompt` + `duration_seconds` into the Fal
input body. A pricing row for the chosen `model_id` is an operator data-seed for α8.3's
terminal usage — **not** a migration, and **not** needed for α8.2 (no usage on pause).

**Q8 — Error mapping.  → same fixed status → `ProviderError` map as α8.1, on the submit
call only.**

| Condition | Mapped error | Class |
|---|---|---|
| 401 / 403 | `ProviderAuthenticationError` | terminal |
| 400 / 422 / other 4xx | `ProviderValidationError` | terminal |
| 429 | `ProviderRateLimited` | transient |
| 5xx / connection | `ProviderUnavailable` | transient |
| `httpx.TimeoutException` | `ProviderTimeout` | transient |
| 200 / 201 (accepted) | `GenerateVideoResponse(status=IN_PROGRESS, …)` | — |

No new error type; transient submit failures raise a transient `ProviderError` → the
runner re-dispatches under the same `request_id` (W7.6.2).

**Q9 — Metadata flags.  → `supports_polling=True`, `supports_webhooks=True`.**
Truthful for Fal and consumed by α8.3's completion service (which branches on metadata,
not provider identity — ADR-0041 D5). Advertising them now is correct even though α8.2
acts on neither.

**Q10 — Registry composition.  → extend `_build_provider_registry` with a VIDEO branch,
independent of the IMAGE branch.**
Real VIDEO iff a Fal key is configured, else mock; IMAGE independently real/mock per the
OpenAI key. Still **exactly one provider per capability**, `resolve` stays a direct
lookup — no selection engine, no fallback, no health-ordering.

**Invariants.**
* **W8.2.1 — observational equivalence with `MockVideoProvider` on the async path.** The
  Fal adapter returns the same DTO (`GenerateVideoResponse`), the same `IN_PROGRESS`
  status, a set `provider_job_id`, and an `output` envelope — the runner cannot tell it
  from the mock; only the values (real Fal `request_id` + URLs, provider id) and
  `usage=None` vs the mock's ignored usage differ.
* **W8.2.2 — the run stops at the pause boundary.** α8.2 introduces no completion,
  resume, poll, or webhook; a paused run stays paused until α8.3.
* **W8.2.3 — the adapter never mutates orchestration state.** The Fal adapter never
  resumes, completes, checkpoints, emits events, or records usage — it *only* returns a
  `ProviderResponse(IN_PROGRESS)`. All state transitions remain owned by the runner (now)
  and the completion service (α8.3). The adapter is a pure request→response leaf with no
  reference to the UoW, event bus, checkpoint store, or usage recorder.
* Reuses **W8.1.1** (config-blind adapter) and **W7.6.2** (one dispatch / one HTTP call).

---

## 4. Open questions for sign-off

| # | Question | Recommendation |
|---|---|---|
| Q1 | Async provider | **Fal.ai** (queue API = natural `IN_PROGRESS`/pause) |
| Q2 | HTTP client | **Raw `httpx`** (reuse α8.1 pattern) |
| Q3 | Submit-only | **One POST → `IN_PROGRESS`**; no poll/wait; run stops at pause (W8.2.2) |
| Q4 | job id vs output | **`provider_job_id` = Fal `request_id`**; `status_url`/`response_url` in opaque `output` for α8.3 |
| Q5 | Usage on pause | **`usage=None`** (runner discards it anyway; α8.3 records terminal usage) |
| Q6 | Auth | **`Authorization: Key <FAL_KEY>`**, injected pre-authenticated client (W8.1.1) |
| Q7 | Model/endpoint | **`model` arg = Fal route**, validated against a supported set; `duration_seconds` threaded |
| Q8 | Error mapping | **Same status→`ProviderError` map as α8.1**, submit-only; no new types |
| Q9 | Metadata flags | **`supports_polling=True`, `supports_webhooks=True`** (truthful; α8.3 consumes) |
| Q10 | Registry composition | **VIDEO branch in `_build_provider_registry`**, independent of IMAGE; one provider per capability |
| — | **Invariant W8.2.1** | Observational equivalence with `MockVideoProvider` on the `IN_PROGRESS` path |
| — | **Invariant W8.2.2** | The run stops at the pause boundary — no completion/resume/poll/webhook this slice |
| — | **Invariant W8.2.3** | The adapter never mutates orchestration state (no resume/complete/checkpoint/emit/usage) — pure request→response leaf |

**Explicit no-change confirmation requested:** α8.2 touches only (a) the provider leaf
(new `fal/` subpackage), (b) `Settings` + env files, (c) `_build_provider_registry` +
a `_build_fal_client` helper in the container, (d) tests + docs. It does **not** change
the runner, dispatcher, recorder, relay, lock manager, `ProviderRegistry` class, neutral
DTOs, `ports.py`, or the `generate-video` pipeline, and introduces **no migration**.

---

## 5. Component / contract sketch (illustrative — not yet implemented)

### 5.1 Adapter (new file in the leaf, e.g. `providers/fal/video.py`)
```python
class FalVideoProvider:  # implements VideoProvider (structural)
    metadata = ProviderMetadata(
        id="fal-video", name="Fal.ai Video", capability=Capability.VIDEO,
        supports_polling=True, supports_webhooks=True, version="1.0",
    )

    def __init__(self, *, client: httpx.AsyncClient) -> None:
        self._client = client  # pre-authenticated (Key header) + base_url + timeout

    async def generate_video(self, req: GenerateVideoRequest) -> GenerateVideoResponse:
        model = req.model or _DEFAULT_MODEL
        if model not in _SUPPORTED_MODELS:
            raise ProviderValidationError(...)          # terminal, no HTTP
        body = {"prompt": req.prompt}
        if req.duration_seconds:
            body["duration"] = req.duration_seconds
        resp = await self._submit(model, body)          # ONE POST (W7.6.2)
        self._raise_for_status(resp)                    # status → ProviderError (Q8)
        data = self._parse(resp)                        # {request_id, status_url, response_url}
        job_id = str(data["request_id"])
        return GenerateVideoResponse(
            request_id=req.request_id, provider=self.metadata.id,
            status=ProviderStatus.IN_PROGRESS,          # W8.2.2 — stop at pause
            provider_job_id=job_id,                     # Q4 — resume coordinate
            output={                                    # versioned opaque envelope for α8.3
                "schema_version": 1,
                "provider": "fal",
                "provider_job_id": job_id,
                "status_url": data.get("status_url"),
                "response_url": data.get("response_url"),
            },
            usage=None,                                 # Q5 — no usage on pause
        )

    async def health(self) -> ProviderHealth:
        return ProviderHealth(healthy=True, detail="static")
```

### 5.2 Registry composition (container — one added VIDEO branch)
```python
def _build_provider_registry(openai_client, fal_client) -> ProviderRegistry:
    registry = ProviderRegistry()
    registry.register(provider=MockLLMProvider(), capabilities=[Capability.LLM])
    registry.register(provider=MockVoiceProvider(), capabilities=[Capability.VOICE])
    image = OpenAIImageProvider(client=openai_client) if openai_client else MockImageProvider()
    video = FalVideoProvider(client=fal_client) if fal_client else MockVideoProvider()
    registry.register(provider=image, capabilities=[Capability.IMAGE])
    registry.register(provider=video, capabilities=[Capability.VIDEO])
    return registry
```

### 5.3 Config (`core/config.py`)
```python
fal_api_key: SecretStr | None = Field(default=None)
fal_base_url: str = Field(default="https://queue.fal.run")
fal_timeout_seconds: float = Field(default=60.0, gt=0)
```

---

## 6. Reviewer sign-off — **APPROVED (2026-07-21)**

Q1–Q10 approved as recommended. Invariants **W8.2.1** and **W8.2.2** approved. Two
additions folded in: **W8.2.3** (the adapter never mutates orchestration state) and a
**versioned opaque checkpoint envelope** (`schema_version: 1`, Q4) so α8.3 gets a stable
payload contract. No-change confirmation accepted — the slice touches only the provider
leaf, `Settings` + env, the container's registry composition, and tests/docs; zero
migration.

**Execution:** branch `phase3/alpha8.2-fal-video`, bump `0.4.22-phase3-alpha8.2-dev`,
implement per §5, green the full CI gate (ruff · black · mypy · import-linter · unit — CI
never calls Fal), then pause for release approval before finalize/tag
`v0.4.22-phase3-alpha8.2`.
