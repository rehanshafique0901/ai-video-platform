# Phase 3 — α8.5b.2 Grounding: Storage Backends & Signed-URL Delivery

> Status: **GROUNDING — facts only, no rulings.** Establishes *what already exists in the code*
> before an α8.5b.2 pre-flight proposes any design. It deliberately does **not** decide scope,
> forks, or invariants — it surfaces the surface area so the pre-flight (and its sign-off) can.
>
> Companion to `PHASE3_ALPHA8_5b_GROUNDING.md` (the umbrella) and `PHASE3_ALPHA8_5b_PREFLIGHT.md`
> (download serving, α8.5b.1). Baseline: `v0.4.31-phase3-alpha8.5b1`.

---

## 0. Why ground first (again)

α8.5b.1 shipped the **`IDownloadDelivery` seam** in production with two decision shapes —
`StreamDelivery` (implemented) and `RedirectDelivery` (**type exists, no producer**). α8.5b.2 is
supposed to "complete the abstraction": add cloud storage adapters + signed URLs behind that
stable contract so large transfers leave the API workers. Before drafting the pre-flight we need
to know exactly **what is already in place vs. genuinely new**, and — the headline finding —
**where the real architectural tension sits** (it is *not* the enum or the migration; it is
multi-backend resolution and where signing lives).

---

## 1. Where the pipeline ends today

```
… → Export (α8.5a) → delivery MediaAsset(storage_backend='local', …)
        → Download (α8.5b.1): GET …/exports/{id}/download
              → DownloadExport → IDownloadDelivery.deliver(DownloadRequest)
                    → LocalStreamDelivery → StreamDelivery (bytes streamed via the API)
                                              ↑ ONLY 'local' works today
```

Every produced/rendered/exported artifact is written through **one** object-storage instance
(`local`), and the download path streams from that **same** instance. There is no cloud backend,
no signed URL, and no way to serve a `MediaAsset` whose `storage_backend` is anything but
`local`. That gap is α8.5b.2.

---

## 2. What already exists (zero-migration reuse)

### 2.1 The delivery seam — already production, redirect-ready
`app/application/interfaces/download_delivery.py`:
- `DownloadRequest(storage_backend, storage_bucket, storage_key, media_type, filename, content_length)`
- `IDownloadDelivery.deliver(request) → DeliveryDecision`
- `DeliveryDecision = StreamDelivery | RedirectDelivery`
- **`RedirectDelivery(url, expires_at)` already defined** but produced by nothing.

`app/application/use_cases/export/download_export.py` — `DownloadExport` **already handles a
`RedirectDelivery` return value transparently** (it returns whatever the seam decides).
`app/api/v1/routers/export_jobs.py` — the endpoint **already renders `RedirectDelivery` as a
`302`** (`RedirectResponse(url=…, 302)`) and `StreamDelivery` as a streamed attachment.

- **Consequence:** the entire request→decision→response path for redirects is **already wired
  and tested** (`test_redirect_delivery_is_passed_through`, `test_redirect_delivery_returns_302`).
  α8.5b.2 only needs to **produce** a `RedirectDelivery` from a cloud adapter — **no endpoint,
  use-case, or router change** (exactly the seam's purpose, Fork A of α8.5b.1).

### 2.2 Object storage — port + local adapter
`app/application/interfaces/object_storage.py` — `IObjectStorage`: `put` / `get` / `exists` /
`delete` by opaque `/`-delimited key; `backend` + `bucket` properties; returns
`StoredObject(backend, bucket, key)`. Configuration-blind (W8.1.1 — root/creds injected).
`app/infrastructure/storage/local_object_storage.py` — `LocalObjectStorage` (writes under
`<root>/<bucket>/<key>`, traversal-guarded, I/O off the loop via `asyncio.to_thread`).

- **`storage_backend_enum = {local, s3, r2, azure_blob, gcs}`** (`db/enums.py:33`) — **all cloud
  backends already enumerated.** A new adapter needs **no enum change and no migration.**
- Config today: only `media_storage_root`, `media_storage_bucket` (`core/config.py:150–158`).

### 2.3 `MediaAsset` — carries the full backend-agnostic storage triple
`storage_backend` / `storage_bucket` / `storage_key` (+ `mime_type`, `size_bytes`, `checksum`,
dims, `source_metadata` JSONB), unique on the triple. A row can *already record*
`storage_backend='s3'` — the schema is ready; only the runtime adapter to serve it is missing.

### 2.4 Container wiring (the important part)
`app/core/container.py`:
- `_get_object_storage()` — builds **one** memoised `LocalObjectStorage(root, bucket)`.
- `_get_download_delivery()` — builds **one** `LocalStreamDelivery(_get_object_storage())`.
- All writers (ingestion, render, export) share that single instance.

---

## 3. What does NOT exist (the real α8.5b.2 surface)

| Concern | Present today? | Migration? | Notes |
|---|---|---|---|
| **Signed / presigned URL** | ❌ none anywhere | No (additive) | No `signed_url()`/`presigned` symbol exists in the repo. `IObjectStorage` has no URL method; `local` has no URL concept at all. |
| **Cloud storage adapters** (S3 / R2 / GCS / Azure) | ❌ only `local` | **No** (enum pre-exists) | Additive `IObjectStorage` impls. R2 is S3-API-compatible (one adapter, different endpoint); GCS/Azure sign differently. |
| **Cloud SDK dependency** | ❌ none | n/a | `pyproject.toml` has no `boto3`/`aioboto3`/`google-cloud-storage`/`azure-storage-blob`. This would be the **first heavy external infra dep** (α8.1's `httpx` was the last new runtime dep). S3 presigning via `botocore.generate_presigned_url` is **offline/sync** (no network, no async client needed). |
| **Cloud config surface** | ❌ none | n/a | No endpoint/region/access-key/secret/bucket/public-base-URL/TTL settings. Must be injected (W8.1.1). |
| **Multi-backend resolution** | ❌ single instance | No | *The central gap.* Writers share one storage; `LocalStreamDelivery` **rejects any non-local backend**. Nothing maps a `MediaAsset.storage_backend` value back to the adapter that can `get`/`delete`/**sign** it. |
| **CDN / public base URL** | ❌ none | No | Redirect could target the object store directly or a CDN in front of it. |
| **Retention / lifecycle / expiry policy** | ❌ none | Likely later | No TTL/GC for delivery artifacts; signed-URL expiry is a *new* time concept (`RedirectDelivery.expires_at` field exists, unused). |

### 3.1 The central architectural tension — multi-backend resolution
Today there is exactly **one** storage instance and **one** delivery adapter, both hard-wired to
`local`. Two distinct resolution problems appear the moment a second backend exists:

1. **Write-side selection** — which backend do *new* artifacts get written to? (A single
   configured "active" backend is the simplest; per-artifact selection is more complex.)
2. **Read/sign-side resolution** — an existing `MediaAsset` already names its `storage_backend`;
   serving/deleting/signing it requires resolving *that* value → the matching adapter, **not**
   whatever the current write backend is. This implies a **backend-keyed registry/resolver**
   (`{backend → IObjectStorage}` and/or `{backend → IDownloadDelivery}`), replacing the single
   memoised instance. This is the main thing the pre-flight must design.

### 3.2 Where does signing live? (a genuine fork for the pre-flight)
- `local` **cannot** produce a meaningful signed URL (no HTTP endpoint). Putting `signed_url()`
  on `IObjectStorage` forces `LocalObjectStorage` to raise "unsupported" — a leaky abstraction.
- The `DeliveryDecision` seam **already** models the split cleanly: `LocalStreamDelivery` streams;
  a cloud delivery adapter (e.g. `S3RedirectDelivery`) redirects. So signing may belong on the
  **delivery adapter** (per-backend `IDownloadDelivery` impls) rather than on the storage port.
- Either shape is additive and unfrozen; this is a design choice, not a constraint.

---

## 4. Governance pre-check (for the pre-flight to formally rule on)

- **Frozen paths (ADR-0042):** none of `interfaces/object_storage.py`,
  `interfaces/download_delivery.py`, `infrastructure/storage/**`, `infrastructure/delivery/**`,
  or the container factories are in `FROZEN_PATHS` (verified against
  `scripts/check_frozen_platform.py`). Expected **Gate 1 PASS**, zero overrides.
- **Render boundary (ADR-0043 / Gate 2):** storage + signing are *below* the render boundary and
  never touch composition. RC5 (master immutable) / W8.5.3 (deliveries replaceable) hold; RP1–RP9
  govern the render layer, not delivery. Expected **PASS**.
- **W8.5b.1 / W8.5b.2 / W8.5b.3 (α8.5b.1):** unchanged. Signed-URL delivery is still a *pure
  transfer* (W8.5b.2) — a redirect hands off byte transfer to the object store; the artifact is
  never re-encoded. Best-effort accounting (W8.5b.3) still applies before the redirect is issued.
- **W8.1.1 (configuration-blind):** cloud credentials must be **injected** at construction, never
  fetched at runtime — the same discipline as every provider adapter.

---

## 5. Natural scope options (observation, not a ruling)

| Option | Backends | Signing | Size | Notes |
|---|---|---|---|---|
| **Minimal S3** | `s3` only | S3 presign (offline `botocore`) | small–medium | R2 is S3-compatible → nearly free follow-on. One SDK. |
| **S3 + R2** | `s3`, `r2` | shared S3 presign, distinct endpoints | medium | Highest leverage per unit of work; one adapter, config-selected endpoint. |
| **All four** | `s3/r2/gcs/azure` | 3 signing schemes, 2–3 SDKs | large | GCS/Azure signing differs materially; heaviest dep footprint. |

Observation: the seam is already redirect-ready, so the **incremental** cost is (a) one cloud
adapter + presigner, (b) the backend-keyed resolver (§3.1), and (c) config + a dependency.
`s3` (with `r2` as a near-free variant) is the smallest slice that proves the whole redirect path
end-to-end; GCS/Azure can be their own follow-ons behind the identical seam.

---

## 6. Open questions for the α8.5b.2 pre-flight to settle

1. **Scope / backends:** S3-only first, or S3+R2 (S3-compatible)? Defer GCS/Azure?
2. **Signing placement:** `signed_url()` on `IObjectStorage` (local raises unsupported) **vs.** a
   per-backend `IDownloadDelivery` adapter (`S3RedirectDelivery`) that owns presigning? (§3.2)
3. **Backend resolution:** introduce a `{backend → adapter}` registry/resolver for read/sign/
   delete, replacing the single memoised instance? Keyed on `MediaAsset.storage_backend`. (§3.1)
4. **Write-side selection:** does α8.5b.2 change where *new* artifacts are written (a configured
   active backend), or only add the ability to *serve* cloud-stored artifacts? (Serving-only keeps
   the slice smaller and defers a data-migration question.)
5. **Expiry semantics:** signed-URL TTL — fixed config value? Populate `RedirectDelivery.expires_at`?
6. **CDN:** redirect to the raw object URL, or to a configurable public/CDN base URL in front of it?
7. **Local parity:** confirm `local` continues to **stream** (no signing) via the same endpoint —
   the `DeliveryDecision` seam already guarantees an identical API contract across backends.
8. **Dependency:** which SDK, and is presigning kept **offline** (no live client at request time)?
9. **Tests:** unit-test presigning without live cloud (fake/`moto`/deterministic signer stub);
   keep any real-cloud checks opt-in (mirrors the FFmpeg integration-test pattern).
10. **Out of scope (confirm):** retention/GC, lifecycle policies, per-artifact backend routing,
    publishing, notifications — all deferred.

---

## 7. Grounding summary (the facts the pre-flight can rely on)

- **The redirect path is already built and tested** end-to-end (seam → use case → `302`); α8.5b.2
  only needs to **produce** a `RedirectDelivery` from a cloud adapter — **no endpoint/use-case/
  router change** (the α8.5b.1 Fork-A seam paying off).
- **Zero migration:** `storage_backend_enum` already lists `s3/r2/azure_blob/gcs`; `MediaAsset`
  already carries the backend triple.
- **The real design work is (a) a backend-keyed resolver** (single instance → `{backend →
  adapter}`) and **(b) where signing lives** (storage port vs. delivery adapter) — not schema.
- **New external dependency:** a cloud SDK (first heavy infra dep since `httpx`); credentials
  injected, never fetched (W8.1.1). S3 presigning can stay **offline**.
- **Both governance gates expected to pass** (unfrozen paths, below the render boundary; W8.5.x
  intact).

No code has been written. The recommended next artifact is an **α8.5b.2 pre-flight** that takes a
position on §6 — starting with backend scope (§5), signing placement (§3.2), and the resolver (§3.1).
