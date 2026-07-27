# Phase 3 — Creator Experience (Dashboard + Notifications + Scheduling) — PRE-FLIGHT

**Status:** Pre-flight (design decisions only). No implementation, migrations, source edits,
branches, commits, or PRs.
**Baseline:** `v0.4.39-phase3-alpha8.8` (version constant `0.4.39-phase3-alpha8.8`,
`backend/app/main.py:103`).
**Grounding:** `docs/engineering/PHASE3_CREATOR_EXPERIENCE_GROUNDING.md` (approved).
**Deliverable:** this document. Implementation begins only after approval.

This pre-flight resolves the grounding's open questions with explicit, justified
recommendations, and confirms — with reasoning — that the slice is strictly additive, needs
no migration, no new port, and no ADR. Where a recommendation *would* have required a new
architectural decision or a freeze override, it is deliberately **deferred** and the reasoning
is recorded (per the directive to recommend an ADR rather than proceed).

---

## 1. Slice boundaries

### 1.1 Recommendation — one milestone, three additive increments

Ship the Creator Experience as milestone **α8.9**, decomposed into three independently
releasable, strictly-additive increments (mirroring the α8.6a/b/c cadence):

| Increment | Capability | Roadmap reconciliation |
|---|---|---|
| **α8.9a** | **Publish notifications** — a fan-out projection turning `PublishJobSucceeded`/`PublishJobFailed` into in-app notifications | **Fulfils the deferred DQ7 / roadmap row α8.6d** (`PLATFORM_STATUS.md:337`). α8.6d retires into α8.9a. |
| **α8.9b** | **Creator scheduling** — accept an optional platform-native publish time at create, mapped to the existing `ContentPackage.publish_at` | New (creator-set scheduling was not a named roadmap slice) |
| **α8.9c** | **Creator dashboard** — a read-only owner-scoped summary aggregating existing product state | New |

Rationale for decomposition: each increment has its own quality gate, its own `-dev` release
review, and its own finalize — matching the disciplined per-increment workflow used since
α8.6a. α8.9a is the smallest, highest-leverage, and closes an already-planned gap; α8.9b is a
pure wiring addition; α8.9c is a thin read model. They are ordered by leverage and by
dependency (none depends on another, so the order is a recommendation, not a hard chain).

Versioning: hold each increment at `0.4.4x-phase3-alpha8.9{a,b,c}-dev` until its release
review (exact numeric patch assigned at implementation time; the numeric part stays monotonic
per the established scheme).

### 1.2 In scope
- α8.9a: a new **projection handler** for the two publish terminal events; new `publish.*`
  notification kinds; registration in the existing `InProcessPublisher([...])` fan-out list.
- α8.9b: an **optional** `publish_at` field on the publish-create ingress, threaded into
  `build_content_package(publish_at=)`; validation; response already surfaces it.
- α8.9c: a new **read-only** `GET /api/v1/dashboard/summary` endpoint + owner-scoped count
  aggregation over existing tables/indexes.

### 1.3 Explicitly deferred (with reasons)
- **Email / push / websocket delivery channels** (roadmap **α8.5b.4**). Requires a new
  `INotifier` port + provider + templates + retry machinery — a larger, port-introducing
  slice. The `notifications.delivered_email_at` column is dormant and stays so. Out of scope
  to keep this slice additive and channel-free (in-app only, as today).
- **Creator-set `publish_jobs.scheduled_at` (worker-deferred *upload*).** Deferred by design
  (see §2.3): its firing precision depends on a reliable poll cadence / scheduler service,
  which does not exist and is a forbidden addition. We expose platform-native scheduling
  (`publish_at`) instead.
- **Rescheduling / cancel / edit of a queued publish.** Set-at-create only; a mutation surface
  (`PATCH /publish-jobs/{id}`) is a separate future slice.
- **Cross-runtime activity feed** (aggregating export/render/workflow jobs into the
  dashboard). α8.9c is scoped to the publishing/creator-facing product state. A general
  activity feed would also require a new owner-scoped **export-jobs list** (currently absent —
  grounding §6) and is deferred.
- **Analytics** (`analytics_events`) — see §5.
- **Second destination adapter** (the other half of the old α8.6d) — unrelated to Creator
  Experience; remains available behind the proven `IDestinationPublisher` seam for a later
  slice.

---

## 2. Scheduling (α8.9b)

### 2.1 API shape
Extend the **existing** `POST /api/v1/publish-jobs` request DTO `PublishJobCreateRequest`
(`backend/app/api/v1/schemas/publish_jobs.py`) with **one optional field**:

- `publish_at: datetime | None = None` — the platform-native go-live time.

No new endpoint. The field is optional and defaults to `None`, so the wire contract is
**backwards-compatible** (omitting it reproduces today's immediate-publish behaviour). The
response already carries it: `PublishJobPublic.content_package.publish_at`
(`ContentPackagePublic`, already on the wire) and `GET /publish-jobs` / `GET /publish-jobs/{id}`
surface it with no change.

> **Boundary note (important).** This modifies the **creation ingress** — `CreatePublishJob`
> (`.../publishing/create_publish_job.py`) — to pass `publish_at` into `build_content_package`.
> It does **not** touch the publish *runtime* execution model (`PublishWorker`,
> `ProcessPublishJob`, the state machine, locks, retries). It is exactly analogous to how
> `title`/`description`/`tags`/`visibility` already flow through create into the deterministic
> `ContentPackage` today. Under the project's established convention ("changes to existing
> files only to wire the new capability into the composition root and API"), this is additive
> ingress wiring, not a runtime change. This is the single existing-file behavioural touch in
> the whole slice; it is called out here for explicit reviewer sign-off.

### 2.2 Behaviour of `scheduled_at`
`publish_jobs.scheduled_at` (worker-side deferred pickup) **retains its current behaviour
unchanged**: it stays `NULL` on create (immediate eligibility) and is used **only** by the
retry-backoff path (`reschedule(...)`, DQ6). Creator input never sets it in α8.9b. The worker
claim scan (`list_claimable`) is untouched.

### 2.3 Relationship between `scheduled_at` and `ContentPackage.publish_at`
These are two independent axes; α8.9b uses only the second:

| Axis | Meaning | Owner | α8.9b |
|---|---|---|---|
| `publish_jobs.scheduled_at` | when **our worker** attempts the upload | platform runtime + retry backoff | unchanged (create → `NULL`) |
| `ContentPackage.publish_at` | when the **destination platform** makes the post public | the destination (YouTube `status.publishAt`) | **exposed to creators** |

**Why `publish_at`, not `scheduled_at`, is the creator scheduling primitive:** with `publish_at`
the job uploads promptly (as today) as a private/scheduled post, and the **platform** performs
the exact-time go-live transition (YouTube `publishAt` — already implemented,
`infrastructure/publishing/destinations/youtube.py:106-111`). This adds **no** new assumption
about our worker's cadence. Creator-set `scheduled_at`, by contrast, would require the worker
to poll around the scheduled instant to fire the upload on time — a guarantee only a scheduler
service could give, and a scheduler service is explicitly forbidden (§8, constraints). Hence
`scheduled_at`-as-creator-control is deferred, and we avoid needing a scheduler entirely.

### 2.4 Time-zone handling
- `publish_at` is a timezone-aware instant. Pydantic parses ISO-8601 with an offset; the
  column is `timestamptz`, so Postgres stores it normalized to UTC (consistent with all
  existing datetime columns).
- **Reject naive datetimes** (no offset) with `422` — a naive "3pm" is ambiguous. The client
  must send an explicit offset/`Z`. (Validation lives in the create use case /
  DTO validator, not the runtime.)

### 2.5 Validation rules (all → `422 ValidationFailedError`, the established shape)
1. `publish_at` must be timezone-aware (naive → 422).
2. `publish_at` must be in the **future** relative to server `now()` with a small skew grace
   (recommend `now() + 60s`), else 422 — you cannot schedule the past.
3. No per-platform capability gate at create. `ContentPackage` is platform-neutral (PUB-9);
   destinations that do not support scheduling (the Mock) treat `publish_at` as "publish now"
   at their boundary. This preserves the platform-agnostic create path and avoids leaking
   destination specifics into the ingress. (An optional upper-bound clamp — e.g. reject
   > platform max horizon — is deferred; it would embed platform policy in create.)

### 2.6 Idempotency
Unchanged and already correct. The partial-unique
`uq_publish_jobs_source_media_asset_social_account` over `status IN
('queued','running','succeeded')` (migration `0014`) means a repeat create for the same
`(source_media_asset, social_account)` returns the existing job (`200` replay) regardless of
`publish_at`. Consequence (documented, not a bug): the **first** create wins; a second create
with a *different* `publish_at` does **not** reschedule (rescheduling is deferred, §1.3). This
is the same first-writer-wins semantics publishing already has.

---

## 3. Publish notifications (α8.9a)

### 3.1 Exact event mapping
A new projection handler consumes **only** the two terminal publish events (never
`PublishJobCreated`), reusing the existing `CreateNotification` writer:

| Outbox event | Notification `kind` | `title` | `body` (neutral) | `payload` (identity subset) |
|---|---|---|---|---|
| `PublishJobSucceeded` | `publish.succeeded` | "Your video was published" | derived from `platform` (+ `platform_post_url` if present) | `publish_job_id`, `project_id`, `social_account_id`, `platform`, `platform_post_id`, `platform_post_url`, `published_at` |
| `PublishJobFailed` | `publish.failed` | "Your video couldn't be published" | the neutral `error.message` (already scrubbed, PUB-8/C8) or a fixed fallback | `publish_job_id`, `project_id`, `social_account_id`, `platform`, `error` |

- **Recipient**: `payload["requested_by_user_id"]` (present on both events — grounding §2).
- **`source_event_id`**: the outbox `event.id` (the dedupe key).
- **No secrets**: payloads copy only the already-neutral event fields (C8). No bearer, no
  bytes, no provider URL.

### 3.2 Notification kinds
`publish.succeeded`, `publish.failed` — dot-notation, mirroring the existing
`export.succeeded` / `export.failed`. `kind` is free-form `Text` (grounding §4), so **no
schema/enum change** is needed. (A scheduled-vs-live distinction for succeeded uploads with a
future `publish_at` is intentionally *not* modelled as a separate kind in α8.9a: the success
event payload does not carry `publish_at`, and inventing a "scheduled" kind would require
enriching the emitter — a publish-runtime change. Deferred; single `publish.succeeded` kind.)

### 3.3 Projection behaviour
A faithful structural twin of `NotificationProjection`
(`.../notifications/notification_projection.py`), as a **separate handler class** (a
projection must not chain into another projection — the fan-out-only rule, ADR-0041 /
`PLATFORM_STATUS.md:294`):
- Not-applicable event type → clean return (relay marks it published).
- Builds a **fresh** `CreateNotification` per event via the existing
  `container.get_create_notification_use_case` factory (one UoW per event) — **reused, not
  duplicated**.
- Registered by appending it to the existing `InProcessPublisher([...])` list in
  `container.init` (`backend/app/core/container.py:343-348`). Producers and the relay are
  untouched (ADR-0042 fan-out property).

### 3.4 Duplicate protection
DB-owned exactly-once, identical to export notifications: the partial-unique
`uq_notifications_user_id_source_event_id` refuses a second `(user_id, source_event_id)` write;
`CreateNotification` maps the `ConflictError` to an idempotent `duplicate` no-op. A relay
redelivery is therefore safe.

### 3.5 Failure handling
- **Malformed payload** (missing/invalid `requested_by_user_id`) → log + clean return (a bad
  immutable event is not retryable; never park the relay) — mirrors the export projection.
- **Genuine DB failure** inside `CreateNotification` → propagates so the relay records the
  attempt and redelivers later (at-least-once).
- **Unknown recipient** (user deleted) → the FK/constraint path yields `ConflictError`/clean
  no-op; never a crash.

---

## 4. Creator dashboard (α8.9c)

### 4.1 Read model only
A single **read-only** aggregation endpoint. No writes, no new table, no projection, no
mutation of any aggregate. It reports owner-scoped counts derived from existing tables.

### 4.2 Aggregation strategy
Owner-scoped `COUNT` queries over already-indexed columns, executed in one use case
(`GetDashboardSummary`) inside a single read UoW:

- **Publish jobs by status** — `COUNT(*) ... WHERE requested_by_user_id = :uid GROUP BY
  status` (uses `ix_publish_jobs_requested_by_user_id_created_at`; status is a small enum). A
  derived **`scheduled`** figure = succeeded/queued jobs whose `content_package->>'publish_at'`
  is in the future is **deferred** (needs a JSONB predicate; keep α8.9c to the five enum
  statuses + total).
- **Unread notifications** — reuse the existing `count_unread(user_id)`
  (`NotificationRepository.count_unread`, index `ix_notifications_user_id_unread`). No new
  code.
- **Connected social accounts** — count of `social_accounts` where `status='connected'` for
  the owner (uses the owner-scoped social read surface).

No cross-table joins; three independent indexed aggregates. Response is a fixed-size object
(no pagination needed — §4.5).

### 4.3 Required repositories (additive read methods on existing repos — **not** new ports)
Mirrors exactly how α8.5b.3r added read methods to `INotificationRepository`:
- `IPublishJobRepository.count_by_status(*, tenant_id, owner_user_id) -> dict[str, int]`
  (additive read method).
- `ISocialAccountRepository.count_connected(*, tenant_id, user_id) -> int` **or** reuse the
  existing `list_for_owner` and count in the use case (recommend the explicit count method for
  an index-only scan; final choice at implementation).
- Notifications: **no new method** — reuse `count_unread`.

These extend existing repository *interfaces* already exposed on the UoW; adding a method to an
existing interface is not introducing a new **port** (see §7.1).

### 4.4 Required endpoints
- `GET /api/v1/dashboard/summary` → `200 { data: DashboardSummaryPublic, meta }`.
  Authenticated via `CurrentUserDep`. That is the whole surface for α8.9c.

Illustrative response contract (design sketch, not code):

```json
{
  "data": {
    "publish_jobs": { "queued": 0, "running": 0, "succeeded": 0, "failed": 0, "canceled": 0, "total": 0 },
    "notifications": { "unread": 0 },
    "social_accounts": { "connected": 0 }
  },
  "meta": { "request_id": "..." }
}
```

### 4.5 Pagination
None. The summary is a fixed-shape aggregate, not a list, so the keyset primitive does not
apply. (Recent-activity *lists* are served by the already-existing `GET /publish-jobs` and
`GET /notifications`; making `GET /publish-jobs` keyset-paginated is a possible additive
enhancement but is **deferred** to keep α8.9c to the summary only.)

### 4.6 Ownership enforcement
Every aggregate filters by the authenticated caller (`tenant_id` + `requested_by_user_id` /
`user_id`) sourced solely from `CurrentUserDep` — never from a body/query param (ADR-0034,
W8.5b.8). A user can only ever see their own counts; there is no cross-tenant or cross-user
read path.

### 4.7 Performance considerations
- All three counts hit existing indexes (`ix_publish_jobs_requested_by_user_id_created_at`,
  `ix_notifications_user_id_unread`, the social-account owner scan). No new index required for
  the enum-grouped publish count (the composite index leads with `requested_by_user_id`).
- Fixed, bounded work per request (≤3 index scans); no N+1, no joins, no full-table scans.
- No caching layer introduced (counts are cheap and always fresh). If a future JSONB-derived
  `scheduled` metric is added, it would warrant its own index — hence it is deferred here.

---

## 5. Analytics — confirmed out of scope

`analytics_events` **remains out of scope** and untouched. Justification (grounding §5):
- It is a **dormant** partitioned baseline table with **no** writer, reader, repository, use
  case, or API anywhere in `app/`.
- Activating it would introduce a new writer/projection, monthly **partition management**, a
  retention/ingest policy, and a query surface — a distinct bounded concern, not required to
  make the publishing pipeline "usable".
- The dashboard's needs are fully met by **live owner-scoped counts** over operational tables
  (§4), so no event-analytics pipeline is needed.
- Adding it now would be a new architectural decision (an analytics projection target) — so it
  is deferred; if pursued later it should get its own grounding/pre-flight and likely an ADR.

---

## 6. API surface (consolidated)

| Increment | Method + path | Auth | Request DTO | Response DTO | Notable errors |
|---|---|---|---|---|---|
| α8.9a | *(none — projection only)* | — | — | consumed via existing `GET /notifications` | — |
| α8.9b | `POST /api/v1/publish-jobs` *(existing, +1 optional field)* | `CurrentUserDep` | `PublishJobCreateRequest` + `publish_at?` | `PublishJobPublic` (unchanged; already exposes `publish_at`) | `422` naive/past `publish_at`; existing `404`/`422`/`200`-replay unchanged |
| α8.9c | `GET /api/v1/dashboard/summary` *(new)* | `CurrentUserDep` | *(none)* | `DashboardSummaryPublic` (new) | `401` unauth; otherwise `200` (even with zero activity) |

- **Authentication**: uniformly `CurrentUserDep` (ADR-0034). No new auth mechanism.
- **Error semantics**: reuse the centralized handlers in `app.core.errors`
  (`ValidationFailedError`→422, `NotFoundError`→404, `UNAUTHENTICATED`→401) and the success
  `envelope` helper. No new error types.
- **DTOs**: `publish_at` added to the existing `PublishJobCreateRequest`; one new
  `DashboardSummaryPublic` (+ small nested models) in a new `schemas/dashboard.py`. No existing
  response DTO changes shape.

---

## 7. Architecture confirmations (every "No" justified)

### 7.1 New ports required? **No.**
- **Notifications projection (α8.9a)** attaches behind the existing `PublisherPort` and reuses
  the existing `CreateNotification` writer + `INotificationRepository` — no new abstraction.
- **Scheduling (α8.9b)** reuses `build_content_package(publish_at=)`, the existing
  `IPublishJobRepository.add(scheduled_at=)`, and the existing YouTube adapter mapping — no new
  seam.
- **Dashboard (α8.9c)** adds **read methods to existing repository interfaces** already on the
  UoW. Extending an existing interface with additive read methods is precisely the α8.5b.3r
  precedent and is **not** a new port (a new port = a new abstraction boundary / a new UoW
  member; none is introduced). No `INotifier`, no scheduler port, no analytics port.

### 7.2 New migrations required? **No.**
- Notification kinds `publish.*` need no DDL — `kind` is free-form `Text` (grounding §4).
- `publish_jobs.scheduled_at` and `content_package` (holding `publish_at`) already exist
  (migration `0014`, grounding §3/§9).
- Dashboard counts use existing indexes (`ix_publish_jobs_requested_by_user_id_created_at`,
  `ix_notifications_user_id_unread`, social owner scan) — no new table/column/index.
- `analytics_events` is untouched (§5).
- Therefore the alembic head is unchanged; the upgrade→downgrade→upgrade gate is unaffected.

### 7.3 ADR genuinely required? **No.**
- **α8.9a** is a textbook instance of the **already-decided** event-projection pattern
  (ADR-0041 §Event projection pattern; `PLATFORM_STATUS.md:286-296`) and fulfils the
  already-planned α8.6d/DQ7 — no new decision.
- **α8.9b** reuses an existing, already-designed model (`publish_at` → YouTube `publishAt`) and
  the ADR-0034 endpoint pattern; choosing `publish_at` over creator-set `scheduled_at` is a
  *scope* decision made **to avoid** a new architectural element (a scheduler) — the opposite
  of needing an ADR.
- **α8.9c** is an additive read model over existing state — no new bounded context, no new
  persistence, ADR-0034 covers the endpoint.
- No freeze override: ADR-0042 (orchestration freeze), ADR-0043 (render boundary), ADR-0045/
  0046 (AI/execution runtime), ADR-0047 (credential-blindness) are all respected — no producer,
  relay, runtime, planner, provider, or export/publish execution path is modified.

> **Deferred → would need an ADR if pursued (recorded, not proposed now):** (a) creator-set
> `scheduled_at` with a firing-time guarantee, which would require an in-process scheduler
> service (ADR-worthy new runtime component); (b) activating `analytics_events` as a projection
> target; (c) an `INotifier` delivery-channel port (email/push, α8.5b.4). Per the directive, if
> implementation drifts toward any of these, **stop and propose an ADR** rather than proceed.

---

## 8. Constraints compliance (restated against this design)

| Constraint | Compliance |
|---|---|
| No orchestration changes | ✅ producers/relay untouched; only a new fan-out consumer is added |
| No runtime changes | ✅ `PublishWorker`/`ProcessPublishJob`/state machine/locks/retries untouched; α8.9b touches only the **create ingress** (§2.1 boundary note) |
| No scheduler service | ✅ deliberately choose platform-native `publish_at`; creator-set `scheduled_at` deferred precisely to avoid a scheduler |
| No AI / Planner / verification changes | ✅ not referenced |
| No Export changes | ✅ export read/write untouched (no export-jobs list added; cross-runtime feed deferred) |
| No Publish runtime changes | ✅ runtime execution model untouched (only optional create-ingress input + a downstream projection) |
| No Asset Promotion changes | ✅ not referenced |
| Strictly additive | ✅ one optional DTO field, one new endpoint, one new projection, additive read methods; existing contracts backwards-compatible |
| Zero freeze overrides | ✅ none required (§7.3) |

---

## 9. Testing

### 9.1 Unit (Stage 4, `pytest -m unit`)
- **α8.9a**: publish projection — event→content mapping for both kinds; recipient extraction;
  malformed payload → log + clean return; non-applicable event → clean return; reuse of the
  `CreateNotification` factory (one UoW per event). Fakes for the publisher/use case.
- **α8.9b**: create-use-case validation — naive `publish_at` → 422; past `publish_at` → 422;
  valid `publish_at` threaded into `ContentPackage.publish_at`; omitted → `None` (today's
  behaviour); idempotent replay ignores a differing `publish_at`.
- **α8.9c**: `GetDashboardSummary` aggregation with fake repos — correct grouping/counts;
  owner scoping (foreign data excluded); zero-activity returns all-zero.

### 9.2 Integration (ephemeral Postgres)
- **α8.9a**: commit a `PublishJobSucceeded` / `PublishJobFailed` outbox event → `RelayService`
  → projection → assert exactly one `notifications` row per recipient; redelivery is an
  idempotent no-op (`(user_id, source_event_id)`).
- **α8.9b**: `CreatePublishJob` with a future `publish_at` against a real DB → assert stored
  `content_package.publish_at`; `scheduled_at` stays `NULL`; idempotent replay.
- **α8.9c**: seed owner data → `GET /dashboard/summary` returns correct counts; a second
  user's data is excluded (ownership isolation).

### 9.3 Stage-gate additions
Add **one** new gate, **Stage 16 — "creator experience integration"** (DB-backed), covering
α8.9a/b/c, consistent with the one-stage-per-slice precedent (Stage 13 generation, Stage 14
publishing, Stage 15 promotion). Unit tests fold into the existing Stage 4; static checks
(ruff/black/mypy/import-linter) and the migration round-trip are unaffected (no DDL). No new
import-linter contract is required (no new architectural boundary is introduced — the projection
lives in the notifications package and only reads event payloads).

> If the reviewer prefers minimal gate sprawl, the α8.9a/b integration tests could instead
> extend **Stage 14 (publishing integration verification)** and α8.9c ride a small API
> integration test, with no new stage. Recommendation stands at a dedicated Stage 16 for
> discoverability and cohesion; final call at implementation.

---

## 10. Recommended implementation order (per increment, when opened)

For each of α8.9a → α8.9b → α8.9c, follow the established order:
**domain/DTO → (read) port method if any → infrastructure → use case → API/wiring →
enforcement/tests → full ephemeral-Postgres gate → `-dev` release review → finalise → docs
sync.** No increment starts before the previous one is released, unless the reviewer authorizes
parallel tracks (they are independent).

---

## 11. Summary

The Creator Experience slice is deliverable as three strictly-additive increments under
milestone **α8.9** with **no migration, no new port, and no ADR**: publish notifications reuse
the decided event-projection pattern and the existing notification writer; scheduling reuses
the already-present `publish_at` model (platform-native, avoiding any scheduler); and the
dashboard is a thin read-only aggregation over existing indexed state. The single behavioural
touch to an existing file (optional `publish_at` on the publish-create ingress) is called out
for explicit sign-off and is additive, leaving the publish runtime frozen. Deferred items
(email/push, creator-set `scheduled_at`, rescheduling, cross-runtime feed, analytics) are
recorded with the reasons they would each require a separate decision/ADR.

**End of pre-flight. Awaiting review before any implementation begins.**
