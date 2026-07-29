# α9.6 — Second Destination Adapter (TikTok) — Pre-flight (design blueprint)

> **Status:** Design blueprint. **Not started** — no implementation, no migration, no branch/commit/PR.
> **Awaiting approval before implementation.**
>
> **Baseline:** `v0.4.48-phase3-alpha9.5` (frozen). **Version under development:**
> `0.4.49-phase3-alpha9.6-dev`. **Roadmap row:** α8.6d′ (`PLATFORM_STATUS.md:350`).
> **Grounding:** [`PHASE3_ALPHA9_6_GROUNDING.md`](./PHASE3_ALPHA9_6_GROUNDING.md) — verdict:
> **no ADR required, no external blocker**.
> **Destination selected:** **TikTok** (ranked 1st of 4 on all five axes — grounding §6).

---

## 1. Scope

**Is:** a second real destination behind the already-proven `IDestinationPublisher` seam —
`TikTokDestination` (Content Posting API, Direct Post, `FILE_UPLOAD`) + `TikTokOAuthClient`
(v2 OAuth, rotating refresh tokens) + a `tiktok_*` settings block + two fail-soft composition-root
registrations + network-free tests + one new CI stage.

**Is not:** a platform enum or capability catalogue (OQ2 / contract §14 — deferred again, grounding
T6); any port, DTO, domain, schema, contract, or runtime change; a migration; a public-URL
exposure capability; Instagram / Facebook / X; a UI; webhook ingestion; changes to
`ProcessPublishJob`, `CreatePublishJob(s)`, notifications, analytics, or the AI plane.

---

## 2. Decisive grounding facts (cited)

- **G1** `platform` is free-text with **no CHECK and no enum** — a new value needs **no migration**
  (grounding F6; `0013_social_accounts.py:49`, `0014_publish_jobs.py:60`).
- **G2** The adapter is handed a **local file path** (`UploadMedia.path`); TikTok `FILE_UPLOAD`
  accepts exactly that — **no public URL needed** (grounding F1, §4.1).
- **G3** `PublishResult.external_post_id` is **required** at the port, but `platform_post_id` is
  **nullable** in domain and DB; `post_url` is already `str | None` (grounding F2).
- **G4** The credential store already **persists rotated refresh tokens** and access-token expiry
  (grounding F3; `credential_service.py:154-172`, `:96-100`).
- **G5** Worker lease is **900 s**; retry is driven solely by `DestinationError.retryable`;
  **PUB-11** classifies post-transmission uncertainty as permanent (grounding §2.6, F7).
- **G6** Registration is **fail-soft**: unconfigured ⇒ absent from `supported_platforms()` ⇒
  create-time 422, never a boot failure (grounding §2.5).
- **G7** Offline determinism via `httpx.MockTransport`; real-network checks live behind the
  env-gated `live_smoke` marker, never selected by CI (grounding F8).

---

## 3. The adapter — `TikTokDestination`

**Location:** `backend/app/infrastructure/publishing/destinations/tiktok.py`
**Implements:** `IDestinationPublisher`; `platform` property returns `"tiktok"`.
**Constructor (configuration-blind, W8.1.1):**

```python
def __init__(
    self,
    *,
    http: httpx.AsyncClient,
    api_base_url: str,
    status_poll_interval_seconds: float,
    status_poll_budget_seconds: float,
    chunk_size_bytes: int,
) -> None: ...
```

Mirrors `YouTubeDestination.__init__` (`youtube.py:55-57`) — an injected `httpx.AsyncClient` plus
resolved settings. The leaf never reads `Settings`.

### 3.1 `publish()` sequence

| Phase | Call | Failure classification |
|---|---|---|
| 0. Guards | non-empty bearer; `size_bytes > 0` | `missing_bearer` / `empty_media`, permanent |
| 1. Creator query | `POST /v2/post/publish/creator_info/query/` | pre-upload ⇒ `tiktok_transient` (retryable) / `unauthorized` / `forbidden` |
| 2. Metadata build | caption + privacy mapping (§3.2, §3.3) | `invalid_metadata`, permanent |
| 3. Init | `POST /v2/post/publish/video/init/` → `{publish_id, upload_url}` | pre-upload ⇒ retryable; audit/spam codes ⇒ permanent (§3.5) |
| 4. Transmit | sequential `PUT` chunks to `upload_url` (§3.4) | **PUB-11 boundary** — see below |
| 5. Poll | `POST /v2/post/publish/status/fetch/` until terminal or budget exhausted (§3.6) | per `fail_reason` (§3.5) |
| 6. Result | `PublishResult` (§3.7) | — |

**PUB-11 boundary (G5).** Identical in spirit to `youtube.py:191-207`. Before the first byte of
phase 4 is sent, transport/5xx/429 failures are **retryable**. From the first transmitted byte
onward, any non-deterministic failure (transport error, unexpected status, poll budget exhausted)
is **permanent** `ambiguous_upload_outcome` — never retried, because TikTok may still complete the
post and a retry would double-post.

### 3.2 `ContentPackage` → TikTok mapping

| `ContentPackage` | TikTok field | Rule |
|---|---|---|
| `title` + `tags` | `post_info.title` (caption) | Compose: `title`, then tags rendered inline as `#tag` (TikTok matches hashtags inline — grounding §4.1, F5). Cap at **2200 UTF-16 runes**; over-limit ⇒ permanent `invalid_metadata`. |
| `description` | — | **Not mapped.** TikTok has a single caption field. Documented, not an error. |
| `visibility` | `post_info.privacy_level` | Dynamic mapping (§3.3) |
| `publish_at` | — | **Rejected** (Q2 ruling below) |
| `thumbnail_media_asset_id` / `media.thumbnail` | — | **Ignored** (best-effort per ADR-0050; grounding T4) |
| `media.path` / `size_bytes` | `source_info` | `source=FILE_UPLOAD`, `video_size`, `chunk_size`, `total_chunk_count` |

Fixed fields: `disable_duet`/`disable_stitch`/`disable_comment` omitted (TikTok defaults honour the
creator's own settings); `is_aigc` **not** set in v1 (the platform does not currently track whether
an export is AI-generated end-to-end — deferred, noted as a known gap).

### 3.3 Visibility mapping (**Q3 ruling**)

TikTok **mandates** that `privacy_level` be one of the creator's `privacy_level_options` from
`/creator_info/query/`, else `privacy_level_option_mismatch` (grounding §4.1). Therefore mapping is
a *negotiation*, not a constant:

| `Visibility` | Preference order (first available option wins) |
|---|---|
| `PUBLIC` | `PUBLIC_TO_EVERYONE` → *(no fallback)* |
| `PRIVATE` | `SELF_ONLY` → *(no fallback)* |
| `UNLISTED` | **no equivalent** → permanent `invalid_metadata` |

If the preferred value is absent from the creator's options (e.g. `PUBLIC` requested but the client
is unaudited, or the creator's account is private), fail **permanently** with
`visibility_unavailable` and a message naming the options actually offered. Never silently downgrade
a creator's requested visibility.

### 3.4 Chunking policy (**Q5 ruling**)

Per the TikTok media-transfer guide (grounding §4.1):

- `size < 5 MB` ⇒ single chunk, `chunk_size = size`, `total_chunk_count = 1`.
- otherwise ⇒ `chunk_size = min(configured, 64 MB)`, `total_chunk_count = floor(size / chunk_size)`
  (TikTok specifies **floor**), so the **final chunk carries the trailing bytes** (≤128 MB).
- Reject `total_chunk_count > 1000` as permanent `invalid_metadata`.
- Each `PUT` sends `Content-Type` (from `media.mime_type`), `Content-Length` (this chunk),
  `Content-Range: bytes {first}-{last}/{total}`. Expect `206` for non-final chunks, `201` for the
  last. Chunks are uploaded **sequentially** (TikTok requires it).
- Streamed from disk chunk-by-chunk — never load the whole artifact into memory.

### 3.5 Error classification → `DestinationError`

| Source | Condition | `code` | `retryable` |
|---|---|---|---|
| guard | empty bearer | `missing_bearer` | ❌ |
| guard | `size_bytes <= 0` | `empty_media` | ❌ |
| init/query | HTTP 401 / `access_token_invalid` / `scope_not_authorized` | `unauthorized` | ❌ |
| init | `unaudited_client_can_only_post_to_private_accounts` | `unaudited_client` | ❌ |
| init | `spam_risk_too_many_posts` / `spam_risk_user_banned_from_posting` | `spam_risk` | ❌ |
| init | `reached_active_user_cap` | `quota_exceeded` | ✅ |
| init | `privacy_level_option_mismatch` | `invalid_metadata` | ❌ |
| init/query | transport error, 429, 5xx **before** transmission | `tiktok_transient` | ✅ |
| transmit | any transport/HTTP anomaly **after** first byte | `ambiguous_upload_outcome` | ❌ |
| poll | `fail_reason = internal` | `tiktok_transient` | ✅ (see note) |
| poll | `fail_reason` ∈ `*_check_failed` | `invalid_media` | ❌ |
| poll | `fail_reason` ∈ `auth_removed`, `spam_risk*` | `spam_risk` / `auth_removed` | ❌ |
| poll | budget exhausted, still processing | `ambiguous_upload_outcome` | ❌ |

**Note on `internal`.** TikTok documents `internal` as retryable, but it surfaces *after* bytes were
transmitted. PUB-11 (G5) takes precedence over the platform's advice: mark it
**`ambiguous_upload_outcome`, permanent**. Rationale — a retry would re-upload and risk a duplicate
post; the platform's own guidance cannot override our double-post invariant. This is an explicit,
deliberate divergence and must be commented in the source.

### 3.6 Status polling (**Q4 ruling**)

- Poll `POST /v2/post/publish/status/fetch/` every `status_poll_interval_seconds` (default **3 s**)
  up to `status_poll_budget_seconds` (default **120 s**).
- Budget is bounded far below the **900 s** worker lease (G5), leaving ample headroom for the
  upload itself; the injected `httpx` timeout bounds each individual call.
- Terminal-success: `PUBLISH_COMPLETE`. Terminal-failure: `FAILED` (classified by `fail_reason`).
- `PROCESSING_UPLOAD` / `PROCESSING_DOWNLOAD` / `SEND_TO_USER_INBOX` ⇒ keep polling.
- Budget exhausted ⇒ permanent `ambiguous_upload_outcome` (PUB-11).
- TikTok caps status polling at 30 req/min/token; a 3 s interval stays within it.

### 3.7 The returned `PublishResult` (**Q1 ruling**)

```python
PublishResult(
    external_post_id=publish_id,          # always available, ≤64 chars, durable
    post_url=None,                        # TikTok exposes no canonical URL at publish time
)
```

`publicaly_available_post_id` is returned **only** for public, moderation-approved posts (grounding
§4.1) — so it cannot be relied on. `publish_id` is TikTok's own durable identifier for the publish
action, satisfies the required `str` (G3), and is the correct honest value. When
`publicaly_available_post_id` **is** present on the terminal poll, prefer its first element as
`external_post_id` and set `post_url = https://www.tiktok.com/@<open_id>/video/<post_id>`; otherwise
fall back to `publish_id` with `post_url=None`.

---

## 4. The OAuth client — `TikTokOAuthClient`

**Location:** `backend/app/infrastructure/publishing/oauth/tiktok_oauth_client.py`
**Implements:** `ISocialOAuthClient` (all four methods).

| Method | Behaviour |
|---|---|
| `authorization_url` | `GET {authorize_url}?client_key=…&scope=…&response_type=code&redirect_uri=…&state=…`. Note TikTok uses **`client_key`**, not `client_id`. |
| `exchange_code` | `POST {token_url}` **form-encoded** (`client_key`, `client_secret`, `code`, `grant_type=authorization_code`, `redirect_uri`). Response carries `open_id`, `scope`, `access_token`, `expires_in`, `refresh_token`, `refresh_expires_in`. ⇒ `OAuthGrant(external_account_id=open_id, display_name=<from user info, else None>, tokens=…)`. Missing `refresh_token` ⇒ `OAuthExchangeError` (mirrors `youtube_oauth_client.py:84-86`). |
| `refresh` | `POST {token_url}` with `grant_type=refresh_token`. **Always** returns the newly issued `refresh_token` in `GrantedTokens` — rotation is mandatory for TikTok and the store already persists it (G4). |
| `revoke` | Best-effort `POST {revoke_url}`; swallow transport errors (mirrors `youtube_oauth_client.py:104-111`). |

`expires_at` is computed as `clock.now() + expires_in` (24 h), which drives the store's proactive
refresh (`_needs_refresh`). Display name is resolved via `GET /v2/user/info/?fields=display_name`
when `user.info.basic` was granted; any failure ⇒ `display_name=None` (never fatal).

**Scopes (default):** `user.info.basic`, `video.publish`.

---

## 5. Configuration — `tiktok_*` block

Added to `app/core/config.py`, mirroring the YouTube block (`config.py:342-388`) verbatim in shape,
including the fail-soft comment:

| Setting | Type | Default |
|---|---|---|
| `tiktok_oauth_client_key` | `str \| None` | `None` |
| `tiktok_oauth_client_secret` | `SecretStr \| None` | `None` |
| `tiktok_oauth_scopes` | `tuple[str, ...]` | `("user.info.basic", "video.publish")` |
| `tiktok_oauth_authorize_url` | `str` | `https://www.tiktok.com/v2/auth/authorize/` |
| `tiktok_oauth_token_url` | `str` | `https://open.tiktokapis.com/v2/oauth/token/` |
| `tiktok_oauth_revoke_url` | `str` | `https://open.tiktokapis.com/v2/oauth/revoke/` |
| `tiktok_api_base_url` | `str` | `https://open.tiktokapis.com` |
| `tiktok_timeout_seconds` | `float` | `120.0` |
| `tiktok_chunk_size_bytes` | `int` | `10_000_000` |
| `tiktok_status_poll_interval_seconds` | `float` | `3.0` |
| `tiktok_status_poll_budget_seconds` | `float` | `120.0` |

Endpoints are overridable so the opt-in live smoke test can target a sandbox.

---

## 6. Composition-root wiring (fail-soft, G6)

Three additions to `app/core/container.py`, each mirroring its YouTube counterpart:

1. `_get_tiktok_http_client()` — memoised `httpx.AsyncClient(timeout=tiktok_timeout_seconds)`;
   closed in `shutdown()`, cleared in `reset()`.
2. `_build_tiktok_oauth_client() -> TikTokOAuthClient | None` — `None` unless **both** client key
   and secret are set; registered into `_get_oauth_clients()` under `"tiktok"` (`:1774-1780`).
3. `_get_destination_registry()` (`:1844-1865`) — adds `adapters["tiktok"] = TikTokDestination(...)`
   under the same both-set condition.

Unconfigured ⇒ `"tiktok"` absent from `supported_platforms()` ⇒ `CreatePublishJob` raises
`ValidationFailedError("no destination adapter is registered for this platform")` at create time
(`create_publish_job.py:105-109`). **CI runs entirely unconfigured**, so CI behaviour is unchanged
and deterministic.

---

## 7. Migration assessment → **NO migration required**

| Candidate change | Needed? | Why |
|---|---|---|
| New `platform` value `"tiktok"` | ❌ | `text`, no CHECK, no enum (G1) |
| Store `publish_id` durably | ❌ | Deferred — accepted limitation (grounding §7) |
| `platform_post_id` nullability | ❌ | Already nullable (G3) |
| Credential storage for rotating refresh tokens | ❌ | Already supported (G4) |
| Webhook ingestion tables | ❌ | Polling is a first-class alternative (grounding §4.1) |

**Accepted limitation (to be recorded in `CHANGELOG.md` and the docs-sync, mirroring the α9.2
precedent).** TikTok's ambiguity window is **wider** than YouTube's: moderation can occasionally
take hours, so a job whose poll budget expires is marked `failed` even though the post may later go
live. This is the deliberate PUB-11 trade-off (never double-post). `publish_id` is retained in the
job's `platform_post_id` on success only; resolving late outcomes would require a durable-handle
column and a reconciliation worker — a future, behaviour-preserving migration.

---

## 8. Ownership boundaries + import-linter

No contract changes. The new modules land **inside** existing guarded packages and inherit both
contracts unchanged (grounding §2.8):

- `app.infrastructure.publishing.destinations` — forbidden from credentials, repositories, UoW, use
  cases, other bounded contexts (`pyproject.toml:386-404`).
- `app.infrastructure.publishing.oauth` + `.destinations` — forbidden from `cryptography`
  (`:369-372`).

`TikTokDestination` imports only: `httpx`, `structlog`, the destination + credential-store **ports**
(for `AuthorizedContext`), and `app.domain.publishing.content_package`. It never touches the
credential store, the account aggregate, or persistence.

---

## 9. Architectural-decision check → **none**

Re-verified against the grounding's T1–T7: every tension resolves with an existing pattern
(multi-step adapter I/O, PUB-11, adapter-local `invalid_metadata`, ADR-0050 best-effort thumbnails,
fail-soft registration, OQ2 free-text platform). **No ADR is required for α9.6.** No frozen boundary
is crossed. If implementation uncovers a genuine contradiction not covered here, **stop and report**.

---

## 10. Test strategy + CI Stage 24

**Unit (Stage 4, `-m unit`, network-free via `httpx.MockTransport`):**

- `tests/unit/infrastructure/publishing/destinations/test_tiktok_destination.py`
  - happy path: creator query → init → single-chunk `PUT` → poll `PUBLISH_COMPLETE` → `PublishResult`
  - multi-chunk sequencing: exact `Content-Range` headers, floor-based `total_chunk_count`, trailing
    bytes on the final chunk, `206`…`201`
  - `<5 MB` single-shot path
  - caption composition (title + inline `#tags`), 2200-rune over-limit ⇒ `invalid_metadata`
  - visibility: `PUBLIC`→`PUBLIC_TO_EVERYONE`; `PRIVATE`→`SELF_ONLY`; `UNLISTED` ⇒ `invalid_metadata`;
    option-absent ⇒ `visibility_unavailable`
  - `publish_at` set ⇒ permanent `invalid_metadata`
  - thumbnail present ⇒ **ignored**, publish still succeeds
  - error matrix (§3.5), including `unaudited_client`, `spam_risk`, `unauthorized`
  - **PUB-11**: transport error after first byte ⇒ `ambiguous_upload_outcome`, `retryable=False`
  - poll budget exhausted ⇒ `ambiguous_upload_outcome`
  - poll `fail_reason=internal` ⇒ permanent (deliberate divergence)
  - `publicaly_available_post_id` present ⇒ post id + URL; absent ⇒ `publish_id` + `post_url=None`
- `tests/unit/infrastructure/publishing/test_tiktok_oauth_client.py`
  - authorize URL shape (`client_key`, state, redirect, scopes)
  - `exchange_code` → `OAuthGrant(external_account_id=open_id, …)`; missing refresh token ⇒
    `OAuthExchangeError`
  - **refresh returns the ROTATED refresh token** (the α9.6 correctness lynchpin, G4)
  - `revoke` swallows transport errors
- `tests/unit/core/test_container_tiktok_wiring.py` — unconfigured ⇒ `"tiktok"` absent from
  `supported_platforms()`; configured ⇒ present.

**Integration (new Stage 24, `-m integration`, real PostgreSQL, no network):**

- `tests/integration/infrastructure/publishing/test_tiktok_destination_runtime.py` — the full
  publish runtime driven end-to-end against a `MockTransport`-backed `TikTokDestination` registered
  under `platform="tiktok"`, proving: job → running → adapter → `succeeded` with
  `platform_post_id = publish_id`; a retryable init failure reschedules with backoff; a PUB-11
  ambiguous outcome settles **failed, not retried**.

**Live smoke (excluded from CI):** `tests/live/test_tiktok_live_smoke.py`, marked `live_smoke`,
gated on `TIKTOK_LIVE_SMOKE=1` + a sandbox bearer + a local file, mirroring
`test_youtube_live_smoke.py:1-36`. TikTok's sandbox (5 sandboxes / 10 target users, no review —
grounding §4.1) makes this genuinely runnable pre-audit.

**Stage 24** is appended to `ci_gate.py` `_stages()` (currently max 23, `:800-812`) with its
docstring updated, per the "each new slice earns its own stage" convention.

---

## 11. Implementation order

1. `config.py` — the `tiktok_*` settings block.
2. `oauth/tiktok_oauth_client.py` — `TikTokOAuthClient`.
3. `destinations/tiktok.py` — `TikTokDestination`.
4. `container.py` — http client, OAuth registration, destination registration, `shutdown`/`reset`.
5. Unit tests (destination, OAuth client, wiring).
6. Integration test + **CI Stage 24** in `ci_gate.py`.
7. Live smoke test (env-gated).
8. `main.py` version → `0.4.49-phase3-alpha9.6-dev`; `CHANGELOG.md` entry incl. the accepted
   limitation (§7).
9. `ruff` → `black` → `mypy` → `lint-imports` → **full ephemeral-PostgreSQL CI gate (all 24 stages)**.
10. Feature branch `feat/alpha9.6-tiktok-destination`, push, open the `-dev` release-review PR.

---

## 12. Mandatory constraints (for implementation)

- **No migration.** No schema, enum, or CHECK change.
- **No ADR.** If a genuine architectural contradiction appears, **stop and report**.
- **No port/DTO/domain/contract change** — `IDestinationPublisher`, `PublishResult`, `UploadMedia`,
  `ContentPackage`, `AuthorizedContext`, `ISocialOAuthClient` are all frozen.
- **No changes** to `ProcessPublishJob`, `CreatePublishJob`, `CreatePublishJobs`, notifications,
  analytics, export, or the AI plane.
- **Credential-blind.** The adapter uses only the injected bearer; it never reads settings, the
  credential store, repositories, or the account aggregate.
- **PUB-11 preserved.** Any post-transmission uncertainty is permanent — never retried.
- **Never silently degrade creator intent** — unsupported `publish_at` and unavailable visibility
  fail loudly; only the thumbnail is silently ignored (ADR-0050 declares it best-effort).
- **Fail-soft + deterministic CI.** CI runs TikTok unconfigured; no test touches the network.
- **Additive only.** No refactoring outside this slice.
- **Stop after** the full gate is green, the branch is pushed, and the `-dev` release-review PR is
  open — no finalise, merge, tag, or docs-sync.
