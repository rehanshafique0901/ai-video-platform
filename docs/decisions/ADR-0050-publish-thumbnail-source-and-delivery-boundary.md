# ADR-0050 — Publish Thumbnail Source & Delivery Boundary

**Status:** Accepted (Phase 3, α9.3 — Publish Thumbnail Support, 2026-07-28). Governance that
**precedes** implementation — like ADR-0044/0045/0047/0048/0049 — because it fixes a **bounded-context
boundary, the destination-adapter contract, and a new post-`videos.insert` failure invariant** before
any thumbnail code exists. Drafted at the α9.3 grounding stop and **Accepted after a review amendment**
that (a) replaced the assumed "no breaking port change" with an **empirically verified** boundary
finding (see §Boundary verification) and (b) added a §Load-bearing invariants section. The amendment
introduced **no new architectural question** — it tightened the recommended Option A conservatively.
The α9.3 pre-flight follows; **no implementation** accompanies this ADR. Grounded by
[`PHASE3_ALPHA9_3_GROUNDING.md`](../engineering/PHASE3_ALPHA9_3_GROUNDING.md).

**Builds on:** **ADR-0042** (orchestration/platform freeze — new capability plugs in **additively**,
never by editing a frozen runtime), **ADR-0043** (render-composition boundary — RC5: publishing never
recomposes/re-encodes; the render/export lineage is a frozen boundary), **ADR-0047** (publishing-
credential ownership — destination adapters are **credential-blind leaves**), **ADR-0048** (DB/contract-
owned idempotency & replay-safety precedent), **ADR-0049** (the discipline for a sanctioned, one-way,
neutral-DTO bridge out of Publishing), and the publishing runtime contract's **PUB-1** (publishing
consumes the export-delivery `MediaAsset` only), **PUB-5** (credential-blind adapters), **PUB-6**
(publishing never mutates upstream state), and **PUB-11** (an upload is never retried once the platform
may have durably accepted the media). [`PUBLISHING_RUNTIME_CONTRACT.md`](../engineering/PUBLISHING_RUNTIME_CONTRACT.md)
§5 and §14 **explicitly deferred** custom thumbnail upload to "a later slice"; this ADR is that slice's
governance.

---

## Context

α9.3 sets a **thumbnail** on a published YouTube video — the visual complement to α9.1's AI
title/description/hashtags (title + thumbnail are the two levers of YouTube click-through). Grounding
(`PHASE3_ALPHA9_3_GROUNDING.md`) established the shape almost entirely from existing parts, and
surfaced three things that are **not** settled by existing patterns.

What is already settled (verified in grounding, cited to `file:line`):

- **The value slot already exists and round-trips.** `ContentPackage.thumbnail_media_asset_id: UUID |
  None` (`domain/publishing/content_package.py:39`) serialises through the `publish_jobs.content_package`
  JSONB (`:50`, `:61`); `build_content_package(..., thumbnail_media_asset_id=None)` (`:81`) accepts it
  but every caller defaults it to `None`. **No migration is required.**
- **A thumbnail *engine* already exists.** `IThumbnailer` + `FfmpegThumbnailer` + `ThumbnailEnricher`
  produce a JPEG frame (`interfaces/thumbnailer.py`, `render/ffmpeg_thumbnailer.py`,
  `use_cases/media/enrichers/thumbnail.py`) — but only inside the **Media enrichment** context.
- **The publish runtime materialises exactly one artifact.** `ProcessPublishJob._publish_and_settle`
  (`use_cases/publishing/process_publish_job.py:165`) resolves + materialises the delivery asset to a
  temp dir (`:182`) and calls `adapter.publish(package, auth, media)` (`:189`). The YouTube adapter
  does `videos.insert` **only** (no `thumbnails.set`; `destinations/youtube.py`).
- **The destination boundary is frozen and has no thumbnail channel.**
  `IDestinationPublisher.publish(package, auth, media)` with `UploadMedia(path, mime_type, size_bytes)`
  and `PublishResult` (`application/interfaces/destination_publisher.py`).

### The three things not settled by existing patterns

1. **Source mismatch (the decisive fact).** Publishing consumes the **export-delivery** `MediaAsset`
   (PUB-1 = `export_jobs.output_media_asset_id`). That asset is registered by
   `process_export_job._register_output` (`use_cases/export/process_export_job.py:307`) as a **derived**
   asset (`source='generated'`, `source_metadata.origin='export'`, lineage to the render master).
   `EnrichGeneratedMedia` **refuses derived assets** (`use_cases/media/enrich_generated_media.py:126`)
   and only enriches **primary** generated videos. ∴ the delivery artifact **never** carries
   `source_metadata.enrichment.thumbnail_media_asset_id`. The contract's stated plan ("reuse the
   enrichment thumbnail when present", §5/§14) points at the **primary generated source** upstream of
   Timeline → Render → Export — **not** at what publishing publishes. So "where does the thumbnail come
   from?" is a genuine architectural choice, not a field read.
2. **Delivery boundary (§8).** Getting thumbnail bytes to the platform changes the **frozen**
   `IDestinationPublisher` / `UploadMedia` contract and adds a **second upload** (`thumbnails.set`) to
   the YouTube adapter.
3. **Post-`videos.insert` failure semantics (PUB-11 interaction).** A thumbnail is a **second** upload
   *after the video is irreversibly live*. If it fails, is the job succeeded or failed/retried? A retry
   that re-runs `publish()` would re-run `videos.insert` → **duplicate public video** — the exact
   hazard PUB-11 exists to prevent.

### The decision point

**Where does a published video's thumbnail come from (D1), how do its bytes cross the frozen
destination boundary without weakening PUB-4/PUB-5 (D2), and what are the failure semantics of the
second upload once the video is already live (D3)?**

D2 and D3 are **shared by all three D1 options** and are fixed once in the Decision. The options below
differ only in **D1 — the thumbnail source.**

- **Option A — Creator-supplied `thumbnail_media_asset_id`** (an owned image `MediaAsset`) on
  publish-create.
- **Option B — Auto-resolve the upstream enrichment thumbnail** by tracing delivery → render master →
  … → the primary generated source's enrichment marker.
- **Option C — Generate a fresh thumbnail from the delivery MP4 at publish time** (reuse
  `IThumbnailer` inside the publish runtime).

---

## Options

### Option A — Creator-supplied `thumbnail_media_asset_id` (explicit, owner-scoped input)

The creator (or a future UI) passes an owned image `MediaAsset` id on `POST /publish-jobs`.
`CreatePublishJob` validates it is the caller's and is an image, then stores it in the **existing**
`ContentPackage.thumbnail_media_asset_id`. `ProcessPublishJob` materialises its bytes as a second
artifact and hands them to the adapter (per D2); YouTube sets it via `thumbnails.set` after a
successful `videos.insert` (per D3).

- **Bounded-context ownership.** Publishing stays a **pure consumer of an owned artifact** — exactly
  its role for the delivery video today (PUB-1). It reads only `media_assets` it already owner-scopes;
  it makes **no** new cross-context reads and takes on **no** media-processing responsibility.
- **Owner-scoping.** The image is resolved with the same `get_owned(tenant_id, owner_user_id)` gate
  used everywhere; a non-owned / non-image / missing id is a clean `404`/`422` at create.
- **No lineage traversal, no Render/Export coupling.** It never touches the ADR-0043 render/export
  lineage or generation/enrichment state.
- **Un-defers a deferred capability.** This is precisely the "custom thumbnail upload" that §5 ("Custom
  upload deferred") and §14 ("No custom thumbnail upload in α8.6") **deferred to a later slice** — this
  ADR is that slice, so it must **formally un-defer** it and amend §5/§14.
- **Determinism / replay.** The chosen id is captured **once** in the immutable `ContentPackage`
  (built at create), so every worker attempt and every idempotent create-replay use the **same** id —
  deterministic and replay-safe by construction (mirrors how `publish_at`/`visibility` already behave).
- **UX.** Honest and controllable: the creator picks the thumbnail. The natural default (surfacing the
  enrichment-derived thumbnail id as a suggested pick) is a **UI/read concern**, not a publish-runtime
  concern — it composes cleanly on top later without changing this boundary.

### Option B — Auto-resolve the upstream enrichment thumbnail (lineage traversal)

At create (or publish) time, resolve the delivery asset → its render master → the master's Timeline/
generated-source lineage → the primary generated source's `source_metadata.enrichment.
thumbnail_media_asset_id`, and use that.

- **Bounded-context ownership.** **Weakens it.** Publishing would read **generation/enrichment state
  and render/export lineage** — new cross-context reads that PUB-1/PUB-6 and ADR-0043 deliberately fence
  off ("publishing consumes the export delivery only", "never mutates/knows upstream").
- **No stored link exists.** The delivery asset records only `origin='export'` + the render **master**
  id (`process_export_job.py:327`); the master → Timeline → primary generated source is **another,
  unstored hop**. Building it means new queries across three frozen contexts.
- **Determinism / robustness.** Fragile and conditional: the thumbnail exists only if enrichment ran,
  reached the current version, and the lineage is intact; multi-source timelines have **no single**
  source thumbnail. The result depends on background-worker state outside publishing's control.
- **Complexity / risk.** Highest cross-context blast radius for a cosmetic feature; couples Publishing
  to Media-enrichment internals that are free to evolve.

### Option C — Generate a fresh thumbnail during publish (reuse `IThumbnailer`)

`ProcessPublishJob` extracts a frame from the materialised delivery MP4 via `IThumbnailer` and uploads
it.

- **Bounded-context ownership.** **Violates the clean separation.** Publishing becomes responsible for
  **media processing** — a `Video → Image` transform that `interfaces/thumbnailer.py` deliberately
  keeps in the enrichment context. Adds an FFmpeg runtime dependency + config to the publish worker.
- **Runtime coupling / latency.** A new CPU/subprocess step and a new failure surface (`ThumbnailError`,
  timeouts) inside the load-bearing publish worker.
- **Determinism.** Frame extraction at a fixed timestamp is *roughly* stable but engine/codec-dependent;
  it re-derives bytes on every attempt rather than referencing a fixed, owned artifact.
- **UX.** No creator control (auto-frame only), and it re-implements enrichment's job in the wrong
  context.

---

## Evaluation

| Criterion | Option A — creator-supplied id | Option B — lineage auto-resolve | Option C — generate in publish |
|---|---|---|---|
| **Publishing ↔ Media ownership** | **Cleanest** — Publishing stays a pure consumer of an owned artifact; no new cross-context reads. | **Weakened** — reads generation/enrichment + render lineage (crosses PUB-1/PUB-6, ADR-0043). | **Violated** — media-processing moves into the publish runtime. |
| **Frozen destination boundary (§8)** | Additive extension (per D2) — same for all three. | Same extension **plus** upstream reads. | Same extension **plus** an engine dependency. |
| **Determinism / reproducibility** | **Strong** — fixed id in the immutable `ContentPackage`. | Conditional on enrichment state/version/lineage. | Re-derives bytes per attempt; engine-dependent. |
| **Idempotency / replay safety** | **Strong** — replay uses the same captured id (like `publish_at`). | Re-resolution may drift between attempts. | Re-generation per attempt; not a stable reference. |
| **Failure semantics after `videos.insert` (D3)** | Best-effort second upload (fixed below) — same for all. | Same, but a resolution failure adds a pre-upload failure mode. | Same, plus a generation failure mode before upload. |
| **Runtime coupling** | **Lowest** — one extra materialise; no new engine/reads. | New cross-context read paths. | New FFmpeg subprocess + config in publish. |
| **Long-term maintainability** | **Highest** — self-contained; boundary unchanged. | Couples to enrichment internals that evolve. | Duplicates enrichment logic in the wrong context. |
| **User experience** | **Best** — explicit creator control; default is a UI concern layered on later. | Automatic-but-surprising; no control; blank when absent. | Automatic-only; no control. |
| **Future extensibility** | **Best** — a later slice can add an optional server-side default (resolve enrichment thumbnail) *once a proper provenance link exists*, without changing this boundary. | Bakes the fragile traversal into the runtime now. | Locks media-processing into publish. |
| **Operational risk** | **Lowest.** | Medium–high (cross-context queries). | Medium–high (new runtime, latency). |
| **Consistency with ADR-0043 / 0047 / 0048 / 0049 / PUB-11** | **Fully consistent** — no lineage crossing (0043), credential-blind (0047/PUB-5), replay-safe via immutable value (0048), Publishing stays self-contained — even cleaner than 0049's bridge (no new dependency at all). | Strains ADR-0043 (lineage) + PUB-1/PUB-6. | Strains the bounded-context discipline 0049 preserved. |

---

## Decision (recommendation — pending approval)

**Recommend Option A** for D1, and **fix D2 and D3** (which apply to any source option) as load-bearing
invariants. Option A is **materially superior**: it is the only source that keeps Publishing a **pure
consumer of an owned artifact** (no new cross-context reads, no media-processing in the publish
runtime), is **deterministic and replay-safe** by capturing the id in the immutable `ContentPackage`,
and stays fully consistent with ADR-0043 (no lineage crossing), PUB-1/PUB-6 (no upstream reads/mutation),
and PUB-5 (credential-blind). Options B and C each buy an "automatic" thumbnail at the cost of a frozen
boundary (B: render/export lineage + enrichment coupling; C: media-processing inside publish) for a
cosmetic feature — a poor trade against the discipline held from α8.x through α9.2.

This ADR would **fix** the following (everything else deferred to the α9.3 pre-flight):

1. **D1 — Source & ownership.** The published thumbnail is an **explicit, creator-supplied, owner-scoped
   image `MediaAsset`**, carried in the **existing** `ContentPackage.thumbnail_media_asset_id`.
   `CreatePublishJob` validates ownership + image kind (`404`/`422`) at create; the id is captured once
   in the immutable `ContentPackage`. **Auto-resolution (Option B) and publish-time generation (Option
   C) are rejected.** This **formally un-defers** the custom-thumbnail capability that
   `PUBLISHING_RUNTIME_CONTRACT.md` §5/§14 deferred, and requires a matching §5/§14 amendment.
2. **D2 — Delivery boundary (§8), additive & credential-blind (empirically verified — see §Boundary
   verification).** The thumbnail crosses as an **optional, worker-materialised, credential-neutral
   artifact handle carried on the `UploadMedia` boundary DTO**. Inspection confirms this needs **no
   change to the frozen `IDestinationPublisher.publish` method interface** and leaves both existing
   adapters behaviourally unchanged (they read only the existing `UploadMedia` fields). The **worker**
   materialises the thumbnail bytes (outside any DB transaction, exactly like the primary artifact);
   the adapter uploads them using the **same short-lived `AuthorizedContext`** and stays a
   **credential-blind leaf** (PUB-5/ADR-0047). The thumbnail is **not** an AI provider and **not** a
   new registry (PUB-4 untouched). *(The exact field name/DTO shape is a pre-flight detail; the
   invariant fixed here is "carried additively on `UploadMedia`, worker-materialised, credential-blind,
   adapter-optional, with the `publish` method interface unchanged.")*
3. **D3 — Best-effort, non-fatal second upload (new invariant, preserves PUB-11).** The thumbnail is
   set **only after** a successful `videos.insert`. A thumbnail-phase failure is **strictly best-effort
   and non-fatal**: the job settles **`succeeded`** (the video is live), the failure is recorded as
   neutral telemetry, and the job is **never retried or failed on a thumbnail-only error** — because a
   retry would re-run `videos.insert` and risk a **duplicate public video**, exactly what **PUB-11**
   forbids. PUB-11's rule for the *primary* upload is unchanged.

### Boundary verification (α9.3 review amendment)

The claim in D2 is **not** left as an assumption. It was verified by direct inspection of the frozen
destination boundary and both concrete adapters:

- **The interface.** `IDestinationPublisher.publish(*, package, auth, media)` and the boundary value
  types (`UploadMedia(path, mime_type, size_bytes)`, `PublishResult`, `DestinationError`) live in
  `application/interfaces/destination_publisher.py` (method at `:71`, `UploadMedia` at `:40`).
- **Both adapters consume only the existing `UploadMedia` fields.** `MockDestination.publish`
  (`infrastructure/publishing/destinations/mock_destination.py:38`) reads only `media.size_bytes`
  (`:54`). `YouTubeDestination.publish` (`infrastructure/publishing/destinations/youtube.py:59`) reads
  only `media.size_bytes` (`:73`), `media.path` / `media.mime_type` (`_transmit`, `:160`). Neither
  reads any field beyond the three that exist today.
- **The sole constructor is the worker.** `UploadMedia(...)` is built only in
  `ProcessPublishJob._publish_and_settle` (`use_cases/publishing/process_publish_job.py:184`).

**Verified outcome.** The existing destination boundary **can** carry an additive thumbnail handle
**without changing the frozen `IDestinationPublisher.publish` method contract**: the thumbnail rides as
a **new *optional* field on the `UploadMedia` DTO** (default `None`), set by the worker and read only by
an adapter that supports thumbnails. Because both current adapters read only the pre-existing fields,
they remain **source- and behaviour-compatible** — the Mock stays deterministic and network-free, and
Stage 14 is unaffected.

**Honest scope of the change (not hidden).** This is nonetheless an **additive, backward-compatible
evolution of the `UploadMedia` boundary value type** (one new optional field). It is **not** a change
to the `IDestinationPublisher` *interface method*, and it is **not** a breaking change to any adapter —
but the ADR records it explicitly rather than claiming the boundary is untouched. Adding a required
parameter to `publish(...)` (which *would* break every adapter) is **rejected** in favour of this
optional-field extension (see Alternatives Considered §3). If, at implementation, this optional-field
extension proves impossible without a breaking interface change, that is new architectural uncertainty
and returns here for a superseding decision.

---

## Load-bearing invariants (fixed by this ADR — may not be weakened without a superseding ADR)

1. **`thumbnail_media_asset_id` is optional.** It is a `UUID | None` on the immutable `ContentPackage`;
   a publish request may omit it.
2. **Publish behaviour is identical when absent.** With no thumbnail, `POST /publish-jobs` and the
   publish runtime behave exactly as today — the thumbnail phase is skipped entirely.
3. **Ownership is verified before publish.** When present, `CreatePublishJob` verifies the id is the
   caller's own image `MediaAsset` (owner-scoped `get_owned`; `404`/`422` otherwise) **before** the job
   is queued.
4. **The thumbnail is immutable for the lifetime of that `ContentPackage`.** The id is captured once at
   create into the immutable `ContentPackage`; it never changes across worker attempts or idempotent
   create-replays (replay returns the existing job unchanged).
5. **Destination adapters never resolve lineage.** Adapters receive a ready, materialised handle and a
   bearer only; they never read `media_assets`, enrichment markers, render/export lineage, or any
   repository (PUB-5 credential-blind leaf; ADR-0043 lineage never crossed by an adapter).
6. **Publishing never generates thumbnails.** The publish runtime does not run `IThumbnailer` or any
   media-processing engine; it only materialises bytes of an already-registered, owned image asset
   (Option C rejected).
7. **Thumbnail upload is best-effort, only after the primary publish succeeds.** `thumbnails.set` runs
   **only** after a successful `videos.insert`; a thumbnail-phase failure never fails the job, which
   settles `succeeded` (the video is live) with the failure recorded as neutral telemetry.
8. **Thumbnail failures never trigger another primary publish attempt.** A thumbnail-only failure is
   **never** retried and never re-enters `publish()`/`videos.insert` — preserving PUB-11's guarantee
   that a creator's channel is never double-posted.

---

## What this ADR does *not* decide (deferred to the α9.3 pre-flight)

- The exact **destination-port signature/DTO** for the optional thumbnail handle (`publish(...)`
  parameter vs. an extended/`UploadThumbnail` DTO) — only the *invariant* in D2 is fixed.
- The concrete **`thumbnails.set`** call shape, image constraints (YouTube format/size limits), and how
  the adapter validates/classifies them (neutral `DestinationError` codes, non-fatal per D3).
- The **API surface**: the `PublishJobCreateRequest.thumbnail_media_asset_id` field, its validation
  messages, and any image-kind gating specifics.
- How a thumbnail-phase failure is **recorded** (event property vs. job-error metadata) given the job
  is `succeeded` — subject to PUB-8's fan-out shape.
- The **CI stage** addition, test strategy (network-free `MockTransport`), and documentation wiring
  (`DECISIONS.md` cross-link, the §5/§14 contract amendment, `SYSTEM_MAP.md`, `PLATFORM_STATUS.md`).
- Whether/when a **future slice** adds an optional server-side default that resolves the enrichment
  thumbnail (Option B's intent) once a proper delivery→source provenance link exists — explicitly **not**
  built here.

---

## Alternatives Considered (beyond A/B/C)

1. **Make the thumbnail mandatory / auto-apply without opt-in.** *Rejected.* Publishing must keep
   working with no thumbnail (the field is `None` today); a thumbnail is an optional enhancement, never
   a publish prerequisite.
2. **Fail or retry the job when `thumbnails.set` fails.** *Rejected.* Directly violates PUB-11 (a retry
   re-runs `videos.insert` → duplicate video). D3 makes the thumbnail phase best-effort.
3. **A breaking change to `IDestinationPublisher.publish` (thumbnail as a required arg).** *Rejected.*
   Needlessly breaks the Mock and forecloses thumbnail-less destinations; the additive optional handle
   (D2) achieves the same with zero breakage.
4. **Store thumbnail bytes/handles on a new table or on the `PublishJob`.** *Rejected.* No consumer
   needs it; the owned image already lives in `media_assets` and its id in `content_package` JSONB — no
   migration, consistent with ADR-0048's "reuse existing storage" posture.
5. **Put thumbnail resolution/generation inside a destination adapter.** *Rejected.* Adapters are
   credential-blind upload leaves (PUB-5); resolving owner-scoped assets or running FFmpeg there would
   break the boundary the whole publishing runtime relies on.

---

## Consequences

- **Positive.** Creators get a real thumbnail on published videos with **zero migration**; Publishing
  stays a self-contained bounded context (no new cross-context reads, no media-processing) — the
  cleanest of the three sources and consistent with every boundary held since α8.x; the destination
  boundary grows by a **backward-compatible optional** channel; PUB-11's no-duplicate guarantee is
  preserved by making the second upload best-effort; the enrichment-thumbnail default remains a clean
  future add-on (UI or a later provenance-linked slice).
- **Cost.** The custom-thumbnail capability is **formally un-deferred** (§5/§14 amended); the
  destination port + YouTube adapter grow a thumbnail path; the worker materialises a second artifact;
  a new best-effort failure mode must be tested; a companion contract amendment lands at implementation.
- **Boundary added (a future slice may not cross without its own ADR).** *The published thumbnail is an
  explicit, creator-supplied, owner-scoped image asset carried in `ContentPackage`; Publishing performs
  no lineage traversal and no thumbnail generation; the thumbnail crosses the destination boundary only
  as an additive, worker-materialised, credential-blind, adapter-optional artifact; and the thumbnail
  upload is strictly best-effort and never triggers a job retry/failure once the video is live (PUB-11
  preserved).*

---

## Change log

| Date | Change |
| --- | --- |
| 2026-07-28 | Proposed (governance, ahead of α9.3), drafted at the grounding stop for review. Fixes the thumbnail **source** (D1 — recommend Option A: explicit creator-supplied, owner-scoped image `MediaAsset` in the existing `ContentPackage.thumbnail_media_asset_id`; rejects Option B lineage auto-resolve and Option C publish-time generation), the **delivery boundary** (D2 — an additive, worker-materialised, credential-blind, adapter-optional thumbnail handle; PUB-4/PUB-5 preserved), and the **post-`videos.insert` failure semantics** (D3 — best-effort, non-fatal second upload that never retries/fails the job once the video is live; PUB-11 preserved). Un-defers the custom-thumbnail capability (`PUBLISHING_RUNTIME_CONTRACT.md` §5/§14). Compares A/B/C across Publishing↔Media ownership, the frozen destination boundary (§8), determinism, idempotency/replay, failure semantics, runtime coupling, maintainability, UX, future extensibility, and consistency with ADR-0043/0047/0048/0049 + PUB-11. |
| 2026-07-28 | **Accepted.** Amended per α9.3 review: (1) added §Boundary verification, which **empirically verifies** (by inspecting `destination_publisher.py`, `mock_destination.py:38-54`, `youtube.py:59-73`/`_transmit`, `process_publish_job.py:184`) that the thumbnail can be carried on an **optional additive `UploadMedia` field with the `IDestinationPublisher.publish` method interface unchanged** and both adapters behaviourally unaffected — replacing the previous assumed "no breaking port change" wording, and honestly recording that the `UploadMedia` value type is additively (backward-compatibly) extended; (2) added §Load-bearing invariants (thumbnail optional; identical behaviour when absent; ownership verified before publish; thumbnail immutable for the `ContentPackage` lifetime; adapters never resolve lineage; publishing never generates thumbnails; best-effort upload only after the primary publish succeeds; thumbnail failures never trigger another primary publish attempt). Recommendation unchanged — **Option A** adopted; **B rejected** (lineage traversal across frozen boundaries), **C rejected** (couples media generation into the publishing runtime). The amendment introduced **no new architectural question**. α9.3 proceeds to pre-flight; **no implementation** until the pre-flight is reviewed and approved. |
