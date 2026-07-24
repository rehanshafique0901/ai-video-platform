# Phase 3 — α8.5b.2 Pre-flight: Storage Backends & Signed-URL Delivery

> Status: **SIGNED OFF.** Second slice of the **distribution** stage. Input:
> `PHASE3_ALPHA8_5b2_GROUNDING.md`. Companions: `PHASE3_ALPHA8_5b_PREFLIGHT.md` (α8.5b.1, the
> seam this slice completes) and `PHASE3_ALPHA8_5a_PREFLIGHT.md`. Baseline:
> `v0.4.31-phase3-alpha8.5b1`.
>
> **Rulings:** Gate 1 (ADR-0042) PASS · Gate 2 (ADR-0043) PASS · **A — resolver = registry**
> (centralised; the rest of the app stays backend-unaware) · **B — signing lives in the delivery
> adapter** (`S3RedirectDelivery`), **not** on `IObjectStorage` (storage owns persistence;
> delivery owns transport) · **C — backends = Local + S3 + R2 only** (R2 is S3-API-compatible;
> one signing implementation) · **D — the cloud SDK is confined to the adapter layer**
> (import-linter-enforced); no SDK types leak above `StorageResolver` / `IDownloadDelivery` ·
> **E — E2 config-selected active write backend** (`storage_active_backend ∈ {local, s3, r2}`,
> default `local`): new writes go to the active backend, reads/deletes/deliveries **always**
> resolve by the artifact's *persisted* backend; **exactly one** active backend (no preferred/
> fallback/mirror/replication); no backfill/migration · **F — fixed centrally-configured TTL**
> presigned URLs (10–15 min), populate `expires_at`; **no** per-request TTL, **no** CDN/edge
> signing · endpoint identical (`200` stream local / `302` redirect cloud) · **W8.5b.4 +
> W8.5b.5** adopted · **zero migration** · version `0.4.32-phase3-alpha8.5b2`.
>
> **Positioning carried from grounding:** α8.5b.2 **completes an existing abstraction** rather
> than introducing a new one — `RedirectDelivery` already exists and is already exercised by
> tests (`test_redirect_delivery_returns_302`). This slice only needs to *produce* one from a
> cloud adapter, plus centralise backend selection behind a resolver.

---

## 0. Gates (answered first)

### Gate 1 — ADR-0042 (orchestration freeze)
> **Does α8.5b.2 touch any frozen orchestration module, checkpoint contract, orchestration
> state, provider protocol, or workflow lifecycle?**

**Answer: No.** This slice adds cloud `IObjectStorage` adapters, an `S3RedirectDelivery`
`IDownloadDelivery` adapter, a backend-keyed resolver, config, and a dependency. All live in
`infrastructure/storage/**`, `infrastructure/delivery/**`, `interfaces/**`, and the container —
none of which are in `FROZEN_PATHS` (verified). Freeze guard stays green, **zero overrides**.

### Gate 2 — ADR-0043 (render composition boundary)
> **Does α8.5b.2 change how media is composed?**

**No — storage + signing are *below* the render and export boundaries.** A signed-URL redirect
hands byte transfer to the object store; the artifact is never re-encoded, resized, or
recomposed. RC5 (master immutable) / W8.5.3 (deliveries replaceable) hold; W8.5b.2 (pure
transfer) is preserved — a redirect is still a pure transfer.

---

## 1. Positioning (what α8.5b.2 *is*)

α8.5b.1 shipped the delivery **seam** with two decision shapes; only `StreamDelivery` had a
producer. α8.5b.2 makes the platform serve an artifact **according to where it is persisted** —
streaming `local`, redirecting `s3`/`r2` to a short-lived signed URL — with **no change to the
endpoint, use case, router, authorization, or export lookup.**

```
GET …/exports/{id}/download            ← unchanged contract (Ruling E)
   ↓ DownloadExport (unchanged)
MediaAsset.storage_backend             ← the ONLY selection input (W8.5b.4)
   ↓ DeliveryResolver (registry)       ← Ruling A
   ├── local → LocalStreamDelivery → 200 StreamDelivery   (bytes via API)
   └── s3/r2 → S3RedirectDelivery  → 302 RedirectDelivery (presigned URL)
                    ↑ owns presigning (Ruling B); cloud SDK confined here (Ruling D)
```

---

## 2. Grounding recap (what exists / what changes)

- **The redirect path is already built + tested end-to-end** — `RedirectDelivery(url,
  expires_at)` exists; `DownloadExport` returns whatever the seam decides; the router already
  renders it as `302`. α8.5b.2 only supplies a **producer**.
- **Zero migration** — `storage_backend_enum = {local, s3, r2, azure_blob, gcs}` and the
  `MediaAsset` storage triple already exist.
- **Single-instance limitation** — today the container memoises one `LocalObjectStorage` and one
  `LocalStreamDelivery`; `LocalStreamDelivery` rejects any non-local backend. This is what the
  resolver replaces.
- **No signing / no cloud SDK / no cloud config** anywhere yet. S3 presigning via
  `botocore.generate_presigned_url` is **offline** (no network, no async client at request time).

---

## 3. Design forks (for sign-off)

### Fork A — Resolver shape *(RULED: registry)*
- **A1 — registry** *(chosen)*: a `{backend → adapter}` map, populated once at container init,
  looked up by `MediaAsset.storage_backend`. Two registries: **`StorageResolver`**
  (`{backend → IObjectStorage}`, for byte read/write/delete by persisted backend) and
  **`DeliveryResolver`** (`{backend → IDownloadDelivery}`, for the delivery decision).
- A2 — factory (construct per call) / A3 — inline `switch` in the use case: rejected — both
  spread backend knowledge and re-create clients.

**Realisation that preserves endpoint stability:** the `DeliveryResolver` is itself exposed to
`DownloadExport` **as an `IDownloadDelivery`** — a thin resolving facade that dispatches on
`request.storage_backend` (which `DownloadRequest` already carries). **Result: `DownloadExport`
needs no change at all** — it still receives one `IDownloadDelivery` and calls `deliver()`. The
registry lives entirely in the composition root + infrastructure.

### Fork B — Signing ownership *(RULED: delivery adapter)*
- **B1 — the delivery adapter owns signing** *(chosen)*: `S3RedirectDelivery` holds the S3
  client + bucket + TTL and produces a `RedirectDelivery(url, expires_at)`. `IObjectStorage`
  gains **no** `signed_url()` method.
- B2 — `signed_url()` on `IObjectStorage`: rejected — forces `LocalObjectStorage` to implement an
  artificial "unsupported" method and conflates persistence with transport.

Rationale: **storage owns persistence, delivery owns transport.** Local can't sign (no HTTP
endpoint); the `DeliveryDecision` split already models exactly this difference.

### Fork C — Backend scope *(RULED: Local + S3 + R2)*
- **C2 — S3 + R2** *(chosen)*: R2 is S3-API-compatible → **one** `S3ObjectStorage` +
  **one** `S3RedirectDelivery`, differing only by injected endpoint/credentials. Validate the
  architecture once, one signing implementation.
- C1 — S3 only: unnecessarily narrow (R2 is nearly free). C3 — + GCS + Azure: rejected for this
  slice (distinct signing schemes + 2–3 SDKs); each is a clean follow-on behind the same seam.

### Fork D — Dependency boundary *(RULED: SDK confined to adapters)*
- The cloud SDK (`boto3`/`botocore` — S3 + R2) is a new runtime dependency, **imported only in**
  `infrastructure/storage/**` and `infrastructure/delivery/**`. No SDK type appears in
  `interfaces/**`, the resolvers' public surface, or any use case. **Enforced mechanically** by
  a new **import-linter** contract (`boto*` forbidden outside those adapter packages) so the
  boundary can't silently erode. Credentials are **injected** at construction (W8.1.1), never
  fetched. Presigning stays **offline** (no live client call on the request path).

### Fork E — Write-side selection *(RULED: E2 — config-selected active backend)*
The resolver makes **reads/deliveries** correct for any persisted backend. New **writes** still
need a target. Options:
- **E1 — serve-only:** writers keep writing `local`; resolvers only add the ability to *serve*
  cloud-stored artifacts. Tightest, but no cloud artifact is produced in practice, so redirect is
  only exercisable via externally-placed objects — thin real value.
- **E2 — config-selected active backend** *(chosen)*: one setting
  `storage_active_backend ∈ {local, s3, r2}` (default `local`) selects which backend **writers**
  target (via `StorageResolver.active()`); **reads/deletes/deliveries always resolve by the
  artifact's persisted `storage_backend`** (never the active one — W8.5b.4). **Exactly one**
  active write backend — deliberately **no** `preferred`/`fallback`/`mirror`/`replication`
  semantics (operational behaviour stays deterministic). No per-artifact routing, **no
  backfill/migration** of existing `local` rows (W8.5b.5). Makes the capability real end-to-end
  with a single knob.
- **Implementation note (either option):** byte-reads that today assume the single instance
  (`ProcessExportJob._materialize` reads the master; enrichment reads generated media) must
  resolve by the asset's persisted backend via `StorageResolver` so a master on one backend is
  read from the right adapter. This is additive (inject the resolver; resolve `asset.storage_backend`)
  and does not change the render composition boundary.

### Fork F — Signed-URL semantics *(RULED: fixed-TTL, direct presigned URL; CDN deferred)*
- **Expiry:** a fixed, centrally-configured TTL (`download_signed_url_ttl_seconds`, default `900`
  — 10–15 min band) used at presign time; populate `RedirectDelivery.expires_at = now + ttl`
  (the field already exists). **No per-request TTL customization.**
- **Target:** redirect to the **presigned object URL** directly. **No CDN signing / edge-caching
  logic** enters this slice (deferred).

---

## 4. Scope (this slice vs. deferred)

| Slice | Concern | Migration | In this slice? |
|---|---|---|---|
| **α8.5b.2** | **Storage backends + signed-URL delivery** — `StorageResolver` + `DeliveryResolver` (registry); `S3ObjectStorage` (S3+R2) + `S3RedirectDelivery` (presign); config + SDK; config-selected active write backend (E2) | **none** (enum + triple pre-exist) | ✅ **YES** |
| α8.5b.3 | Notification dispatch (`INotifier` + relay subscriber on `ExportJobSucceeded`) | none (table exists) | ❌ deferred |
| α8.6 | **Publishing** — `PublishJob` + `SocialAccount` + destination OAuth (new bounded context) | yes (new tables) | ❌ deferred |
| later | GCS + Azure adapters (distinct signing) | none | ❌ deferred |

**Explicitly excluded from α8.5b.2:** GCS/Azure adapters, CDN integration (beyond an optional
base-URL config), retention/GC/lifecycle policies, per-artifact backend routing, backfill of
existing `local` artifacts, share links / team access / public tokens, notifications, publishing.

---

## 5. Proposed invariant (new) + reaffirmations

- **W8.5b.4 (new) — Delivery selection is derived solely from the artifact's persisted storage
  backend.** The delivery mechanism for an artifact is a pure function of
  `MediaAsset.storage_backend` via the resolver. It is **never** influenced by request headers,
  query/endpoint parameters, feature flags, client preferences, or the *active write* backend.
  The same artifact always delivers the same way — no inconsistent delivery for one object.
- **W8.5b.5 (new) — The active write backend affects only future writes.** Changing
  `storage_active_backend` changes where *new* `MediaAsset`s are persisted and **never** changes
  the location or interpretation of existing `MediaAsset`s. Every existing artifact remains
  readable and deliverable from its own persisted `(storage_backend, storage_bucket,
  storage_key)` forever. Backend changes are operational, not migratory.
- **Reaffirmed:** **W8.5b.1** (download observational/read-only — only writes accounting),
  **W8.5b.2** (pure transfer — a redirect re-encodes nothing), **W8.5b.3** (accounting
  best-effort, isolated, non-blocking), **RC5** (master immutable), **W8.5.3** (deliveries
  replaceable), **W8.1.1** (credentials injected, never fetched).

---

## 6. Migration verdict

**Zero migration.** `storage_backend_enum` already lists `s3`/`r2`; `MediaAsset` already carries
`(storage_backend, storage_bucket, storage_key)`. α8.5b.2 is infrastructure/runtime only:
adapters + resolvers + config + a dependency + DI wiring + tests. No table, column, or enum
change; no backfill (existing `local` artifacts keep streaming).

---

## 7. Test plan

- **`DeliveryResolver` (resolving facade)** — dispatches on `request.storage_backend`: `local`
  → `LocalStreamDelivery` (stream), `s3`/`r2` → `S3RedirectDelivery` (redirect); an unknown/
  unconfigured backend → a clean error mapped to `404`/`409` (no leak). W8.5b.4: identical input
  → identical decision; request headers/params never alter it.
- **`S3RedirectDelivery`** — with a **fake/stub presigner** (no live cloud): produces
  `RedirectDelivery(url, expires_at≈now+ttl)`; presigning is offline (no network on the request
  path); R2 vs S3 differ only by injected endpoint.
- **`S3ObjectStorage`** — put/get/exists/delete against a **stub S3 client** (or opt-in `moto`,
  mirroring the FFmpeg opt-in integration pattern); real-cloud checks stay opt-in/skipped.
- **`DownloadExport` unchanged** — existing α8.5b.1 tests still pass verbatim (endpoint
  stability); add: a cloud-backed `MediaAsset` → `302` with `Location` + `expires_at`; a
  local-backed asset → `200` stream (parity).
- **Router** — same endpoint yields `200` stream (local) or `302` redirect (cloud) with no
  contract change; `401` unauth, `404` foreign — all still via the JSON error envelope.
- **SDK isolation** — the new **import-linter** contract fails if `boto*` is imported outside the
  storage/delivery adapter packages.
- **Full gate** — ruff, black, mypy, import-linter (incl. the new contract), unit; **freeze
  guard green, zero overrides**.

---

## 8. Versioning

Runtime capability → **`0.4.32-phase3-alpha8.5b2`**, tag `v0.4.32-phase3-alpha8.5b2` (mirrors the
dotless token; roadmap concept *α8.5b.2*). Standard two-commit release ritual.

---

## 9. Deliverable (on sign-off)

α8.5b.2 **provides:** backend-aware delivery behind the **unchanged** `GET …/exports/{id}/download`
— `local` streams, `s3`/`r2` redirect to a short-lived presigned URL — via a centralised
**registry resolver** keyed solely on the artifact's persisted `storage_backend` (W8.5b.4), with
signing owned by the **delivery adapter** and the cloud SDK **confined to the adapter layer**
(import-linter-enforced). **Zero migration**, freeze guard green, **zero ADR-0042 overrides**,
RC5/W8.5.3/W8.5b.1–3 preserved.

α8.5b.2 **explicitly excludes:** GCS/Azure, CDN (beyond optional base URL), retention/lifecycle,
per-artifact routing, backfill, share links, notifications, publishing.

> **Crisp definition:** α8.5b.2 completes the α8.5b.1 delivery seam by adding S3/R2 storage +
> signed-URL redirect behind a backend-keyed resolver, so an artifact is served according to
> where it lives — with the **only** observable API difference being *stream vs redirect*.

---

## 10. Sign-off checklist (maps to the rulings)

- [ ] **Gate 1** (ADR-0042) PASS · **Gate 2** (ADR-0043) PASS
- [ ] **Fork A** — registry resolver (`StorageResolver` + `DeliveryResolver`); app stays
      backend-unaware; `DownloadExport` unchanged
- [ ] **Fork B** — signing in `S3RedirectDelivery`; **no** `signed_url()` on `IObjectStorage`
- [ ] **Fork C** — Local + S3 + R2 only (one S3-compatible adapter/signer)
- [ ] **Fork D** — cloud SDK confined to storage/delivery adapters; **import-linter contract** added
- [ ] **Fork E** — E2 config-selected active backend (default `local`); exactly one active
      backend (no preferred/fallback/mirror/replication); no backfill
- [ ] **Fork F** — fixed centrally-configured TTL presigned URL + `expires_at`; no per-request
      TTL; no CDN
- [ ] **W8.5b.4 + W8.5b.5** adopted; W8.5b.1–3 / RC5 / W8.5.3 / W8.1.1 reaffirmed
- [ ] **Endpoint identical** — only `200 stream` vs `302 redirect` differs (primary acceptance criterion)
- [ ] **Zero migration**; version + tag confirmed (`0.4.32-phase3-alpha8.5b2`)
