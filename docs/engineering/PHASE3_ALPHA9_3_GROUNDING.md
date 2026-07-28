# α9.3 — Publish Thumbnail Support — Grounding

> **Status:** Read-only discovery. **Facts only** — no design, no implementation. This document
> does **not** authorise work; it establishes the ground truth for the *next* selected slice and
> surfaces whether an architectural decision (ADR) is genuinely required.
>
> **Baseline:** `v0.4.45-phase3-alpha9.2` (frozen). Only this file was created to produce it.
>
> **Selected slice (post-α9.2 re-ranking):** **Publish Thumbnail Support** — set a thumbnail on a
> published YouTube video, the visual complement to α9.1's title/description/hashtags (together they
> are the two levers of YouTube click-through). Chosen because the discovery report's entire ranked
> top-5 (§2.10, §2.1, §2.2, §2.3, §2.9) plus two runners-up (dashboard §2.8, analytics) have all
> shipped (α8.8 → α9.2), and thumbnail (§2.4) is the highest-value **internal, no-external-account**
> publishing feature remaining.

---

## 1. Objective

Give a creator a thumbnail on their published video. In discovery terms this is **§2.4 Thumbnail
generation / custom thumbnails**, restricted to what is internally buildable without external
platform onboarding.

## 2. What already exists (named facts)

| Fact | Location | Note |
|---|---|---|
| `ContentPackage.thumbnail_media_asset_id: UUID \| None` | `domain/publishing/content_package.py:39` | Field exists; `to_dict`/`from_dict` round-trip it through the `publish_jobs.content_package` JSONB. |
| `build_content_package(..., thumbnail_media_asset_id=None)` | `content_package.py:81` | Accepts the value but **defaults to `None`**. |
| Thumbnail generation engine | `interfaces/thumbnailer.py`, `infrastructure/render/ffmpeg_thumbnailer.py`, `use_cases/media/enrichers/thumbnail.py` | `IThumbnailer` + `FfmpegThumbnailer` + `ThumbnailEnricher` produce a JPEG frame; **shipped** (α8.4c). |
| Enrichment marker | `use_cases/media/enrich_generated_media.py:208` | Writes `source_metadata.enrichment.thumbnail_media_asset_id` on the **enriched parent**. |
| YouTube adapter | `infrastructure/publishing/destinations/youtube.py` | Does **`videos.insert`** (resumable upload) only. **No `thumbnails.set`.** |
| Destination boundary port | `application/interfaces/destination_publisher.py` | `IDestinationPublisher.publish(package, auth, media)`; `UploadMedia(path, mime_type, size_bytes)`; `PublishResult`. **No thumbnail channel.** |
| Publish runtime | `use_cases/publishing/process_publish_job.py` | Materialises **one** artifact (the delivery asset) to a temp dir, calls `adapter.publish(...)`. PUB-11 ambiguous-outcome handling around the single upload. |
| Create path | `use_cases/publishing/create_publish_job.py:148` | Calls `build_content_package(...)` **without** `thumbnail_media_asset_id`; the API `PublishJobCreateRequest` has no thumbnail field. |

## 3. The decisive grounding fact (source mismatch)

The contract says (`PUBLISHING_RUNTIME_CONTRACT.md` §5 / §14):
> `thumbnail_media_asset_id` — *"Reuse `source_metadata.enrichment.thumbnail_media_asset_id` when
> present (α8.4c). Custom upload deferred."* / §14: *"No custom thumbnail upload in α8.6 (reuse the
> enrichment-derived thumbnail)."*

But that reuse plan **does not reach the artifact publishing uploads**:

- Publishing consumes the **export-delivery** `MediaAsset` (PUB-1) = `export_jobs.output_media_asset_id`.
- That asset is registered by `process_export_job._register_output` (`:307`) as a **derived** asset:
  `source='generated'` **with** `source_metadata.origin='export'` + master lineage.
- `EnrichGeneratedMedia` (`:126`) **refuses derived assets** (`noop reason="derived"`) and only
  enriches **primary** generated videos.
- ∴ The delivery artifact **never** carries `enrichment.thumbnail_media_asset_id`. The enrichment
  thumbnail lives on the **primary generated source video**, upstream of Timeline → Render → Export.

So "reuse the enrichment thumbnail" is not a local field read at publish time — it requires either
tracing lineage back across the frozen render/export boundary (ADR-0043) to the primary source, or a
different thumbnail source entirely.

## 4. Missing pieces (facts)

- *Domain:* none new (the `ContentPackage` field already exists).
- *Application:* `CreatePublishJob` must obtain a thumbnail id and pass it through; `ProcessPublishJob`
  must **materialise a second artifact** (the thumbnail bytes) and hand it to the adapter.
- *Infrastructure:* `YouTubeDestination` needs a `thumbnails.set` call (a **second upload phase**).
- *Interface:* the frozen destination boundary (`IDestinationPublisher.publish` / `UploadMedia`)
  has **no way to carry a thumbnail** today.
- *API:* `PublishJobCreateRequest` has no thumbnail field (if the source is user-supplied).
- *Persistence:* **none** (JSONB field exists; no migration).
- *Testing:* thumbnail-materialisation + `thumbnails.set` (network-free `MockTransport`) + failure-mode.

## 5. Architectural decisions surfaced (this is why an ADR is genuinely required)

Three genuine decisions, at least two of which touch **frozen contracts**:

### D1 — Where does the thumbnail come from?
- **(A) User-supplied** `thumbnail_media_asset_id` (an owned `kind='image'` asset) on publish-create.
  Deterministic, no lineage tracing — **but this is exactly the "custom thumbnail upload" that §5/§14
  explicitly deferred** to "a later slice with its own contract."
- **(B) Auto-resolve the upstream enrichment thumbnail** by tracing delivery → render master → …
  → primary generated source's `enrichment.thumbnail_media_asset_id`. Honours the contract's stated
  intent but **adds new lineage reads that cross the frozen render/export boundary** (ADR-0043); no
  direct delivery→source link is stored today.
- **(C) Generate a fresh thumbnail from the delivery MP4 at publish time** (reuse `IThumbnailer`).
  **Pulls the Media-enrichment capability into the Publishing runtime** — a new cross-context
  dependency and a new determinism surface inside publish.

### D2 — The destination boundary contract (§8) changes.
Delivering thumbnail bytes to the adapter requires extending `IDestinationPublisher.publish` and/or
`UploadMedia` (frozen §8 port), and adding a `thumbnails.set` second upload to YouTube. Even a
backward-compatible extension is a **documented change to the destination boundary** the contract
froze.

### D3 — Second-upload failure semantics (new to PUB-11).
PUB-11 governs the single `videos.insert`. A thumbnail is a **second** upload **after the video is
already irreversibly live**. New ruling required: if the video succeeds but `thumbnails.set` fails, is
the job **succeeded (best-effort thumbnail)** or **failed/retried**? A retry that re-runs the whole
`publish()` would risk a **duplicate public video** — the exact hazard PUB-11 exists to prevent — so
the thumbnail phase almost certainly must be **best-effort and non-fatal**, which is a new invariant.

## 6. Migration / gate assessment

- **No migration** (the JSONB field pre-exists; no schema touched).
- Would earn its **own CI stage** (per "each new slice earns its own stage", Stages 15–20).

## 7. Conclusion — ADR REQUIRED (hard stop)

This slice is **not** purely additive: it changes the **frozen destination-boundary contract** (§8),
adds a **new publish-runtime failure mode** beyond PUB-11 (D3), and the contract **itself deferred
thumbnail upload to "a later slice with its own contract treatment."** Per the standing workflow, an
architectural decision that crosses a frozen boundary requires an **ADR** before pre-flight or
implementation.

**Recommended ADR scope (`ADR-0050 — Publish thumbnail support`):** rule D1 (thumbnail source), D2
(how the thumbnail crosses the destination boundary without weakening PUB-4/PUB-5 credential-blindness),
and D3 (best-effort, non-fatal second-upload semantics preserving PUB-11's no-duplicate guarantee),
plus the matching `PUBLISHING_RUNTIME_CONTRACT.md` §5/§14 amendment.

**Stopping here** for a decision on the ADR (and specifically on D1's source option) before any
drafting or implementation, exactly as ADR-0048 / ADR-0049 were handled.
