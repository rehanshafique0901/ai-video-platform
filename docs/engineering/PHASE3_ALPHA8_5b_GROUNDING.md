# Phase 3 — α8.5b Grounding: Delivery & Distribution (download / storage backends / publishing / notifications)

> Status: **GROUNDING — facts only, no rulings.** This document establishes *what already
> exists in the code* before an α8.5b pre-flight proposes any design. It deliberately does
> **not** decide scope or forks; it surfaces the surface area so the pre-flight (and its
> sign-off) can make those calls.
>
> Companion to `PHASE3_ALPHA8_5a_PREFLIGHT.md` (the export engine that produced the delivery
> artifacts this slice would deliver). Baseline: `v0.4.30-phase3-alpha8.5a`.

---

## 0. Why ground first

α8.5a's grounding paid off because `export_jobs` + the `export_*` enums turned out to be
**pre-modeled** (ADR-0030), making the whole slice zero-migration. Before drafting α8.5b we
need the same answer for its four candidate concerns — **download-serving, cloud storage,
publishing, notifications** — because they have *very different* migration and risk profiles.
The headline result: **three of the four are zero-migration and additive; publishing is the
only greenfield bounded context.**

---

## 1. Where the pipeline currently ends

```
Provider → Completion → Ingestion → MediaAsset → Timeline
    → Render Composition → Canonical Master MediaAsset (RC5, immutable)
        → Enrichment (thumbnail / preview / GIF / waveform)
        → Export (α8.5a) → delivery MediaAsset(source="generated", source_metadata.origin="export")
                                    ↑ THIS is where the platform stops today
```

A finished video and its delivery encodings exist as `media_assets` rows with durable storage
coordinates — but **there is no way for a user to actually obtain the bytes, and no way to
send them anywhere.** That gap is α8.5b.

---

## 2. What already exists (zero-migration reuse)

### 2.1 Object storage — port + local adapter
`app/application/interfaces/object_storage.py` — `IObjectStorage`: `put` / `get` / `exists` /
`delete` by opaque `/`-delimited key, returning a `StoredObject(backend, bucket, key)`.
Backend-neutral, configuration-blind (W8.1.1 — root/bucket injected).
`app/infrastructure/storage/local_object_storage.py` — `LocalObjectStorage` (writes under
`<root>/<bucket>/<key>`, traversal-guarded, I/O off the event loop via `asyncio.to_thread`).
`storage_backend_enum = {local, s3, r2, azure_blob, gcs}` — **cloud backends are already
enumerated**, so an S3/R2/GCS/Azure adapter needs **no enum change and no migration**.
Config: `media_storage_root`, `media_storage_bucket`.

- **Gap:** the port has **no `signed_url()` / presigned-URL** method, and no
  retention/lifecycle concept. Local adapter has no notion of a URL at all.

### 2.2 `MediaAsset` — the deliverable
`app/infrastructure/db/models/media.py` §12. Carries the full storage triple
(`storage_backend` / `storage_bucket` / `storage_key`), `mime_type`, `size_bytes`,
`checksum_sha256`, `width` / `height`, `duration_seconds`, and a flexible `source_metadata`
JSONB. α8.5a registers each export as `MediaAsset(source="generated",
source_metadata.origin="export"` + master lineage`)`. Uniqueness on the storage triple.

### 2.3 `ExportJob` — the canonical delivery artifact row (download columns already exist)
`app/infrastructure/db/models/jobs.py` §17. Already has:
`output_media_asset_id`, **`download_count`** (`server_default 0`), **`last_downloaded_at`**,
`file_size_bytes`, `finished_at`, plus the ADR-0030 partial-unique index over
`(render_job_id, format, quality, orientation)`. The schema comment names this row explicitly
as *"the canonical artefact row (download_count, last_downloaded_at, output_media_asset_id all
live on it)."*

- **Consequence:** **download-serving needs no migration.** The counters exist and are already
  surfaced read-only by `ExportJobPublic` (`download_count` / `last_downloaded_at`) — but
  **nothing in the codebase increments them yet.**

### 2.4 Notifications — table fully modeled
`app/infrastructure/db/models/notifications.py` §23. `Notification(user_id, kind (free text),
title, body, payload JSONB, delivered_in_app_at, delivered_email_at, read_at, archived)` with
an unread partial index. **Notification persistence needs no migration.**

- **Gap:** there is **no `INotifier` port and no notify use case** — the table has no writer.

### 2.5 Event outbox + relay + poll-worker pattern (the execution substrate)
- Transactional **outbox** (`event_outbox`, §25) drained by `RelayService` →
  `PublisherPort` → in-process subscribers (e.g. `GeneratedMediaIngestionSubscriber` on
  `WorkflowRunSucceeded`). Lightweight fan-out.
- **Poll-worker** pattern, three live instances, all identical in shape
  (`claim → lease → transform → idempotent settle → emit event`): `RenderWorker`,
  `MediaEnrichmentWorker`, `ExportWorker`. A new delivery worker (if publishing needs one)
  would slot in additively.
- Export already emits `ExportJobSucceeded` / `ExportJobFailed` — natural triggers for
  notification and/or auto-publish subscribers **without touching α8.5a**.

### 2.6 Ownership & routing seams
`IProjectRepository.get_ownership` (system-only, never an HTTP route) is the established
cross-aggregate ownership gate (used by α8.3b/α8.4a/α8.5a). Owner-scoped nested routers exist
(`/projects/{id}/render-jobs/{id}/exports/...`). Auth/session middleware in place.

---

## 3. What does NOT exist (the real α8.5b surface)

| Concern | Present today? | Migration needed? | Notes |
|---|---|---|---|
| **Download-serving endpoint** (bytes / stream / redirect) | ❌ none | **No** | Every router returns a JSON envelope; no `FileResponse`/`StreamingResponse`/redirect anywhere. The export router comment literally says *"Download-serving is deferred."* `download_count`/`last_downloaded_at` exist but are never written. |
| **Signed / presigned URL** | ❌ none | No (port method is additive) | `IObjectStorage` has no URL method; local adapter has no URL concept. Needed for redirect-style delivery and for cloud backends. |
| **Cloud storage adapters** (S3 / R2 / GCS / Azure) | ❌ only `local` | **No** (enum pre-exists) | Additive `IObjectStorage` implementations; `storage_backend_enum` already lists all four. |
| **Publishing** (social/external destinations) | ❌ **nothing** | **Yes — new tables** | No `publish_jobs`, no `social_accounts`/connected-destinations, no publish-status enum anywhere in the canonical schema §0–§32. Greenfield bounded context. |
| **Connected-account credentials / OAuth to destinations** | ❌ none | **Yes** | Destination OAuth (YouTube/TikTok/IG/FB) is distinct from the existing user-login OAuth (`oauth_identities`) and from AI-provider credentials. |
| **Notification dispatch use case** | ❌ (table only) | No | `INotifier` port + a subscriber on lifecycle events. |
| **Retention / storage lifecycle policy** | ❌ none | Likely | No TTL/GC for delivery artifacts. |

### 3.1 Important distinction — "provider" ≠ "destination"
The frozen provider-capability ports (`providers.py`, `provider_dispatcher.py`) are **AI
generation** providers (image/video). A publishing target (YouTube/TikTok/IG/FB) is an
**outbound destination**, not a generation capability. Grounding conclusion: publishing must
**not** be modeled as a provider capability — that would pull an external-delivery concern
into the frozen generation surface. It is a new downstream bounded context.

---

## 4. Governance pre-check (for the pre-flight to formally rule on)

- **ADR-0042 (Gate 1):** download / storage / publishing / notifications are all strictly
  *downstream* of the frozen orchestration surface — none reads or mutates
  runner/completion/dispatcher/checkpoints/provider-protocol/usage. Expected **PASS**, same as
  α8.5a. (Publishing adds new tables, but new additive tables outside the frozen surface have
  not required a freeze override in any prior slice.)
- **ADR-0043 (Gate 2):** delivery is *below* the render boundary. Download reads bytes (trivi-
  ally RC5-safe); publishing reads a delivery `MediaAsset` and transmits it (never recomposes /
  re-renders). RC5 (master immutable) + RC6 (determinism) hold; RP1–RP9 govern the render
  layer, not delivery.
- **W8.5.3 already established:** master is canonical; deliveries are replaceable. Publishing/
  download consume *deliveries*, reinforcing that hierarchy — deleting a published/downloaded
  encoding never affects the master.

---

## 5. Natural scope split (observation, not a ruling)

The four concerns have sharply different size/risk. This mirrors how α8.4 became a–e and α8.5
became a/b, and strongly suggests α8.5b should itself be split rather than shipped as one slice:

| Candidate | Migration | Size | Immediate user value | External deps |
|---|---|---|---|---|
| **Download-serving** of existing exports (+ increment `download_count`/`last_downloaded_at`) | none | small | **highest** ("download my finished video") | none |
| **Signed URLs + cloud storage adapters** (S3/R2/GCS) | none | medium | infra (scales delivery) | cloud creds (injected, W8.1.1) |
| **Notification dispatch** on export lifecycle | none | small | medium | none (in-app), SMTP later |
| **Publishing** to social destinations | **new tables** | large | high, but heaviest | destination OAuth + platform APIs |

Observation: **download-serving is the smallest, zero-migration, highest-value increment** and
is the piece that makes the platform end-to-end usable for the core use case (a person creating
and downloading a social video). Publishing is a legitimately separate bounded context that
warrants its own grounding/pre-flight and almost certainly its own migration.

---

## 6. Open questions for the α8.5b pre-flight to settle

1. **Scope:** ship download-serving alone first (proposed working name **α8.5b**), and split
   cloud-storage (**α8.5c**?), notifications, and publishing (**α8.5d**?) into their own slices?
2. **Download delivery mechanism:** stream bytes through the app (`StreamingResponse` from
   `IObjectStorage.get`) vs. a `302` redirect to a signed URL (requires the new port method +
   works only for cloud backends)? A hybrid (stream for `local`, redirect for cloud) is
   possible.
3. **Download identity & counting:** is the download endpoint keyed on the `export_job`
   (canonical artifact row, so `download_count` increments there) or on the delivery
   `media_asset`? Is the counter increment best-effort (outside the byte stream) and idempotent
   under retries?
4. **Auth scope:** owner-only via `get_ownership`, or introduce shareable/tokened links (a
   larger surface — likely a later slice)?
5. **Publishing (if/when scoped):** is a "publish" a new aggregate that *consumes* an export
   artifact (never triggers export)? Are destinations modeled as connected accounts with their
   own OAuth? Are retries/idempotency modeled separately from export (they must be — different
   failure domain)?
6. **Notifications:** trigger via a relay subscriber on `ExportJobSucceeded`/`Failed`
   (lightweight, fits the relay) or a poll worker (only if dispatch becomes heavy)?

---

## 7. Grounding summary (the facts the pre-flight can rely on)

- **Download-serving, cloud storage, and notifications are all zero-migration** — the columns
  (`download_count`, `last_downloaded_at`), the `storage_backend` enum values, and the
  `notifications` table already exist.
- **Publishing is the only greenfield bounded context** and the only concern that needs a
  migration and external destination credentials; it should be treated as a separate slice and
  must not be modeled as a frozen provider capability.
- **The execution substrate is fully in place** (outbox/relay, poll-worker pattern, ownership
  gate, export lifecycle events) — every α8.5b concern is additive on established seams.
- **Both governance gates are expected to pass** (downstream of ADR-0042; below the ADR-0043
  render boundary; consistent with W8.5.1–W8.5.3).

No code has been written. The recommended next artifact is an **α8.5b pre-flight** that takes
a position on §6 (starting with the scope split in §5).
