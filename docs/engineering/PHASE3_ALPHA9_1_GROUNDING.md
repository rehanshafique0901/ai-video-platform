# α9.1 — AI Caption & Hashtag Generation — Grounding (read-only)

> **Status:** Grounding complete. **A genuine architectural decision was discovered → an ADR is
> required** (see §10). Per the standing workflow, work **stops here** for ADR authorship before
> pre-flight.
> **Baseline:** `v0.4.43-phase3-alpha9.0` (frozen; `main == origin/main` @ `f74abcd`, no open PRs,
> only `main` remote branch). Slice source of truth:
> [`NEXT_VERTICAL_SLICES_DISCOVERY.md`](./NEXT_VERTICAL_SLICES_DISCOVERY.md) §2.3 / §4 (#4).
> **Method:** repository-only; every claim cited to `file:line`; no behaviour inferred; no code,
> migration, commit, branch, or PR produced.

## 0. Objective

Auto-generate a video's publish metadata — **title, description, hashtags/tags** — via the LLM
capability, as an **opt-in** creator convenience, **without disturbing any frozen runtime**. The
platform pre-declared this slice: PUB-9 ("metadata generation is deterministic in v1; **LLM
metadata is a later slice** §5") and Decision 4 / §14 of
[`PUBLISHING_RUNTIME_CONTRACT.md`](./PUBLISHING_RUNTIME_CONTRACT.md) ("its own later slice with its
own contract").

---

## 1. Existing LLM abstraction (objective 1)

Modelled as a **capability-scoped provider** (ADR-0041), not a bespoke `ILLM`.

| Concern | Verified source |
|---|---|
| **Capability enum** | `Capability.LLM = "llm"` — `app/application/interfaces/providers.py:31` |
| **Interface** | `LLMProvider(Provider, Protocol)`: `async def generate_text(self, req: GenerateTextRequest) -> GenerateTextResponse` — `app/infrastructure/ai/providers/ports.py:45` |
| **Request DTO** | `GenerateTextRequest(request_id, prompt, model=None, max_tokens=None, params={})` — `providers.py:121`. **No `seed` field.** |
| **Response DTO** | `GenerateTextResponse(ProviderResponse)` adds `text: str`; envelope carries `request_id, provider, status, output, usage, error` — `providers.py:94,128` |
| **Mock impl** | `MockLLMProvider` — `app/infrastructure/ai/providers/mocks/mock_llm.py:16`: pure deterministic echo `f"[mock-llm] {prompt}"`, always `SUCCEEDED` inline, `usage=ProviderUsage(unit="tokens", quantity=len(prompt.split()))`; ignores `model`/`max_tokens`/`params` |
| **Dependency injection** | `_build_provider_registry` — `app/core/container.py:409` registers `MockLLMProvider` **unconditionally** ("LLM / VOICE are always mock"); only IMAGE (OpenAI) / VIDEO (Fal) are conditionally real. **No real LLM adapter exists.** |
| **Prompt execution flow** | Workflow only: `AdvanceWorkflowRun._execute_commands` → `ProviderDispatcherPort.dispatch` → `StepCommandDispatcher._generate_text` (`app/infrastructure/ai/dispatcher.py:87`) → `registry.resolve(Capability.LLM).generate_text(...)` |
| **Model selection** | No runtime model *selection* — registry is one-provider-per-capability, `resolve` is a direct lookup, no fallback/priority (`container.py:409` docstring). `GenerateTextRequest.model` is a passthrough string; usage recording separately requires a **`model_id` UUID** (runner fails fast `MODEL_ID_MISSING`, `advance_workflow_run.py:663`) |
| **Retry behaviour** | Dispatcher does **not** retry (exactly one call/command); the runner catches `ProviderError` and classifies via `exc.transient` → transient (retry step) vs terminal (fail step) — `advance_workflow_run.py:684`. Request id is replay-stable `"{run.id}:{step_index}:{command_index}"` (`:674`) |
| **Failure semantics** | Typed `ProviderError` hierarchy (`ProviderUnavailable/RateLimited/Timeout/Authentication/Validation/NoProviderAvailable`) — `providers.py:184`; providers never leak HTTP. Mock never raises. |
| **Timeouts** | No LLM-specific timeout in `Settings` (`openai_timeout_seconds` is IMAGE-only — `app/core/config.py:74`) |
| **Usage/cost** | `record_usage_in_uow` inside the runner txn (`advance_workflow_run.py:735`), idempotent on `request_id`; LLM accounting axis = completion tokens (`use_cases/usage/accounting.py`); `credit_ledger` deferred (`credits_consumed=0`) |

**Verified critical fact:** the LLM capability + mock + dispatcher exist, but **no production code
path emits `generate_text`** today — registered workflows emit only `generate_image` /
`generate_video` (`app/domain/workflow/registry.py:306,317`). **α9.1 would be the first real LLM
consumer.**

---

## 2. Caption entry point — full pipeline trace (objective 2)

The pipeline **exists as composed use cases across bounded contexts** — there is **no single
Prompt→Publish orchestrator** (verified). Stage-by-stage, from source:

| Stage | Exists? | Evidence | LLM today? | Frozen? |
|---|---|---|---|---|
| **Prompt** | ✅ | `Prompt` aggregate `app/domain/prompts/prompt.py:39` (ADR-0036, **no `version`**); `GenerateVideoRequest.prompt` `use_cases/generation/request.py:23` | no | generation plane |
| **Planning** | ✅ | `plan_from_prompt` → `GenerationPlan`/`Shot` `app/domain/generation/planner.py:108`, `plan.py:37`; **pure & deterministic**, no LLM (`planner.py:6`) | no | ✅ frozen |
| **Generation** | ✅ | `GenerateVideo` (`use_cases/generation/generate_video.py:3`) + `AdvanceWorkflowRun`/`StepCommand`; assets → `generation_assets`; `IngestGeneratedMedia`/`PromoteGenerationAssets` → `media_assets` | image/video only | ✅ frozen |
| **Verification** | ✅ | `verify_image` `app/domain/generation/verification.py:114`; `verify_timeline` `timeline_verification.py`; wired `generate_video.py:363,234`. **Per-shot/timeline, deterministic** | no | ✅ frozen |
| **Repair** | ✅ | `decide_repair` (`ACCEPT/RETRY/GIVE_UP`) `app/domain/generation/repair.py:33`, wired `generate_video.py:365`; regenerates only the failed shot | no | ✅ frozen |
| **Export** | ✅ | Delivery chain: `ProcessRenderJob` → master `MediaAsset`; `ProcessExportJob` → delivery `MediaAsset` + `ExportStatus.SUCCEEDED` (`use_cases/export/process_export_job.py:237`) | no | ✅ frozen |
| **Publish** | ✅ | `CreatePublishJob` reads `export_jobs.output_media_asset_id`, builds `ContentPackage` (`create_publish_job.py:115,148`); `ProcessPublishJob` uploads | no | ✅ frozen |

CI validates this chain end-to-end: `ci_gate.py` Stage 13 "generation end-to-end slice
(Prompt→Planner→Resolver→Generate→Verify→Repair→Timeline→FFmpeg→MP4→persistence)" and Stages 14–18
for publish/export/adjacent contexts.

### Earliest vs latest valid insertion point

- **Earliest technically-possible:** at **Planning** (the prompt/title/plan are known). But
  captions describe the *finished, published* video, and planning + generation + verification +
  repair + render + export are **all frozen, deterministic** plane code. Inserting an LLM here
  would (a) mutate a frozen runtime, (b) cross the `app.domain.generation` ↔ AI-plane and
  generation↔publishing boundaries, and (c) break PUB-9/planner determinism. **Rejected.**
- **Latest valid:** at **publish creation**, upstream of the frozen publish *runtime*
  (`ProcessPublishJob` untouched), feeding the **existing optional overrides**
  (`PublishJobCreateRequest.title/description/tags` → `build_content_package`). This is the **only
  seam that (1) touches `ContentPackage`, (2) sits entirely outside every frozen runtime, and (3)
  already accepts caller-supplied metadata.**

**Conclusion:** the insertion point that preserves the frozen architecture is a **new opt-in
suggest/preview step at/just-before publish-create**, producing values that flow through the
*unchanged* override path. No generation/render/export/publish runtime changes.

---

## 3. `ContentPackage` ownership (objective 3)

| Property | Verified source |
|---|---|
| **Ownership** | Publishing bounded context. `app/domain/publishing/content_package.py:30`; import-linter "Publishing domain is an isolated bounded context" forbids `app.domain.publishing` importing `app.domain.generation`/`workflow` (`pyproject.toml:377`) |
| **Lifecycle** | Built **once** at `CreatePublishJob` (`create_publish_job.py:148`), persisted on the job, **never mutated** by the worker (worker reads `job.content_package`) |
| **Mutability** | Immutable — `@dataclass(frozen=True, slots=True)` (`content_package.py:30`) |
| **Construction** | `build_content_package(...)` — pure, deterministic; `title←(title or project_title or "Untitled video")`, `description←description or title`, `tags←()`, `visibility←PRIVATE` (`content_package.py:77`) |
| **Consumers** | `CreatePublishJob` (writer); `ProcessPublishJob` + `IDestinationPublisher` (YouTube adapter) reader (`process_publish_job.py`, `destinations/youtube.py`) |
| **Persistence** | Serialized to `publish_jobs.content_package` **JSONB** (`db/models/publishing.py`), `to_dict`/`from_dict` round-trip |

**Determination:** generated caption *values* (title/description/tags) belong **inside
`ContentPackage`** — that is the established, frozen contract for "what to publish." They must
**not** enter via a new field or a mutated package; they enter through the **existing override
inputs** to `build_content_package`. Generation *provenance* (which model/template) does **not**
belong in the immutable `ContentPackage` value contract (see §5).

---

## 4. Persistence (objective 4)

**Determination: remain ephemeral for the suggestion itself; persist accepted values only in the
existing `PublishJob.content_package` JSONB; persist cost only in the existing `usage_records`. No
new persistence.**

Ground:
- Publish metadata already persists **only** as `publish_jobs.content_package` JSONB, written once
  at creation (§3). Accepted suggestions reuse this exact path.
- A **suggest/preview** call returns proposed values to the caller and **writes nothing** unless
  the caller then creates a publish job with them → no new store.
- **Media** persistence is out of scope: captions are publish-time metadata, not media artifacts;
  `media_assets` has no title/description/tags concept and the media library is fenced from the
  execution plane (ADR-0046 X8, `pyproject.toml:411`). Persisting captions in Media would violate
  that boundary. **Rejected.**
- **LLM cost** persists in existing `usage_records` (α7.5), idempotent on `request_id`.
- Matches discovery: "*Persistence: none (JSONB)*", "*no migration*" (§2.3, §4 #4).

---

## 5. Prompt provenance (objective 5)

- **Existing provenance infra:** (a) `Prompt` aggregate has **no `version` field** — ADR-0036
  explicitly rules prompts are "generation inputs, not editorial content" with no `VersionMixin`
  (`app/domain/prompts/prompt.py:14`); it *does* carry `model_id: UUID | None` (`:47`). (b)
  `usage_records` records `model_id` + `request_id` + `capability` per provider call. (c) The
  generation slice records rich per-shot provenance (seed/attempts) but inside `generation_assets`,
  a frozen plane we must not touch.
- **Missing seam:** there is **no** representation for *caption/LLM prompt-template version*. The
  LLM `prompt` string is ephemeral on `GenerateTextRequest`; nothing versions the *template* used
  to build a caption prompt.
- **Determination:** represent provenance as an **optional lightweight value object returned with
  the suggestion** — `generator` (e.g. `"llm"`/`"template"`), `model`, `prompt_template_version`
  (a code-level constant/string, not a DB row), `generated_at`, `is_fallback`. Whether this VO is
  **echoed into `content_package` properties or kept response-only** is an **ADR-level decision**
  (§10). No new provenance table is warranted (would duplicate `usage_records`/violate ADR-0036's
  "no versioning" posture).

---

## 6. Idempotency & determinism (objective 6)

| Concern | Verified behaviour / determination |
|---|---|
| **Publish replay** | `CreatePublishJob` is idempotent on active `(source_media_asset_id, social_account_id)` and **does not rebuild metadata on replay** (`create_publish_job.py:128`). α9.1 must not weaken this. |
| **Retries** | LLM usage recording is idempotent on `request_id` (α7.5); a suggest call that records usage needs a defined `request_id` (client-supplied or derived) to avoid double-billing under retry. |
| **Regeneration** | A stateless suggest endpoint may be called repeatedly (user re-rolls captions); each call is independent and writes nothing — safe by construction. No dedupe row required. |
| **Duplicate-publish protection** | Unchanged — still enforced by the existing active-job idempotency key; captions do not participate in that key. |
| **Deterministic behaviour** | Current publish path is deterministic (PUB-9). An LLM makes the *suggestion* non-deterministic; the **deterministic template must remain the mandatory fallback** (LLM disabled/unavailable/over-limit → deterministic result), so the default publish path's invariant is preserved. Under CI, the LLM is the deterministic `MockLLMProvider`, keeping Stage tests reproducible. |

---

## 7. YouTube interaction (objective 7)

Verified mapping in `app/infrastructure/publishing/destinations/youtube.py`:
- Limits (`:43`): **title ≤100**, **description ≤5000**, **tags ≤500 total characters** (sum of
  tag lengths — not per-tag, not count).
- `_build_request_body` (`:80`): `title→snippet.title`, `description→snippet.description`,
  non-empty `tags→snippet.tags=list(...)`; `publish_at`→`status.privacyStatus="private"` +
  `status.publishAt` ISO8601, else `status.privacyStatus=visibility.value`;
  `selfDeclaredMadeForKids=False`.
- Over-limit / empty title → **permanent `invalid_metadata`** failure.

**Determination:** the generator must produce metadata within the **strictest destination limits**
(title ≤100, tags ≤500 total chars) so a generated caption never becomes a permanent publish
failure. **No adapter responsibility leaks into generation:** the YouTube adapter continues to be
the sole enforcer/mapper at the boundary (defence in depth); the generator merely targets those
limits. **PUB-4 preserved** — the generator is neither a destination nor an AI provider *inside*
publishing; destination adapters remain credential-blind leaves (`pyproject.toml:390`).

---

## 8. Existing AI capabilities to reuse (objective 8)

**Reuse — do not build parallel infrastructure:**
1. `Capability.LLM` + `LLMProvider.generate_text` (`ports.py:45`).
2. `GenerateTextRequest` / `GenerateTextResponse` DTOs (`providers.py:121`).
3. `MockLLMProvider` (deterministic CI default) (`mock_llm.py:16`).
4. `ProviderRegistry` + `_build_provider_registry` wiring (`registry.py`, `container.py:409`).
5. Provider dispatch seam (`StepCommandDispatcher` / `ProviderDispatcherPort`) **or** a direct
   registry-resolved call from a new infra adapter (pre-flight to choose; both already exist).
6. `ProviderError` hierarchy + `transient` classification (`providers.py:184`).
7. `usage_records` via `record_usage_in_uow` / RecordUsage (`advance_workflow_run.py:735`,
   `use_cases/usage/`).
8. `build_content_package` overrides as the value sink (`content_package.py:77`).

**New (additive only):** a publishing-owned **port** (`IPublishMetadataGenerator`) + an
**infrastructure adapter** bridging to the LLM plane + a `GeneratePublishMetadata` **use case** +
an **opt-in API** — no parallel AI stack.

---

## 9. Migration assessment (objective 9)

**No migration is required — proof:**
1. **Values** persist via existing `publish_jobs.content_package` JSONB through the existing
   override path (§3, §4) — no new column/table.
2. **Cost** persists via existing `usage_records` (§1, §6) — no new column/table.
3. **Provenance** (§5) is a response-side VO (and optionally JSONB properties inside the existing
   `content_package`) — no schema object, and it would violate ADR-0036 to add prompt versioning.
4. No new uniqueness/ownership invariant is introduced (contrast α9.0, which genuinely needed
   `0015` for a new partition-compatible unique index). Nothing here requires DB enforcement.
5. `validate_schema.py` / `compare_erd.py` derive expectations from ORM metadata; with **no ORM
   change**, both remain green with no edits.

∴ Zero migrations; explicitly proven by the absence of any new persisted field, table, index, or
invariant.

---

## 10. ADR assessment (objective 10) → **ADR REQUIRED**

**Threshold (as applied in α8/α9):** an ADR is written only when a decision introduces or changes a
**cross-cutting invariant or bounded-context boundary** that existing patterns don't already settle
(e.g. ADR-0046 execution/media boundary, ADR-0047 credential ownership, ADR-0048 analytics
exactly-once). α9.1 **clears that bar** — exactly one genuine decision:

> **How does the deterministic Publishing plane consume the non-deterministic AI LLM plane for
> publish metadata, while preserving PUB-4, the credential-blind/bounded-context isolation, and the
> PUB-9 determinism guarantee?**

This is architectural, not mechanical, because it fixes:
1. **Boundary & ownership** — a **new publishing-owned port** + infra adapter is the *first*
   sanctioned dependency from the publishing application plane **into** the AI LLM plane. The
   direction and owning layer must be pinned (import-linter) so `app.domain.publishing` and the
   destination leaves never import the AI plane (PUB-4 + four existing contracts).
2. **Determinism vs LLM** — α9.1 changes a documented invariant (PUB-9). The ADR must rule the
   feature **opt-in, suggestion-only, with the deterministic template as mandatory fallback**.
3. **Provenance** — response-only VO vs. echoed into `content_package` (§5).
4. **Cost/idempotency posture** — whether the suggest call records `usage_records` (needs
   `request_id` + resolved LLM `model_id`) or is an unmetered v1 preview (§6).

The publishing contract **explicitly anticipated a dedicated decision here** ("its own later slice
with its own contract" — Decision 4 / §5 / §14; PUB-9), and the discovery report expects "a new
engineering contract" (§2.3 pt 4). Weight is comparable to ADR-0047/0048.

**Recommended:** `ADR-0049 — AI publish-metadata boundary & determinism` (opt-in, suggestion-only,
deterministic fallback, publishing-owned port + infra adapter, no migration), plus a companion
engineering-contract addendum during implementation.

### Out of scope for α9.1 (confirm at pre-flight)
Auto-applying captions without opt-in; a real LLM provider adapter (mock stays CI default);
thumbnail generation (§2.4); multilingual captions; per-destination metadata variants; any separate
metadata-history table.

---

## 11. Conclusion & stop

- **Reuse:** LLM capability + mock + dispatcher + registry; `ContentPackage`/`build_content_package`
  overrides; `usage_records`. **New/additive:** publishing-owned metadata port + infra adapter +
  `GeneratePublishMetadata` use case + opt-in suggest API. **No migration; no frozen-runtime change;
  PUB-4 + PUB-9 preserved via mandatory deterministic fallback.**
- **Insertion point:** opt-in suggest/preview at publish-create feeding the existing override path
  (the latest, frozen-safe seam).
- **Blocking gate:** the single genuine architectural decision requires **ADR-0049**.

**Stopping here for ADR authorship** (per the stop condition). On approval, I will draft only
ADR-0049 and await sign-off before pre-flight.
