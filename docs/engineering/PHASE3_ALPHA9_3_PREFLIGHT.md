# α9.3 — Publish Thumbnail Support — Pre-flight (design, pre-implementation)

> **Status:** Approved-for-review. Design blueprint for α9.3, bound by
> [ADR-0050](../decisions/ADR-0050-publish-thumbnail-source-and-delivery-boundary.md) (**Accepted**)
> and [`PHASE3_ALPHA9_3_GROUNDING.md`](./PHASE3_ALPHA9_3_GROUNDING.md). **No code yet** — per the
> established workflow this pre-flight **stops for review before implementation.**
> **Baseline:** `v0.4.45-phase3-alpha9.2` (frozen).
> **Pre-flight architectural-decision check:** every element stays inside existing ports-&-adapters /
> DI / API / worker patterns and the boundary ADR-0050 already fixed and **empirically verified**. The
> single boundary evolution — one optional field on the `UploadMedia` DTO — is exactly what ADR-0050
> §Boundary verification authorised. **No new architectural decision is introduced** (see §12).

---

## 1. Scope (what ships in α9.3)

An **optional, creator-supplied thumbnail** for a published video (ADR-0050 Option A). `POST
/publish-jobs` gains an optional `thumbnail_media_asset_id`; when present it is validated (owned +
image) at create, captured into the immutable `ContentPackage.thumbnail_media_asset_id` (field already
exists), materialised by the publish worker, and set on YouTube via `thumbnails.set` **after** a
successful `videos.insert` as a **best-effort, non-fatal** second upload. Strictly additive; **no
migration**. Under CI the destination is the deterministic `MockDestination`; the YouTube path is
covered network-free via `httpx.MockTransport`.

**Out of scope (deferred, additive future work):** auto-resolving the enrichment thumbnail (ADR-0050
Option B), publish-time thumbnail generation (Option C), and surfacing the thumbnail outcome on the
API/event (v1 records it as structured-log telemetry only — §7).

---

## 2. API DTO + router (create path)

`app/api/v1/schemas/publish_jobs.py` — add **one optional field** to `PublishJobCreateRequest`:

```python
    thumbnail_media_asset_id: UUID | None = None
```

- No Pydantic validator needed beyond the type: **ownership + image-kind are enforced in the use case**
  (they require a DB read), yielding the platform's standard `404`/`422` envelope.
- `ContentPackagePublic` already exposes `thumbnail_media_asset_id` (`schemas/publish_jobs.py:63`), so
  the response round-trips it with **no change**.
- `app/api/v1/routers/publish_jobs.py`: pass `body.thumbnail_media_asset_id` into
  `CreatePublishJob.execute(...)` (one new kwarg). No other router change.

---

## 3. `CreatePublishJob` — validate + capture (owner-scoped)

`app/application/use_cases/publishing/create_publish_job.py` — add a
`thumbnail_media_asset_id: UUID | None = None` parameter to `execute(...)`, resolved **inside the
existing UoW block**, after the source resolves and **before** `build_content_package`:

```python
if thumbnail_media_asset_id is not None:
    thumb = await self._uow.media.get_owned(
        thumbnail_media_asset_id, tenant_id, owner_user_id
    )
    if thumb is None:
        raise NotFoundError("thumbnail media asset not found",
                            details={"thumbnail_media_asset_id": str(thumbnail_media_asset_id)})
    if thumb.kind != "image":
        raise ValidationFailedError("thumbnail media asset must be an image",
                                    details={"kind": thumb.kind})
```

then thread it through:

```python
package = build_content_package(
    media_asset_id=source_media_asset_id,
    project_title=project_title,
    thumbnail_media_asset_id=thumbnail_media_asset_id,   # NEW
    title=title, description=description, tags=tags,
    visibility=visibility, publish_at=publish_at,
)
```

- **Owner-scoping (Invariant 3):** the same `get_owned(tenant_id, owner_user_id)` gate used for every
  other asset; a non-owned / missing id → `404`, a non-image → `422`, **before** the job is queued.
- **Immutability (Invariant 4):** the id lands in the immutable `ContentPackage` built once here. The
  existing idempotency pre-check/`ConflictError` recovery returns the **existing** job unchanged, so a
  replay with a *different* thumbnail does **not** mutate the original (documented + tested).
- No cross-context reads, no lineage: `media.get_owned` is the same repository publishing already uses.

`ContentPackage` / `build_content_package` need **no change** (the field + parameter already exist —
`domain/publishing/content_package.py:39,81`).

---

## 4. Destination boundary — the single, verified additive change

`app/application/interfaces/destination_publisher.py` — add a small frozen DTO and **one optional
field** on `UploadMedia`; the `IDestinationPublisher.publish` **method signature is unchanged**
(ADR-0050 §Boundary verification):

```python
@dataclass(frozen=True, slots=True)
class UploadThumbnail:
    """A materialised thumbnail image handle (worker-materialised; credential-neutral)."""
    path: str
    mime_type: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class UploadMedia:
    path: str
    mime_type: str
    size_bytes: int
    thumbnail: UploadThumbnail | None = None    # NEW — optional; adapters may ignore it
```

- **Backward-compatible:** both existing adapters read only `path`/`mime_type`/`size_bytes`
  (`mock_destination.py:54`, `youtube.py:73`/`_transmit`), so they compile and behave identically; the
  Mock stays deterministic/network-free. This is the exact additive extension ADR-0050 verified.
- `PublishResult` is **unchanged** (v1 records the thumbnail outcome via structured logging — §7), so
  the boundary grows by exactly one optional DTO field and one new value type — nothing else.

---

## 5. `ProcessPublishJob` — materialise the thumbnail (best-effort)

`app/application/use_cases/publishing/process_publish_job.py`, inside `_publish_and_settle`, after the
delivery `asset` resolves and within the existing `tempfile.TemporaryDirectory` block:

```python
thumbnail: UploadThumbnail | None = None
thumb_id = job.content_package.thumbnail_media_asset_id
if thumb_id is not None:
    async with self._uow:
        thumb_asset = await self._uow.media.get_owned(
            thumb_id, job.tenant_id, job.requested_by_user_id)
    if thumb_asset is not None:
        try:
            thumb_path = await self._materialize(thumb_asset, Path(tmp) / "thumbnail")
            thumbnail = UploadThumbnail(path=thumb_path, mime_type=thumb_asset.mime_type,
                                        size_bytes=thumb_asset.size_bytes)
        except ObjectStorageError:
            thumbnail = None          # best-effort: proceed without it (Invariant 2/7)
    # thumb_asset is None (e.g. soft-deleted after create) → best-effort skip, video still publishes

upload = UploadMedia(path=artifact_path, mime_type=asset.mime_type,
                     size_bytes=asset.size_bytes, thumbnail=thumbnail)
result = await adapter.publish(package=job.content_package, auth=auth, media=upload)
```

- **Best-effort resolution:** if the thumbnail asset is missing/unresolvable at publish time, the video
  **still publishes** (thumbnail skipped) — never a job failure (Invariants 2, 7).
- **Reuses `_materialize`** (the existing helper, `process_publish_job.py:223`) — same storage-resolver
  path, same "outside any DB transaction" discipline.
- **The worker (not the adapter) resolves + materialises** the asset → the adapter stays a
  credential-blind leaf that never touches `media_assets` (Invariant 5; PUB-5).

---

## 6. YouTube `thumbnails.set` — non-fatal second upload

`app/infrastructure/publishing/destinations/youtube.py` — after a **successful** `_transmit` yields the
`PublishResult` (video id), if `media.thumbnail is not None`, attempt one `thumbnails.set`:

```python
result = await self._transmit(session_url, media)          # PUB-11 governs this, unchanged
if media.thumbnail is not None:
    await self._try_set_thumbnail(result.external_post_id, auth.access_token, media.thumbnail)
return result
```

`_try_set_thumbnail` does a media upload to `POST {api_base}/upload/youtube/v3/thumbnails/set?videoId=…`
with the image bytes + bearer, wrapped so **any** failure (transport, non-2xx, timeout) is **swallowed**
and logged (`publish.thumbnail_failed`) — it **never raises `DestinationError`** and never re-enters
`videos.insert` (Invariants 7, 8; PUB-11 preserved: the video is already live, so a thumbnail failure
must not trigger a retry that could double-post). Success logs `publish.thumbnail_set`.

`MockDestination.publish` accepts the new optional field and logs a deterministic outcome
(`thumbnail present → set`); it remains network-free and deterministic (Stage 14 unaffected).

---

## 7. Failure semantics (ADR-0050 D3)

| Situation | Behaviour | Job outcome |
|---|---|---|
| No `thumbnail_media_asset_id` | thumbnail phase skipped entirely | identical to today (Invariant 2) |
| Thumbnail asset missing/unresolvable at publish | worker skips thumbnail; video publishes | `succeeded` |
| `videos.insert` fails | existing PUB-11 / retry classification | `retry`/`failed` (unchanged) |
| `videos.insert` succeeds, `thumbnails.set` fails | swallowed + logged `publish.thumbnail_failed` | **`succeeded`** (video live) |
| `videos.insert` + `thumbnails.set` succeed | logged `publish.thumbnail_set` | `succeeded` |

- **v1 recording = structured-log telemetry only.** Surfacing the thumbnail outcome on the API/event
  is deferred (additive; would need a `PublishResult`/event-property extension — out of scope, ADR-0050
  "does not decide"). No `PublishResult` change in α9.3.

---

## 8. Idempotency & determinism

- **Immutable capture:** the thumbnail id is fixed in the `ContentPackage` at first create; every worker
  attempt materialises the *same* asset. A create-replay returns the existing job unchanged (Invariant 4).
- **Best-effort never loops:** a thumbnail-only failure is never retried and never re-runs
  `videos.insert` (Invariant 8; PUB-11).
- **Determinism:** the `MockDestination` returns a deterministic post identity and a deterministic
  thumbnail outcome; the YouTube path is asserted via `httpx.MockTransport`. No new nondeterminism.

---

## 9. Dependency-injection wiring

**None new.** α9.3 adds **no** new use case, port, adapter, or registry — `CreatePublishJob`,
`ProcessPublishJob`, the destination registry, and both adapters are already wired
(`container.py:1838`). Only method parameters and one DTO field are threaded through. No `config.py`
change is required.

---

## 10. Import-linter contract

**No new contract; no existing contract weakened.** The thumbnail asset is resolved + materialised by
the **worker** (an application use case that already depends on `IUnitOfWork`/`media`), never by the
adapter. The adapter still imports only the destination + credential-store ports and the domain
`ContentPackage` — so **"Destination adapters are credential-blind leaves"**
(`pyproject.toml:390`) still holds unchanged (Invariant 5). Stage 3 (`import-linter`) re-proves it.

---

## 11. Testing plan

**Unit (`pytest -m unit`):**
- Schema: `PublishJobCreateRequest` accepts/omits `thumbnail_media_asset_id`; `ContentPackagePublic`
  round-trips it.
- `CreatePublishJob`: owned image → captured in `ContentPackage`; **non-owned id → `NotFoundError`
  (404)**; **non-image kind → `ValidationFailedError` (422)**; absent → `None`; replay returns the
  existing job (thumbnail immutable). Via the existing fake UoW/media repo.
- `ProcessPublishJob`: builds `UploadMedia.thumbnail` when the id is present + resolvable; **best-effort
  skip** when the thumbnail asset is missing/unresolvable (video still publishes → `published`).
- `YouTubeDestination` via `httpx.MockTransport`: (a) `videos.insert` + `thumbnails.set` both 2xx →
  `PublishResult` returned, `thumbnail_set` logged; (b) `videos.insert` 2xx + `thumbnails.set` fails
  (5xx/transport) → **still returns the `PublishResult`, no raise** (job succeeds); (c) no thumbnail →
  no `thumbnails.set` call issued.
- `MockDestination`: deterministic outcome with/without a thumbnail; stays network-free.

**Integration (new CI Stage 21, `requires_db=True`):**
`tests/integration/infrastructure/publishing/test_publish_thumbnail.py` — seed a unique user + project +
succeeded export + an owned image asset; drive the **real** `/api/v1/publish-jobs` endpoint + Mock
destination end-to-end and assert: create-with-thumbnail persists the id in `content_package`; the job
reaches `succeeded`; **owner isolation** (another user's image → `404`/`422`); **best-effort** (soft-
delete the thumbnail asset before the worker runs → the job still `succeeds`). Determinism confirmed by
repeated + reordered runs; the test cleans up the rows it created (publishing tables are mutable).

**CI gate:** add Stage 21 "publish thumbnail integration" to `backend/scripts/ci_gate.py` (docstring +
`_stages()`), mirroring Stages 15–20 ("each new slice earns its own stage").

---

## 12. Migration assessment + architectural-decision check

- **Migration: none.** No ORM/table/column/index/enum change; the thumbnail id persists via the existing
  `content_package` JSONB and references an already-registered `media_assets` row.
  `validate_schema.py` / `compare_erd.py` derive from unchanged ORM → stay green with no edits.
- **New architectural decision? No.** The only boundary evolution (the optional `UploadMedia.thumbnail`
  field + `UploadThumbnail` value type) is precisely what ADR-0050 **verified and authorised**; the
  failure semantics are ADR-0050 D3; the source is ADR-0050 D1 (Option A). Everything else is standard
  create-path validation, worker materialisation, and adapter I/O. **No stop required.**

---

## 13. Files touched (all additive unless noted)

**Edited (additive):**
`app/application/interfaces/destination_publisher.py` (`UploadThumbnail` + optional `UploadMedia.thumbnail`);
`app/api/v1/schemas/publish_jobs.py` (`PublishJobCreateRequest.thumbnail_media_asset_id`);
`app/api/v1/routers/publish_jobs.py` (pass the kwarg);
`app/application/use_cases/publishing/create_publish_job.py` (validate + capture);
`app/application/use_cases/publishing/process_publish_job.py` (materialise the thumbnail, best-effort);
`app/infrastructure/publishing/destinations/youtube.py` (`_try_set_thumbnail`, non-fatal);
`app/infrastructure/publishing/destinations/mock_destination.py` (accept + deterministically log);
`backend/scripts/ci_gate.py` (Stage 21); `CHANGELOG.md`; unit + integration test modules.
**Docs at documentation-sync:** `PUBLISHING_RUNTIME_CONTRACT.md` §5/§14 amendment (un-defer custom
thumbnail; record D1/D2/D3 + the best-effort invariant), `SYSTEM_MAP.md`, `PLATFORM_STATUS.md`,
`DECISIONS.md` cross-link to ADR-0050.
**Not touched (frozen):** `ContentPackage`/`build_content_package` (field already present), the
`IDestinationPublisher.publish` **method interface**, `PublishResult`, the credential store, and all
generation/render/export runtimes.

---

## 14. Stop

Pre-flight complete; no new architectural decision surfaced (all within ADR-0050). Per the established
workflow, **stopping for review before implementation.** On approval I will implement α9.3 exactly as
specified, then run the full ephemeral PostgreSQL gate and open the `-dev` release-review PR.
