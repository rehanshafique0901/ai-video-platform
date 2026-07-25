# Publishing Runtime Contract — α8.6 (Creator Workflow)

> **Type:** Engineering design document (implementation contract). **Not an ADR.**
> The governing decisions live in **ADR-0041** (provider runtime contract — the event
> projection pattern + the AI-vs-publishing registry separation, W8.5c.4),
> **ADR-0042** (orchestration platform freeze — publishing stays additive),
> **ADR-0044** (α8.5x runtime architecture — sequencing ruling X-C: publishing comes
> *after* the runtime), and **ADR-0046** (Execution Runtime boundaries — X8 keeps
> `generation_assets` execution-owned). One **new** decision this slice introduces —
> the platform holding *user-owned external credentials* — is deferred to a dedicated
> **ADR-0047 (Publishing credential ownership)**, written before α8.6a implementation
> (see §9).
>
> **Milestone:** **α8.6 — Publishing / Creator Workflow.** This is a **new bounded
> context**, sequenced after the AR runtime (α8.5x) and Planner V2 (α8.7). It is an
> **Execution-plane capability** (stateful, side-effecting, uploads) but its **own
> domain module**, distinct from the Execution *Runtime* (the α8.5x generation
> increments that also carry the "α8.6" label in older docs). Everything frozen by
> ADR-0042/0044/0045/0046 stays byte-for-byte unchanged — publishing is strictly
> additive.
>
> **Status:** **DRAFT — pending sign-off.** No implementation until this is approved.
> Baseline `v0.4.35-phase3-alpha8.7` is untouched.
>
> **One-line purpose:** **Given one finished export artifact, publish it to one
> external destination** — as its own bounded context, downstream of export, without
> ever bridging into generation, render, or export ownership.

---

## 0. Why this document exists

The platform can already turn an idea into a finished, downloadable video. What it
cannot do is the last, product-defining step: **put that video in front of an
audience**. Publishing is the difference between "an impressive backend" and "a
compelling product" — the moment the story becomes:

```
Idea → AI creation → Finished video → One-click publish
```

Grounding surfaced one discovery more important than "there is no publishing code":
there are **two finished-video paths**, intentionally separated today, and publishing
must **not** become the accident that fuses them.

```
Path A (proven, user-downloadable)          Path B (new AR runtime)
  Workflow → Render → Export                   Planner V2 → Generation Runtime
      → delivery MediaAsset → Download             → generation_assets (final_video_asset_id)
```

Path B is execution-owned and, by design (ADR-0046 X8 / W8.6.8), never writes into
`media_assets`; its promotion use case (`PublishGenerationAssets`) is deliberately
unbuilt. This contract fixes publishing's boundaries **before** any schema or adapter
exists, so the last mile is added without eroding the boundaries the previous slices
paid to establish.

---

## 1. The one-sentence boundary

> **Publishing answers _"how do we distribute a finished video?"_ — never _"how do we
> finish generating one?"_. It consumes a finished export-delivery `MediaAsset`,
> attaches user intent (destination + metadata), and hands bytes to an external
> platform. It selects nothing, plans nothing, renders nothing, and re-encodes
> nothing.**

The pipeline, unchanged and unbridged by this slice:

```
Generation → Render → Export → Delivery MediaAsset → Publish
```

---

## 2. Scope & the approved decisions

The first slice is a **single vertical**: *given one finished export artifact,
publish it successfully to one destination.* Not "a publishing platform." Each
recorded decision below is a sign-off from the pre-flight review.

| # | Decision | Ruling | Consequence for this contract |
|---|---|---|---|
| 1 | Which finished video does publishing consume? | **A — the export delivery `MediaAsset`** | PUB-1. Publishing reads `export_jobs.output_media_asset_id`; the AR-runtime → publish bridge is a **separate future slice**, explicitly out of scope (§14). |
| 2 | How is a `PublishJob` initiated? | **A — explicit, user-initiated Creator Workflow** | PUB-2. No projection auto-creates a publish from `ExportJobSucceeded`; publishing is intent-bearing. |
| 3 | OAuth credentials — dedicated ADR? | **A — yes, ADR-0047** | §9 lists the ADR requirements; α8.6a does not start until ADR-0047 is accepted. |
| 4 | First destination + caption source? | **A — YouTube + a Mock destination; deterministic metadata** | §5/§8. No LLM caption generation in α8.6; it is its own later slice. |

---

## 3. Where publishing sits (plane, context, and the two paths)

- **Plane:** Execution. Publishing is stateful and side-effecting (it uploads bytes to
  the outside world). It is **not** a fourth plane (SYSTEM_MAP) and **not** a Decision
  concern.
- **Bounded context:** its **own** — `PublishJob` + `SocialAccount` + destination
  adapters, decoupled from generation, render, export, and the frozen orchestration
  core. "Not a separate plane" ≠ "not a bounded context."
- **Source of truth for _what_ to publish (PUB-1):** the **export delivery
  `MediaAsset`** (`export_jobs.output_media_asset_id`), the canonical downloadable
  artifact for a `(format, quality, orientation)`. Publishing **never** reads
  `generation_assets`, **never** triggers an export or render, and **never**
  recomposes or re-encodes (RC5, W8.5.1–W8.5.3).

---

## 4. Bounded context & module layout

Publishing mirrors the platform's established seams (`ExportJob`/`ExportWorker` for
the job runtime; the provider registry/dispatcher for a *parallel* destination
registry; `NotificationProjection` for event fan-out) but shares **no** frozen path.

Proposed layout (finalised per increment):

```
domain/publishing/            content_package.py, publish_job.py (state machine + VOs),
                              social_account.py, destination.py (Platform enum)
application/interfaces/       destinations.py         (IDestinationPublisher, PublishRequest/Result, DestinationError)
                              social_credentials.py   (ISocialCredentialStore — the credential-service port)
                              repositories.py (+)      (ISocialAccountRepository, IPublishJobRepository)
application/use_cases/publishing/
                              connect_social_account.py, revoke_social_account.py,
                              create_publish_job.py, process_publish_job.py, publish_worker.py,
                              _events.py, results.py
infrastructure/publishing/destinations/
                              ports.py (typed per-platform Protocols), registry.py (DestinationRegistry),
                              youtube.py, mock.py
infrastructure/publishing/credentials/
                              <credential vault adapter per ADR-0047>
infrastructure/repositories/  social_account_repository.py, publish_job_repository.py (ORM + UoW)
api/v1/routers/               publishing.py
```

### 4.1 Naming rule (hard constraint)

`PublisherPort` / `InProcessPublisher` already mean **the outbox event relay**. The
social-publishing port **MUST NOT** reuse that name. Use **`IDestinationPublisher`**
(the port a use case calls) and per-platform Protocols (`YouTubeDestination`, …). The
two concepts must be impossible to confuse.

### 4.2 Destinations are not AI providers (PUB-3)

Destinations get a **parallel registry** (`DestinationRegistry`), never the AI
capability catalogue, `ProviderRegistry`, dispatcher, or resolver (`capabilities.yaml`
already excludes `publishing`; W8.5c.4). A YAML destination catalogue + validator
(forking α8.5c tooling) is **deferred** until ≥2 real destinations exist (§14, open
question Q3) — the first slice registers adapters in code.

---

## 5. The `ContentPackage` contract

`ContentPackage` is the immutable, **platform-agnostic** description of *what to
publish*. It is built once (deterministically in α8.6; see PUB-2) and mapped to a
platform-specific request by each destination adapter at the boundary.

| Field | Type | Notes |
|---|---|---|
| `media_asset_id` | UUID | The **export delivery** `MediaAsset` (PUB-1). Tenant/owner carried alongside. |
| `title` | str | Deterministic in α8.6 (template, e.g. project/prompt-derived). |
| `description` | str | Deterministic template (caption). LLM generation deferred. |
| `tags` | tuple[str, …] | Deterministic template (hashtags). |
| `visibility` | enum | `public` / `unlisted` / `private`. |
| `thumbnail_media_asset_id` | UUID \| None | Reuse `source_metadata.enrichment.thumbnail_media_asset_id` when present (α8.4c). Custom upload deferred. |
| `publish_at` | datetime \| None | Optional schedule; None = publish as soon as claimed. |

Rules:
- **Platform-agnostic core, boundary validation:** platform-specific limits (title
  length, tag count, allowed visibility) are validated **inside** the destination
  adapter, which raises a typed `DestinationError` — the core `ContentPackage` stays
  neutral.
- **Deterministic metadata (α8.6):** the metadata builder is a pure function of the
  source artifact + project context. The future `ContentPackage → LLM Metadata
  Generator` is a separate slice with its own contract.

---

## 6. `PublishJob` lifecycle & state machine

`PublishJob` is the intent-bearing aggregate, modelled on `ExportJob` (queue → lease →
CAS transitions → settle + outbox), plus scheduling and bounded retries.

```
                 ┌──────────── cancel ───────────┐
                 ▼                                │
 (create) → QUEUED ──claim(due)──▶ RUNNING ──ok──▶ SUCCEEDED   (stores platform_post_id, published_at)
   │  ▲                              │
   │  │ retry (attempt<max,          │ transient failure
   │  │ backoff, scheduled_at=next)  │
   │  └──────────────────────────────┤
   │                                 │ permanent failure  ▶ FAILED (stores error)
   └── (publish_at in future) ── stays QUEUED until scheduled_at ≤ now
```

- **States:** `QUEUED`, `RUNNING`, `SUCCEEDED`, `FAILED`, `CANCELLED`. Scheduling is a
  `scheduled_at` predicate on `QUEUED` (a claimable job is `QUEUED AND (scheduled_at IS
  NULL OR scheduled_at ≤ now())`) — not a separate state, matching the export/worker
  poll pattern.
- **Transitions are CAS-fenced** on `status` (PUB-8); no ad-hoc status writes.
- **Retries are bounded and deterministic:** transient destination failures increment
  `attempt`, set `scheduled_at = now + backoff(attempt)`, and return to `QUEUED` until
  `max_attempts`, then `FAILED`. Permanent failures (auth revoked, invalid media) go
  straight to `FAILED`.
- **Idempotency (PUB-7):** each attempt sends a stable idempotency key
  (`publish_job_id` [+ attempt policy]) so an at-least-once worker never double-posts;
  the DB owns uniqueness via a partial-unique constraint (mirror of `ExportJob`).
- **Success record:** `platform_post_id` (+ URL) and `published_at` are persisted.

---

## 7. Worker model

- **`PublishWorker.run_once()`** polls claimable jobs (`QUEUED`, due) in batches —
  the **`ExportWorker` pattern**, not the completion engine (that is provider-async
  resume) and not the relay (that is event fan-out).
- **`ProcessPublishJob`**: lease (`publish_job:<id>`, and/or `project_publish:<project_id>`
  to serialise per project — the reserved lock prefix), CAS `QUEUED→RUNNING`, load the
  `SocialAccount` + a **pre-authenticated client** from the credential service (§9),
  stream the delivery `MediaAsset` bytes, call `IDestinationPublisher.publish(...)`,
  then settle (`SUCCEEDED`/retry/`FAILED`) and emit outbox events — **all heavy I/O
  outside the DB transaction**.
- **Scheduling gap (known):** there is **no in-repo time scheduler** today. α8.6 uses
  a `scheduled_at` due-column scan; an external cadence (or a future scheduler slice)
  calls `run_once`. A cron/scheduler is explicitly **not** built here.

---

## 8. Destination boundary

- **Port:** `IDestinationPublisher` (§4.1). Input: a `ContentPackage` + a
  pre-authenticated client/token + a byte stream. Output: a neutral `PublishResult`
  (`platform_post_id`, url) or a typed `DestinationError` (`transient` vs `permanent`).
- **Adapters are credential-blind (PUB-4, W8.1.1 analogue):** an adapter **receives** a
  ready-to-use client; it never reads config, fetches, stores, refreshes, or decides
  the storage of credentials. It knows one thing: how to talk to its platform's upload
  API.
- **First destination (Decision 4):** **YouTube** (resumable upload API — the most
  upload-friendly of the four) as the first *real* adapter, plus a **`MockDestination`**
  for deterministic tests and CI. TikTok / Instagram / Facebook follow in α8.6c behind
  the same port.

---

## 9. OAuth & credential ownership boundary (⇒ ADR-0047)

This is the first time the platform holds **user-owned external credentials**. That is
a genuine boundary move, so it earns a dedicated ADR **before α8.6a code**. Three
responsibilities stay strictly separated:

```
Publishing domain            owns intent (SocialAccount linkage, PublishJob)
      │
      ▼
Credential service           owns storage + encryption-at-rest + refresh + revoke
      │
      ▼
Destination adapter          owns the platform API call (credential-blind)
```

- **`SocialAccount` aggregate (α8.6a):** `(user_id, platform, external_account_id,
  display_name, scopes, status ∈ {connected, expired, revoked}, timestamps)`. Owner-
  scoped like notifications (PUB-9); a foreign/missing id is a uniform 404.
- **Credential service (`ISocialCredentialStore`):** stores/retrieves/refreshes
  **encrypted** access+refresh tokens; hands the worker a pre-authenticated client.
  The publishing domain and adapters never see plaintext storage concerns.
- **Login OAuth ≠ destination OAuth:** the existing `oauth_identities` table is
  identity-only (no tokens) and is **not** reused for destination credentials.

**ADR-0047 must decide:** credential ownership; storage location (dedicated table vs.
external vault); encryption-at-rest mechanism + key management; token lifecycle &
refresh ownership; revocation semantics (and cascade to in-flight `PublishJob`s);
access boundaries (who may decrypt, and when); and the explicit statement that
adapters are credential-blind (PUB-4/PUB-5). This contract defers those specifics to
the ADR rather than pre-empting them.

---

## 10. Event model

- Publishing emits terminal outbox events — `publishing.publish_job.succeeded` and
  `publishing.publish_job.failed` — following the `generation.*` / `ExportJobSucceeded`
  naming and the transactional-outbox pattern.
- **Downstream is pure fan-out (PUB-10):** the notification projection (or a sibling
  subscriber) may consume these to notify the user. Publishing itself **never** chains
  into another projection (fan-out `Event → {A,B,C}`, never a chain).
- **No auto-publish (PUB-2):** there is **no** subscriber on `ExportJobSucceeded` that
  creates a `PublishJob`. Export completion means "the artifact exists," not "publish
  it."

---

## 11. Invariants (PUB-1 … PUB-10)

- **PUB-1 — Publishing consumes a finished export-delivery `MediaAsset` only.** Never
  `generation_assets`; never triggers export/render; never recomposes/re-encodes.
- **PUB-2 — Publishing is intent-bearing and user-initiated.** A `PublishJob` exists
  only from an explicit user action; nothing auto-publishes from `ExportJobSucceeded`.
- **PUB-3 — Destinations are not AI providers.** Separate registry; never the AI
  catalogue / provider registry / dispatcher / resolver.
- **PUB-4 — Destination adapters are credential-blind.** They receive a pre-
  authenticated client; they never read, fetch, store, refresh, or decide credential
  storage (W8.1.1 analogue).
- **PUB-5 — Credential storage & encryption belong to the credential service.** Domain
  owns intent; adapter owns the API call; the credential service owns secrets
  (ADR-0047). No responsibility leaks across these lines.
- **PUB-6 — Publishing never mutates the frozen platform or upstream state.** It reads
  a finished artifact and writes only its own `PublishJob`/`SocialAccount` state +
  outbox events (ADR-0042; projection discipline).
- **PUB-7 — Uploads are idempotent under at-least-once.** A stable idempotency key
  prevents double-posting; the DB owns uniqueness (partial-unique, mirror of
  `ExportJob`).
- **PUB-8 — Status advances only through the `PublishJob` state machine.** CAS-fenced
  transitions; bounded, deterministic retries; no ad-hoc status writes.
- **PUB-9 — `SocialAccount` and `PublishJob` are owner-scoped.** Foreign/missing ids
  are a uniform 404 (anti-enumeration, W8.5b.8 analogue).
- **PUB-10 — Terminal publishing events are consumed as pure fan-out.** Downstream
  projections read them and write their own state; publishing never chains projections.

Each invariant that can be mechanically guarded gets **documentation +
implementation + enforcement** (import-linter / test) in its increment — the α8.7
discipline continues. Candidate guards: an import-linter contract that
`domain.publishing` / `infrastructure.publishing.destinations` cannot import the AI
`providers`/`resolver` packages (PUB-3), and a test proving adapters take an injected
client and never touch the credential store (PUB-4).

---

## 12. Migration plan

All publishing aggregates are owner-facing CRUD with worker CAS and (for credentials)
encrypted columns — so they are **ORM + Unit of Work** (like `export_jobs` /
`notifications`), **not** raw-SQL/ORM-less (that convention is for seeded catalogues
and execution ledgers). Migrations are additive; no frozen path changes.

| Increment | Migration | Tables |
|---|---|---|
| α8.6a | `0013_social_accounts` | `social_accounts`, `social_credentials` (encrypted tokens; shape per ADR-0047) |
| α8.6b | `0014_publish_jobs` | `publish_jobs` (status, `scheduled_at`, `attempt`/`max_attempts`, `content_package` JSONB, `source_media_asset_id`, `social_account_id`, `platform_post_id`, `error`, partial-unique for PUB-7) |
| α8.6c | (adapters only) | none required for YouTube/Mock; a destination catalogue table arrives only if/when the YAML catalogue is adopted (Q3) |

---

## 13. Increment breakdown & CI

Small, independently reviewable increments — each its own PR answering one question.

- **α8.6a — Account Connections.** `SocialAccount`, destination OAuth connect/callback/
  revoke, encrypted token storage + refresh. **Gated on ADR-0047.** No publishing yet.
- **α8.6b — Publish Runtime.** `PublishJob` + state machine + `PublishWorker` +
  `ProcessPublishJob` + retries + terminal events, driving the **`MockDestination`**
  end-to-end. Proves the runtime with no external API.
- **α8.6c — Destination Adapters.** The real **YouTube** adapter behind
  `IDestinationPublisher`; TikTok / Instagram / Facebook follow behind the same port.

**CI:** publishing earns its **own stage (Stage 14)**, not an expansion of the
generation slice's Stage 13 (SYSTEM_MAP §5). The `MockDestination` keeps Stage 14
deterministic and network-free.

---

## 14. Non-goals / explicitly deferred

- **No `generation_assets → publish` bridge.** Path B stays separate; promoting AR-
  runtime output (`PublishGenerationAssets`) into `media_assets` for publishing is a
  **future slice** (PUB-1; ADR-0046 X8).
- **No LLM caption/hashtag generation.** Deterministic metadata only in α8.6; the
  `ContentPackage → LLM Metadata Generator` is its own slice (Decision 4).
- **No auto-publish** from export completion (PUB-2).
- **No time scheduler.** `scheduled_at` + external `run_once` cadence only (§7).
- **No custom thumbnail upload** in α8.6 (reuse the enrichment-derived thumbnail).
- **No YAML destination catalogue/validator** until ≥2 real destinations (§4.2, Q3).

---

## 15. Open questions for sign-off

- **Q1 — YAML destination catalogue timing.** Defer the α8.5c-style
  `destinations.yaml` + validator until ≥2 real destinations (recommended), or build
  the parallel tooling up front in α8.6c?
- **Q2 — Per-project publish serialisation.** Lease on `publish_job:<id>` only, or also
  `project_publish:<project_id>` to serialise concurrent publishes of the same project
  (recommended, uses the reserved lock prefix)?
- **Q3 — `SocialAccount` cardinality.** One connected account per `(user, platform)` in
  α8.6, or allow multiple (e.g. two YouTube channels) from the start?
- **Q4 — Retry policy shape.** Confirm `max_attempts` + backoff curve, and which
  destination failures are classified `transient` vs `permanent`.
- **Q5 — ADR-0047 scope.** Confirm the credential-ownership ADR covers exactly the
  items in §9 before α8.6a begins.
```
