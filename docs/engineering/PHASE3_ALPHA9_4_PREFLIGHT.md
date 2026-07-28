# α9.4 — Multi-Destination Publishing — Pre-flight (design blueprint)

> **Status:** Design blueprint. Approved-for-implementation shape. Additive; **no migration, no
> ADR, no frozen-path change** (see [`PHASE3_ALPHA9_4_GROUNDING.md`](./PHASE3_ALPHA9_4_GROUNDING.md)
> §4). Baseline `v0.4.46-phase3-alpha9.3`.
>
> **One-line scope:** a single creator action that **fans out** a publish of one finished export to
> **N** of the caller's connected destination accounts, reusing the existing per-account
> `CreatePublishJob` unchanged.

---

## 1. Scope

**In:** one new fan-out use case + one new additive API endpoint that publishes a single export to
a list of the caller's connected accounts, returning a **per-account outcome** (created / idempotent
replay / per-account error). Shared metadata (title/description/tags/visibility, `publish_at`,
`thumbnail_media_asset_id`) is supplied once and applied to every account.

**Out (unchanged / deferred):** the single-create endpoint (`POST /publish-jobs`) stays exactly as
is; no `PublishBatch` entity / `publish_batch_id` / grouped-status aggregate (status is per-job via
existing reads); no new destinations (§2.6); no runtime/worker/adapter change; no migration; no ADR.

---

## 2. Decisions (ruling the grounding §5 questions)

- **D1 — Best-effort fan-out with fail-fast on shared inputs.** The **shared** preconditions are
  validated **once, up front** and hard-fail the whole request: unknown/foreign **export** →
  `404`; export not `succeeded`/no delivery artifact → `422`; unknown/foreign **thumbnail** →
  `404`; non-image thumbnail → `422`. After the shared inputs pass, each account is processed
  independently and its outcome is reported per item — **one bad account never blocks the others**.
- **D2 — Dedicated additive endpoint.** `POST /api/v1/publish-jobs/batch`. The existing
  `POST /api/v1/publish-jobs` is untouched (backward compatible).
- **D3 — Per-account outcome list, overall `201`.** Response `data` is an ordered array (input
  order) of items: `{ social_account_id, created: bool, publish_job: PublishJobPublic | null,
  error: { code, message } | null }`. Overall status is `201` (the action was accepted and
  processed); per-account failures live in the items (they are expected, not request errors).
- **D4 — Bounded, de-duplicated account list.** `social_account_ids`: non-empty, max length
  **20**, duplicates rejected at the schema boundary (`422`). (20 is a generous cap for
  "all my channels"; keeps a single request bounded.)
- **D5 — Metadata built once, per-account by construction.** Each per-account create receives the
  identical shared inputs → `build_content_package` is deterministic (PUB-9), so all jobs carry
  equivalent `ContentPackage`s. No metadata is computed differently per account.
- **D6 — Idempotency unchanged (PUB-7).** A repeat batch (or an account already published for this
  export) yields `created=false` + the existing job for that account — the exact single-create
  replay semantics, per item.
- **D7 — Transaction boundary = per account.** Each account's create is its own
  `CreatePublishJob.execute` (its own UoW + `PublishJobCreated` emission), mirroring how the
  fan-out consumers already work one-event-at-a-time. A mid-list failure does **not** roll back
  already-created jobs (best-effort, D1). This is intentional and matches "N independent jobs."

---

## 3. Interface — new fan-out use case

`application/use_cases/publishing/create_publish_jobs.py` (plural):

```python
@dataclass(frozen=True, slots=True)
class PublishFanOutItem:
    social_account_id: UUID
    created: bool
    job: PublishJob | None
    error: FanOutError | None          # {code, message} — None on success

@dataclass(frozen=True, slots=True)
class CreatePublishJobsResult:
    items: tuple[PublishFanOutItem, ...]   # input order preserved

class CreatePublishJobs:
    def __init__(self, create_one: CreatePublishJob) -> None: ...
    async def execute(self, *, owner_user_id, tenant_id, export_job_id,
                      social_account_ids: Sequence[UUID],
                      title=None, description=None, tags=None, visibility=None,
                      publish_at=None, thumbnail_media_asset_id=None,
                      ip=None) -> CreatePublishJobsResult: ...
```

- It **composes the existing `CreatePublishJob`** (dependency-injected) — no duplicated
  ownership/readiness/idempotency logic.
- **Shared-input fail-fast (D1):** to fail the export/thumbnail preconditions **once** rather than
  N times, the fan-out calls `CreatePublishJob.execute` for the **first** account; a `NotFoundError`
  / `ValidationFailedError` whose cause is a **shared** input (export or thumbnail) is re-raised to
  become the request-level `404`/`422`. Per-account errors (account not owned/not connected/
  unsupported platform) are **caught** and recorded as an item `error`, never raised.
  - Concretely: the fan-out classifies a caught `NotFoundError`/`ValidationFailedError` by its
    `details` key — `export_job_id`/`thumbnail_media_asset_id` ⇒ **shared, re-raise**;
    `social_account_id`/`platform` ⇒ **per-account, record**. (Deterministic, uses the existing
    error `details` already set in `create_publish_job.py`.)
- Order preserved; duplicates already removed at the schema layer (D4).

## 4. API

`api/v1/schemas/publish_jobs.py` — new request model:

```python
class PublishJobBatchCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    export_job_id: UUID
    social_account_ids: Annotated[list[UUID], Field(min_length=1, max_length=20)]
    title: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=5000)
    tags: list[str] | None = Field(default=None, max_length=50)
    visibility: Visibility | None = None
    publish_at: datetime | None = None          # reuses the α8.9b future/tz-aware validator
    thumbnail_media_asset_id: UUID | None = None
    # validator: reject duplicate social_account_ids (422)
```

Response item DTO: `PublishJobBatchItemPublic { social_account_id, created, publish_job:
PublishJobPublic | None, error: {code, message} | None }`.

`routers/publish_jobs.py` — new endpoint (existing `_to_public` reused):

```
POST /api/v1/publish-jobs/batch
  body: { export_job_id, social_account_ids[1..20], <shared metadata overrides> }
  → 201 { data: [PublishJobBatchItemPublic, …], meta }   (fan-out processed; per-account outcomes)
  → 404 { error: NOT_FOUND }         (export/thumbnail not the caller's — shared, fail-fast)
  → 422 { error: VALIDATION_FAILED } (export not ready / thumbnail not image / bad body / dup ids)
  → 401 (CurrentUserDep)
```

DI: a `CreatePublishJobsDep` in `deps.py` wired via a container factory that injects the existing
`CreatePublishJob` (which already receives the UoW + `supported_platforms`).

## 5. Failure semantics (summary)

| Condition | Result |
|---|---|
| Export unknown/foreign | request `404` (shared, before any job) |
| Export not ready / no artifact | request `422` (shared) |
| Thumbnail unknown/foreign | request `404` (shared) |
| Thumbnail not an image | request `422` (shared) |
| Body invalid / empty list / >20 / duplicate ids | request `422` (schema) |
| Account not owned | item `error{code:"not_found"}`, `created=false`, others proceed |
| Account not connected | item `error{code:"validation_failed"}` (status) |
| Account platform unsupported | item `error{code:"validation_failed"}` (platform) |
| Account already published (this export) | item `created=false` + existing job (PUB-7 replay) |
| Account newly queued | item `created=true` + queued job |

## 6. Invariants preserved

- **PUB-1/PUB-2** — still one export delivery artifact; still explicit user intent (no auto-publish).
- **PUB-7** — per-`(source_media_asset_id, social_account_id)` idempotency unchanged (each item).
- **PUB-9** — deterministic metadata (built once, identical inputs per account).
- **PUB-11** — untouched (execution/adapter path unchanged).
- **Owner-scoping** — every account + export + thumbnail is owner-gated exactly as single-create.
- **Determinism / replay safety** — worker/relay unchanged; N independent jobs drain as today.

## 7. Test plan

**Unit (`tests/unit/...`):**
- `test_create_publish_jobs.py` (new) — fan-out orchestration with a faked `CreatePublishJob`:
  all-created; mixed created/replay; a per-account 404/422 recorded as an item while others
  succeed; **shared** export 404/422 and thumbnail 404/422 **re-raised** (request-level); input
  order preserved; empty result never on success path.
- `test_publish_jobs_schema.py` (extend) — batch request: min/max length, duplicate-id rejection,
  reuse of the `publish_at` validator, non-uuid rejection.

**Integration (CI Stage 22 — `multi-destination publishing`):**
- Seed one succeeded export + **two** connected `mock` accounts → `POST /publish-jobs/batch` →
  `201`, two items `created=true`, **two** `publish_jobs` rows; the `PublishWorker` drains **both**
  to `succeeded`; two `PublishJobCreated`+`PublishJobSucceeded` outbox chains.
- Best-effort: two accounts where one is foreign/not-connected → one item success, one item error,
  the valid job still queued + drains.
- Shared fail-fast: unknown export → request `404`, **zero** jobs created.
- HTTP-contract: auth `401`; duplicate ids / empty / >20 → `422`.

## 8. Migration / ADR assessment

- **Migration:** none (F6 — pure fan-out over existing `publish_jobs`).
- **ADR:** none (grounding §4). A short **contract addendum** to
  `PUBLISHING_RUNTIME_CONTRACT.md` §2/§14 records the v1→multi scope expansion at docs-sync time.
- **Import-linter:** no new contract (no new cross-boundary dependency).

## 9. Implementation order

1. `CreatePublishJobs` fan-out use case (composes `CreatePublishJob`; shared-vs-per-account error
   classification via error `details`).
2. Request + item DTOs (schema) with the cardinality/dedupe validators.
3. `POST /publish-jobs/batch` router + `deps.py`/container wiring.
4. Unit tests (use case + schema).
5. Integration test + CI **Stage 22**.
6. Full ephemeral PostgreSQL gate; fix to green.
7. Version bump `0.4.47-phase3-alpha9.4-dev`; one feature commit; push; open `-dev` release-review PR.
