# ADR-0047 — Publishing Owns User-Provided Destination Credentials; Adapters Never Touch Them

**Status:** Accepted (governance boundary). Unlike **ADR-0046**, which shipped *with*
its implementation, this ADR is governance that **precedes** implementation — like
**ADR-0044/0045** — because it defines a **new security boundary before any credential
code exists**. It must be **Accepted before α8.6a** (Account Connections) begins. The
implementation (the `social_accounts` / `social_credentials` tables, the credential
service, and the OAuth connect/refresh/revoke flow) lands in α8.6a and cites this ADR.

**The inflection point.** Every secret the platform has held so far is a **platform**
secret (an `OPENAI_API_KEY`, a `FAL_API_KEY`, S3/R2 keys) — injected into a
pre-authenticated client at the composition root and never fetched by an adapter
(**W8.1.1**, ADR-0041). α8.6 Publishing introduces the platform's **first user-owned
external credentials**: a creator's YouTube / TikTok / Instagram OAuth tokens. That is
a genuine boundary move — the platform now stores, encrypts, refreshes, and revokes
secrets *on behalf of a user* — so it earns its own ADR rather than being folded into
the publishing runtime contract.

```
 Publishing domain        owns INTENT
   (SocialAccount,          — references a credential by handle, never by value
    PublishJob)                     │
                                    ▼
 Credential service       owns SECRETS
   (ISocialCredentialStore) — storage · encryption-at-rest · refresh · revoke · audit
                                    │  hands out a usable, pre-authenticated client
                                    ▼
 Destination adapter      owns the PLATFORM API CALL
   (YouTube / TikTok / …)   — credential-blind: never fetches, decrypts, or stores tokens
```

**Builds on:** **ADR-0041** (W8.1.1 configuration-blind adapters — this ADR extends
that principle from platform secrets to *user-scoped* secrets), **ADR-0042** (the
orchestration freeze — publishing is additive), **ADR-0044** (X-C: publishing is a
separate bounded context after the runtime), **ADR-0046** (X8: the generation domain
and the content/platform domain stay decoupled), and the
`PUBLISHING_RUNTIME_CONTRACT.md` (PUB-5 credential-blind adapters, PUB-10 "credential
ownership requires ADR-0047").

---

## The frozen boundaries (C1–C8)

These are governance a future slice may **not** cross without its own ADR. They are the
credential-plane counterpart to ADR-0046's X1–X8, and they make PUB-5/PUB-10
enforceable.

- **C1 — Publishing owns user-provided destination credentials.** No other bounded
  context stores or reads a third-party account's tokens. This is **distinct** from
  identity login OAuth: the existing `oauth_identities` table links `(provider,
  subject) → user_id` and holds **no tokens**; it is not reused for destination
  credentials.
- **C2 — Credentials are encrypted at rest, always.** Access and refresh tokens are
  **never** persisted in plaintext. Encryption-at-rest is mandatory and the key lives
  **outside** the credential row (external key management), so a database dump alone
  cannot yield usable tokens.
- **C3 — A dedicated credential service owns the token lifecycle.** Store / retrieve /
  refresh / revoke live behind a single port (`ISocialCredentialStore`). The publishing
  domain and API reference a credential by an opaque handle (`credential_reference`),
  **never** by value.
- **C4 — Destination adapters are credential-blind.** An adapter receives a usable,
  pre-authenticated client/context at call time; it **never** fetches secrets, decrypts
  tokens, persists tokens, or decides storage. *Adapters publish content; they do not
  own credential management.* (Restates PUB-5; generalises W8.1.1 to user-scoped
  secrets.)
- **C5 — Refresh is the credential service's responsibility.** Expiry is detected and
  tokens refreshed centrally — never by an adapter or the worker inline. A refresh that
  cannot succeed surfaces to the runtime as a **permanent** publish failure
  (expired/revoked class, per the contract's Q4 taxonomy), not a retryable one.
- **C6 — Revocation is explicit and observable.** User disconnect, provider-side
  revocation, and hard expiry all move the owning `SocialAccount` to a non-`connected`
  state (`revoked` / `expired`); an in-flight `PublishJob` bound to a revoked credential
  fails **permanently** — never a silent retry against a dead token.
- **C7 — Access is least-privilege and audited.** Only the credential service may cause
  a decryption, and only to mint a short-lived pre-authenticated client for a specific
  publish attempt; who/when is recorded. There is **no** general read path that returns
  plaintext tokens.
- **C8 — Credentials never leave the boundary.** Tokens appear in **no** logs, outbox
  event payloads (events carry `credential_reference` / ids, never secrets), API
  responses, or error messages.

---

## Design rulings frozen by this ADR

Decided forks — now boundaries, not preferences. Column-level shapes and the concrete
crypto library are implementation details for α8.6a, but the following are fixed:

- **R1 — Secret is stored separately from profile.** `social_accounts` holds
  **non-secret** metadata (`user_id`, `platform`, `external_account_id`,
  `display_name`, `credential_reference`, `status`, timestamps); `social_credentials`
  holds the **encrypted** tokens. Rationale: an account can persist in an
  `expired`/`revoked` state without a live token; encryption and rotation are localised
  to one table; and profile reads never touch ciphertext.
- **R2 — Envelope encryption with externally-managed keys (default).** The recommended
  mechanism is application-level envelope encryption — a per-record data key sealed by a
  KMS/root key — or an equivalent managed secrets store. Whatever α8.6a chooses **must**
  satisfy C2/C7: the key is **not** in the database, is **rotatable**, and decryption is
  **centralised** in the credential service. Committing keys to the repo or config is
  forbidden.
- **R3 — Refresh + revocation ownership is the credential service's** (C5/C6); the
  publishing runtime only observes the resulting `SocialAccount.status`.
- **R4 — Multiple credentials per `(user, platform)`** are supported (contract Q3):
  uniqueness is `(user_id, platform, external_account_id)`, and each `SocialAccount`
  points at exactly one `credential_reference`.

---

## What this ADR is *not*

It freezes the **credential-ownership boundary**, not features. The concrete OAuth
connect/callback UX, the specific crypto library, per-provider scope sets, token-storage
column layout, and a future general secrets-management platform are all implementation
or later decisions. Deferred by design (not forbidden): key-rotation automation,
hardware-backed keys/HSM, per-tenant key isolation, and a shared credential service for
non-publishing integrations — each its own slice if it ever arrives.

---

## Consequences

- **Positive.** The platform gains a single, audited place where user secrets live,
  encrypted, with centralised refresh/revoke. Destination adapters become trivially
  testable and impossible to misuse (they only ever see a ready client — C4). Splitting
  account profile from secret (R1) lets an account be revoked/expired without losing its
  identity, and lets every future destination reuse the same service. The boundary makes
  PUB-5/PUB-10 mechanically checkable.
- **Cost.** A new encryption/key-management dependency and the operational burden of key
  storage + rotation; one extra indirection (the credential service) between the worker
  and the platform API; and the discipline that tokens must be scrubbed from logs,
  events, and errors (C8) — verified by tests, not convention.

---

## Change log

| Date | Change |
| --- | --- |
| 2026-07-26 | Accepted (governance). Freezes the publishing credential-ownership boundary (C1–C8) and rulings R1–R4 ahead of α8.6a. Extends W8.1.1 from platform secrets to user-owned destination credentials; makes `PUBLISHING_RUNTIME_CONTRACT.md` PUB-5/PUB-10 enforceable. Implementation (`social_accounts` / `social_credentials`, the credential service, OAuth connect/refresh/revoke) lands in α8.6a and cites this ADR. |
