# α8.9b — Creator Scheduling · Grounding (read-only)

**Baseline:** `v0.4.40-phase3-alpha8.9a` · **Status:** facts only, no design.

Scope of this document: verify, against the source at the baseline, exactly what already
exists for scheduling a YouTube publication, what is missing, and whether any migration / ADR
is required. This is the narrow follow-up to the umbrella
[`PHASE3_CREATOR_EXPERIENCE_GROUNDING.md`](./PHASE3_CREATOR_EXPERIENCE_GROUNDING.md) (§ scheduled
publishing) and the [`PHASE3_CREATOR_EXPERIENCE_PREFLIGHT.md`](./PHASE3_CREATOR_EXPERIENCE_PREFLIGHT.md)
(α8.9b row), re-confirmed here at the current head.

## 1. Two distinct "scheduling" concepts (verified)

The platform already distinguishes two orthogonal mechanisms; α8.9b concerns **only the second**:

| Mechanism | Column / field | Meaning | α8.9b? |
|---|---|---|---|
| **Worker-side deferral** | `publish_jobs.scheduled_at` (migration `0014`) | The `PublishWorker` claim scan (`list_claimable(now=…)`) will not pick up a job until `scheduled_at <= now`. Used today only for retry back-off. | **No** — untouched. |
| **Platform-native scheduling** | `ContentPackage.publish_at` | The job runs **immediately** (uploads now); the destination holds the video private and flips it live at `publish_at`. | **Yes** — this is creator scheduling. |

Choosing `publish_at` (platform-native) means **no scheduler, no worker-loop change, no runtime
redesign**: the existing runtime behaviour is preserved exactly (job uploads on the next worker
pass, as today).

## 2. What already exists (reusable, unchanged)

- **`ContentPackage.publish_at: datetime | None`** — the field is already a first-class part of the
  immutable content package (`domain/publishing/content_package.py`), already serialised in
  `to_dict`/`from_dict` (JSONB `content_package`), and already reconstructed on read.
- **`build_content_package(..., publish_at: datetime | None = None)`** — the builder already accepts
  `publish_at` and threads it into the `ContentPackage`. **It is simply never passed a value today.**
- **YouTube adapter mapping — already implemented.** `YouTubeDestination._build_request_body`
  (`infrastructure/publishing/destinations/youtube.py:106–113`) already maps a non-null
  `publish_at` to the Data API v3 request:
  ```
  status["privacyStatus"] = "private"
  status["publishAt"]      = package.publish_at.isoformat()
  ```
  (When `publish_at` is null it falls back to `package.visibility`.) **No adapter change is needed.**
- **`ContentPackagePublic.publish_at`** — the read DTO (`api/v1/schemas/publish_jobs.py`) already
  projects `publish_at` back to clients via `ContentPackagePublic.from_domain`.
- **Runtime, idempotency, ownership, retry, API surface** — all unchanged and reused:
  `CreatePublishJob` (ownership + readiness + `(source_media_asset, social_account)` idempotency),
  `PublishWorker`/`ProcessPublishJob`, dual-lock + version-fenced CAS + bounded retries, and the
  top-level `/api/v1/publish-jobs` endpoints.

## 3. The single missing piece (the gap)

The **creator-facing ingress** does not accept a schedule:

- `PublishJobCreateRequest` (`api/v1/schemas/publish_jobs.py`) has **no `publish_at` field**.
- `CreatePublishJob.execute(...)` has **no `publish_at` parameter**, and its
  `build_content_package(...)` call (`create_publish_job.py:145`) does **not** pass one.
- The router `create_publish_job` (`routers/publish_jobs.py:81`) therefore cannot forward a schedule.

Everything downstream of the request already honours `publish_at`. So α8.9b is a **pure ingress
addition**: request field + validation → use-case param → existing `build_content_package(publish_at=…)`.

## 4. Validation precedent (verified)

- Pydantic `field_validator` is the established request-validation seam (`schemas/media.py`,
  `schemas/auth.py`); a raised `ValueError` inside a validator surfaces as a **422** through the
  app's exception handlers.
- `PublishJobCreateRequest` currently has **no** `model_config` (no `extra="forbid"`). Adding
  `extra="forbid"` would be a behaviour change (it would start rejecting previously-ignored keys),
  so it is **out of scope** — α8.9b only adds an optional field + its validator.

## 5. Idempotency interaction (verified, must be preserved)

`CreatePublishJob` returns the **existing** active/fulfilled job (router → 200) for a repeat
`(source_media_asset, social_account)` — the content package (title/description/visibility, and
now `publish_at`) of the *replay* request is **not** applied to the existing job. This mirrors the
current handling of every other content field and must be preserved: **a schedule is captured only
on first create**; a replay does not reschedule.

## 6. Persistence / migration

`publish_at` lives **inside** the existing `content_package` JSONB column. There is **no new column**
and therefore **no migration**. (`publish_jobs.scheduled_at` already exists and is not touched.)

## 7. Ports / ADRs

- **No new port.** No repository, destination, or credential interface changes.
- **No ADR.** No frozen boundary is crossed: the publish runtime, execution model, destination
  adapters, credential-blindness (ADR-0047), and orchestration (ADR-0042) are all untouched. This
  is an additive request field feeding an already-wired path.

## 8. Test assets available to reuse

- Unit: `tests/unit/.../publishing/` (create-publish-job use-case tests) and the
  `content_package` builder.
- Integration: `tests/integration/infrastructure/publishing/test_publish_runtime_end_to_end.py`
  (real create → worker → destination) and `tests/integration/api/test_publish_jobs.py` (API).
- The YouTube adapter's `httpx.MockTransport` request-body tests already assert the
  `publishAt`/`privacyStatus` mapping.

## 9. Conclusion (facts)

α8.9b is implementable as a **strictly additive ingress**: an optional, validated (timezone-aware +
future) `publish_at` on `PublishJobCreateRequest`, threaded through `CreatePublishJob.execute` into
the existing `build_content_package(publish_at=…)`. **No migration, no new port, no ADR, no
scheduler, no runtime change.** The only new behaviour a creator observes is that a scheduled
publish uploads now and goes live at `publish_at` (YouTube-native), which the adapter already does.
