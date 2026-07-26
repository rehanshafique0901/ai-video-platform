# α8.6a Grounding — Account Connections (Publishing Credential Ownership)

> **Type:** Grounding (read-only facts). **No code, no schema, no baseline change.**
> Establishes the facts the α8.6a pre-flight will build on.
>
> **The one question:** *How does the platform safely connect a user's external
> destination account?* — and nothing else.
>
> **Explicitly out of scope (later increments):** publish execution, uploading,
> scheduling, caption/hashtag generation, destination APIs. Those are α8.6b/c.
>
> **Governed by:** `PUBLISHING_RUNTIME_CONTRACT.md` (PUB-3/4/5/9/10) and **ADR-0047**
> (credential ownership — boundaries C1–C8, rulings R1–R4). **Approved direction:**
> envelope encryption with an externally-managed master key.

---

## 0. The boundary being proven

α8.6a proves the platform can **safely hold and use a user-owned external identity** —
before any social upload capability exists. The milestone is no longer "prove the media
pipeline"; it is "prove we can store, encrypt, refresh, and revoke a creator's
third-party account credential without that secret ever leaking into adapters, logs,
events, or API responses."

```
User → OAuth consent → SocialAccount (profile + status)
                            │  credential_reference (opaque handle)
                            ▼
                     Credential service  ── decrypt/refresh (sole authority) ──▶ AuthorizedContext
                            │
                            ▼
                   Encrypted credential store  ── ciphertext + wrapped DEK ──▶ external master key
```

---

## 1. Existing identity boundary (Q1)

**Authentication flow (facts).** Bearer JWT → `get_current_user`
(`backend/app/api/v1/deps.py`) verifies the token via the `pyjwt`-backed token issuer
(signing key `Settings.jwt_secret`, a `SecretStr`, HS256), checks **session liveness**
and **user liveness**, and returns a live `User` domain entity
(`backend/app/domain/identity/user.py`). Password auth lives at `/api/v1/auth/*`
(`backend/app/api/v1/routers/auth.py`).

**Ownership scoping (facts).** Three patterns exist and will be mixed by publishing:
- tenant + owner: `projects.get_owned(id, tenant_id, owner_user_id)`,
  `media.get_owned(...)` (`backend/app/infrastructure/repositories/*_repository.py`);
- user-only: notifications filter `user_id = :uid`
  (`backend/app/infrastructure/repositories/notification_repository.py`);
- **anti-enumeration:** a foreign/missing id is an indistinguishable **404** (W8.5b.8).

**`oauth_identities` (facts).** The model exists
(`backend/app/infrastructure/db/models/identity.py`) and links `(provider, subject) →
user_id` with `UNIQUE(provider, subject)` and `UNIQUE(user_id, provider)`. It carries
**no tokens**, and **no repository, use case, or endpoint** references it — it is a
schema-only SSO-login seam from the identity baseline.

**Conclusion — login identity ≠ publishing destination identity.** They share the word
"OAuth" but are **different aggregates with different lifecycles**:

| | `oauth_identities` (login) | `SocialAccount` (publishing) |
|---|---|---|
| Answers | "Which SSO identity *is* this user?" | "Which external channel may this user publish to, with what credential?" |
| Secret held | none (identity assertion only) | an access/refresh **token we must store, refresh, revoke** |
| Lifecycle | permanent identity link | connect / expire / refresh / revoke |
| Cardinality | one per `(provider, subject)` | **multiple** per `(user, platform)` (contract Q3) |

`oauth_identities` is **not** reused (ADR-0047 C1). Publishing introduces its own
`SocialAccount` aggregate.

---

## 2. Credential storage boundary (Q2)

**Fact: there is no encryption-at-rest layer today.** Every secret so far is a
**process-level platform** secret — a `SecretStr` in `Settings`
(`backend/app/core/config.py`: `openai_api_key`, `fal_api_key`, `s3_secret_access_key`,
`jwt_secret`) injected into a pre-authenticated client at the composition root and never
persisted. No user-owned secret is stored anywhere. The `cryptography` package is
already present **transitively** via `pyjwt[crypto]`; α8.6a should declare it as a
**direct** dependency rather than lean on the transitive edge.

**Mapping (per ADR-0047 R1 — secret stored separately from profile):**

```
social_accounts (non-secret)          social_credentials (encrypted)
  id, user_id, platform,        ──▶     credential_reference (FK/handle),
  external_account_id,                   ciphertext(access+refresh),
  display_name,                          wrapped_dek, key_version, algorithm,
  credential_reference,                  access_token_expires_at (non-secret),
  status, timestamps                     rotated_at, timestamps
  UNIQUE(user_id, platform, external_account_id)
```

**Facts to fix in pre-flight (directional now):**
- **Own table for credentials — yes** (R1); profile reads never touch ciphertext.
- **Access + refresh tokens share the credential row** (both inside one encrypted
  payload); rotation replaces them atomically.
- **Rotation metadata** lives on the credential row: `wrapped_dek`, `key_version`,
  `algorithm`, `rotated_at` (enables key rotation without re-consent — C2/R2).
- **Expiry tracking:** a **non-secret** `access_token_expires_at` drives proactive
  refresh (the timestamp is not a secret; the token is).
- **Revocation state** lives on `social_accounts.status ∈ {connected, expired,
  revoked}`; on revoke the credential row is deleted/tombstoned (C6).

**Persistence style — ORM + Unit of Work** (owner CRUD + in-place updates + encryption
columns), matching `export_jobs` / `notifications`, **not** the raw-SQL/ORM-less
convention (that is for seeded catalogues + execution ledgers). New migration **`0013`**
(latest is `0012_execution_runtime`).

---

## 3. Port boundary (Q3)

**Convention (facts).** Ports are ABCs/Protocols with neutral DTOs in
`backend/app/application/interfaces/*.py`; adapters implement them in
`backend/app/infrastructure/…`; wiring happens only in `backend/app/core/container.py`.

**Proposed `ISocialCredentialStore` shape** (signatures finalised in pre-flight). The
governing rule (ADR-0047 C3/C4): callers ask for **authorized access**, never for
decryption.

```
connect(social_account, granted_tokens) -> credential_reference     # stores encrypted
authorize(social_account | credential_reference) -> AuthorizedContext  # refreshes if near expiry
revoke(credential_reference) -> None
```

- `AuthorizedContext` is a short-lived, ready-to-use auth context (e.g. a bearer header
  / pre-authenticated client) — this is exactly what an α8.6c destination adapter
  receives, keeping it **credential-blind** (PUB-5 / C4).
- The credential service is the **sole** module that decrypts (C7). Domain/application
  code never sees a plaintext token; the API never returns one (C8).
- The store is **publishing-scoped** — see §6 (no generic secrets service).

---

## 4. Configuration path (Q4)

**Facts.** `Settings` (`config.py`, `get_settings()` `@lru_cache`) declares secrets as
`SecretStr`; `container.init(settings)` builds pre-authenticated clients from them and
injects them (W8.1.1); optional keys degrade gracefully (e.g. absent `openai_api_key`
→ mock provider). Tests substitute settings via `Settings(...)` overrides / fixtures.

**Master-key entry (directional, per ADR-0047 R2 envelope model):**
- A new **publishing-scoped** setting — e.g. `publishing_credential_master_key:
  SecretStr | None` (or a KMS key id + region for a managed backend).
- **Envelope model:** the externally-managed master key wraps a per-record data
  encryption key (DEK); the DB stores only `ciphertext + wrapped_dek + key_version` —
  the master key is **never** in the database (C2).
- **Injection:** `container` builds the credential service with the master key (or KMS
  client) and injects it as a composition-root singleton; adapters get an
  `AuthorizedContext` from the service, **never** the key.
- **Tests:** a deterministic local master key (or fake KMS) via settings override.
- **Production is fail-closed:** if the master key is absent, publishing is **disabled**
  — there is **no plaintext fallback** (contrast the optional-provider-key pattern,
  which falls back to a mock; credentials must never fall back to plaintext).

---

## 5. Enforcement (Q5)

**Existing templates (facts, `backend/pyproject.toml` `[tool.importlinter]`):** the
domain-isolation contract (`app.domain` ⇏ infra/app/api), the **provider-capability
leaf** contract (`app.infrastructure.ai.providers` ⇏ orchestration/api/workflow), and
the **cloud-SDK confinement** contract (`boto3/botocore` confined to storage/delivery
adapters). These are the exact patterns to reuse.

**Proposed α8.6a guards (documentation + implementation + enforcement, per invariant):**
1. **Credential-store / crypto confinement** (model on the boto3 contract): the
   `cryptography` (and any KMS SDK) imports are confined to
   `app.infrastructure.publishing.credentials`; forbidden from `app.domain`,
   `app.application`, `app.api`, `app.core`, and other infra leaves.
2. **Domain purity for publishing** (already covered by the domain contract, made
   explicit): `app.domain.publishing` ⇏ infra/app/api and ⇏ the AI
   `providers`/`resolver` packages (PUB-3/PUB-4).
3. **Destination-adapter leaf** (lands with α8.6c; noted now): destination adapters
   must **not** import the credential store package — they receive an injected
   `AuthorizedContext` (PUB-5 / C4).
4. **Tests:** owner-scoping (foreign `SocialAccount` id → 404); `connect` persists
   **only ciphertext** (no plaintext token in the row); a `revoked`/`expired` account
   yields **no** `AuthorizedContext`; and no token appears in logs/events/API responses
   (C8).

---

## 6. Caution — no generic "Secrets Service" (honoured)

α8.6a stays **publishing credential ownership** only. It does **not** introduce a
platform-wide `SecretsManager` over AI keys + everything else. Concretely: the
`ISocialCredentialStore` port, the master-key setting, and the `social_credentials`
table are all **publishing-scoped**. If the platform later needs a unified secret-
management layer, that is its own architectural decision (its own ADR).

---

## 7. Conclusion & what the α8.6a pre-flight will decide

**Confirmed facts:**
- Login identity (`oauth_identities`, token-less SSO) ≠ publishing destination identity
  (`SocialAccount` + stored, refreshable, revocable token) — separate aggregates; do
  not reuse `oauth_identities`.
- No encryption-at-rest layer exists; α8.6a must add one, **publishing-scoped**
  (envelope encryption, externally-managed master key; `cryptography` promoted to a
  direct dependency).
- Storage is **ORM + UoW**, split profile vs. secret (R1), new migration `0013`.
- A dedicated credential service is the **sole decryptor**; callers request authorized
  access, never decryption; adapters are credential-blind.
- Enforcement reuses the existing import-linter leaf/confinement patterns + targeted
  tests.

**The α8.6a pre-flight will then fix:** the exact column shapes for `social_accounts` /
`social_credentials`; the `ISocialCredentialStore` signatures + the `AuthorizedContext`
type; the concrete envelope implementation (`cryptography` AES-GCM with a KMS-wrapped
DEK vs. a local dev key) and the config field names + fail-closed behaviour; the OAuth
connect / callback / revoke endpoint shapes; and the precise import-linter contracts +
test list.
