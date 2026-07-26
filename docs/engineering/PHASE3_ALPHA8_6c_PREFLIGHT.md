# Phase 3 — α8.6c Pre-flight: Destination Adapters (YouTube + Mock)

> **Status: DRAFT — awaiting sign-off.** Third and final increment of the **α8.6
> Publishing / Creator Workflow** bounded context. Input:
> `PHASE3_ALPHA8_6c_GROUNDING.md` (PR #40). Governing artifacts:
> `PUBLISHING_RUNTIME_CONTRACT.md` (§4–§9, §13–§15, PUB-1…PUB-10) and **ADR-0047**
> (credential ownership — consumed, not re-opened). Baseline:
> `v0.4.37-phase3-alpha8.6b`.
>
> **The one question α8.6c answers:** *How does the platform upload one finished export
> artifact to a **real** destination (YouTube) — while leaving the runtime, ports, domain,
> schema, and architecture exactly as α8.6b left them?*
>
> **Objective (verbatim):** *replace the Mock destination with the first production-quality
> destination while leaving the architecture unchanged.*
>
> **Locked from grounding:** α8.6c is **two new infrastructure leaves** behind unchanged
> ports — `YouTubeDestination` (`IDestinationPublisher`) and `YouTubeOAuthClient`
> (`ISocialOAuthClient`) — plus a thin injected HTTP transport, configuration-blind
> `Settings`, and composition-root wiring. **No port change, no migration, no runtime
> expansion, no new domain type.**
>
> **§10 records the approved rulings EQ1–EQ5.** Nothing is implemented until this pre-flight
> is approved.

---

## 0. Gates (answered first)

### Gate 1 — ADR-0042 (orchestration platform freeze)
> Does α8.6c touch any frozen orchestration module, checkpoint contract, provider protocol,
> render/export composition, or workflow lifecycle?

**No.** α8.6c adds two leaves under `infrastructure/publishing/**` and wires them at the
composition root. It changes no frozen path, no runtime use case, no repository, no schema.
Freeze guard stays green, **zero overrides**.

### Gate 2 — ADR-0047 (credential ownership) — consumed, not re-opened
> Does α8.6c stay credential-blind?

**Yes, and the boundary is mechanically enforced.** The `YouTubeDestination` upload leaf
receives **only** an `AuthorizedContext` (bearer) and never touches the credential store,
tokens, refresh, or key material (PUB-5 / C4). The `YouTubeOAuthClient` handles tokens
**in transit** to the credential service (which remains the sole decryptor + at-rest owner,
C7) and never persists or encrypts them. Both packages are already forbidden from importing
`cryptography` and the credential adapter by import-linter (grounding §7). **α8.6c adds no
crypto and no change to ADR-0047.**

### Gate 3 — Publishing invariants (PUB-1…PUB-10) + the new PUB-11
Existing invariants are inherited unchanged (α8.6c adds no runtime). This slice introduces
**one new invariant, PUB-11** (destination-upload idempotency / ambiguous-outcome
handling), ruled in §10 EQ3 and to be recorded in `PUBLISHING_RUNTIME_CONTRACT.md` §11 and
the `PLATFORM_STATUS.md` invariant catalog as part of this slice.

---

## 1. Positioning (what α8.6c *is* / *is not*)

α8.6c makes the publish path **actually work against a real platform**. It is the first
*real* destination; the runtime that drives it is unchanged.

```
Connect (α8.6a seam, now real)                       Publish (α8.6b runtime, unchanged)
StartSocialConnection ─▶ YouTubeOAuthClient.authorization_url         PublishWorker.run_once()
        │  user consents at Google                                            │
CompleteSocialConnection ─▶ exchange_code ─▶ GrantedTokens ─▶ credential      ▼
        │                    (encrypted at rest by the credential service)  ProcessPublishJob
        ▼                                                                     │ authorize() → AuthorizedContext
 SocialAccount(status=connected, platform="youtube")                          │ materialize bytes (PUB-1)
                                                                              ▼ registry.for_platform("youtube")
                              YouTubeDestination.publish(package, auth, media) ──▶ Data API v3 videos.insert (resumable)
                                                                              ▼
                                          settle SUCCEEDED / retry / FAILED  ── PublishJob{Succeeded,Failed}
```

**Is:** `YouTubeDestination` (resumable `videos.insert`, `ContentPackage`→request mapping,
Google-error → `DestinationError(retryable)` classification, PUB-11 ambiguous-outcome
handling); `YouTubeOAuthClient` (authorize URL / code exchange / refresh / revoke against
Google); a thin injected `httpx` transport seam; configuration-blind YouTube `Settings`;
composition-root wiring; comprehensive network-free unit tests + an opt-in live smoke test.

**Is not:** any port/DTO/domain/schema/runtime change; a second destination; a YAML
catalogue; analytics; creator UX; a scheduler; custom thumbnails; AI metadata.

---

## 2. No new data model, no migration

**Confirmed against the grounding:** α8.6c requires **no migration**. `social_accounts`
(α8.6a) already stores `platform` as free text with `credential_reference` and
`UNIQUE(user_id, platform, external_account_id)`; `publish_jobs` (α8.6b) already carries
`platform` + `content_package`. A YouTube connection is an ordinary `SocialAccount` row with
`platform="youtube"`; a YouTube publish is an ordinary `publish_jobs` row routed by its
`platform`. The contract (§12) explicitly says α8.6c needs *"none required for
YouTube/Mock"*. **No enum change, no `EXPECTED_ENUM_COUNT` bump, no ERD change.**

*(If implementation uncovers a genuinely new persistence need — none is foreseen — it is
escalated as a scope change before any migration is written.)*

---

## 3. The two new leaves

### 3.1 `YouTubeDestination(IDestinationPublisher)` — `infrastructure/publishing/destinations/youtube.py`
- **`platform` = `"youtube"`.**
- **`publish(*, package, auth, media)`** performs the **YouTube Data API v3 `videos.insert`
  resumable upload**:
  1. **Initiate** the resumable session — `POST …/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status`
     with `Authorization: Bearer {auth.access_token}`, the mapped JSON metadata body, and
     `X-Upload-Content-Type` / `X-Upload-Content-Length` from `media`. Captures the
     session URL (`Location`).
  2. **Transmit** the bytes — `PUT {session_url}` streaming `media.path`. On success parses
     the returned video resource → `PublishResult(external_post_id=id,
     post_url="https://www.youtube.com/watch?v={id}")`.
- **`ContentPackage` → request mapping (grounding §4), validated inside the adapter:**
  `title`→`snippet.title` (≤100), `description`→`snippet.description` (≤5000),
  `tags`→`snippet.tags` (≤~500 total chars), `visibility`→`status.privacyStatus` (1:1),
  `publish_at`→`status.publishAt` (forces `private`); thumbnail deferred. Limit violations ⇒
  `DestinationError(retryable=False, code="invalid_metadata")`.
- **Credential-blind leaf:** imports only the destination port, the credential-store port
  (for the `AuthorizedContext` DTO), the domain `ContentPackage`, and the injected transport.
- **No internal retry loop** — it performs at most the resumable protocol's own
  status/finalize calls and returns a single `DestinationError` classification to the
  runtime, which owns backoff/attempts.

### 3.2 `YouTubeOAuthClient(ISocialOAuthClient)` — `infrastructure/publishing/oauth/youtube_oauth_client.py`
- **`authorization_url(state, redirect_uri)`** → Google OAuth 2.0 consent URL
  (`accounts.google.com/o/oauth2/v2/auth`) with `client_id`, `scope`
  (`youtube.upload` + `youtube.readonly` for channel identity), `access_type=offline`,
  `prompt=consent` (to guarantee a refresh token), and `state`.
- **`exchange_code(code, redirect_uri)`** → `POST oauth2.googleapis.com/token` →
  `OAuthGrant(external_account_id, display_name, GrantedTokens(access, refresh, expires_at,
  scopes))`. `external_account_id`/`display_name` come from a `channels?mine=true` lookup so
  the `SocialAccount` identifies the actual channel (uniqueness key
  `(user_id, "youtube", channel_id)`).
- **`refresh(refresh_token)`** → token endpoint `grant_type=refresh_token` → fresh
  `GrantedTokens`; failure raises `OAuthExchangeError`.
- **`revoke(token)`** → `POST oauth2.googleapis.com/revoke`, best-effort/idempotent.
- **Configuration-blind:** client id/secret/endpoints/scopes injected at construction; never
  reads `Settings` itself.
- Wired into `_get_oauth_clients()` so the **credential service** uses it for
  refresh/revoke and `Start/CompleteSocialConnection` use it for connect.

### 3.3 HTTP transport seam (EQ2)
Both leaves take an injected **`httpx.AsyncClient`** (already a project dependency,
`pyproject.toml:28`). The composition root builds it with the resolved base URLs + timeouts;
tests inject a client built with **`httpx.MockTransport`** for fully deterministic,
network-free assertions. No Google SDK, no new dependency, no new application-layer port.

---

## 4. Configuration (configuration-blind, `app/core/config.py`)

New optional `Settings` fields (mirroring the `publishing_oauth_*` grouping at
`config.py:304`+), injected at the composition root only:

| Field | Type | Notes |
|---|---|---|
| `youtube_oauth_client_id` | `str \| None` | Google OAuth client id |
| `youtube_oauth_client_secret` | `SecretStr \| None` | Google OAuth client secret |
| `youtube_oauth_scopes` | `tuple[str,…]` | default `("…/youtube.upload","…/youtube.readonly")` |
| (endpoints) | defaults | Google auth/token/revoke/upload base URLs, overridable for the live smoke test |

**Fail-soft registration:** if the client id/secret are unset, YouTube is simply **not
registered** in either the destination registry or the OAuth-client map (a create for
`platform="youtube"` then fails create-time validation via `supported_platforms()`), exactly
paralleling the α8.6a master-key fail-soft. No boot failure is added.

---

## 5. Composition wiring (`app/core/container.py`) — the only edits outside the leaves

- `_get_oauth_clients()` (`container.py:1579`): add `"youtube": YouTubeOAuthClient(
  http=…, client_id=…, client_secret=…, scopes=…, clock=get_clock())` when configured.
- `_get_destination_registry()` (`container.py:1645`): add `"youtube": YouTubeDestination(
  http=…, endpoints=…)` when configured.
- `supported_platforms()` (consumed by `CreatePublishJob`, `container.py:1661`) then admits
  `"youtube"` automatically — **no create-path code change**.

No other wiring, no change to `ProcessPublishJob`, `PublishWorker`, `CreatePublishJob`, the
repository, the router, the domain, or the UoW.

---

## 6. PUB-11 — destination-upload idempotency (new invariant, EQ3)

> **PUB-11 — A destination upload is never retried once the platform may have durably
> accepted the media.** If the outcome after byte transmission is **ambiguous** (connection
> dropped after the `PUT`, timeout awaiting the finalize response, a success status whose
> body cannot be parsed into a video id), the adapter classifies it as a **permanent,
> manual-review** failure (`DestinationError(retryable=False,
> code="ambiguous_upload_outcome")`) — **never** retryable — to prevent duplicate
> publication. Only failures that occur **strictly before upload begins**, or that are
> **unambiguously transient before acceptance** (session-initiation `5xx`/`429`/network,
> pre-`PUT` timeouts), are retryable.

Concretely for `videos.insert` (which has **no native idempotency key**):

| Phase / outcome | Classification |
|---|---|
| Session initiate — `5xx`, `429`, network error, timeout | **retryable** (`code="youtube_transient"`) |
| Session initiate — `400`/`403`/invalid metadata | **permanent** (`code="invalid_metadata"`/`"forbidden"`) |
| `401` at any point | **permanent** (`code="unauthorized"`) — credential concern, fail-closed |
| Byte transmission — clean, definitive non-acceptance error before/at start (adapter can prove no video was created) | **retryable** (`code="youtube_transient"`) |
| Byte transmission — **ambiguous** after bytes may have been accepted | **permanent** (`code="ambiguous_upload_outcome"`) — PUB-11 |
| Success but unparseable video id | **permanent** (`code="ambiguous_upload_outcome"`) — PUB-11 |

This deliberately favours **correctness over automatic retry** (EQ3): the platform may lose
an occasional legitimately-retryable upload to a permanent failure, but it will **never**
double-post to a creator's public channel. The runtime's job-level partial-unique
idempotency (`(source_media_asset_id, social_account_id)`) already prevents a *second job*
for the same artifact/account; PUB-11 covers the *within-attempt* ambiguity the job key
cannot see.

---

## 7. Testing & CI (EQ4 — network-free Stage 14 + opt-in live smoke)

**Stage 14 stays deterministic + offline.** All YouTube tests use an injected
`httpx.MockTransport`:

- **Request-mapping tests** — assert the resumable initiate request carries the correct
  URL, headers (`Authorization`, `X-Upload-Content-*`), and the exact `snippet`/`status`
  JSON for representative `ContentPackage`s (visibilities, `publish_at`, tag limits).
- **Resumable-protocol tests** — initiate → session URL captured → `PUT` bytes → video id
  parsed → `PublishResult`.
- **Error-classification tests** — every row of the §6 table: `5xx`/`429` → retryable;
  `400`/`403`/`401` → permanent; ambiguous post-transmission → permanent PUB-11.
- **OAuth-client tests** — `authorization_url` shape; `exchange_code` → `OAuthGrant`
  (+ channel identity lookup); `refresh` → `GrantedTokens`; `refresh` failure →
  `OAuthExchangeError`; `revoke` idempotent.
- **`MockDestination` remains the CI default** for the end-to-end runtime test.

**Opt-in live smoke test** (EQ4) — a separate test gated behind an env var (e.g.
`YOUTUBE_LIVE_SMOKE=1` + real credentials), **excluded from CI** (skipped by default), that
performs one real unlisted upload to verify the adapter against Google. It is documentation
+ a manual pre-release check, never part of the deterministic gate.

**Import-linter:** no new contract required — the existing "Destination adapters are
credential-blind leaves" and "Encryption primitives confined…" contracts already cover the
new files. A test asserts `YouTubeDestination.publish` receives only `AuthorizedContext` and
never accesses the credential store (mirrors the α8.6b credential-blind assertion).

---

## 8. Invariant mapping (PUB-1…PUB-11)

| Invariant | How α8.6c satisfies it |
|---|---|
| PUB-1 | Uploads only the export-delivery `MediaAsset` bytes handed in via `UploadMedia`. |
| PUB-2 | No auto-publish added; publish still requires an explicit `PublishJob`. |
| PUB-3 | All new code under `infrastructure/publishing/**`; bounded-context isolation intact. |
| PUB-4 | `YouTubeDestination` registered in the in-code `DestinationRegistry`, not the AI catalogue/resolver/dispatcher. |
| PUB-5 | Upload leaf takes `AuthorizedContext` + a file path only; import-linter + test forbid credential-store access. |
| PUB-6 | Reads a finished artifact + calls one external API; mutates no upstream state. |
| PUB-7 | Runtime unchanged — dual lease, CAS, bounded retries, DB-owned idempotency all inherited. |
| PUB-8 | Terminal outbox events unchanged; fan-out only. |
| PUB-9 | Deterministic `ContentPackage`; the adapter only maps/validates, never generates. |
| PUB-10 | Credentials owned by the α8.6a service (ADR-0047); the upload leaf consumes `authorize()` output only. |
| **PUB-11** | Ambiguous post-transmission outcomes are permanent manual-review failures — never retried (§6). |

---

## 9. Increment plan (implementation order)

1. **PUB-11 + contract sync** — add PUB-11 to `PUBLISHING_RUNTIME_CONTRACT.md` §11 and the
   `PLATFORM_STATUS.md` invariant catalog (doc-only, lands with the slice).
2. **Config** — configuration-blind YouTube `Settings` fields (`app/core/config.py`).
3. **HTTP transport seam** — the injected `httpx.AsyncClient` construction helper.
4. **`YouTubeOAuthClient`** (`infrastructure/publishing/oauth/`) + unit tests.
5. **`YouTubeDestination`** (`infrastructure/publishing/destinations/`) + unit tests
   (mapping / resumable protocol / §6 classification / PUB-11).
6. **Composition wiring** — `_get_oauth_clients()` + `_get_destination_registry()` +
   fail-soft registration.
7. **Opt-in live smoke test** (env-gated, excluded from CI).
8. **Full Stage 14 ephemeral-DB gate** (all stages green; `MockDestination` still the CI
   default) → feature commit at `-dev` → release review → finalize → tag.

---

## 10. Approved rulings (EQ1–EQ5) + implementation constraints

### EQ1 — Ship **both** `YouTubeOAuthClient` and `YouTubeDestination`
α8.6c is the first *real* destination; shipping only the upload adapter would leave the path
unusable (no way to obtain/refresh real Google credentials). Keep the existing separation:
`YouTubeOAuthClient` owns OAuth (connect/exchange/refresh/revoke); `YouTubeDestination` owns
only the upload API; **`AuthorizedContext` remains the sole object crossing into the
destination adapter.** These responsibilities are **not** merged.

### EQ2 — Thin `httpx` behind an injected transport
Minimal, configuration-blind dependency surface; use the already-present `httpx`
(`pyproject.toml:28`) with an injected `AsyncClient`/`MockTransport`. **No Google SDK**
unless a later requirement genuinely needs functionality the REST API cannot reasonably
provide.

### EQ3 — Correctness over automatic retries → **PUB-11**
Once upload bytes may have been accepted and the outcome is ambiguous, classify a
**terminal manual-review** failure, not a retryable one. Continue retrying only pre-upload
or unambiguously-transient failures (§6). Documented explicitly as invariant **PUB-11**.

### EQ4 — Network-free CI + optional live smoke
Stage 14 stays deterministic/offline via `httpx.MockTransport`; ship comprehensive unit
tests (request/response mapping, error classification, resumable protocol) plus a separate
**opt-in** live smoke test behind an env var, **excluded from CI** (§7).

### EQ5 — Established doc cadence
`PHASE3_ALPHA8_6c_GROUNDING.md` (PR #40) and this `PHASE3_ALPHA8_6c_PREFLIGHT.md` each ship
as their own review PR. **No implementation begins until this pre-flight is approved.**

### Standing implementation constraints (explicit)
- **No port changes** unless grounding proves them unavoidable — grounding proves they are
  **not** needed.
- **No migration** unless a genuinely new persistence requirement appears — none is foreseen
  (§2); any such need is escalated before a migration is written.
- **Preserve the credential-blind boundary** from ADR-0047 — enforced by import-linter + a
  test (§7).
- **Do not expand the publishing runtime** beyond adding the real destination
  implementation (+ its OAuth client).
- **Defer:** YAML destination catalogue, a second destination, analytics, creator UX,
  scheduling, thumbnails, AI-generated metadata.

---

## 11. Non-goals / explicitly deferred (restated)

- **No second real destination** (TikTok/Instagram/Facebook) — later, same port.
- **No YAML destination catalogue/validator** — until ≥2 real destinations (Q1).
- **No analytics, creator UX, or scheduler.**
- **No custom thumbnail upload** — reuse the enrichment-derived thumbnail (deferred).
- **No AI-generated metadata** — deterministic `ContentPackage` only (PUB-9).
- **No port change, no migration, no runtime/domain/schema expansion.**
- **No change to any frozen path or to ADR-0047** — strictly additive.

---

> **Objective restated:** replace the Mock destination with the first production-quality
> destination while leaving the architecture unchanged. On approval, implementation proceeds
> in the §9 order on a feature branch, holding at the `-dev` version; the full ephemeral-
> Postgres Stage 14 gate must pass before release review, followed by the normal
> `-dev` → release review → finalize → tag workflow.
