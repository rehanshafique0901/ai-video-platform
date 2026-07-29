# α9.6 — Second Destination Adapter — Grounding (read-only)

> **Status:** Read-only discovery grounding. **Facts only** — no implementation, no design, no
> migration, no branch/commit/PR.
> **Outcome: NO ADR is required and NO external blocker prevents a clean additive adapter** (see
> §9) → proceed automatically to pre-flight.
>
> **Baseline:** `v0.4.48-phase3-alpha9.5` (frozen, immutable). Selected from
> [`NEXT_VERTICAL_SLICES_DISCOVERY.md`](./NEXT_VERTICAL_SLICES_DISCOVERY.md) **§2.6** — this is the
> roadmap's **α8.6d′** (`PLATFORM_STATUS.md:350`). Every in-repo claim is cited to a file:line.
> Every external claim is cited to the platform's own current developer documentation
> (retrieved **2026-07-29**).

---

## 1. Selection rationale (why this slice, why now)

The publishing plane is complete end-to-end for **one** real destination: OAuth connect →
credential-blind authorize → deterministic `ContentPackage` → resumable upload → thumbnail →
notifications → scheduling → multi-destination fan-out → analytics. The discovery report names
additional destinations as *"likely the single highest product value"* remaining
(`NEXT_VERTICAL_SLICES_DISCOVERY.md:559`) and observes that for a social-video tool *"the destination
set **is** the product"* (`:220`).

α9.4 (multi-destination fan-out) shipped an orchestration layer that can currently only fan out to
**one real platform**. That slice's value is unrealised until a second destination exists. This is
the direct completion of already-built work.

---

## 2. The existing boundary (verified from source)

### 2.1 `IDestinationPublisher` — what an adapter is handed and must return

`backend/app/application/interfaces/destination_publisher.py` (133 lines):

| Element | Shape | Line |
|---|---|---|
| `IDestinationPublisher.platform` | `@property -> str` (free-text key) | `:87-90` |
| `IDestinationPublisher.publish` | `async (*, package: ContentPackage, auth: AuthorizedContext, media: UploadMedia) -> PublishResult` | `:92-105` |
| `IDestinationRegistry.for_platform` | `(platform: str) -> IDestinationPublisher` | `:111-116` |
| `IDestinationRegistry.supported_platforms` | `() -> frozenset[str]` | `:118-123` |
| `UploadMedia` | `path: str`, `mime_type: str`, `size_bytes: int`, `thumbnail: UploadThumbnail \| None` | `:54-71` |
| `UploadThumbnail` | `path: str`, `mime_type: str`, `size_bytes: int` | `:39-51` |
| `PublishResult` | `external_post_id: str` (**required**), `post_url: str \| None` | `:74-79` |
| `DestinationError` | `(message, *, retryable: bool, code: str)` | `:23-36` |

**F1 — the adapter receives a LOCAL FILE PATH, not a URL.** `UploadMedia.path` is a local artifact
path materialised by the worker (`process_publish_job.py:213-226`). The platform has **no public-URL
exposure capability** for export artifacts. Any destination requiring a publicly fetchable
`video_url` would therefore need new infrastructure outside this slice. This is the single most
decisive architectural filter (see §5).

**F2 — `external_post_id` is required at the port, but nullable in storage.**
`PublishResult.external_post_id: str` is non-optional (`:78`), yet both the domain entity
(`domain/publishing/publish_job.py:51`) and the column
(`models/publishing.py:157`, `platform_post_id: Mapped[str | None]`) are nullable. `post_url` is
already `str | None`. An adapter must return *some* stable platform identifier; it need not be a
canonical "post id".

### 2.2 Credential + OAuth abstractions

| Element | Shape | Location |
|---|---|---|
| `AuthorizedContext` | `access_token: str`, `expires_at: datetime \| None`, `scopes: tuple[str, ...]` | `interfaces/social_credential_store.py:49-60` |
| `GrantedTokens` | `access_token`, `refresh_token`, `expires_at`, `scopes` | `:34-46` |
| `ISocialOAuthClient` | `authorization_url` / `exchange_code` / `refresh` / `revoke` | `interfaces/social_oauth_client.py:39-60` |
| `OAuthGrant` | `external_account_id: str`, `display_name: str \| None`, `tokens: GrantedTokens` | `:25-36` |

**F3 — refresh-token ROTATION is already supported.** `SocialCredentialService._refresh`
(`credentials/credential_service.py:154-172`) returns the refreshed `GrantedTokens` **wholesale**
when the provider issues a new refresh token, and preserves the old one only when the provider
returns `None`. The rotated token is re-encrypted and persisted (`:96-100`). Access-token expiry is
persisted in its own column (`social_credentials.access_token_expires_at`,
`models/publishing.py:101`) and drives proactive refresh via `_needs_refresh` (`:145-148`).
*This matters because TikTok rotates refresh tokens on every refresh (§4.1).*

### 2.3 `ContentPackage` — the platform-neutral payload

`domain/publishing/content_package.py:30-40`:

```30:40:backend/app/domain/publishing/content_package.py
@dataclass(frozen=True, slots=True)
class ContentPackage:
    """Immutable, platform-agnostic publish payload (deterministic in α8.6b)."""

    media_asset_id: UUID
    title: str
    description: str
    tags: tuple[str, ...]
    visibility: Visibility
    thumbnail_media_asset_id: UUID | None
    publish_at: datetime | None
```

`Visibility` is `PUBLIC | UNLISTED | PRIVATE` (`:22-27`), defaulting to `PRIVATE` (`:100`).

**F4 — the package is genuinely platform-neutral.** The only YouTube-shaped things live *inside the
adapter*: `selfDeclaredMadeForKids` (`youtube.py:120`), the `privacyStatus`/`publishAt` field names
(`:118-125`), and the `_MAX_TITLE`/`_MAX_DESCRIPTION`/`_MAX_TAGS_TOTAL_CHARS` limits (`:47-49`). No
domain change is needed for a second destination.

**F5 — there is no separate `hashtags` field.** `tags: tuple[str, ...]` is the only tag carrier.
Platforms that express hashtags inline in the caption (TikTok, Instagram) must compose them into the
caption inside the adapter.

### 2.4 Platform-value constraints — the migration question

**F6 — `platform` is free-text everywhere; a new value needs NO migration.**

| Layer | Constraint | Location |
|---|---|---|
| Migration `0013_social_accounts` | `platform text NOT NULL` — **no CHECK, no enum** | `:49` |
| Migration `0014_publish_jobs` | `platform text NOT NULL` — **no CHECK** | `:60` |
| ORM | `Mapped[str] = mapped_column(Text, nullable=False)` | `models/publishing.py:62,151` |
| API schema | `platform: str = Field(min_length=1, max_length=64)` | `schemas/social_accounts.py:22` |
| `StartSocialConnection` | rejects if key absent from `_oauth_clients` | `start_social_connection.py:33-38` |
| `CreatePublishJob` | rejects if key absent from `supported_platforms` | `create_publish_job.py:105-109` |
| `DestinationRegistry` | permanent `unsupported_destination` at runtime | `registry.py:27-33` |

This was a deliberate ruling: **OQ2 — "`platform` stays `text`; a platform enum/catalogue is
introduced only when multiple real destinations justify it"**
(`PHASE3_ALPHA8_6a_PREFLIGHT.md:348`). Availability is gated purely by composition-root
registration.

### 2.5 Registration + fail-soft

`container.py:1774-1780` (`_get_oauth_clients`) and `:1844-1865` (`_get_destination_registry`) both
register a platform **only when its credentials are configured**. Unconfigured ⇒ absent from
`supported_platforms()` ⇒ a create for that platform fails **create-time validation**
(`ValidationFailedError`, 422, `details={"platform": ...}`), never a boot failure
(`config.py:342-348`). A new destination follows this template exactly.

### 2.6 Worker semantics an adapter inherits

| Property | Value | Location |
|---|---|---|
| Lease | **900 s** | `process_publish_job.py:60` |
| Backoff | `min(3600, 30 * 2**(attempt-1))` s | `:313-316` |
| Max attempts | 5 | `create_publish_job.py:54` |
| Retry driver | `DestinationError.retryable` + `attempt < max_attempts` | `:270-296` |
| Idempotency backstop | partial unique index on `(source_media_asset_id, social_account_id)` where `status IN ('queued','running','succeeded')` | `models/publishing.py:166-173` |

**F7 — PUB-11 is the established ambiguity template.** Once bytes are transmitted, any uncertain
outcome is classified **permanent** (`ambiguous_upload_outcome`), never retried, to avoid
double-posting (`youtube.py:18-22, 191-207, 276-292`).

### 2.7 Determinism guarantees

**F8 — the offline-test pattern is proven and reusable.** YouTube unit tests inject
`httpx.MockTransport` (`tests/unit/infrastructure/publishing/destinations/test_youtube_destination.py:36-40`);
integration tests use the Mock destination only (`supported_platforms={"mock"}`,
`test_publish_runtime_end_to_end.py:285`); the real-network test is marked `live_smoke`, env-gated,
and **never selected by CI** (`tests/live/test_youtube_live_smoke.py:1-36`; markers at
`pyproject.toml:206-210`). Highest existing CI stage: **23** (`ci_gate.py:800-812`).

### 2.8 Import-linter guard already in force

`pyproject.toml:386-404` — *"Destination adapters are credential-blind leaves"*: 
`app.infrastructure.publishing.destinations` may not import credentials, repositories, UoW, use
cases, or other bounded contexts. `:369-372` additionally forbids `cryptography` in destinations and
OAuth clients. A new adapter inherits both contracts with **no contract change**.

---

## 3. Candidate set

Destinations **already anticipated by the repository**: TikTok, Instagram, Facebook, X
(`NEXT_VERTICAL_SLICES_DISCOVERY.md:218`; `PUBLISHING_RUNTIME_CONTRACT.md:250,398`;
`AI_RUNTIME_PLANES.md:102`; `ADR-0047:14,29`; `SYSTEM_MAP.md:114`). LinkedIn and Vimeo are **not**
anticipated anywhere in the repository and are excluded as out-of-roadmap.

---

## 4. Per-candidate verification (external, retrieved 2026-07-29)

### 4.1 TikTok — Content Posting API (Direct Post)

| Dimension | Fact | Source |
|---|---|---|
| **Upload API** | `POST /v2/post/publish/video/init/` → `{publish_id, upload_url}` | [Direct Post ref](https://developers.tiktok.com/doc/content-posting-api-reference-direct-post) |
| **Local-file upload** | **Yes** — `source=FILE_UPLOAD`, `PUT` to `upload_url` | ibid. |
| **Public URL needed** | **No** (`PULL_FROM_URL` is the *alternative*, and requires domain-ownership verification — `url_ownership_unverified`) | ibid. |
| **Resumable/chunked** | Sequential `PUT` chunks; `Content-Range: bytes {first}-{last}/{total}`; chunks 5 MB–64 MB (final ≤128 MB); 1–1000 chunks; `206` per chunk, `201` on last | [Media transfer guide](https://developers.tiktok.com/doc/content-posting-api-media-transfer-guide) |
| **Upload URL TTL** | 1 hour | Direct Post ref |
| **Caption** | `post_info.title`, max **2200 UTF-16 runes**; hashtags/mentions matched **inline** | ibid. |
| **Privacy** | `PUBLIC_TO_EVERYONE` / `MUTUAL_FOLLOW_FRIENDS` / `FOLLOWER_OF_CREATOR` / `SELF_ONLY`; **must match** options from `/v2/post/publish/creator_info/query/` else `privacy_level_option_mismatch` | ibid. |
| **Custom thumbnail** | **Not supported** — cover chosen by `video_cover_timestamp_ms` (a frame offset) only | ibid. |
| **Scheduling** | **Not supported** — no scheduled-publish field on Direct Post | ibid. |
| **Completion model** | **Asynchronous.** Poll `POST /v2/post/publish/status/fetch/` (30 req/min/token) → `PROCESSING_UPLOAD` / `PROCESSING_DOWNLOAD` / `SEND_TO_USER_INBOX` / `PUBLISH_COMPLETE` / `FAILED` | [Get Post Status](https://developers.tiktok.com/doc/content-posting-api-reference-get-video-status) |
| **Post id availability** | `publicaly_available_post_id` is returned **only** if the post is public **and** moderation-approved. Processing: <30 s (512 MB), ~1 min (1 GB), >2 min (4 GB); moderation usually <1 min but **occasionally hours** | ibid. |
| **Webhooks** | **Optional, not required** — `post.publish.complete`, `post.publish.publicly_available`, etc. Polling is a fully supported first-class alternative | ibid. |
| **Failure taxonomy** | Explicit `fail_reason` table cleanly splits retryable (`internal`) from permanent (`auth_removed`, `spam_risk_*`, `*_check_failed`) | ibid. |
| **Scopes** | `video.publish` (Direct Post); `video.upload` (draft) | Direct Post ref |
| **Token model** | access **24 h**; refresh **365 d**; **refresh token ROTATES** — "You must use the newly-returned token if the value is different" | [Token management](https://developers.tiktok.com/doc/oauth-user-access-token-management) |
| **Sandbox** | **Yes** — up to 5 sandboxes/app, 10 target users, full OAuth + posting flow, **no review required**. Caveat: "Sandbox mode does not offer access to Content Posting API for public videos" | [Sandbox](https://developers.tiktok.com/doc/add-a-sandbox/) |
| **Approval gating** | Unaudited: `SELF_ONLY` only, **5 users/24 h**, posting accounts must be private. Audited unlocks public. Both: ~15 posts/day/creator | [Content sharing guidelines](https://developers.tiktok.com/doc/content-sharing-guidelines) |
| **Approval risk note** | Audit guidance explicitly rejects internal-only tools: *"Not acceptable: A utility tool to help upload contents to the account(s) you or your team manages."* | ibid. |
| **Rate limit** | init: 6 req/min per user token | Direct Post ref |

### 4.2 Instagram Reels — Instagram Platform content publishing

| Dimension | Fact | Source |
|---|---|---|
| **Upload API** | Container model: `POST /<IG_USER_ID>/media` (`media_type=REELS`) → poll `GET /<CONTAINER_ID>?fields=status_code` until `FINISHED` → `POST /<IG_USER_ID>/media_publish` | [Content publishing](https://developers.facebook.com/docs/instagram-platform/content-publishing) |
| **Local-file upload** | **Conditional.** Standard flow requires `video_url` — *"We will cURL your image using the passed in URL so it must be on a public server."* Binary upload exists via `upload_type=resumable` → `POST https://rupload.facebook.com/ig-api-upload/<VER>/<CONTAINER_ID>`, but is documented as **"available only for apps that have implemented Facebook Login for Business"** | [Resumable uploads](https://developers.facebook.com/docs/instagram-platform/content-publishing/resumable-uploads/) |
| **Public URL needed** | **Yes, unless** Facebook Login for Business is adopted | ibid. |
| **Caption** | `caption` parameter | Content publishing |
| **Privacy** | **No per-post visibility control** — Reels publish publicly to a professional account | ibid. |
| **Custom thumbnail** | `cover_url` (**public URL**) or `thumb_offset` (frame offset) | [IG User Media](https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-user/media/) |
| **Scheduling** | **Not supported** natively | Content publishing |
| **Completion model** | **Asynchronous** — container status polling required before publish | ibid. |
| **Account prerequisite** | Instagram **professional** (Business/Creator) account | [Overview](https://developers.facebook.com/docs/instagram-platform/overview) |
| **Token model** | short-lived 1 h → long-lived **60 d**, refreshed via `/refresh_access_token` (token must be ≥24 h old, unexpired) | [Business Login](https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/business-login/) |
| **Approval gating** | Serving accounts you don't own ⇒ **Advanced Access** ⇒ **App Review + Business Verification**, incl. a screencast and *"a live, accessible test environment where reviewers can log in"* | [App Review](https://developers.facebook.com/docs/instagram-platform/app-review/) |
| **Sandbox** | No true sandbox; **Standard Access** works only for accounts with a developer/tester role on the app | ibid. |
| **Rate limit** | `content_publishing_limit` endpoint; documented caps in the 25–50 posts/24 h range | Content publishing |

### 4.3 Facebook Reels — Video API (Pages)

| Dimension | Fact | Source |
|---|---|---|
| **Upload API** | `POST /<PAGE_ID>/video_reels` with `upload_phase=start` → `POST /video-upload/<VIDEO_ID>` (`application/octet-stream`) → `upload_phase=finish` | [Reels publishing](https://developers.facebook.com/docs/video-api/guides/reels-publishing/) |
| **Local-file upload** | **Yes** — binary octet-stream with byte offsets | ibid. |
| **Resumable** | **Yes** — resume from `upload_phase.bytes_transfered` as `offset` | ibid. |
| **Scheduling** | **Yes** — `video_state` ∈ `DRAFT` / `SCHEDULED` / `PUBLISHED` | ibid. |
| **Identity model** | Binds to a **Facebook Page**, not a user; needs a **Page access token** from a user with the `CREATE_CONTENT` task | ibid. |
| **Scopes** | `pages_show_list`, `pages_read_engagement`, `pages_manage_posts` | ibid. |
| **Extra behaviour** | Automatic copyright check at upload | ibid. |
| **Approval gating** | Meta App Review + Business Verification (same regime as §4.2) | App Review |
| **Product value** | Lower — Facebook Pages are a weaker channel for short-form creators than TikTok/IG | — |

### 4.4 X (Twitter)

| Dimension | Fact | Source |
|---|---|---|
| **Pricing** | Free tier **discontinued**; legacy Basic ($200/mo) and Pro ($5,000/mo) **closed to new signups** since Feb 2026; new developers are on **pay-per-use** (~$0.015/post, $0.20 for link posts) | [postproxy](https://postproxy.dev/blog/x-api-pricing-2026/), [twitterapi.io](https://twitterapi.io/blog/x-api-cost-breakdown-2026) |
| **Media upload** | `POST /2/media/upload` (INIT/APPEND/FINALIZE); v2 media upload still described as *rolling out*, with v1.1 the historical path | [X media upload](https://docs.x.com/x-api/media/media-upload) |
| **Video suitability** | ≤512 MB; short-form video is not X's primary format | ibid. |
| **Assessment** | **Recurring monetary cost + API-surface churn**; lowest product value per unit of risk | — |

---

## 5. Cross-candidate comparison against the frozen boundary

| Requirement (from §2) | TikTok | Instagram Reels | Facebook Reels | X |
|---|---|---|---|---|
| Accepts **local file** (`UploadMedia.path`) — F1 | ✅ `FILE_UPLOAD` | ⚠️ only via FB Login for Business | ✅ octet-stream | ✅ APPEND |
| Needs **public URL infrastructure** | ❌ no | ⚠️ yes (default path) | ❌ no | ❌ no |
| Fits **credential-blind bearer** (`AuthorizedContext`) | ✅ | ✅ | ⚠️ Page token indirection | ✅ |
| **Refresh** compatible with store (F3) | ✅ rotation supported | ✅ | ✅ | ✅ |
| `title`/`description` mappable | ✅ caption | ✅ caption | ✅ title+description | ✅ |
| `tags` mappable | ✅ inline | ✅ inline | ✅ inline | ✅ |
| `visibility` mappable | ⚠️ dynamic per creator | ❌ no per-post control | ⚠️ partial | ⚠️ partial |
| `publish_at` mappable | ❌ | ❌ | ✅ `SCHEDULED` | ❌ |
| `thumbnail` mappable (α9.3) | ❌ timestamp only | ⚠️ public `cover_url` | ⚠️ | ❌ |
| Synchronous post id | ❌ async + moderation | ❌ async container | ⚠️ async | ✅ |
| **Webhooks required** | ❌ polling supported | ❌ polling supported | ❌ | ❌ |
| **Sandbox without approval** | ✅ 5 sandboxes/10 users | ❌ role-based only | ❌ | ❌ |
| **Deterministic offline test** (F8) | ✅ | ✅ | ✅ | ✅ |
| **Migration required** (F6) | ❌ none | ❌ none | ❌ none | ❌ none |

---

## 6. Ranking

Scored on the five requested axes (5 = best).

| Candidate | 1. Product value | 2. Feasibility | 3. Approval risk (inverted) | 4. Architectural fit | 5. Deterministic testability | **Total** |
|---|---|---|---|---|---|---|
| **TikTok** | **5** | **5** | **3** | **5** | **5** | **23** |
| Instagram Reels | 5 | 2 | 2 | 2 | 5 | 16 |
| Facebook Reels | 2 | 4 | 2 | 3 | 5 | 16 |
| X | 2 | 3 | 4 | 4 | 5 | 18 |

**Ranked: TikTok (1) › X (2) › Instagram Reels = Facebook Reels (3=).**

X's total is inflated by low *approval* risk while carrying **recurring monetary cost** and the
lowest product value; it is not a serious first choice for a short-form video tool.

**TikTok is the recommended second destination.** The decisive, non-subjective reasons:

1. **It is the only high-product-value candidate that accepts a local file with no public-URL
   infrastructure** (F1). Instagram's default path requires the platform to expose export artifacts
   at public URLs — new infrastructure and a new security surface, outside an additive slice.
2. **It is the only candidate with a real sandbox** that exercises the full OAuth + upload flow with
   **no app review** — so the adapter can be manually smoke-verified pre-approval.
3. **Its credential semantics already match the store**, including refresh-token rotation (F3).
4. **Its failure taxonomy maps directly** onto `DestinationError(retryable=...)`.

---

## 7. Tensions found, and why each resolves inside the existing boundary

| # | Tension | Resolution within the frozen boundary | New architecture? |
|---|---|---|---|
| **T1** | TikTok publish is **asynchronous**; the port is synchronous | Poll `/status/fetch/` **inside** `publish()` under a bounded budget. The worker lease is **900 s** (§2.6) and YouTube already performs multi-step I/O (initiate → transmit → thumbnail). Exhausting the budget after bytes were sent is exactly **PUB-11** (F7) ⇒ permanent `ambiguous_upload_outcome`. | **No** |
| **T2** | `post_id` is returned **only** for public, moderation-approved posts — so `PublishResult.external_post_id` (required, F2) may be unobtainable | Return the durable **`publish_id`** (TikTok's own identifier for the publish action, ≤64 chars) as `external_post_id`, with `post_url=None` (already `str \| None`). Storage is nullable (F2). Contract-legal. | **No** — but a **pre-flight ruling** is required (see §8, Q1) |
| **T3** | `publish_at` (α8.9b) has **no TikTok equivalent** | Adapter-local permanent `DestinationError(code="invalid_metadata")`, mirroring YouTube's existing metadata validation (`youtube.py:96-112`). Silently ignoring a creator's schedule is the alternative. | **No** — **pre-flight ruling** (Q2) |
| **T4** | `thumbnail_media_asset_id` (α9.3) is **unsupported** — TikTok covers are frame timestamps | ADR-0050 already declares thumbnails **optional, best-effort, non-fatal**; `MockDestination` already accepts-and-ignores (`mock_destination.py:63-69`). Adapter ignores it. | **No** — already covered by ADR-0050 |
| **T5** | `visibility` must match the **creator's dynamic** `privacy_level_options`; `UNLISTED` has no equivalent | Query `/creator_info/query/` first (TikTok **mandates** this), then map with an adapter-local table; unmappable ⇒ permanent `invalid_metadata`. | **No** — **pre-flight ruling** (Q3) |
| **T6** | Two destinations may justify a **capability catalogue** | `PUBLISHING_RUNTIME_CONTRACT.md` §14 defers a YAML catalogue *until* ≥2 real destinations — i.e. it becomes *eligible*, not *required*. T3/T5 are satisfiable adapter-locally today. | **No** — explicitly deferred again |
| **T7** | Unaudited clients are limited to `SELF_ONLY` + 5 users/24 h | External/operational only. Fail-soft registration (§2.5) means an unconfigured TikTok is simply absent from `supported_platforms()`. | **No** |

**Accepted limitation (to be recorded, mirroring the α9.2 precedent):** TikTok's `publish_id` is a
durable handle that *could* resolve an ambiguous outcome later, but persisting it would require a
new column. Under the no-migration constraint, α9.6 accepts the same PUB-11 ambiguity already
accepted for YouTube — with a **wider window**, because TikTok moderation can occasionally take
hours. A future migration can add a durable handle column with no external behaviour change.

---

## 8. Questions for the pre-flight to rule on

- **Q1.** Confirm `external_post_id := publish_id` when no public post id is available, with
  `post_url=None`. (Recommended: yes — contract-legal, honest, storage already nullable.)
- **Q2.** `publish_at` on TikTok: **reject at the adapter as permanent `invalid_metadata`** vs.
  silently ignore. (Recommended: reject — never silently violate a creator's stated intent.)
- **Q3.** `Visibility` → `privacy_level` mapping table, including the `UNLISTED` gap and the
  mandatory `creator_info/query` pre-call.
- **Q4.** Inline status-poll budget (bounded, ≪ the 900 s lease) and its interaction with PUB-11.
- **Q5.** Chunking policy (5 MB–64 MB, ≤1000 chunks, final ≤128 MB) and single-shot for <5 MB files.
- **Q6.** New CI stage number (**24**) for TikTok destination tests.

---

## 9. Verdict

**No ADR is required.** Every tension (T1–T7) resolves inside the existing port, using patterns
already proven by the YouTube adapter (multi-step I/O, PUB-11 ambiguity, adapter-local metadata
validation, fail-soft registration). No port, schema, contract, domain, or runtime change is needed.

**No external-platform blocker prevents a clean additive adapter.** TikTok accepts a local-file
chunked upload with no public-URL infrastructure, offers a review-free sandbox, and its token
semantics already match the credential store. App audit gates *public visibility in production* —
it does not gate implementation, CI, or deterministic testing.

**Genuinely required for α9.6:** two new infrastructure leaves (`TikTokDestination`,
`TikTokOAuthClient`), a `tiktok_*` settings block, two composition-root registrations, offline unit
tests via `MockTransport`, an opt-in `live_smoke` test, and one new CI stage.

→ **Proceeding automatically to the α9.6 pre-flight.**
