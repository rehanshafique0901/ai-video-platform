# α8.9b — Creator Scheduling · Pre-flight (design decisions)

**Baseline:** `v0.4.40-phase3-alpha8.9a` · **Milestone:** α8.9b (second increment of the α8.9
Creator Experience). Grounded by [`PHASE3_ALPHA8_9b_GROUNDING.md`](./PHASE3_ALPHA8_9b_GROUNDING.md).

**Goal:** let a creator schedule a YouTube publication using the existing pipeline, by supplying an
optional publish time on the create request. **Strictly additive; no scheduler, no runtime redesign.**

## Decisions

### SC1 — Mechanism: platform-native `ContentPackage.publish_at` (not worker deferral)
Creator scheduling maps to **YouTube-native scheduling** via `ContentPackage.publish_at`. The job is
queued and uploaded **immediately** by the existing worker; the adapter sets `privacyStatus=private`
+ `publishAt`, and YouTube flips the video live at that time. `publish_jobs.scheduled_at`
(worker-side deferral, used for retry back-off) is **left untouched**. This preserves current runtime
behaviour exactly and needs no scheduler/cron/timer/loop.

### SC2 — Ingress shape: one optional field
Add `publish_at: datetime | None = None` to `PublishJobCreateRequest`. Absent/`null` ⇒ today's
behaviour (immediate publish at the requested `visibility`). No other request/response change; the
response already echoes it via `ContentPackagePublic.publish_at`.

### SC3 — Validation (at the API boundary, Pydantic `field_validator`)
On a non-null `publish_at`:
1. **Timezone-aware only** — reject naive datetimes (`tzinfo is None`/no UTC offset) → **422**.
2. **Future only** — reject `publish_at <= now(UTC)` → **422**.
3. **Normalise to UTC** — return `v.astimezone(UTC)` so the stored/echoed value is canonical and the
   `ContentPackage` stays deterministic.

Rationale for boundary-level validation: it is the *creator-facing ingress* (the slice's stated
scope), returns 422 automatically, requires no clock injection into the use case, and matches the
existing `field_validator` precedent (`schemas/media.py`, `schemas/auth.py`). The use case remains a
trust-the-validated-input consumer, exactly as it already is for `title`/`visibility`.

*Note on "future only" at parse time:* the check uses `datetime.now(UTC)` at request time — the same
instant the request is being admitted — which is the correct reference for "schedule in the future".

### SC4 — Threading
`CreatePublishJob.execute(..., publish_at: datetime | None = None)` forwards `publish_at` into the
existing `build_content_package(publish_at=…)`. The router passes `body.publish_at`. Nothing else
changes: ownership, readiness, source resolution, event emission, and persistence are identical.

### SC5 — Idempotency preserved (schedule captured on first create only)
The `(source_media_asset, social_account)` idempotency is unchanged: a replay returns the existing
job and does **not** apply the replay's `publish_at` (consistent with every other content field).
Documented behaviour, covered by a test. No rescheduling endpoint in this slice.

### SC6 — No migration, no new port, no ADR
`publish_at` lives inside the existing `content_package` JSONB — no schema change. No interface
changes. No frozen boundary crossed (runtime/execution/adapter/credential-blindness/orchestration all
untouched). If implementation surfaces any contradiction, stop and propose an ADR — none is expected.

### SC7 — Explicitly deferred (out of scope)
Worker-side deferral via `scheduled_at`; rescheduling/cancel-schedule endpoints; recurring schedules;
per-platform schedule validation beyond tz-aware+future; scheduling for non-YouTube destinations;
timezone display/formatting; α8.9c dashboard.

## Files touched (additive)

| File | Change |
|---|---|
| `api/v1/schemas/publish_jobs.py` | `publish_at` field + `field_validator` (tz-aware, future, →UTC) |
| `application/use_cases/publishing/create_publish_job.py` | `publish_at` param → `build_content_package(publish_at=…)` |
| `api/v1/routers/publish_jobs.py` | pass `body.publish_at` to `execute(...)` |
| `backend/app/main.py` | version → `0.4.41-phase3-alpha8.9b-dev` |
| `CHANGELOG.md` | α8.9b entry |
| tests (unit / integration / API) | scheduling coverage (below) |

## Testing

- **Unit — schema validator:** accepts a future tz-aware time (normalised to UTC); rejects naive;
  rejects past/now; `None` passes through.
- **Unit — use case:** `publish_at` is threaded into the persisted `ContentPackage`; absent ⇒
  `publish_at is None`; idempotent replay ignores a new `publish_at`.
- **Unit — YouTube body (existing seam):** confirm `publish_at` ⇒ `privacyStatus=private` +
  `status.publishAt` (already covered; assert still green).
- **Integration (DB, existing Stage 14):** create with `publish_at` → the persisted
  `publish_jobs.content_package` round-trips `publish_at`; end-to-end create→worker→destination still
  succeeds with a scheduled package.
- **API (`tests/integration/api/test_publish_jobs.py`):** `POST /publish-jobs` with a future tz-aware
  `publish_at` → 201 and the response `content_package.publish_at` echoes it (UTC); naive → 422;
  past → 422.

No new CI stage: scheduling is an additive ingress on the existing Publishing bounded context, so its
integration coverage extends **Stage 14** in place (mirroring how α8.6a/α8.6b share Stage 14).

## Release
Hold at `0.4.41-phase3-alpha8.9b-dev`; one feature commit; push; open release-review PR; stop.
