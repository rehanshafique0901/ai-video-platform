# α8.6b Grounding — Publish Runtime (`PublishJob` + `PublishWorker`)

> **Type:** Grounding (read-only facts). **No code, no schema, no baseline change.**
> Establishes the facts the α8.6b pre-flight will build on.
>
> **The one question:** *Given one connected `SocialAccount` and one finished export
> artifact, how does the platform execute a single, retry-safe, idempotent upload?* —
> and nothing else.
>
> **Explicitly out of scope (later increment):** the real YouTube adapter and any
> other real destination API. α8.6b drives the runtime end-to-end against a
> deterministic **`MockDestination`** only. Real adapters are α8.6c.
>
> **Governed by:** `PUBLISHING_RUNTIME_CONTRACT.md` §6 (lifecycle/state machine), §7
> (worker model), §8 (destination boundary), §10 (events), §11 (PUB-1…PUB-10), §12
> (migration `0014_publish_jobs`), §15 (Q1–Q5 rulings); and `ADR-0047` (the α8.6a
> credential boundary this slice consumes but never re-opens).

---

## 0. The boundary being proven

α8.6b proves the platform can **run one upload as a durable, resumable job** — poll →
lease → CAS state machine → bounded retries → idempotent, at-least-once-safe upload →
terminal outbox event — **without** owning credentials (α8.6a already does), **without**
talking to a real network destination (α8.6c does that), and **without** touching any
frozen orchestration/export/generation path (PUB-6).

```
User intent ─▶ PublishJob (QUEUED)
                    │  PublishWorker.run_once() polls due jobs
                    ▼
              ProcessPublishJob ── dual lease (publish_job:<id> + project_publish:<project_id>)
                    │  CAS QUEUED→RUNNING
                    │  authorize(social_account_id) ─▶ AuthorizedContext   (α8.6a, credential-blind to the adapter)
                    │  stream delivery MediaAsset bytes                    (PUB-1)
                    │  IDestinationPublisher.publish(ContentPackage, ctx, bytes)  ─▶ MockDestination
                    ▼
              settle: SUCCEEDED (platform_post_id) │ retry(QUEUED, backoff) │ FAILED(error)
                    │  emit publishing.publish_job.succeeded / .failed (outbox, fan-out only)
```

Everything below records the concrete seams this depends on and — critically — the
seams that **do not exist yet** and must be created in α8.6b.

---

## 1. The precedent to mirror: `ExportJob` / `ExportWorker` (facts)

The contract (§6–§7) says `PublishJob` is "modelled on `ExportJob`." The precedent is
concrete and complete:

| Concept | Export precedent | File |
|---|---|---|
| Status enum (StrEnum) | `ExportStatus`: `queued`, `running`, `succeeded`, `failed`, `canceled`; `is_terminal` over `{succeeded, failed, canceled}` | `app/domain/export/export_status.py` |
| Aggregate (frozen dataclass, OCC `version`) | `ExportJob` (+ `ExportJobClaim` poll DTO carrying `export_job_id` + resolved `project_id`) | `app/domain/export/export_job.py` |
| Poll worker | `ExportWorker.run_once() → ExportPollResult`; batch `list_claimable(limit)`, then per-job `process(...)` | `app/application/use_cases/export/export_worker.py` |
| Single-job processor | `ProcessExportJob.process(project_id, export_job_id)` | `app/application/use_cases/export/process_export_job.py` |
| Three-phase I/O discipline | **Phase 1 (DB txn):** acquire lock → commit → read → CAS `mark_running` → commit. **Phase 2 (no txn):** materialize + transcode + `put` bytes. **Phase 3 (DB txn):** register asset → CAS `mark_succeeded` → emit event → commit. **Finally:** release lock. | same |
| Lock key / owner | `export_job:{id}` / `export-worker:{uuid4}`; default lease 900s | same (lines ~105–116) |
| CAS transitions | `UPDATE … WHERE id=? AND status=<expected> … RETURNING`; returns `None` on CAS miss; `version = version + 1` | `app/infrastructure/repositories/export_job_repository.py` (`mark_running`/`mark_succeeded`/`mark_failed`) |
| Claim scan | `list_claimable(limit)`: global FIFO `WHERE status='queued' ORDER BY (created_at, id) LIMIT n`; **does not** atomically claim — claim happens via `mark_running` CAS inside the processor | same |
| Idempotency | **Partial-unique on a business tuple**, not an `idempotency_key` column: `uq_export_jobs_render_job_id_format_quality_orientation`, `postgresql_where=status IN ('queued','running','succeeded')`; `IntegrityError → ConflictError` in `add()` | `app/infrastructure/db/models/jobs.py`, migration `0003_export_jobs_partial_unique` |
| Creation use case | `CreateExportJob`: owner-scope via `projects.get_owned(...)`, pre-check `get_active(...)`, insert `status=QUEUED`, emit `ExportJobCreated`, race-recover on `ConflictError` | `app/application/use_cases/export/create_export_job.py` |

**Result-status vocabulary** the processor returns (not persisted job state):
`"exported" / "failed" / "skipped"(reason="locked") / "noop"`.

**Key fact for α8.6b:** the export machine is the exact shape the contract asks for. The
publish machine is a **rename + one extra lock + retry/backoff + `scheduled_at`**, not a
new pattern.

---

## 2. Distributed locks (facts) — the dual-lock question (Q2)

- **Port:** `IDistributedLockManager` (`app/application/interfaces/locks.py`):
  `acquire(key, owner, lease) → Lease | None`, `renew`, `release`, `reclaim_expired`.
  Exposed on the UoW as `uow.locks`.
- **Impl:** `SqlAlchemyDistributedLockManager` — a single atomic
  `INSERT … ON CONFLICT (lock_key) DO UPDATE … WHERE lease_until < now() RETURNING`
  (acquire-or-steal-expired). Lock keys are caller-supplied opaque strings.
- **Precedent uses one lock** (`export_job:{id}`). α8.6b (Q2, contract §7) requires
  **two**: `publish_job:<publish_job_id>` (execution) **and**
  `project_publish:<project_id>` (product serialisation). Both go through the same
  `uow.locks.acquire`; the pre-flight must decide **acquisition order + release order**
  (acquire job-lock then project-lock; release in reverse) and what a *second-lock miss*
  means (skip with `reason="project_locked"`, releasing the first lock).

**Factual tension for the pre-flight — where does `project_id` come from?**
`MediaAsset` carries `tenant_id` + `owner_user_id` **directly**, but
`project_id` is **nullable** (`app/domain/media/media_asset.py:55`). The export delivery
asset is not guaranteed to carry a project link. So the `project_publish:<project_id>`
lock **cannot** universally rely on `MediaAsset.project_id`. The pre-flight should decide
whether `publish_jobs` carries an explicit `project_id` column (recommended, mirrors how
`ExportJobClaim` resolves `project_id` by joining `render_jobs`) rather than depending on
the nullable asset link.

---

## 3. The finished-video source seam (PUB-1)

- **Read the delivery asset (owner-scoped):** `uow.media.get_owned(media_id, tenant_id,
  owner_user_id) → MediaAsset | None` (`app/application/interfaces/repositories.py`);
  a foreign/missing id returns `None` (uniform 404, anti-enumeration).
- **The delivery asset id** is `export_jobs.output_media_asset_id` — set by
  `ProcessExportJob.mark_succeeded(...)`. PUB-1 forbids reading `generation_assets`.
- **Stream bytes:** `IObjectStorage.get(key) → bytes` and `put(key, data, content_type)`
  (`app/application/interfaces/object_storage.py`); backend selection via
  `IStorageResolver.active()` / `.resolve(backend)`
  (`app/application/interfaces/storage_resolver.py`). `MediaAsset` carries the
  `(storage_backend, storage_bucket, storage_key, mime_type, size_bytes)` triple, exactly
  as `DownloadExport` and `ProcessExportJob` read it.

**Fact:** every seam needed to fetch and stream the finished MP4 already exists and is
owner-scoped; α8.6b consumes it read-only (PUB-6).

---

## 4. The α8.6a foundation α8.6b consumes (facts) — and the one shape gap

α8.6b builds directly on the shipped credential boundary:

- **`ISocialCredentialStore.authorize(social_account_id) → AuthorizedContext`**
  (`app/application/interfaces/social_credential_store.py`): returns a **fresh,
  already-refreshed** `AuthorizedContext` (`access_token`, `expires_at`, `scopes`) or
  raises `CredentialUnavailableError` (revoked / expired-unrefreshable / missing —
  **fail-closed**) / `CredentialDecryptionError` (tamper). The credential service is the
  sole decryptor (ADR-0047 C7).
- **`ISocialAccountRepository`** (`repositories.py`): `get_owned`, `list_for_owner`,
  `upsert_connected`, `mark_revoked` — owner-scoped, already on `uow.social_accounts`.
- **`SocialAccount` aggregate + `AccountStatus`** (`app/domain/publishing/`).

**Shape gap the pre-flight must resolve.** Contract §7 says the worker loads a
"**pre-authenticated client**" from the credential service. The shipped seam returns an
`AuthorizedContext` (a **bearer token**, not a client object). No "client" abstraction
exists. Cleanest reconciliation (a pre-flight decision, not asserted here):
`IDestinationPublisher.publish(...)` receives the `AuthorizedContext` (bearer) + the
`ContentPackage` + a byte stream, and the adapter constructs its own platform client from
the bearer — keeping the adapter credential-blind (PUB-5) while requiring **no** new
"client" type in α8.6b.

---

## 5. Events + fan-out (facts) — the pattern to copy

- **Outbox add (co-transactional):** `uow.outbox.add(aggregate_type, aggregate_id,
  event_type, payload, occurred_at, metadata)` (`event_outbox_repository.py`) — insert +
  flush inside the caller's txn.
- **Export naming precedent** (`app/application/use_cases/export/_events.py`):
  `aggregate_type="export_job"` (snake_case); event types `ExportJobCreated`,
  `ExportJobSucceeded`, `ExportJobFailed` (PascalCase). The contract (§10) names the
  publishing terminal events `publishing.publish_job.succeeded` /
  `publishing.publish_job.failed` — **note the naming differs** (dotted, lowercased).
  The pre-flight should reconcile: adopt the contract's dotted names, or follow the
  existing PascalCase `_events.py` convention. (This is a naming ruling, not a
  behavioural one.)
- **Fan-out consumer precedent:** `NotificationProjection`
  (`app/application/use_cases/notifications/notification_projection.py`) filters
  `_HANDLED_EVENT_TYPES` and calls a fresh `CreateNotification` with
  `source_event_id=event.id` (exactly-once per recipient per source event). PUB-8:
  publishing events are **fan-out only** — a projection consuming them is allowed;
  publishing never chains into another projection. A notification kind
  (`publish.succeeded` / `publish.failed`) is a natural but **optional** α8.6b add.
- **No auto-publish (PUB-2):** there is (correctly) **no** subscriber on
  `ExportJobSucceeded` that creates a job. α8.6b must not add one.

---

## 6. Migration style for `0014_publish_jobs` (facts)

ORM + Unit-of-Work tables (like `export_jobs` / `social_accounts`), **not**
raw-SQL/ORM-less (that convention is for seeded catalogues + execution ledgers; contract
§12).

- **Enum creation:** `CREATE TYPE … AS ENUM (...)` (see `0013_social_accounts` for the
  `op.execute` DDL style; `0001_baseline` for the `_ENUMS` dict style). A new
  `publish_status` enum (`queued`, `running`, `succeeded`, `failed`, `canceled`) mirrors
  `export_status`. **Registry impact:** `backend/tests/test_enums.py`
  `EXPECTED_ENUM_COUNT` (currently `27`) will need +1 (same pattern as α8.6a's fix).
- **Partial-unique idempotency:** mirror `0003_export_jobs_partial_unique` —
  `unique=True, postgresql_where=text("status IN ('queued','running','succeeded')")` over
  the publish business tuple. The pre-flight must choose the tuple (candidate:
  `(source_media_asset_id, social_account_id)` — "don't double-publish the same artifact
  to the same account while a job is active"). Also mirror the ORM `Index(...)` in
  `app/infrastructure/db/models/` and the `IntegrityError → ConflictError` mapping.
- **Columns (contract §12):** `id`, `status`, `scheduled_at`, `attempt`, `max_attempts`,
  `content_package` JSONB, `source_media_asset_id`, `social_account_id`,
  `platform_post_id`, `error`, plus ownership (`tenant_id` + owner, direct — the
  `MediaAsset`/`ExportJob` ownership convention), timestamps, `version`. `project_id` per
  §2. FKs: `social_account_id → social_accounts(id)`; `source_media_asset_id →
  media_assets(id)`.
- **`bump_version` trigger:** `0001_baseline` attaches `tg_<tbl>_biu_version_bump` to
  version-bumped tables; a new job table with OCC `version` should be added consistently
  (decide in pre-flight — export uses explicit `version = version + 1` in CAS updates).
- **ERD sync:** Stage 9 compares `docs/database/ERD.md`; a new `publish_jobs` entity +
  its FKs must be added there (as α8.6a added Cluster 13).

---

## 7. What does **not** exist yet (the α8.6b build surface)

Everything below is absent today and is exactly what α8.6b creates — all additive:

| Missing seam | Mirror of | Layer |
|---|---|---|
| `PublishStatus` (StrEnum) + `publish_status` PG enum | `ExportStatus` / `export_status` | domain + migration |
| `PublishJob` aggregate (+ `PublishJobClaim` poll DTO) | `ExportJob` / `ExportJobClaim` | `domain/publishing/` |
| `ContentPackage` value object (deterministic metadata; PUB-9) | *(new; contract §5)* | `domain/publishing/` |
| `IPublishJobRepository` (`add`, `get_owned`, `list_claimable`, `mark_running`, `mark_succeeded`, `mark_failed`, `reschedule_for_retry`) on `uow.publish_jobs` | `IExportJobRepository` | `application/interfaces/` |
| `IDestinationPublisher` port + `PublishResult` / `DestinationError(transient\|permanent)` | *(new; contract §8)* | `application/interfaces/` |
| `MockDestination` (deterministic, network-free) | *(new; the only adapter this slice)* | `infrastructure/publishing/destinations/` |
| `CreatePublishJob` use case (owner-scope, build `ContentPackage`, enqueue) | `CreateExportJob` | `application/use_cases/publishing/` |
| `ProcessPublishJob` (dual lock, CAS, authorize, stream, publish, settle, retry/backoff, events) | `ProcessExportJob` | `application/use_cases/publishing/` |
| `PublishWorker.run_once()` | `ExportWorker` | `application/use_cases/publishing/` |
| `_events.py` (terminal publish events) | export `_events.py` | `application/use_cases/publishing/` |
| Publish API (create/get/list; **read + enqueue only**, no worker endpoint) | `export_jobs.py` router | `api/v1/routers/` |
| Container factories `get_process_publish_job_use_case()` / `get_publish_worker()` | export factories | `core/container.py` |
| Migration `0014_publish_jobs` | `0001`/`0003`/`0013` | `alembic/` |

**Worker triggering (fact, not a gap to fill):** there is currently **no** CLI, HTTP,
or cron entrypoint that invokes `ExportWorker.run_once()` **or** `RenderWorker` — they
exist only as container factories, invoked by an external operator/cadence. `PublishWorker`
should follow the **same** model (contract §7: "no in-repo time scheduler"; `scheduled_at`
due-scan + external `run_once`). α8.6b should **not** add a scheduler.

---

## 8. Enforcement precedent (facts) — what CI already guards

- **Import-linter (`backend/pyproject.toml`):** α8.6a added "publishing domain is an
  isolated bounded context" and "encryption primitives confined to the credential
  adapter." α8.6b extends the bounded-context contract to the new publish modules and
  should add PUB-3/PUB-4 guards: `domain.publishing` / `infrastructure.publishing` must
  **not** import the AI `providers` / `resolver` / `generation` packages; and a test that
  `MockDestination` receives an injected `AuthorizedContext`/bytes and never touches the
  credential store (PUB-5).
- **CI Stage 14** already exists for publishing integration (α8.6a). α8.6b's
  publish-runtime integration tests extend Stage 14 (the `MockDestination` keeps it
  deterministic + network-free; contract §13) — it does **not** expand Stage 13.
- **Test doubles precedent:** `FakeUnitOfWork` + `FakeDistributedLockManager` (steal-on-
  expiry, owner fencing) + `FakeEventOutboxRepository` + `FakeExportJobRepository`
  (in-memory CAS) in `tests/unit/.../export/_helpers.py` and `auth/_fakes.py` are the
  exact fakes a `ProcessPublishJob` unit suite mirrors (`test_process_export_job.py`
  includes the `reason="locked"` skip test to copy for the dual-lock case).

---

## 9. Open questions for the α8.6b pre-flight (surfaced, not decided)

These are the decisions the pre-flight must rule — each has a concrete factual basis
above:

1. **`project_id` source for the `project_publish` lock** (§2) — explicit `publish_jobs.project_id` column vs. nullable `MediaAsset.project_id`. *(Recommended: explicit column.)*
2. **Idempotency tuple** for the partial-unique index (§6) — candidate `(source_media_asset_id, social_account_id)`.
3. **"Pre-authenticated client" reconciliation** (§4) — pass `AuthorizedContext` (bearer) into `IDestinationPublisher.publish(...)`; no new client type.
4. **Event naming** (§5) — adopt the contract's dotted `publishing.publish_job.succeeded/.failed` vs. the existing PascalCase `_events.py` convention.
5. **Dual-lock acquire/release order + second-lock-miss semantics** (§2).
6. **Retry/backoff shape** (Q4) — `max_attempts=5`, exponential backoff with cap; `reschedule_for_retry` sets `scheduled_at = now + backoff(attempt)` and returns to `QUEUED`; retryable vs permanent classification owned by the destination adapter's `DestinationError`.
7. **Optional notification projection** (§5) — add `publish.succeeded/.failed` kinds now or defer.
8. **`bump_version` trigger vs explicit CAS `version+1`** (§6) — match the export convention.

---

## 10. Non-goals (restated, so the pre-flight stays scoped)

- **No real destination API** (YouTube/TikTok/etc.) — α8.6c. α8.6b is `MockDestination` only.
- **No LLM captions/hashtags** — deterministic `ContentPackage` metadata only (PUB-9).
- **No auto-publish** from export completion (PUB-2).
- **No time scheduler / cron** — `scheduled_at` due-scan + external `run_once` (§7).
- **No custom thumbnail upload**; reuse the enrichment-derived thumbnail when present.
- **No `generation_assets → publish` bridge** (PUB-1; ADR-0046 X8).
- **No changes to any frozen path** (ADR-0042/0044/0045/0046) — strictly additive.

---

### Summary of established facts

The publish runtime is a **faithful clone of the export runtime** plus (a) a second
serialisation lock, (b) `scheduled_at` + bounded retry/backoff, and (c) a credential-blind
destination boundary that consumes the shipped α8.6a `authorize()` seam. Every upstream
seam it needs (owner-scoped `MediaAsset` read, object-storage streaming, distributed
locks, transactional outbox, fan-out projection, owner-scoped job creation, ORM+UoW
migration + partial-unique idempotency, import-linter isolation, CI Stage 14) already
exists and is proven. α8.6b introduces **no new architectural pattern** — only new domain
types, one migration (`0014`), and the `IDestinationPublisher`/`MockDestination` seam. The
eight open questions in §9 are the sole substance of the pre-flight.
