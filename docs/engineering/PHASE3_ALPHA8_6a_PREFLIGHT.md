# Phase 3 — α8.6a Pre-flight: Account Connections (Publishing Credential Ownership)

> **Status: DRAFT — awaiting sign-off.** First increment of the **α8.6 Publishing /
> Creator Workflow** bounded context. Input: `PHASE3_ALPHA8_6a_GROUNDING.md` (APPROVED,
> PR #32). Governing artifacts: `PUBLISHING_RUNTIME_CONTRACT.md` (PUB-1…PUB-10) and
> **ADR-0047** (credential ownership — C1–C8, R1–R4). Baseline: `v0.4.35-phase3-alpha8.7`.
>
> **The one question α8.6a answers:** *How does the platform safely connect a user's
> external destination account?* — store, refresh, and revoke a user-owned OAuth
> credential behind an encryption boundary. **Not** publish execution, uploading,
> scheduling, captions, or the destination upload API (α8.6b / α8.6c).
>
> **Locked decisions (from grounding sign-off):** `oauth_identities` is not reused ·
> `SocialAccount` is a new aggregate · credentials are publishing-owned · envelope
> encryption required · **fail-closed** behaviour required · **no** generic
> `SecretsManager` · **AES-256-GCM + wrapped DEK** approved · `cryptography` becomes a
> **direct** dependency · deterministic dev/test key allowed **outside production only**,
> **no auto-generation in production**.

---

## 0. Gates (answered first)

### Gate 1 — ADR-0042 (orchestration platform freeze)
> Does α8.6a touch any frozen orchestration module, checkpoint contract, orchestration
> state, provider protocol, or workflow lifecycle?

**No.** α8.6a adds a **new bounded context** (`domain/publishing`, publishing use cases,
`infrastructure/publishing/**`, two new tables, one router). It reads nothing from and
writes nothing to the workflow/generation runtime. Freeze guard stays green, **zero
overrides**.

### Gate 2 — ADR-0047 (credential ownership boundary)
> Does the design conform to the frozen boundaries C1–C8 and rulings R1–R4?

**Yes** — mapped explicitly in §11. The credential service is the sole decryptor (C7);
adapters are credential-blind (C4); secret is stored separately from profile (R1);
envelope encryption with an externally-managed master key (R2); refresh/revocation are
the service's responsibility (R3); multiple credentials per `(user, platform)` (R4).

---

## 1. Positioning (what α8.6a *is* / *is not*)

α8.6a proves the platform can **hold and use a user-owned external identity** — before
any upload capability exists. It ships the connection lifecycle end-to-end against a
**Mock** OAuth client so the credential boundary is provable in CI without a real
destination API.

```
POST /social-accounts/connect ──▶ authorization_url (+ signed state)
                                        │  user consents at provider
GET  /social-accounts/callback ◀────────┘  ?state&code
        │ verify state (CSRF)  → exchange_code → GrantedTokens
        ▼
   Credential service  ── AES-256-GCM(DEK) · wrap(DEK, master key) ──▶ social_credentials
        │ (sole decryptor)
        ▼
   SocialAccount (profile + status)                         social_accounts
```

**In scope:** `SocialAccount` aggregate; `social_accounts` + `social_credentials`
tables (migration 0013); the credential service + envelope crypto adapter;
`ISocialCredentialStore` + `AuthorizedContext`; an `ISocialOAuthClient` port with a
**Mock** implementation; connect / callback / revoke / list endpoints; config + wiring;
import-linter contracts + tests.

**Out of scope (later):** `PublishJob`/worker (α8.6b); real YouTube OAuth client +
upload adapter (α8.6c); caption/metadata generation; scheduling; a destination
catalogue (deferred until ≥2 real destinations — contract Q1).

---

## 2. Data model (migration 0013)

Two tables. **Profile and secret are separate** (R1). Raw-SQL DDL like `0012`, **with**
matching ORM models (`app/infrastructure/db/models/publishing.py`) — these are *not*
ORM-less, so they are **not** allowlisted in `validate_schema.py`; the schema validator
must see the ORM models.

### `social_accounts` (non-secret profile + status)

| column | type | notes |
|---|---|---|
| `id` | `uuid` PK | `gen_random_uuid()` |
| `tenant_id` | `uuid` NOT NULL | FK `tenants(id)` ON DELETE RESTRICT (scoping parity with projects) |
| `user_id` | `uuid` NOT NULL | FK `users(id)` ON DELETE CASCADE |
| `platform` | `text` NOT NULL | `youtube` \| `mock`; **text, not enum** (grows per destination — OQ2) |
| `external_account_id` | `text` NOT NULL | id on the platform (e.g. channel id) |
| `display_name` | `text` | non-secret (e.g. channel title) |
| `status` | `social_account_status` NOT NULL | enum `connected` \| `expired` \| `revoked`, default `connected` |
| `scopes` | `text[]` NOT NULL | granted scopes (non-secret), default `'{}'` |
| `connected_at` | `timestamptz` | |
| `revoked_at` | `timestamptz` | |
| `created_at` / `updated_at` | `timestamptz` NOT NULL | `now()` |

- `UNIQUE (user_id, platform, external_account_id)` — R4 / contract Q3 (multiple
  accounts per `(user, platform)`; reconnect updates the same row).
- Indexes: `ix_social_accounts_user_id`, `ix_social_accounts_tenant_id`.

### `social_credentials` (encrypted secret — 1:1 with account)

| column | type | notes |
|---|---|---|
| `id` | `uuid` PK | |
| `social_account_id` | `uuid` NOT NULL **UNIQUE** | FK `social_accounts(id)` ON DELETE CASCADE (1:1) |
| `ciphertext` | `bytea` NOT NULL | AES-256-GCM of the token bundle `{access, refresh}` (GCM tag appended) |
| `nonce` | `bytea` NOT NULL | per-encryption 96-bit nonce (uniqueness invariant) |
| `wrapped_dek` | `bytea` NOT NULL | per-record DEK wrapped by the master key |
| `key_version` | `text` NOT NULL | master-key id/version — drives rotation |
| `algorithm` | `text` NOT NULL | `AES-256-GCM`, default |
| `access_token_expires_at` | `timestamptz` | **non-secret** — drives proactive refresh |
| `rotated_at` | `timestamptz` | last re-encrypt/refresh |
| `created_at` / `updated_at` | `timestamptz` NOT NULL | |

**Invariant:** the database never contains a plaintext access or refresh token, nor any
token material decryptable without the application's master-key boundary (ADR-0047 C1/C2).
`credential_reference` is simply `social_account_id` (opaque to callers).

---

## 3. Service boundary & port

The **key rule** (C3/C4): callers ask for *authorized access for a `SocialAccount`* —
never "decrypt this token." Port lives at
`app/application/interfaces/social_credential_store.py` (ABC + frozen dataclasses +
neutral errors, matching `exporter.py` style).

```python
@dataclass(frozen=True, slots=True)
class GrantedTokens:            # input at connect/refresh (from the OAuth exchange)
    access_token: str
    refresh_token: str | None
    expires_at: datetime | None
    scopes: tuple[str, ...]

@dataclass(frozen=True, slots=True)
class AuthorizedContext:        # what an α8.6c adapter receives — never the refresh token, never key material
    access_token: str           # short-lived, already refreshed if it was near expiry
    expires_at: datetime | None
    scopes: tuple[str, ...]

class CredentialUnavailableError(Exception): ...   # revoked / expired-unrefreshable / missing → fail closed
class CredentialDecryptionError(Exception): ...    # tamper / wrong key (GCM integrity)

class ISocialCredentialStore(ABC):
    async def store(self, social_account_id: UUID, tokens: GrantedTokens) -> None: ...
    async def authorize(self, social_account_id: UUID) -> AuthorizedContext: ...
    async def revoke(self, social_account_id: UUID) -> None: ...
```

- The concrete **credential service**
  (`app/infrastructure/publishing/credentials/credential_service.py`) is the **only**
  module that decrypts (C7). `authorize()` refreshes when within an expiry skew (using
  the injected `ISocialOAuthClient`), re-encrypts, and returns a fresh
  `AuthorizedContext`; a `revoked`/`expired`-and-unrefreshable/missing credential raises
  `CredentialUnavailableError` (**fail closed** — no plaintext, no silent degrade).
- `AuthorizedContext` carries the minimal bearer only. (Hardening option, OQ4: expose
  `apply(request)` instead of the raw `access_token` so adapters can't even read it.)

### OAuth client port (connection mechanism, not the upload API)

Connecting inherently needs authorization-url + code-exchange + refresh + revoke. That is
a **credential-acquisition** concern, distinct from the upload API. α8.6a defines the port
and ships a **Mock**; the real **YouTube** OAuth client lands with its upload adapter in
α8.6c (OQ1).

```python
@dataclass(frozen=True, slots=True)
class OAuthGrant:
    external_account_id: str
    display_name: str | None
    tokens: GrantedTokens

class ISocialOAuthClient(ABC):                      # per-platform; configuration-blind (W8.1.1)
    def authorization_url(self, *, state: str, redirect_uri: str) -> str: ...
    async def exchange_code(self, *, code: str, redirect_uri: str) -> OAuthGrant: ...
    async def refresh(self, *, refresh_token: str) -> GrantedTokens: ...
    async def revoke(self, *, token: str) -> None: ...
```

---

## 4. Envelope encryption (behind the credential adapter)

No custom crypto — use `cryptography`'s AEAD primitives only, confined to
`app/infrastructure/publishing/credentials/envelope.py`.

```
master key (externally provided, never in DB)
   │ wrap
   ▼
per-record DEK (random 256-bit)            ── AES-KeyWrap / AES-GCM ──▶ wrapped_dek
   │ AES-256-GCM(nonce)
   ▼
{access_token, refresh_token} (JSON)       ──────────────────────────▶ ciphertext (+tag)
```

- **AES-256-GCM** for token encryption: authenticated (integrity), random 96-bit nonce
  per write (uniqueness), tamper → `CredentialDecryptionError`.
- **Per-record DEK** wrapped by the master key; `key_version` recorded → rotation =
  re-wrap DEK (or re-encrypt) without re-consent (R2).
- **Master key provider** is pluggable behind a tiny `IMasterKeyProvider` seam: α8.6a
  ships an **env-injected** master key (managed externally by the deployment's secret
  store); a cloud-KMS provider (where the key never leaves KMS) is a future swap, **not**
  α8.6a (OQ5 — keeps scope tight and avoids adding a KMS SDK now).

---

## 5. Configuration path & fail-closed

`app/core/config.py` → `app/core/container.py` → credential service (mirrors the
injected-secret pattern; W8.1.1). New **publishing-scoped** settings:

```python
publishing_credential_master_key: SecretStr | None   # externally managed; None allowed only in non-prod
publishing_credential_key_version: str = "v1"
publishing_oauth_redirect_base_url: str               # for the callback redirect_uri
```

- **Production is fail-closed (C2, approved):** if `environment == "prod"` and the master
  key is absent, **container init raises** (publishing unavailable). There is **no
  auto-generation** and **no plaintext fallback**.
- **Dev/test:** a deterministic injected key is allowed (`environment != "prod"`); tests
  substitute it via `Settings(...)` overrides / fixtures.
- The container builds the credential service (master key + OAuth clients + repos) as a
  composition-root singleton; adapters receive an `AuthorizedContext`, never the key.

---

## 6. Aggregate, repository & use cases

- **Domain:** `app/domain/publishing/social_account.py` — `SocialAccount` aggregate +
  `AccountStatus` enum (`connected`/`expired`/`revoked`). Pure; no infra imports.
- **Repository:** `ISocialAccountRepository` added to `repositories.py` and exposed on
  `IUnitOfWork` as `uow.social_accounts` (ORM repo in
  `infrastructure/repositories/social_account_repository.py`); owner-scoped `get_owned`
  with the uniform **404** anti-enumeration behaviour.
- **Use cases** (`app/application/use_cases/publishing/`):
  - `StartSocialConnection(user, platform)` → returns `authorization_url`. Connection
    state is a **signed, short-lived state token** (HMAC/JWT carrying `user_id`,
    `platform`, `nonce`, `exp`) — CSRF-safe and stateless, **no extra table** (OQ3).
  - `CompleteSocialConnection(state, code)` → verify state → `exchange_code` →
    upsert `SocialAccount` profile → `credential_store.store(...)`; returns the account
    (**never** tokens).
  - `RevokeSocialAccount(user, account_id)` → owner check → OAuth `revoke` →
    `credential_store.revoke(...)` → `status = revoked`.

---

## 7. Endpoints (`/api/v1/social-accounts`)

Follows existing router conventions (bearer auth via `get_current_user`, owner-scoped
404). The **callback** is the provider's browser redirect target, so it authenticates via
the **signed `state`** (not a bearer), which also carries the acting `user_id`.

| method + path | auth | effect |
|---|---|---|
| `POST /social-accounts/connect` | bearer | body `{platform}` → `{authorization_url}` |
| `GET /social-accounts/callback` | `state` token | `?state&code` → exchange + encrypt/store → connected account (no tokens in body) |
| `POST /social-accounts/{id}/revoke` | bearer + owner | invalidate + `status=revoked` → `204` |
| `GET /social-accounts` | bearer | list the caller's connected accounts (profile only) |

---

## 8. Enforcement (import-linter + tests)

**Import-linter contracts** (modelled on the existing boto3-confinement + provider-leaf
contracts):

1. **Crypto confinement** — `cryptography` is importable **only** inside
   `app.infrastructure.publishing.credentials`. `forbidden_modules = ["cryptography"]`
   from `app.domain`, `app.application`, `app.api`, `app.core`, and
   `app.infrastructure.publishing.destinations` (indirect via the composition root
   allowed).
2. **Publishing bounded-context purity** — `app.domain.publishing` forbidden from
   `app.domain.generation` and `app.domain.workflow` (PUB-3/PUB-4).
3. **Destination-adapter leaf** *(declared with α8.6c, noted now)* —
   `app.infrastructure.publishing.destinations` must not import
   `app.infrastructure.publishing.credentials` (adapters receive an injected
   `AuthorizedContext` — PUB-5/C4).

**Tests:**
- *Crypto:* round-trip encrypt/decrypt; ciphertext ≠ plaintext; tamper → integrity error;
  wrong/rotated key handling; nonce differs across two writes.
- *Store:* after `store()`, the row contains **no** plaintext token bytes (only
  `ciphertext`/`nonce`/`wrapped_dek`).
- *Authorize:* near-expiry triggers refresh (mock OAuth → new token → row updated);
  `revoked`/`expired`-unrefreshable/missing → `CredentialUnavailableError` (**fail
  closed**).
- *Config:* `environment=prod` + missing master key → container init raises; dev key
  allowed in non-prod; **no** auto-generation.
- *Ownership:* foreign `account_id` → **404** on revoke.
- *Callback:* forged/expired/tampered `state` → rejected; valid → account connected,
  **no tokens** in the response.
- *No leakage:* connect/authorize emit no token in structured logs or outbox events (C8).
- *Fitness:* `import-linter` green.

---

## 9. Migration plan (0013)

`0013_social_accounts.py` (`down_revision = "0012_execution_runtime"`), raw-SQL DDL like
0012: `CREATE TYPE social_account_status`; `CREATE TABLE social_accounts`;
`CREATE TABLE social_credentials`; indexes + constraints; `downgrade` drops both tables +
the enum so the ci_gate upgrade→downgrade→upgrade roundtrip stays clean. Purely
**additive**. ORM models added in the same slice so `validate_schema.py` matches (these
tables are **not** allowlisted as ORM-less).

---

## 10. Increment & CI

Single feature branch `feat/alpha8.6a-account-connections`, committed at `-dev`, then a
release review. CI: existing stages 1–13 stay green; the new unit/integration tests run
in the standard suites (crypto + store + use-case units; router + repository integration
against the live/ephemeral Postgres). No new CI stage is required for α8.6a (Stage 14 is
reserved for the publish runtime in α8.6b per the contract).

---

## 11. Invariant conformance map

| Rule | Where honoured |
|---|---|
| **PUB-3** separate bounded context | new `domain/publishing`, `infrastructure/publishing/**`; purity contract (§8.2) |
| **PUB-5** adapters credential-blind | `AuthorizedContext` only; destination-leaf contract (§8.3) |
| **PUB-10 / C1** publishing owns credentials, encrypted at rest | `social_credentials`, envelope crypto (§2, §4) |
| **C2** encrypted at rest, master key never in DB | wrapped DEK + externally-provided key; fail-closed (§4, §5) |
| **C3** dedicated credential service owns lifecycle | `ISocialCredentialStore` + service (§3) |
| **C4** adapters credential-blind | `AuthorizedContext`; no store import from destinations (§3, §8.3) |
| **C5 / R3** refresh & revoke are the service's job | `authorize()` refresh + `revoke()` (§3, §6) |
| **C6** explicit revocation | `RevokeSocialAccount` + `status=revoked` (§6, §7) |
| **C7** service is sole decryptor | crypto-confinement contract (§8.1) |
| **C8** credentials never leave the boundary | no-leakage tests; no tokens in responses/logs/events (§7, §8) |
| **R1** secret stored separately from profile | two tables (§2) |
| **R2** envelope encryption, externally-managed key | §4 |
| **R4** multiple credentials per (user, platform) | `UNIQUE(user_id, platform, external_account_id)` (§2) |

---

## 12. Open questions for sign-off

| # | Question | Recommendation |
|---|---|---|
| **OQ1** | OAuth client in α8.6a? | **Yes — ship `ISocialOAuthClient` + a Mock now; real YouTube OAuth client with its upload adapter in α8.6c.** Proves the boundary end-to-end without a real destination API. |
| **OQ2** | `platform` as `text` vs pg enum? | **`text` + app-level validation** (grows per destination; avoids an enum migration each time). `status` stays a pg enum (small, stable). |
| **OQ3** | Connection state: signed stateless token vs a `pending_connections` table? | **Signed short-lived state token** (reuse the JWT boundary) — CSRF-safe, no extra table. |
| **OQ4** | `AuthorizedContext` exposes `access_token` vs `apply(request)`? | **Expose `access_token` now**; keep `apply()` as a later hardening option. |
| **OQ5** | Master key: env-injected now vs a cloud-KMS SDK now? | **Env-injected master key behind `IMasterKeyProvider` now** (still a correct envelope scheme); a real KMS provider is a future swap, not α8.6a — avoids adding a KMS SDK and keeps scope on "publishing credential ownership." |
