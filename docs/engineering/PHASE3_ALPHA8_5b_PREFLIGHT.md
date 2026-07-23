# Phase 3 — α8.5b.1 Pre-flight: Download Serving (deliver an export artifact to the user)

> Status: **SIGNED OFF.** First slice of the **distribution** stage. Input:
> `PHASE3_ALPHA8_5b_GROUNDING.md`. Companion to `PHASE3_ALPHA8_5a_PREFLIGHT.md`.
>
> **Rulings:** Gate 1 (ADR-0042) PASS · Gate 2 (ADR-0043) PASS (RC5 + W8.5.3) · **A** —
> introduce the narrow `IDownloadDelivery` seam (`DownloadRequest → DeliveryDecision`), ship
> **`LocalStreamDelivery` only** (no `signed_url()`, no cloud/CDN/S3/R2 — those are α8.5b.2) ·
> **B** — best-effort, non-transactional, non-retrying accounting (a counter failure is
> telemetry loss, never a user-visible failure) · **C** — endpoint keyed on `export_job_id`,
> resolving `output_media_asset_id`; requires `status==succeeded` AND
> `output_media_asset_id IS NOT NULL` · **D** — owner-only via the existing gate; team/share/
> public/expiring links deferred · **F** — storage boundary unchanged (`MediaAsset` owns
> location; `ExportJob` references the canonical output only) · **W8.5b.1 + W8.5b.2 +
> W8.5b.3** adopted · version `0.4.31-phase3-alpha8.5b1`.
>
> **Architectural conclusion carried from grounding:** α8.5b is **not one feature** — it is
> four downstream capabilities with very different risk profiles. This pre-flight scopes only
> the smallest, zero-migration, highest-value one (**download serving**) and *explicitly
> defers* storage backends, notifications, and publishing to their own slices.

---

## 0. Gates (answered first)

### Gate 1 — ADR-0042 (orchestration freeze)
> **Does α8.5b.1 touch any frozen orchestration module, checkpoint contract, orchestration
> state, provider protocol, or workflow lifecycle?**

**Answer: No.** Download serving **reads** a finished delivery `MediaAsset` (produced by
α8.5a) and streams its bytes to the owner; its only writes are the pre-existing
`export_jobs.download_count` / `last_downloaded_at` accounting columns (and, if adopted, an
audit event). It touches nothing on the frozen surface. Freeze guard stays green, **zero
overrides**.

### Gate 2 — ADR-0043 (render composition boundary)
> **Does α8.5b.1 change how media is composed?**

**No — download is *below* the render and export boundaries.** It performs **no** encoding,
transcoding, re-composition, or re-timing — it is a pure **read + transfer** of already-stored
bytes. RC5 (master immutable) and W8.5.3 (deliveries are replaceable) are reinforced, not
challenged: serving an artifact never mutates the artifact, the master, or anything upstream.

---

## 1. Positioning (what download serving *is*)

α8.5a made the artifact exist; α8.5b.1 makes it **obtainable**. It is an authenticated,
owner-scoped **read path** from an `export_jobs` row to the bytes of its
`output_media_asset_id`.

```
User (authenticated, owner)
  ↓  GET …/exports/{export_job_id}/download
ExportJob (succeeded)  →  output_media_asset_id  →  MediaAsset (storage triple)
  ↓  IDownloadDelivery.deliver(media_asset)
bytes streamed (local)   — or, later —   302 → signed URL (cloud, α8.5b.2)
  ↓  best-effort accounting: download_count += 1, last_downloaded_at = now
```

---

## 2. Grounding recap (what exists / what changes)

- **Download columns already exist (zero migration)** — `export_jobs.download_count`
  (`server_default 0`), `last_downloaded_at`, `file_size_bytes`, `output_media_asset_id`
  (`db/models/jobs.py` §17). The schema names this the *"canonical artefact row."* Nothing
  writes the counters yet.
- **`MediaAsset` owns the storage location** — full triple (`storage_backend` /
  `storage_bucket` / `storage_key`) + `mime_type` + `size_bytes` (§12). `ExportJob` references
  the delivery **only** through `output_media_asset_id`.
- **`IObjectStorage.get(key)` already returns bytes** — sufficient for local streaming today;
  it has **no** `signed_url()` method (added in α8.5b.2, not here).
- **Read/ownership seams exist** — `GetExportJob` already fetches an export job and verifies
  ownership through project + render job; `IProjectRepository.get_ownership` is the system-only
  gate. The export router is nested at `/projects/{pid}/render-jobs/{rid}/exports`.
- **No byte-serving anywhere today** — every router returns a JSON envelope; the export router
  comment says *"Download-serving is deferred."*

---

## 3. Design forks (for sign-off)

### Fork A — Delivery mechanism *(recommend: introduce the seam now, ship local streaming only)*
Two options for how bytes reach the client:
- **A1 — API streams bytes** (`StreamingResponse` from `IObjectStorage.get`). Simple, no new
  abstraction, but API workers carry large-file transfer (poor cloud scaling).
- **A2 — signed-URL redirect** (`302` to a presigned URL). Cloud-ready, scales, but requires a
  signed-URL abstraction + a cloud backend (neither exists yet → that is α8.5b.2).

**Recommendation (matches sign-off condition):** introduce a thin **`IDownloadDelivery`** port
now so the endpoint is written against the *scaling* shape, but implement **only**
`LocalStreamDelivery` (streams via `IObjectStorage`). The port returns a **delivery decision**,
not raw bytes:

```
IDownloadDelivery.deliver(asset)  →  DownloadResult
    ├── StreamDelivery(iterator, media_type, filename, content_length)   # local, now
    └── RedirectDelivery(url, expires_at)                                # cloud, α8.5b.2
```

The router renders a `DownloadResult` into either a `StreamingResponse` (+
`Content-Disposition: attachment`) or a `RedirectResponse(302)`. **No cloud code, no
`signed_url()` on `IObjectStorage`** enters this slice — only the neutral seam that lets
α8.5b.2 add `S3Delivery`/`R2Delivery` with **no endpoint change**. This keeps storage
abstraction out of user-download semantics (sign-off condition #2).

### Fork B — Download accounting *(recommend: best-effort, post-initiation, non-blocking)*
- **B1 — increment on successful *initiation*** (after auth + artifact resolved + delivery
  decided, before/independent of the byte stream completing), in a **short dedicated
  transaction**, **best-effort** (a counter failure never fails the download; a download never
  blocks on the counter). Sets `download_count += 1`, `last_downloaded_at = now()`.
- Alternatives: increment only after full transfer (unreliable for streams/redirects — the app
  can't observe a CDN transfer completing); or emit an async event and let a consumer count
  (heavier — deferred). **Recommendation: B1.** Accounting is metrics, not correctness; it must
  never corrupt or block delivery (see W8.5b.3).

### Fork C — Artifact identity / endpoint *(recommend: key on the ExportJob)*
- **C1 — endpoint keyed on `export_job_id`** (`GET …/exports/{export_job_id}/download`),
  resolving to `output_media_asset_id`. The `export_jobs` row is the canonical artifact row
  (counters live there), and the existing nested router + `GetExportJob` ownership check are
  reused verbatim. *Alternative:* a `/media/{id}/download` route keyed on `media_asset` —
  rejected for α8.5b.1 (loses the export accounting home; broader media-download semantics are
  a separate concern). Only a **`succeeded`** export with a live `output_media_asset_id` is
  downloadable (else `409`/`404`).

### Fork D — Authorization scope *(recommend: owner-only now; share links deferred)*
- **D1 — owner-only**, via the established `get_ownership` path already used by `GetExportJob`
  (404 on foreign/opaque access — no existence leak). **Deferred:** team/project-role access,
  and public/tokened **share links** (a larger surface — its own slice). Sign-off condition #1
  keeps this slice bounded.

### Fork F — Storage-boundary confirmation *(no change — recorded for the sign-off)*
`MediaAsset` **owns** the storage location; `ExportJob` references delivery bytes **only**
through `output_media_asset_id`. α8.5b.1 does **not** move, copy, or re-key any object, and does
**not** add cloud backends. (This is the §6.4 grounding question — answered: no boundary change.)

---

## 4. Scope split (the α8.5b family — this slice vs. deferred)

| Slice | Concern | Migration | In this slice? |
|---|---|---|---|
| **α8.5b.1** | **Download serving** (endpoint, auth, delivery seam, accounting) | **none** | ✅ **YES** |
| α8.5b.2 | Storage backends + `IObjectStorage.signed_url()` + `S3/R2/GCS/Azure` delivery adapters | none (enum pre-exists) | ❌ deferred |
| α8.5b.3 | Notification dispatch (`INotifier` + relay subscriber on `ExportJobSucceeded`) | none (table exists) | ❌ deferred |
| α8.6 | **Publishing** — `PublishJob` + `SocialAccount` + destination OAuth (new bounded context) | **yes (new tables)** | ❌ deferred |

**Explicitly excluded from α8.5b.1:** cloud storage adapters, signed URLs, CDN, notifications,
publishing, social integrations, share links, team access, retention/GC.

**Hard boundary (sign-off condition #5):** social platforms are **outbound destinations**, not
AI-generation providers — **no** social/destination concept enters the frozen provider
capability ports/enums. Publishing (α8.6) gets its own aggregate that *consumes* an export
artifact and never triggers export.

---

## 5. Proposed invariants

- **W8.5b.1 (new) — Download serving is observational and read-only.** It reads a finished
  delivery `MediaAsset` and transfers its bytes; it never mutates the artifact, the master, the
  storage object, orchestration/render/export lifecycle state, or any upstream entity. Its
  *only* writes are the `export_jobs` accounting fields (`download_count`,
  `last_downloaded_at`) and, if adopted, an audit event.
- **W8.5b.2 (new) — Delivery is a pure transfer of an existing artifact.** No encoding,
  transcoding, re-composition, re-timing, or creative transformation occurs on the download
  path (reinforces RC5 + W8.5.3 — deliveries are replaceable byte artifacts of the canonical
  master). Only `succeeded` exports with a live `output_media_asset_id` are servable.
- **W8.5b.3 (new) — Accounting never blocks or corrupts delivery.** Download-count/last-download
  updates are **best-effort** and isolated from the transfer: an accounting failure must not
  fail or delay the download, and delivery must not depend on the counter being written. (Exact
  counts are metrics, not correctness.)

---

## 6. Migration verdict

**Zero migration.** The accounting columns, `output_media_asset_id`, and the storage triple all
exist. α8.5b.1 is application code only: an `IDownloadDelivery` port + `LocalStreamDelivery`
adapter, a `DownloadExport` use case (lookup + ownership + `succeeded`/artifact guards +
delivery decision + best-effort accounting), one router endpoint, DI wiring, tests. One
additive repository method may be needed to bump the counters (a self-versioned CAS or a simple
`UPDATE … SET download_count = download_count + 1` — additive, non-frozen).

---

## 7. Test plan

- **Unit** — `DownloadExport`: owner + `succeeded` + live artifact → `StreamDelivery` with
  correct `media_type`/`filename`/`content_length` and a best-effort accounting bump; foreign
  user → `404` (no leak); non-`succeeded` / missing `output_media_asset_id` → `409`/`404`;
  missing storage object → `ObjectStorageError` surfaced as `404`/`410`; **accounting failure
  does not fail the download** (W8.5b.3); a fake `IDownloadDelivery` returning
  `RedirectDelivery` renders `302` (proves the cloud seam without cloud code).
- **Router** — `GET …/exports/{id}/download` streams bytes with
  `Content-Disposition: attachment; filename=…` and increments the counter; `401` unauth,
  `404` foreign.
- **Full gate** — ruff, black, mypy, import-linter, unit; **freeze guard green, zero
  overrides**.

---

## 8. Versioning

Runtime capability → **`0.4.31-phase3-alpha8.5b1`**, tag `v0.4.31-phase3-alpha8.5b1` (naming
mirrors the dotless `alpha8.5a` token; the roadmap concept is *α8.5b.1*). Standard two-commit
release ritual. *(Naming is a minor decision for sign-off — alternative:
`0.4.31-phase3-alpha8.5b-download`.)*

---

## 9. Deliverable (on sign-off)

α8.5b.1 **provides:** an authenticated, owner-scoped `GET …/exports/{id}/download` that streams
a finished export artifact via a neutral `IDownloadDelivery` seam (local streaming now,
signed-URL-redirect-ready for α8.5b.2), with best-effort download accounting — **zero
migration**, freeze guard green, **zero ADR-0042 overrides**, RC5/W8.5.3 preserved.

α8.5b.1 **explicitly excludes:** cloud storage adapters, signed URLs, CDN, notifications,
publishing, social integrations, share links, team access, retention.

> **Crisp definition:** α8.5b.1 adds a **download-serving read path** that delivers a completed
> export's bytes to its owner through a scaling-ready delivery seam and records best-effort
> download metrics — entirely downstream of the ADR-0042 frozen surface and below the ADR-0043
> render boundary.

---

## 10. Sign-off checklist (maps to the stated conditions)

- [ ] **Gate 1** (ADR-0042) PASS · **Gate 2** (ADR-0043) PASS
- [ ] **Fork A** — `IDownloadDelivery` seam introduced; **local streaming only** (no cloud, no
      `signed_url()`) — *storage abstraction does not become a cloud migration*
- [ ] **Fork B** — best-effort, non-blocking accounting (B1)
- [ ] **Fork C** — endpoint keyed on `export_job_id`; `succeeded` + live artifact only
- [ ] **Fork D** — owner-only; share links / team access deferred
- [ ] **Fork F** — storage boundary unchanged (`MediaAsset` owns location)
- [ ] **W8.5b.1 / W8.5b.2 / W8.5b.3** adopted
- [ ] **Download-serving is its own bounded slice**; storage/notifications/publishing deferred
- [ ] **No social/destination concept in frozen provider capabilities**
- [ ] Version + tag confirmed
