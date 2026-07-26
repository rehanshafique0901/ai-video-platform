# α8.6c Grounding — Destination Adapters (YouTube + Mock)

> **Type:** Grounding (read-only facts). **No code, no schema, no baseline change.**
> Establishes the facts the α8.6c pre-flight will build on.
>
> **The one question:** *Given the already-proven publish runtime, how does the platform
> upload one finished export artifact to a **real** destination (YouTube) — while leaving
> the architecture exactly as α8.6b left it?* — and nothing else.
>
> **Explicitly out of scope (deferred):** a second real destination (TikTok/IG/FB), a YAML
> destination catalogue, analytics, creator UX, scheduling, custom thumbnails, and
> AI-generated metadata. α8.6c **replaces the Mock destination with the first
> production-quality destination** and adds nothing else.
>
> **Governed by:** `PUBLISHING_RUNTIME_CONTRACT.md` §4.1–§4.2 (the destination port /
> "destinations are not AI providers"), §5 (`ContentPackage`), §6 (retry/failure classes),
> §8 (destination boundary), §9 (OAuth ≠ upload), §13 (α8.6c increment), §14–§15 (deferrals
> + Q1–Q5); and **ADR-0047** (the credential boundary this slice consumes but never
> re-opens). Baseline: `v0.4.37-phase3-alpha8.6b`.

---

## 0. The boundary being proven

α8.6b built the durable, retry-safe, idempotent publish **runtime** and proved it
end-to-end against a deterministic `MockDestination`. α8.6c fills the **one hole** that
runtime was designed around: a **real** `IDestinationPublisher` (YouTube) plus the real
`ISocialOAuthClient` that lets a user actually connect + refresh a Google credential.

```
(unchanged α8.6b runtime)                         (α8.6c fills these two leaves)
CreatePublishJob → PublishJob(QUEUED)
        │  PublishWorker.run_once()
        ▼
  ProcessPublishJob ── dual lease ── CAS QUEUED→RUNNING
        │  authorize(social_account_id) → AuthorizedContext   ◀── refreshed via ISocialOAuthClient
        │  materialize export-delivery bytes (PUB-1)               └── α8.6c: YouTubeOAuthClient
        │  registry.for_platform("youtube").publish(package, auth, media)
        ▼                                                    └── α8.6c: YouTubeDestination
  settle SUCCEEDED / retry / FAILED  ── emit PublishJob{Succeeded,Failed}
```

Everything above the two arrows already exists and is frozen-by-precedent for this slice.
The rest of this document records the concrete seams α8.6c plugs into and the **two new
leaf files** it introduces.

---

## 1. The destination adapter surface already exists — *no port change* (verify #1)

α8.6b shipped the entire boundary in
`app/application/interfaces/destination_publisher.py`. Its own docstring states: *"α8.6b
ships the `mock` adapter; the real YouTube adapter is α8.6c (**no port change**)."*

| Element | Shape (fact) |
|---|---|
| `IDestinationPublisher.platform` | `property -> str` — the free-text platform key (`"mock"` / `"youtube"`) |
| `IDestinationPublisher.publish` | `async publish(*, package: ContentPackage, auth: AuthorizedContext, media: UploadMedia) -> PublishResult` |
| `UploadMedia` | frozen `(path: str, mime_type: str, size_bytes: int)` — a **local temp path**; the worker materialises bytes to a temp workspace outside any txn and hands the adapter a file handle (PUB-1) |
| `PublishResult` | frozen `(external_post_id: str, post_url: str \| None)` |
| `DestinationError` | `Exception(message, *, retryable: bool, code: str = "destination_error")` — the adapter's own failure classification |
| `IDestinationRegistry` | `for_platform(platform) -> IDestinationPublisher` (unknown ⇒ permanent `DestinationError`) + `supported_platforms() -> frozenset[str]` |

**Fact:** α8.6c is a **new leaf** `infrastructure/publishing/destinations/youtube.py`
implementing `IDestinationPublisher`, plus one line registering it in the composition root.
No interface, DTO, or use-case signature changes.

---

## 2. The YouTube upload boundary — *two distinct halves* (verify #2)

The contract (§9) draws a hard line between two YouTube concerns; the code already models
both as **separate ports**, and they must **not** be merged:

```
Credential acquisition/refresh  → ISocialOAuthClient   (connect / exchange / refresh / revoke)
        │  fed to the credential service (sole decryptor, ADR-0047 C7)
        ▼
Credential storage + refresh    → ISocialCredentialStore.authorize() → AuthorizedContext (bearer)
        │
        ▼
Upload API call                 → IDestinationPublisher.publish(...)  (credential-blind)
```

- **`ISocialOAuthClient`** (`app/application/interfaces/social_oauth_client.py`):
  `authorization_url(state, redirect_uri)`, `exchange_code(code, redirect_uri) -> OAuthGrant`,
  `refresh(refresh_token) -> GrantedTokens`, `revoke(token)`. Its docstring already says
  *"the real YouTube OAuth client lands with its upload adapter in α8.6c (OQ1)."* α8.6a
  shipped `MockSocialOAuthClient` (`infrastructure/publishing/oauth/mock_oauth_client.py`)
  as the exact seam the real client fills.
- **`IDestinationPublisher`** (§1): the upload half. For YouTube this is the **Data API v3
  `videos.insert` resumable upload** (contract §8 names YouTube for its "resumable upload
  API — the most upload-friendly of the four").
- **Configuration-blind (W8.1.1):** both concrete clients receive client id/secret/endpoints
  **by injection at the composition root** and never read `Settings` themselves. The
  precedent: `MockSocialOAuthClient.__init__` takes `authorize_endpoint`, `access_ttl`,
  `scopes`, `clock` — all injected.

**Fact (scope):** a *usable* YouTube path needs **both** leaves — without the real OAuth
client there is no way to obtain/refresh a real Google token, so the upload adapter could
never run end-to-end. Both are within "destination adapters (YouTube + Mock)" and are the
subject of pre-flight ruling EQ1.

---

## 3. `AuthorizedContext` is reused as-is — *zero change* (verify #3)

`AuthorizedContext` (`app/application/interfaces/social_credential_store.py:49`) is a frozen
`(access_token: str, expires_at: datetime | None, scopes: tuple[str, ...])` — a **bearer
only**, carrying **no refresh token and no key material** (PUB-5 / ADR-0047 C4).

- `ProcessPublishJob._publish_and_settle` (`process_publish_job.py:167`) calls
  `auth = await self._credential_store.authorize(job.social_account_id)` and passes that
  same `auth` straight into `adapter.publish(package=…, auth=auth, media=…)` (line 189).
- `YouTubeDestination.publish` receives that identical DTO. It reads only `auth.access_token`
  as the `Authorization: Bearer …` header.

**Fact:** the only object crossing into the destination adapter is `AuthorizedContext`. No
new authorization plumbing, no new DTO. `ProcessPublishJob` remains the sole caller of
`authorize()`.

---

## 4. `ContentPackage → destination` mapping happens at the adapter (verify #4)

`ContentPackage` (`app/domain/publishing/content_package.py`) is the immutable,
platform-agnostic payload built once at create time and serialised to the
`publish_jobs.content_package` JSONB:

```
media_asset_id: UUID
title: str
description: str
tags: tuple[str, ...]
visibility: Visibility            # public | unlisted | private   (default private)
thumbnail_media_asset_id: UUID | None
publish_at: datetime | None
```

The contract (§5) states platform-specific mapping + limit validation happens **inside** the
destination adapter, keeping the core neutral. The natural YouTube `videos.insert` mapping:

| `ContentPackage` | YouTube request | Adapter-side validation (⇒ permanent `DestinationError`) |
|---|---|---|
| `title` | `snippet.title` | non-empty; ≤ 100 chars |
| `description` | `snippet.description` | ≤ 5000 chars |
| `tags` | `snippet.tags` | total serialised tag length ≤ ~500 chars |
| `visibility` | `status.privacyStatus` | 1:1 — `public`/`unlisted`/`private` map directly |
| `publish_at` | `status.publishAt` (RFC-3339) | requires `privacyStatus = private` |
| `thumbnail_media_asset_id` | — | **deferred** (contract §14 — no custom thumbnail upload in α8.6) |

**Fact:** the visibility vocabulary is already YouTube-shaped (public/unlisted/private), so
the mapping is direct. All platform-limit rejection is the adapter's job, expressed as
`DestinationError(retryable=False)`.

---

## 5. Metadata generation responsibilities — *none in the adapter* (verify #5)

- `build_content_package(...)` (`content_package.py:77`) is a **pure, deterministic
  template** (PUB-9): title from the project (fallback `"Untitled video"`), description
  defaults to the title, empty tags, `visibility=PRIVATE` — **no LLM, no randomness**.
- It runs at **create time** inside `CreatePublishJob`, **not** in the worker or adapter.
- The destination adapter **maps + validates** the already-built package; it never
  *generates* metadata.

**Fact:** α8.6c adds **no** metadata generation. AI caption/hashtag generation
(`ContentPackage → LLM Metadata Generator`) is an explicitly deferred separate slice
(contract §14). The adapter's only metadata responsibility is the §4 mapping/validation.

---

## 6. Retry / failure behaviour at the adapter boundary (verify #6)

The runtime already owns the retry machinery; the adapter owns only **classification**.

- `DestinationError(retryable: bool, code: str)` is the entire contract between adapter and
  runtime. `ProcessPublishJob` catches it and calls `_settle_retry_or_fail(retryable=…,
  code=…, message=…)` (`process_publish_job.py:152`).
- **Backoff (already implemented, DQ6):** `min(3600, 30 · 2^(attempt-1))`, `max_attempts=5`,
  no jitter (deterministic). Retryable + attempts remain ⇒ `reschedule_for_retry`;
  permanent or exhausted ⇒ `mark_failed` + `PublishJobFailed`.
- **The runtime never inspects provider error codes** (contract §6). YouTube's job is to
  translate Google outcomes into the two classes:
  - **retryable** — `5xx`, `429`/rate-limit, network/timeout, transient `quotaExceeded`.
  - **permanent** — `400` invalid metadata, `403` forbidden/insufficient-permissions,
    rejected/unsupported media, malformed request.
- **Credential + storage errors are handled by the worker *before/around* the adapter, not
  by it:** `CredentialUnavailableError`/`CredentialDecryptionError` from `authorize()` ⇒
  **permanent** fail-closed (line 142); `ObjectStorageError` during materialise ⇒
  **retryable** (line 148). The adapter never sees these.

**Fact + tension for pre-flight (idempotency).** `videos.insert` has **no native
idempotency key**, so a retry *after bytes were accepted but the response was lost* risks a
**duplicate public post**. The runtime's partial-unique idempotency is on
`(source_media_asset_id, social_account_id)` at the **job** level — it does not protect
against a mid-upload ambiguous outcome within a single attempt. This is the subject of
pre-flight ruling EQ3 (the recommendation being: once upload bytes have been accepted and
the outcome is ambiguous, classify **permanent** — never auto-retry into a possible
double-post).

---

## 7. Destination adapters remain credential-blind leaves — *already enforced* (verify #7)

Two import-linter contracts in `backend/pyproject.toml` already box in anything added to the
`destinations` package:

- **"Destination adapters are credential-blind leaves"** (`pyproject.toml:389`):
  `app.infrastructure.publishing.destinations` is **forbidden** from importing
  `app.infrastructure.publishing.credentials`, `app.infrastructure.repositories`,
  `app.infrastructure.uow`, `app.application.use_cases`, `app.domain.generation`,
  `app.domain.workflow`.
- **"Encryption primitives are confined to the publishing credential adapter"**
  (`pyproject.toml:357`): both `publishing.destinations` **and** `publishing.oauth` are
  forbidden from importing `cryptography`.

So a new `youtube.py` destination leaf may import **only** the destination port, the
credential-store port (for the `AuthorizedContext` DTO), the domain `ContentPackage`, and an
injected HTTP transport. The `YouTubeOAuthClient` lives in `publishing.oauth` (already
crypto-forbidden) and legitimately deals with tokens *in transit* to the credential service,
but never persists or encrypts them (that is the credential service's job, C7).

**Fact:** the credential-blind boundary is guarded mechanically today; α8.6c inherits it
automatically. The α8.6b e2e test (`test_publish_runtime_end_to_end.py`) additionally
asserts the runtime never leaks credentials.

---

## 8. Composition wiring touchpoints — *the only edits outside the two new leaves*

All in `app/core/container.py`:

- `_get_destination_registry()` (`container.py:1645`) — currently
  `DestinationRegistry({"mock": MockDestination()})`. α8.6c adds `"youtube":
  YouTubeDestination(transport=…, endpoints=…)`.
- `_get_oauth_clients()` (`container.py:1579`) — currently `{"mock":
  MockSocialOAuthClient(...)}`. α8.6c adds `"youtube": YouTubeOAuthClient(...)`. This dict
  feeds **both** the credential service (refresh/exchange/revoke) and
  `StartSocialConnection`/`CompleteSocialConnection` (connect).
- `supported_platforms()` (consumed by `CreatePublishJob`, `container.py:1661`) then
  admits `"youtube"` automatically — no create-path change.
- **Config (`app/core/config.py`):** new configuration-blind `Settings` fields for the
  Google OAuth client id/secret (`SecretStr`) + endpoint/scope defaults, injected at the
  composition root (mirrors how `publishing_oauth_*` fields at `config.py:304`+ are already
  organised). If the YouTube credentials are unset, YouTube is simply not registered — a
  fail-soft parallel to the α8.6a master-key story.

**Fact:** no changes to `ProcessPublishJob`, `PublishWorker`, `CreatePublishJob`, the
repository, the router, the domain, or the UoW.

---

## 9. Testing & CI facts (verify — network-free Stage 14)

- **Stage 14** is the publishing stage (α8.6a + α8.6b); the contract (§13) requires it stay
  **deterministic + network-free**. `MockDestination` remains the CI default.
- **Existing fakes/precedent:** α8.6b unit tests use in-memory fakes; `MockSocialOAuthClient`
  is the deterministic OAuth precedent. The real YouTube leaves must be tested with a
  **fake HTTP transport** (assert request mapping, error classification, and the resumable
  two-step protocol) — **no live Google calls in CI**.
- **Live smoke test:** there is currently **no** opt-in live-integration harness for
  external APIs; α8.6c introduces a smoke test gated behind an env var and **excluded from
  CI** (EQ4).

---

## 10. What does **not** exist yet (the α8.6c build surface)

Everything below is absent today and is exactly what α8.6c adds — all additive, all behind
existing ports:

| Missing seam | Mirror of | Layer |
|---|---|---|
| `YouTubeDestination(IDestinationPublisher)` (resumable `videos.insert`, mapping + error classification) | `MockDestination` | `infrastructure/publishing/destinations/youtube.py` |
| `YouTubeOAuthClient(ISocialOAuthClient)` (authorize URL / exchange / refresh / revoke against Google) | `MockSocialOAuthClient` | `infrastructure/publishing/oauth/youtube_oauth_client.py` |
| A thin injected async HTTP transport seam (httpx) for both leaves | *(new; EQ2)* | `infrastructure/publishing/http/` (or similar) |
| YouTube `Settings` fields (client id/secret/endpoints/scopes) | `publishing_oauth_*` fields | `core/config.py` |
| Registry + oauth-client wiring for `"youtube"` | the `"mock"` wiring | `core/container.py` |
| Unit tests (mapping / classification / resumable protocol) + opt-in live smoke | α8.6b destination tests | `tests/unit/.../destinations`, `tests/.../oauth` |

**No new port. No new migration. No new domain type. No runtime/use-case change.**

---

## 11. Open questions for the α8.6c pre-flight (surfaced; ruled by EQ1–EQ5)

Each has a concrete factual basis above and is resolved by the approved rulings the
pre-flight will record:

1. **Scope — OAuth client + destination, or destination only?** (§2, §8) — *EQ1.*
2. **HTTP/dependency approach** — thin httpx behind an injected transport vs. Google SDK.
   (§8) — *EQ2.*
3. **Upload idempotency / ambiguous-outcome classification** — the `videos.insert`
   double-post risk (§6) — *EQ3* (+ a new documented publishing invariant).
4. **CI shape** — network-free unit + opt-in live smoke (§9) — *EQ4.*
5. **Artifact cadence** — grounding + pre-flight docs, each its own PR (§0) — *EQ5.*

---

## 12. Non-goals (restated, so the pre-flight stays scoped)

- **No second real destination** (TikTok/Instagram/Facebook) — later, behind the same port.
- **No YAML destination catalogue/validator** — deferred until ≥2 real destinations (Q1).
- **No analytics, no creator UX, no scheduler** — out of α8.6 entirely.
- **No custom thumbnail upload** — reuse the enrichment-derived thumbnail (deferred, §14).
- **No AI-generated metadata** — deterministic `ContentPackage` only (PUB-9).
- **No port change, no migration, no runtime expansion** — α8.6c only adds the real
  destination implementation (+ its OAuth client) behind unchanged seams.
- **No change to any frozen path or to ADR-0047** — strictly additive.

---

### Summary of established facts

α8.6c is the narrowest possible slice: **two new infrastructure leaves**
(`YouTubeDestination` behind the unchanged `IDestinationPublisher`, and `YouTubeOAuthClient`
behind the unchanged `ISocialOAuthClient`), a thin injected HTTP transport, a handful of
configuration-blind `Settings` fields, and their composition-root wiring. Every seam it
consumes — `AuthorizedContext`, `ContentPackage`, the dual-lock/retry runtime, the
partial-unique idempotency, the outbox events, the import-linter credential-blind guards,
and CI Stage 14 — already exists and is proven. The runtime, ports, domain, repository,
router, and schema are untouched. The single genuinely new design concern is the
`videos.insert` **ambiguous-outcome idempotency** question (§6), which the pre-flight
resolves as a documented publishing invariant (EQ3).
