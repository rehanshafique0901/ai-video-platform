# Phase 3 — α8.6b Pre-flight: Publish Runtime (`PublishJob` + `PublishWorker`)

> **Status: SIGNED OFF (2026-07-27).** DQ1–DQ8 ruled (see §10); **DQ7 deferred** (events
> only, no notification projection this slice). Second increment of the **α8.6 Publishing
> / Creator Workflow** bounded context. Input: `PHASE3_ALPHA8_6b_GROUNDING.md` (APPROVED,
> PR #36, merged `0a8dcb6`). Governing artifacts: `PUBLISHING_RUNTIME_CONTRACT.md`
> (§6–§13, PUB-1…PUB-10) and **ADR-0047** (credential ownership — consumed, not
> re-opened). Baseline: `v0.4.36-phase3-alpha8.6a`.
>
> **The one question α8.6b answers:** *Given one connected `SocialAccount` and one
> finished export artifact, how does the platform execute a **single, retry-safe,
> idempotent upload**?* — proven end-to-end against a deterministic **`MockDestination`**.
> **Not** the real YouTube/TikTok API (α8.6c), not LLM captions, not a scheduler.
>
> **Locked from grounding:** the publish runtime is a **faithful clone of the export
> runtime** + a second serialisation lock + `scheduled_at`/bounded retries + a
> credential-blind destination boundary consuming the shipped α8.6a `authorize()` seam.
> **No new execution model. No architectural expansion beyond `domain/publishing` +
> `infrastructure/publishing`. Strictly additive.**
>
> **§10 resolves all eight grounding questions with recommendations.** Nothing is
> implemented until this pre-flight is approved.

---

## 0. Gates (answered first)

### Gate 1 — ADR-0042 (orchestration platform freeze)
> Does α8.6b touch any frozen orchestration module, checkpoint contract, provider
> protocol, render/export composition, or workflow lifecycle?

**No.** α8.6b adds to the **existing publishing bounded context** only: new
`domain/publishing` types, new use cases, `infrastructure/publishing/destinations/**`,
one new table (`publish_jobs`, migration `0014`), one router. It **reads** a finished
export delivery `MediaAsset` (PUB-1, PUB-6) and **writes** only its own job state +
outbox events. It re-encodes nothing (RC5) and mutates no upstream state. Freeze guard
stays green, **zero overrides**.

### Gate 2 — ADR-0047 (credential ownership) — consumed, not re-opened
> Does α8.6b stay credential-blind?

**Yes.** The runtime obtains authorization **only** via the shipped
`ISocialCredentialStore.authorize(social_account_id) → AuthorizedContext` (bearer only;
sole decryptor is the α8.6a credential service, C7). Destination adapters receive an
`AuthorizedContext` and never touch the credential store, tokens, refresh, or key
material (PUB-5 / C4). **α8.6b adds no crypto, no new credential path, and no change to
ADR-0047.** (See §10 DQ3.)

### Gate 3 — Publishing invariants (PUB-1…PUB-10)
Mapped in §9. The load-bearing ones for this slice: PUB-1 (export-delivery asset only),
PUB-2 (explicit user intent, no auto-publish), PUB-5 (credential-blind adapters),
PUB-6 (never mutate upstream), PUB-7 (worker/lease + bounded retries + DB-owned
idempotency), PUB-8 (fan-out events), PUB-9 (deterministic metadata).

---

## 1. Positioning (what α8.6b *is* / *is not*)

α8.6b proves the platform can **run one upload as a durable, resumable job** — with no
real network destination. It is the runtime skeleton; α8.6c fills in the real adapter.

```
POST /publish-jobs { export_job_id, social_account_id, metadata? }   (PUB-2: explicit intent)
        │  CreatePublishJob: owner-scope, resolve source asset + project_id, build ContentPackage
        ▼
   PublishJob (QUEUED, scheduled_at)                                   publish_jobs
        │  PublishWorker.run_once() polls due jobs  (external cadence — no in-repo scheduler)
        ▼
   ProcessPublishJob
        │  lease publish_job:<id>  then  project_publish:<project_id>   (dual lock, §10 DQ5)
        │  CAS QUEUED→RUNNING
        │  authorize(social_account_id) → AuthorizedContext            (α8.6a; credential-blind)
        │  stream delivery MediaAsset bytes                            (PUB-1)
        │  IDestinationPublisher.publish(package, ctx, bytes) → MockDestination
        ▼
   settle: SUCCEEDED(platform_post_id, published_at) │ retry(QUEUED, backoff) │ FAILED(error)
        │  emit PublishJobSucceeded / PublishJobFailed (outbox; fan-out only, PUB-8)
        ▼
   (optional) NotificationProjection → publish.succeeded / publish.failed
```

**Is:** `PublishJob` aggregate + state machine; `CreatePublishJob`; `ProcessPublishJob`
(dual lock, three-phase I/O); `PublishWorker`; deterministic `ContentPackage` builder;
`IDestinationPublisher` port + `MockDestination`; terminal outbox events; owner-scoped
read/create API; migration `0014`.

**Is not:** any real destination API; LLM metadata; auto-publish; a cron/scheduler;
custom thumbnail upload; a `generation_assets → publish` bridge; any change to a frozen
path or to ADR-0047.

---

## 2. Data model — migration `0014_publish_jobs` (additive, ORM + UoW)

ORM + Unit-of-Work (like `export_jobs` / `social_accounts`), **not** raw-SQL/ORM-less.

### New enum `publish_status` (mirrors `export_status`)
`queued`, `running`, `succeeded`, `failed`, `canceled`. Terminal = `{succeeded, failed,
canceled}`. **Registry impact:** `backend/tests/test_enums.py` `EXPECTED_ENUM_COUNT`
`27 → 28`.

### `publish_jobs`
| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | `gen_random_uuid()` |
| `tenant_id` | uuid NOT NULL → `tenants(id)` | direct ownership (MediaAsset/ExportJob convention) |
| `requested_by_user_id` | uuid NOT NULL → `users(id)` | owner; mirrors `export_jobs` |
| `project_id` | uuid **NOT NULL** → `projects(id)` | **DQ1** — powers `project_publish:<project_id>` lock; resolved server-side at create |
| `source_export_job_id` | uuid NOT NULL → `export_jobs(id)` | provenance / the artifact chosen |
| `source_media_asset_id` | uuid NOT NULL → `media_assets(id)` | the export delivery asset (PUB-1) |
| `social_account_id` | uuid NOT NULL → `social_accounts(id)` | the destination account |
| `platform` | text NOT NULL | denormalised from the account for adapter routing (matches α8.6a `platform` as text, OQ2) |
| `status` | `publish_status` NOT NULL | state machine |
| `scheduled_at` | timestamptz NULL | NULL ⇒ immediately claimable; future ⇒ deferred (`publish_at`) |
| `attempt` | integer NOT NULL DEFAULT 0 | bounded retry counter |
| `max_attempts` | integer NOT NULL DEFAULT 5 | Q4 |
| `content_package` | jsonb NOT NULL | deterministic metadata snapshot (PUB-9) |
| `platform_post_id` | text NULL | on success |
| `platform_post_url` | text NULL | on success |
| `error` | jsonb NULL | on permanent/exhausted failure (neutral class + message) |
| `published_at` | timestamptz NULL | on success |
| `finished_at` | timestamptz NULL | terminal timestamp |
| `version` | integer NOT NULL DEFAULT 1 | OCC (mirrors `export_jobs`) |
| `created_at` / `updated_at` | timestamptz NOT NULL DEFAULT now() | |

**Idempotency (partial-unique, DQ2):**
`uq_publish_jobs_source_media_asset_social_account` UNIQUE on
`(source_media_asset_id, social_account_id)` `postgresql_where = status IN
('queued','running','succeeded')`. Mirror `0003_export_jobs_partial_unique`;
`IntegrityError → ConflictError` in `add()`.

**Indexes:** `ix_publish_jobs_status_scheduled_at` on `(status, scheduled_at)` (claim
scan); `ix_publish_jobs_requested_by_user_id_created_at` (owner list);
`ix_publish_jobs_social_account_id`.

**Version-bump trigger (DQ8):** add `publish_jobs` to the `bump_version` trigger set and
also do explicit `version = version + 1` in CAS updates — **exactly** as `export_jobs`
does (§10 DQ8).

**ERD:** extend `docs/database/ERD.md` Cluster 13 with `publish_jobs` + its four FKs
(Stage 9 sync).

---

## 3. Domain (`app/domain/publishing`)

- **`PublishStatus(StrEnum)`** — mirrors `ExportStatus`; `is_terminal` property.
- **`PublishJob`** — frozen dataclass, slim row projection (id, ownership, `project_id`,
  `source_export_job_id`, `source_media_asset_id`, `social_account_id`, `platform`,
  `status`, `scheduled_at`, `attempt`, `max_attempts`, `content_package`,
  `platform_post_id`, `platform_post_url`, `error`, `published_at`, `finished_at`,
  `version`, timestamps). Transitions live in the repository (CAS), not on the entity —
  exactly the `ExportJob` shape.
- **`PublishJobClaim`** — poll DTO: `publish_job_id`, `project_id` (project_id is a real
  column now, so no join needed — simpler than `ExportJobClaim`).
- **`ContentPackage`** — immutable, platform-agnostic value object (contract §5):
  `media_asset_id`, `title`, `description`, `tags: tuple[str, …]`, `visibility`
  (`public`/`unlisted`/`private`), `thumbnail_media_asset_id | None`, `publish_at | None`.
  Serialised to/from the `content_package` JSONB.
- **`ContentPackageBuilder`** (pure, PUB-9) — deterministic function of `(project,
  source artifact, optional user overrides)`. No LLM, no randomness. Default title from
  `project.title`; default description/tags from a fixed template; `visibility` defaults
  to **`private`** (safe default) unless the request specifies otherwise;
  `thumbnail_media_asset_id` reuses `source_metadata.enrichment.thumbnail_media_asset_id`
  when present (α8.4c), else `None`. Platform-specific limit validation happens **inside**
  the adapter (contract §5), not here.

---

## 4. Ports (`app/application/interfaces`)

### `IPublishJobRepository` (on `uow.publish_jobs`) — mirrors `IExportJobRepository`
- `add(...) → PublishJob` (insert `QUEUED`; `IntegrityError → ConflictError`)
- `get_active(source_media_asset_id, social_account_id) → PublishJob | None` (idempotency pre-check)
- `get_owned(publish_job_id, tenant_id, owner_user_id) → PublishJob | None` (uniform 404)
- `list_for_owner(...) → list[PublishJob]` (keyset, newest-first)
- `list_claimable(limit) → list[PublishJobClaim]` — `WHERE status='queued' AND (scheduled_at IS NULL OR scheduled_at ≤ now()) ORDER BY (created_at, id) LIMIT n`
- `mark_running(id) → PublishJob | None` (CAS `queued→running`)
- `mark_succeeded(id, *, platform_post_id, platform_post_url, published_at) → PublishJob | None` (CAS `running→succeeded`)
- `mark_failed(id, *, error) → PublishJob | None` (CAS `running→failed`)
- `reschedule_for_retry(id, *, attempt, scheduled_at) → PublishJob | None` (CAS `running→queued`, bumps `attempt`, sets `scheduled_at`)

All CAS updates use `UPDATE … WHERE id=? AND status=<expected> … RETURNING`, return
`None` on miss, and `version = version + 1` (exactly the export pattern).

### `IDestinationPublisher` (§10 DQ3) — the credential-blind boundary
```
class IDestinationPublisher(ABC):
    platform: str                      # which platform this adapter serves
    async def publish(
        self,
        *,
        package: ContentPackage,
        auth: AuthorizedContext,       # α8.6a bearer — NO client type, NO credential store
        media: <byte stream / path>,
    ) -> PublishResult: ...            # raises DestinationError(kind=transient|permanent)
```
- **`PublishResult`** (frozen): `platform_post_id`, `url`.
- **`DestinationError`** (typed): `kind ∈ {transient, permanent}` + neutral message. The
  **adapter** classifies provider outcomes into these two classes; the runtime never
  inspects provider error codes (contract §6, Q4).
- **No "client" type is introduced** — reconciling the contract's "pre-authenticated
  client" language with the shipped `AuthorizedContext` seam (§10 DQ3).

---

## 5. Infrastructure

- **`MockDestination(IDestinationPublisher)`** (`infrastructure/publishing/destinations/`)
  — deterministic, network-free: returns a stable `PublishResult` derived from
  `(publish_job_id / media_asset_id)`; a test hook can force a `transient` then
  `permanent` error to exercise retry + failure. Keeps CI Stage 14 hermetic.
- **`PublishJobRepository(IPublishJobRepository)`** (`infrastructure/repositories/`) —
  SQLAlchemy, mirrors `export_job_repository.py` line-for-line in structure.
- **UoW wiring:** `uow.publish_jobs = PublishJobRepository(session)` in
  `SqlAlchemyUnitOfWork` + the integration `_TestUnitOfWork`.
- **DestinationRegistry (PUB-4):** a **tiny in-code** `platform → IDestinationPublisher`
  map (Mock now; YouTube in α8.6c). **Not** the AI capability catalogue / resolver /
  dispatcher. No YAML catalogue (Q1 deferred until ≥2 real destinations).

---

## 6. Use cases (`app/application/use_cases/publishing`)

### `CreatePublishJob` (mirrors `CreateExportJob`; PUB-2 explicit intent)
Input: `export_job_id`, `social_account_id`, optional metadata overrides (`title`,
`description`, `tags`, `visibility`, `publish_at`).
1. **Owner-scope + resolve chain:** load the export job owner-scoped; require it
   `succeeded` with `output_media_asset_id` (PUB-1); resolve `project_id` from the
   export→render chain (`render_jobs.project_id`, non-null) — **this is how `PublishJob`
   comes to own a concrete `project_id`** (§10 DQ1). Load the `SocialAccount`
   owner-scoped and require `status = connected`.
2. **Build `ContentPackage`** deterministically (§3, PUB-9); `scheduled_at = publish_at`
   if future else `NULL`.
3. **Idempotency:** pre-check `get_active(source_media_asset_id, social_account_id)`;
   insert `QUEUED`; on `ConflictError`, race-recover via a second `get_active` (Fork-E
   pattern).
4. Emit `PublishJobCreated`; commit.

### `ProcessPublishJob` (mirrors `ProcessExportJob`; three-phase I/O)
- **Phase 1 (DB txn):** acquire `publish_job:<id>` → commit; if `None` → skip
  `reason="locked"`. Acquire `project_publish:<project_id>` → commit; if `None` →
  release job lock, skip `reason="project_locked"` (job stays `QUEUED`, re-polled later)
  (§10 DQ5). Read job; validate `RUNNING`-able; CAS `mark_running`; commit.
- **Phase 2 (NO txn — heavy I/O outside the DB, no lock held across network):**
  `authorize(social_account_id) → AuthorizedContext`; stream the delivery `MediaAsset`
  bytes (`media.get_owned` + `IStorageResolver.resolve(backend).get(key)`); resolve the
  adapter from the registry by `platform`; `adapter.publish(package, auth, media)`.
- **Phase 3 (DB txn):** on success → `mark_succeeded(platform_post_id, url,
  published_at)` + `emit PublishJobSucceeded`. On `DestinationError(transient)` with
  `attempt+1 < max_attempts` → `reschedule_for_retry(attempt+1, now + backoff)`. On
  `permanent` **or** exhausted attempts → `mark_failed(error)` + `emit
  PublishJobFailed`. Commit.
- **Finally:** release `project_publish` then `publish_job` (reverse order), each in its
  own txn.
- **Credential-blind (PUB-5):** the adapter receives only the `AuthorizedContext`;
  `CredentialUnavailableError` from `authorize()` is treated as **permanent** (the
  account is revoked/expired-unrefreshable) → `mark_failed`.

### `PublishWorker.run_once() → PublishPollResult` (mirrors `ExportWorker`)
Batch `list_claimable(limit=settings.publish_batch_size)` inside one UoW read, then
per-claim `ProcessPublishJob.process(...)`. **No trigger/endpoint/cron** is added — the
worker is a container factory invoked by an external cadence, exactly like
`ExportWorker`/`RenderWorker` today (§10 DQ7 note; contract §7).

---

## 7. Events (§10 DQ4)

- **Outbox `event_type` = PascalCase**, matching the existing `_events.py` convention
  (`ExportJobSucceeded`, generation `*` events): **`PublishJobCreated`**,
  **`PublishJobSucceeded`**, **`PublishJobFailed`**; `aggregate_type = "publish_job"`.
  (The contract §10's dotted `publishing.publish_job.*` was illustrative; code
  consistency wins.)
- **Notification kinds (dotted, matching `export.succeeded`):** `publish.succeeded` /
  `publish.failed` (see §10 DQ7).
- **Fan-out only (PUB-8):** publishing never chains into another projection.

---

## 8. API (`app/api/v1/routers/publish_jobs.py`) — read + enqueue only

- `POST /api/v1/publish-jobs` → `CreatePublishJob`; **201** new / **200** idempotent
  replay (owner-scoped). Body: `export_job_id`, `social_account_id`, optional metadata.
- `GET /api/v1/publish-jobs/{id}` → owner-scoped read (uniform 404).
- `GET /api/v1/publish-jobs` → owner list (keyset, reuses α5a pagination).
- **No worker/trigger endpoint** (PUB-2; worker is external, mirrors export).
- Public schema omits internals (never exposes tokens; `content_package` echoed as-is).

---

## 9. Invariant mapping (PUB-1…PUB-10)

| Invariant | How α8.6b satisfies it |
|---|---|
| PUB-1 | Reads `export_jobs.output_media_asset_id`; never `generation_assets`. |
| PUB-2 | `PublishJob` created only via `POST /publish-jobs`; no `ExportJobSucceeded` subscriber. |
| PUB-3 | All new code under `domain/publishing` + `infrastructure/publishing`; import-linter isolation. |
| PUB-4 | In-code `DestinationRegistry`, not the AI catalogue/resolver/dispatcher. |
| PUB-5 | Adapters take `AuthorizedContext` + bytes only; import-linter + test forbid credential-store access. |
| PUB-6 | Reads a finished artifact; writes only `publish_jobs` + outbox; no upstream mutation, no re-encode. |
| PUB-7 | Poll → dual lease → CAS transitions → bounded deterministic retries → DB-owned partial-unique. |
| PUB-8 | Terminal outbox events; independent projection; no chaining. |
| PUB-9 | `ContentPackageBuilder` is a pure template function; no LLM. |
| PUB-10 | Credentials owned by the α8.6a service (ADR-0047); α8.6b consumes `authorize()` only. |

---

## 10. Resolved design questions (recommendations — awaiting sign-off)

The eight questions surfaced by the grounding (§9), each ruled with a recommendation.

### DQ1 — Should `PublishJob` own an explicit `project_id`? → **YES (recommended)**
`MediaAsset.project_id` is **nullable**, so it cannot reliably key the
`project_publish:<project_id>` lock. `CreatePublishJob` resolves a **non-null**
`project_id` server-side from the export→render chain (`render_jobs.project_id`) and
persists it on `publish_jobs`. This makes the serialisation lock always well-defined and
is the durable seam for future republish / replace-published-version / destination
ordering. *(Directly answers your first attention point.)*

### DQ2 — Idempotency tuple → **`(source_media_asset_id, social_account_id)`, active set `{queued,running,succeeded}`**
Prevents accidentally publishing the *same artifact to the same account* twice while a
job is active or already succeeded (mirrors `export_jobs` including `succeeded`). A
deliberate **re-publish** of the same video to the same account is a distinct future
flow, not an accidental double-post. Enforced by partial-unique + `ConflictError`.

### DQ3 — "Pre-authenticated client" vs. shipped `AuthorizedContext` → **pass the bearer; no client type**
`IDestinationPublisher.publish(package, auth: AuthorizedContext, media)` receives the
α8.6a bearer directly. No "client" abstraction is introduced; the adapter builds any
platform SDK client it needs from the bearer, staying credential-blind (PUB-5 / C4).
`ProcessPublishJob` is the only caller of `authorize()`. *(Directly answers your second
attention point — the runtime consumes only the existing `AuthorizedContext`.)*

### DQ4 — Event naming → **PascalCase outbox `event_type`**
`PublishJobCreated` / `PublishJobSucceeded` / `PublishJobFailed`, `aggregate_type =
"publish_job"` — consistent with `_events.py` and the generation events. Notification
**kinds** stay dotted (`publish.succeeded` / `publish.failed`) to match `export.*`.

### DQ5 — Dual-lock order + second-lock-miss → **job-lock first, project-lock second; skip-and-retry**
Acquire `publish_job:<id>` (execution guard) then `project_publish:<project_id>`
(product serialisation); release in reverse. A **job-lock** miss → `skipped
reason="locked"`. A **project-lock** miss → release the job lock, `skipped
reason="project_locked"`; the job stays `QUEUED` and is retried on the next poll (no
attempt increment — it never ran).

### DQ6 — Retry/backoff → **`max_attempts=5`, capped exponential (Q4)**
`backoff(attempt) = min(cap, base · 2^(attempt-1))` with `base = 30s`, `cap = 1h`,
**no random jitter** (deterministic for CI). `transient` + attempts remain →
`reschedule_for_retry`; `permanent` or exhausted → `mark_failed`. The
**adapter** classifies `transient` vs `permanent` via `DestinationError.kind`; the
runtime never reads provider codes. `CredentialUnavailableError` = permanent.

### DQ7 — Notification projection → **DEFERRED (ruled 2026-07-27)**
α8.6b emits `PublishJobSucceeded` / `PublishJobFailed` outbox events **only**. The
notification projection remains a **downstream consumer** and is **not** built in this
slice — it must not expand α8.6b scope (PUB-8: events are fan-out; consumers evolve
independently). Wiring a `publish.*` notification handler is a follow-up.

### DQ8 — Version bump → **mirror `export_jobs` exactly**
Add `publish_jobs` to the `bump_version` trigger set **and** set `version = version + 1`
explicitly in CAS updates — identical to `export_jobs` (the trigger authoritatively
increments; the explicit value keeps the `RETURNING` snapshot correct). No new
convention.

---

## 11. Enforcement

- **Import-linter (`backend/pyproject.toml`):** extend the publishing bounded-context
  contract to the new modules; add a **PUB-4** guard that `domain.publishing` /
  `infrastructure.publishing` must **not** import `app.infrastructure.ai.providers`,
  `app.domain.resolver`, or `app.domain.generation`; and keep destination adapters as a
  **leaf** (a `MockDestination` cannot import the credential store).
- **Tests:**
  - *Unit* — `ProcessPublishJob` (happy path, dual-lock skip ×2, transient→retry,
    permanent→fail, exhausted→fail, `CredentialUnavailable`→permanent), `PublishWorker`
    FIFO/due drain, `CreatePublishJob` (owner-scope, idempotency, project_id resolution,
    deterministic `ContentPackage`), `MockDestination`, `ContentPackageBuilder`
    determinism — reusing `FakeUnitOfWork`/`FakeDistributedLockManager`/
    `FakeEventOutboxRepository` from `tests/.../export/_helpers.py`.
  - *Integration (Stage 14)* — `PublishJobRepository` CAS + partial-unique + owner-scope;
    end-to-end `run_once` against `MockDestination` on ephemeral Postgres; a
    **credential-blind assertion** (adapter never receives/queries the credential store).
- **CI:** extend **Stage 14** (publishing) — not Stage 13; `MockDestination` keeps it
  deterministic + network-free (contract §13).

---

## 12. Non-goals / explicitly deferred (restated)

- **No real destination API** (YouTube/TikTok/etc.) — α8.6c.
- **No LLM captions/hashtags** — deterministic `ContentPackage` only (PUB-9).
- **No auto-publish** from export completion (PUB-2).
- **No time scheduler/cron** — `scheduled_at` due-scan + external `run_once` (§7).
- **No custom thumbnail upload** — reuse the enrichment-derived thumbnail.
- **No YAML destination catalogue/validator** — until ≥2 real destinations (Q1).
- **No `generation_assets → publish` bridge** (PUB-1; ADR-0046 X8).
- **No change to any frozen path or to ADR-0047** — strictly additive.

---

## 13. Migration & increment plan

1. Migration `0014_publish_jobs` (enum + table + partial-unique + indexes + trigger).
2. Domain (`PublishStatus`, `PublishJob`, `PublishJobClaim`, `ContentPackage`,
   `ContentPackageBuilder`).
3. Ports (`IPublishJobRepository`, `IDestinationPublisher` + `PublishResult` /
   `DestinationError`) + `uow.publish_jobs` wiring.
4. Infra (`PublishJobRepository`, `MockDestination`, `DestinationRegistry`).
5. Use cases (`CreatePublishJob`, `ProcessPublishJob`, `PublishWorker`) + `_events.py`
   (emits `PublishJob{Created,Succeeded,Failed}` only — **no** notification handler, DQ7).
6. API (router + schemas + deps + container factories + `publish_batch_size` config).
7. Enforcement (import-linter + unit + integration/Stage 14) + ERD + enum-count sync.
8. Full ephemeral-DB gate (migration up→down→up, integration, static) → feature commit
   (`-dev`) → release review.

---

## 14. Sign-off rulings (2026-07-27)

- **DQ1–DQ8** — approved as drafted, **except DQ7 → DEFERRED** (α8.6b emits
  `PublishJobSucceeded` / `PublishJobFailed` events only; no notification projection).
- **`ContentPackage` default `visibility`** — **`private`** (confirmed).
- **API shape** — **top-level `/api/v1/publish-jobs`** (confirmed; not nested under
  project).
- **Execution model** — a **faithful adaptation of the `ExportJob` worker pattern**; no
  new execution model.
- **Scope** — **strictly additive** within the Publishing bounded context; no
  architectural expansion beyond the approved contract + ADR-0047.

> Implementation approved. It proceeds in the §13 order on a feature branch, holding at
> the `-dev` version pending release approval; the full ephemeral-Postgres gate
> (migration up→down→up, integration, static) must pass before release review.
