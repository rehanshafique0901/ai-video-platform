# α9.1 — AI Caption & Hashtag Generation — Pre-flight (design, pre-implementation)

> **Status:** Approved-for-review. Design blueprint for α9.1, bound by
> [ADR-0049](../decisions/ADR-0049-ai-publish-metadata-boundary.md) (**Accepted**) and
> [`PHASE3_ALPHA9_1_GROUNDING.md`](./PHASE3_ALPHA9_1_GROUNDING.md). **No code yet** — per the
> established workflow this pre-flight **stops for review before implementation.**
> **Baseline:** `v0.4.43-phase3-alpha9.0` (frozen).
> **Pre-flight architectural-decision check:** the design below stays entirely within existing
> ports-&-adapters / DI / API / import-linter patterns and the boundaries ADR-0049 already fixed —
> **no new architectural decision is introduced** (see §12). One product-level ruling (metering is
> out of scope for v1) is additive/reversible, not architectural.

---

## 1. Scope (what ships in α9.1)

An **opt-in, suggestion-only** endpoint that returns AI-suggested `title` / `description` /
`tags` (hashtags) for a creator's finished/owned export, which the creator may accept, edit, or
discard before creating a publish job through the **existing** `POST /publish-jobs` override path.
Strictly additive; no frozen-runtime edit; no migration. Under CI the LLM is the deterministic
`MockLLMProvider`. A **real** LLM provider adapter and **usage metering** are explicitly **out of
scope** (deferred, additive future work — ADR-0049 §"does not decide", and §12 below).

**Insertion point (grounding §2, ADR-0049):** the latest frozen-safe seam — a new suggest use case
upstream of publish-create; its output flows through the unchanged `build_content_package` overrides.

---

## 2. Exact publishing-owned interface (pre-flight Q1)

New file `app/application/interfaces/publish_metadata_generator.py` — **owned by the Publishing
application layer**, **neutral** (no `ContentPackage`, no `app.domain.publishing` import), mirroring
the shape of `IImageGenerator`:

```python
class PublishMetadataGenerationError(RuntimeError):
    """Raised by an adapter when it cannot produce valid metadata (provider error, timeout,
    quota, unparetable/over-limit output). The Publishing use case catches this and falls back
    to the deterministic template (ADR-0049 Invariant 3)."""


@dataclass(frozen=True, slots=True)
class PublishMetadataRequest:
    request_id: str                 # caller-minted; tracing + (future) usage dedup
    context: str                    # neutral video description (project title + prompt text)
    max_title_chars: int            # strictest destination cap (default 100 — YouTube)
    max_description_chars: int      # default 5000
    max_tags_total_chars: int       # strictest destination cap (default 500 — YouTube)
    max_tag_count: int              # default 15
    locale: str | None = None


@dataclass(frozen=True, slots=True)
class MetadataProvenance:           # EPHEMERAL — response-only, never persisted (Invariant 5)
    generator: str                  # "llm" | "template"
    is_fallback: bool
    model: str | None = None
    prompt_template_version: str | None = None


@dataclass(frozen=True, slots=True)
class GeneratedPublishMetadata:
    title: str
    description: str
    tags: tuple[str, ...]
    provenance: MetadataProvenance


class IPublishMetadataGenerator(ABC):
    @abstractmethod
    async def generate(self, req: PublishMetadataRequest) -> GeneratedPublishMetadata:
        """Produce suggested metadata within the given caps, or raise
        PublishMetadataGenerationError. Implementations MUST NOT import app.domain.publishing."""
        ...
```

- **Ownership direction (ADR-0049):** the Publishing `GeneratePublishMetadata` use case depends on
  this port; the AI subsystem *implements* it. Neutral DTOs guarantee the adapter creates **no**
  dependency on the Publishing bounded context.

---

## 3. Use case + fallback ownership

New file `app/application/use_cases/publishing/generate_publish_metadata.py`:

```python
class GeneratePublishMetadata:
    def __init__(self, uow: IUnitOfWork, generator: IPublishMetadataGenerator) -> None: ...

    async def execute(
        self, *, tenant_id: UUID, owner_user_id: UUID, export_job_id: UUID, request_id: str
    ) -> GeneratedPublishMetadata:
        # 1) Owner-scoped context (reuse existing reads; read-only, no commit):
        async with self._uow:
            source = await self._uow.publish_jobs.resolve_source(
                export_job_id, tenant_id=tenant_id, owner_user_id=owner_user_id)
            if source is None:
                raise NotFoundError(...)              # 404 — not owned / no such export
            project = await self._uow.projects.get_owned(source.project_id, tenant_id, owner_user_id)
        context = _build_context(project)             # project name (+ prompt text if present)

        # 2) Advisory AI call with mandatory deterministic fallback (Invariants 1–3):
        try:
            return await self._generator.generate(PublishMetadataRequest(context=context, ...))
        except PublishMetadataGenerationError:
            return _deterministic_fallback(project)   # template; generator="template", is_fallback=True
```

- **The deterministic fallback lives in Publishing** (where the template lives), reusing the same
  defaults as `build_content_package` (title←project name / "Untitled video", description←title,
  tags←()). The AI adapter never imports it → one-way boundary preserved.
- **No writes, no commit** — suggestion is side-effect-free. Ownership is proven by the existing
  `resolve_source` join (404 otherwise). Export readiness is **not** required for a suggestion.

---

## 4. AI-subsystem infrastructure adapter (pre-flight Q7 mock strategy)

New file `app/infrastructure/ai/metadata/llm_publish_metadata_generator.py` — **provided by the AI
subsystem**, implements the neutral port, mirrors `PollinationsImageGenerator` (infra implementing an
application port):

```python
PROMPT_TEMPLATE_VERSION = "cap-hashtag/v1"

class LlmPublishMetadataGenerator(IPublishMetadataGenerator):
    def __init__(self, registry: ProviderRegistry, *, timeout_seconds: float, model: str | None): ...

    async def generate(self, req):
        provider = cast(LLMProvider, self._registry.resolve(Capability.LLM))
        prompt = _render_prompt(PROMPT_TEMPLATE_VERSION, req)          # deterministic template
        try:
            resp = await asyncio.wait_for(
                provider.generate_text(GenerateTextRequest(request_id=req.request_id, prompt=prompt,
                                                           model=self._model)),
                timeout=self._timeout,
            )
        except (ProviderError, asyncio.TimeoutError, ...) as e:
            raise PublishMetadataGenerationError(...) from e
        if resp.status is not ProviderStatus.SUCCEEDED:
            raise PublishMetadataGenerationError(...)
        title, description, tags = _parse(resp.text)                   # deterministic parse
        title, description, tags = _enforce_caps(title, description, tags, req)  # trim/truncate
        if not title:
            raise PublishMetadataGenerationError("empty title")
        return GeneratedPublishMetadata(title, description, tags,
                 MetadataProvenance(generator="llm", is_fallback=False,
                                    model=self._model, prompt_template_version=PROMPT_TEMPLATE_VERSION))
```

- **Mock strategy (Q7):** no bespoke mock is needed — the process-wide `ProviderRegistry` already
  resolves `Capability.LLM` to the deterministic `MockLLMProvider` (`container.py:_build_provider_
  registry`, "LLM always mock"). So `LlmPublishMetadataGenerator` over the default registry is
  **fully deterministic** in unit + integration + CI. `_parse` is written to deterministically read
  the mock's `"[mock-llm] {prompt}"` echo into a stable `(title, description, tags)` triple. For
  use-case unit tests, a tiny in-file `FakePublishMetadataGenerator` (success + raising variants) is
  used so the use case is tested without any AI wiring.
- **MUST NOT import** `app.domain.publishing`, `app.application.use_cases.publishing`, or
  `app.infrastructure.publishing` (enforced by §10).

---

## 5. Request/response DTOs (pre-flight Q3, API surface)

New file `app/api/v1/schemas/publish_metadata.py`:

```python
class PublishMetadataSuggestRequest(BaseModel):
    export_job_id: UUID

class MetadataProvenancePublic(BaseModel):     # ephemeral, response-only
    generator: str
    is_fallback: bool
    model: str | None = None
    prompt_template_version: str | None = None

class PublishMetadataSuggestionPublic(BaseModel):
    title: str
    description: str
    tags: list[str]
    provenance: MetadataProvenancePublic
    @classmethod
    def from_domain(cls, m: GeneratedPublishMetadata) -> "PublishMetadataSuggestionPublic": ...
```

**Endpoint** — new router `app/api/v1/routers/publish_metadata.py`, mounted in `main.py`:
`POST /api/v1/publish-metadata/suggestions` (authenticated via `CurrentUserDep`). The router mints a
`request_id` (e.g. `uuid4().hex`), calls the use case, envelopes the result. Always `200` on an
owned export (AI success **or** deterministic fallback); `404` if the export is not the caller's.

---

## 6. Dependency-injection wiring (pre-flight Q2)

In `app/core/container.py` (mirrors `_get_image_generator` + `get_create_publish_job_use_case`):

```python
_publish_metadata_generator: IPublishMetadataGenerator | None = None

def _get_publish_metadata_generator() -> IPublishMetadataGenerator:
    global _publish_metadata_generator
    if _publish_metadata_generator is None:
        _publish_metadata_generator = LlmPublishMetadataGenerator(
            registry=_get_provider_registry(),                 # LLM = MockLLMProvider by default
            timeout_seconds=_get_settings().llm_metadata_timeout_seconds,
            model=None,
        )
    return _publish_metadata_generator

def get_generate_publish_metadata_use_case() -> GeneratePublishMetadata:
    return GeneratePublishMetadata(uow=get_unit_of_work(),
                                   generator=_get_publish_metadata_generator())
```

- `app/api/v1/deps.py`: `GeneratePublishMetadataDep = Annotated[GeneratePublishMetadata,
  Depends(container.get_generate_publish_metadata_use_case)]`.
- `app/core/config.py`: add `llm_metadata_timeout_seconds: float = 15.0` (config only, **no schema**).
- The API layer sees only the port via the container — the existing "API never imports
  infrastructure" contract is respected; the use case sees only the port ("use_cases never import
  infrastructure").

---

## 7. Failure semantics (pre-flight Q4)

| Failure | Adapter | Use case | Client |
|---|---|---|---|
| Provider error (`ProviderUnavailable/RateLimited/Timeout/Authentication/Validation/NoProviderAvailable`) | maps → `PublishMetadataGenerationError` | catches → deterministic template | `200`, `provenance.is_fallback=true`, `generator="template"` |
| Timeout (`asyncio.wait_for`) | maps → `PublishMetadataGenerationError` | fallback | `200` fallback |
| Non-`SUCCEEDED` / empty / unparseable / over-limit-unfixable | raise `PublishMetadataGenerationError` | fallback | `200` fallback |
| Export not owned / missing | — | `NotFoundError` | `404` |

- **Invariants enforced:** advisory-only (1), never a `PublishJob` prerequisite (2 — this endpoint is
  entirely separate from create), graceful degradation on **any** AI failure (3). AI failure is
  **never** a 5xx or a publish blocker.

## 8. Timeout behaviour (pre-flight Q5)

The adapter wraps the provider call in `asyncio.wait_for(..., timeout=llm_metadata_timeout_seconds)`
(default 15s). The mock returns instantly, so CI never times out; the wrapper protects the future
real adapter. On timeout → `PublishMetadataGenerationError` → deterministic fallback. (Consistent
with W8.1.1: real provider HTTP timeouts are also baked into the provider's own client.)

## 9. Idempotency expectations (pre-flight Q6)

- **Stateless & side-effect-free:** the suggest call performs **owner-scoped reads only** and
  persists nothing, so it needs **no DB idempotency key**. Re-calling is always safe.
- **Determinism:** under the mock LLM the output is deterministic (reproducible tests); under a
  future real LLM, suggestions may vary per call by design (creator can re-roll).
- **Duplicate-publish protection unchanged:** the only persisted artifact is the eventual
  `PublishJob`, created through the **existing** idempotency key `(source_media_asset_id,
  social_account_id)` (unaffected by this slice).
- **User-edit precedence (Invariant 4):** structural — suggestions are returned to the client;
  whatever the creator finally submits to `POST /publish-jobs` wins (the create path already treats
  caller-supplied `title/description/tags` as authoritative).
- **Metering:** v1 does **not** record `usage_records` (unmetered preview); `request_id` is carried
  for tracing and future metering. (Additive future work; not architectural — §12.)

---

## 10. Import-linter contract (pre-flight Q9) — **yes, one new contract**

A new `[[tool.importlinter.contracts]]` in `backend/pyproject.toml` mechanically pins ADR-0049's
one-way boundary ("the AI bounded context never depends on Publishing"):

```
name = "AI plane never imports the Publishing bounded context (ADR-0049)"
type = "forbidden"
source_modules = ["app.infrastructure.ai", "app.domain.generation", "app.domain.workflow"]
forbidden_modules = [
    "app.domain.publishing",
    "app.application.use_cases.publishing",
    "app.infrastructure.publishing",
]
```

Already-sufficient existing contracts (no change needed): "Application use_cases never import
infrastructure" (keeps the publishing use case pointing only at the port), "Publishing domain is an
isolated bounded context", and "Destination adapters are credential-blind leaves" (keep destinations
AI-blind — Invariant 6). The neutral port lives in `app.application.interfaces` (shared), so the AI
adapter implementing it imports **no** Publishing module — the new contract proves it.

---

## 11. Testing plan (pre-flight Q8)

**Unit (`pytest -m unit`):**
- Use case: (a) AI success → returns LLM suggestions; (b) generator raises → deterministic template
  with `is_fallback=True`/`generator="template"`; (c) non-owned export → `NotFoundError`; via a
  `FakePublishMetadataGenerator`.
- Adapter over the default registry (mock LLM): deterministic parse of the echo; cap enforcement
  (title/description truncation, tag total-chars + count trimming); error mapping (stub provider
  raising `ProviderError` → `PublishMetadataGenerationError`; forced `TimeoutError` → same;
  non-`SUCCEEDED` → same); provenance fields.
- DTO validation (`PublishMetadataSuggestRequest`, `...SuggestionPublic.from_domain`).
- Container wiring smoke: `_get_publish_metadata_generator()` resolves; use case builds; LLM stays
  mock (extends `test_container_provider_registry`).

**Integration (new CI Stage 19, `requires_db=True`):**
`tests/integration/infrastructure/publishing/test_ai_publish_metadata.py` — seed a unique user +
project + succeeded export; call the suggest use case (real UoW + default mock-LLM adapter) and
assert: deterministic suggestion; owner isolation (another user's export → 404); fallback path
(inject a raising generator) → deterministic template; and a round-trip that a `PublishJob` created
with the returned metadata persists exactly those values in `content_package` (proving Invariant 5
+ destination-adapter blindness — Invariant 6). Determinism confirmed by repeated + reordered runs.

**CI gate:** add Stage 19 "ai publish-metadata integration" to `backend/scripts/ci_gate.py`
(docstring + `_stages()`); Stage 3 runs `import-linter` and will enforce the new contract.

---

## 12. Migration assessment + architectural-decision check

- **Migration (grounding §9): none.** No ORM change, no table/column/index/enum, no new invariant.
  Values persist via existing `content_package` JSONB; provenance is ephemeral (Invariant 5); no
  metering table. `validate_schema.py` / `compare_erd.py` derive from unchanged ORM → stay green
  with no edits. Proven by the absence of any persisted field.
- **New architectural decision? No.** Every element is a standard, precedented pattern (ABC port in
  `application/interfaces` implemented by an infra adapter, container factory, `CurrentUserDep`
  endpoint, one additive import-linter contract) operating **inside** the boundary ADR-0049 fixed.
  The only judgement calls — unmetered v1 preview, strictest-destination caps as constants, the
  prompt-template/parse format — are additive, reversible implementation details, not architectural
  boundaries. **No stop required.**

---

## 13. Files touched (all additive unless noted)

**New:** `app/application/interfaces/publish_metadata_generator.py`;
`app/application/use_cases/publishing/generate_publish_metadata.py`;
`app/infrastructure/ai/metadata/llm_publish_metadata_generator.py` (+ `__init__.py`);
`app/api/v1/schemas/publish_metadata.py`; `app/api/v1/routers/publish_metadata.py`; unit + integration
test modules.
**Edited (additive):** `app/core/container.py` (adapter accessor + use-case factory);
`app/api/v1/deps.py` (`GeneratePublishMetadataDep`); `app/api/v1/main.py` (include router);
`app/core/config.py` (`llm_metadata_timeout_seconds`); `backend/pyproject.toml` (import-linter
contract); `backend/scripts/ci_gate.py` (Stage 19); `CHANGELOG.md`; docs
(`schema.md` unchanged — no schema; `SYSTEM_MAP.md` + `PLATFORM_STATUS.md` at documentation-sync).
**Not touched (frozen — Invariant 7):** `ProcessPublishJob`, `CreatePublishJob`/`build_content_
package`, the YouTube/Mock destination adapters, and all generation/render/export runtimes.

---

## 14. Stop

Pre-flight complete; no new architectural decision surfaced. Per the established workflow, **stopping
for review before implementation.** On approval I will implement α9.1 exactly as specified, then run
the full ephemeral PostgreSQL gate.
