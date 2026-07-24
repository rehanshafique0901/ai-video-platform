# Phase 3 — α8.5b.3 Grounding: Notifications

> Status: **GROUNDING — facts only, no rulings.** Establishes *what already exists in the code*
> before an α8.5b.3 pre-flight proposes any design. It deliberately does **not** decide scope,
> forks, or invariants — it surfaces the surface area so the pre-flight (and its sign-off) can.
>
> Companion to `PHASE3_ALPHA8_5b_GROUNDING.md` (the distribution umbrella),
> `PHASE3_ALPHA8_5b2_PREFLIGHT.md` (storage backends, α8.5b.2). Baseline:
> `v0.4.32-phase3-alpha8.5b2`.

---

## 0. Why ground first (again)

α8.5b.3 is the **last concern inside the distribution bounded context**: once an export
succeeds, the user should be *told* their video is ready. It is deliberately sequenced **before**
α8.6 (publishing) so the current context is completed before a new one is opened.

The headline reason to ground first is that notifications *look* trivial ("the table already
exists, just insert a row on `ExportJobSucceeded`") but hide **two real decisions** the pre-flight
must make explicitly: **(a)** what "notify" means here — an in-app record only, or also outbound
email — and **(b)** how a notification stays exactly-once when the relay that triggers it is
**at-least-once**. Everything else is genuinely additive reuse.

---

## 1. Where the pipeline ends today

```
… → Export (α8.5a) → ExportJobSucceeded (event_outbox row)
        → RelayService.relay_once() → InProcessPublisher.publish(event)
              → GeneratedMediaIngestionSubscriber   (only reacts to WorkflowRunSucceeded)
              → (no notification consumer)  ← the gap α8.5b.3 fills
        → Download (α8.5b.1/.2): GET …/exports/{id}/download → 200 stream / 302 redirect
```

The terminal export lifecycle events (`ExportJobSucceeded` / `ExportJobFailed`) are **already
emitted to the outbox** by the export worker. Nothing consumes them for user-facing signalling.
The user has no way to learn an export finished except by polling `GET …/exports/{id}`.

---

## 2. What already exists (zero-migration reuse)

### 2.1 The `notifications` table — modelled, schema-only, **zero usage**
Created in `alembic/versions/0001_baseline.py`; ORM at
`app/infrastructure/db/models/notifications.py`. Full column set:

| Column | Type | Null | Default |
|---|---|---|---|
| `id` | uuid | no | `gen_random_uuid()` (PK) |
| `user_id` | uuid | no | FK → `users(id) ON DELETE CASCADE` |
| `kind` | text | no | — (free-text, **no enum**) |
| `title` | text | no | — |
| `body` | text | yes | — |
| `payload` | jsonb | no | `'{}'` |
| `delivered_in_app_at` | timestamptz | yes | — |
| `delivered_email_at` | timestamptz | yes | — |
| `read_at` | timestamptz | yes | — |
| `archived` | boolean | no | `false` |
| `created_at` / `updated_at` | timestamptz | no | `now()` (+ `touch_updated_at` trigger) |

Indexes: `ix_notifications_user_id_unread (user_id, created_at) WHERE read_at IS NULL AND
archived = false`; `ix_notifications_user_id_created_at (user_id, created_at)`. Rows are **mutable**
(not in `_IMMUTABLE_TABLES`) — read/archive/delivery stamps are expected to change.

- **No domain entity**, **no repository** (nothing in `interfaces/repositories.py` or
  `infrastructure/repositories/`), **no use case, no API route, no test** references it. It is a
  pure schema stub today.
- **Two delivery-channel stamps** (`delivered_in_app_at`, `delivered_email_at`) are baked into the
  schema — the original data model anticipated **two channels** (in-app + email). No uniqueness on
  any source-event column (see §3, the idempotency crux).

### 2.2 The outbox relay — production-shaped, at-least-once, with parking
- `app/application/interfaces/publisher.py` — `PublisherPort.publish(OutboxEvent)`, `EventHandler`
  protocol. Docstring contract: **handlers must be idempotent on `event.id`**; a raise = failed
  publish → redelivered on a later pass (**at-least-once**, ADR-0041 D9).
- `app/infrastructure/publisher/in_process_publisher.py` — `InProcessPublisher(handlers)`; fans
  out to handlers **synchronously, in registration order**, in the relay's own async task.
- `app/application/use_cases/relay/relay_service.py` — `relay_once()` = one transactional batch:
  `fetch_unpublished(FOR UPDATE SKIP LOCKED)` → publish each → `mark_published` / `mark_failed`
  (`attempts += 1`) → one commit. Defaults: batch `100`, `max_attempts=10`. On the attempt that
  reaches `max_attempts` the row is **parked** (excluded from future fetches; no DLQ table) with a
  structured `outbox.publish_failed parked=True` ERROR log.
- **No production driver.** `relay_once()` has no scheduled caller in `main.py`/scripts (same as
  *every* poll worker — see §2.5). It is a library primitive today.

### 2.3 The trigger events — already emitted, recipient carried
`app/application/use_cases/export/_events.py`:
- **`ExportJobSucceeded`** payload carries **`requested_by_user_id`** (+ `export_job_id`,
  `render_job_id`, `output_media_asset_id`, `file_size_bytes`, `format`, `quality`, `orientation`).
  Metadata `{"actor": "export_worker"}`.
- **`ExportJobFailed`** — same base + `error: {code, message}`.

The **recipient is in the event payload** for export events — no ownership lookup needed. Other
candidate triggers and their recipient hints:

| Event | File | Recipient hint |
|---|---|---|
| `ExportJobSucceeded` / `ExportJobFailed` | `export/_events.py` | `requested_by_user_id` (in payload) |
| `RenderJobSucceeded` / `RenderJobFailed` | `render/_events.py` | `project_id` → `projects.owner_user_id` (lookup) |
| `WorkflowRunSucceeded` / `WorkflowRunFailed` | `workflow/_events.py` | `project_id` (+ optional `actor_user_id` metadata) |

### 2.4 The subscriber pattern — one working precedent
`app/application/use_cases/media/generated_media_subscriber.py` —
`GeneratedMediaIngestionSubscriber` is the **only** `EventHandler`. It:
- subscribes to **one** event (`WorkflowRunSucceeded`), returns cleanly for all others;
- runs **synchronously inside the relay's `publish()`** (no separate worker/queue);
- builds a fresh use case via a container factory (own UoW per event);
- is **idempotent** by delegating to a deterministic-key + DB-uniqueness write (not by checking
  `event.id`);
- logs + returns cleanly on a malformed payload (not retryable → avoids poison parking);
- raises on genuine failure → relay retries.
- Wired in `container.init()`: `InProcessPublisher([GeneratedMediaIngestionSubscriber(…)])`.

This is the template a notification consumer would most naturally follow.

### 2.5 The worker pattern — the alternative shape
`RenderWorker` / `ExportWorker` / `MediaEnrichmentWorker` / `CompletionEngine.poll_once` all share
`run_once()` → scan claimable rows → per-item `lease → CAS claim → heavy I/O outside txn → settle`.
Each has a `*_batch_size` (and lease) setting in `config.py`. This is the shape used when work is
**CPU-bound or long-running I/O** that should stay **off** the relay fan-out.

### 2.6 Recipient contact data
`users.email` (**`citext NOT NULL`**, `app/infrastructure/db/models/identity.py` + domain
`app/domain/identity/user.py`) — a delivery address is always available. `email_verified_at`
(nullable) exists. **No phone / SMS / push-token / notification-preference columns** anywhere.

---

## 3. What does NOT exist (the genuine gaps)

- **No notification write path** — no domain entity, no `INotificationRepository`, no
  "create notification" use case, no consumer of any lifecycle event for signalling.
- **No `INotifier` / email / SMS / push port or adapter** anywhere. (`webhook_deliveries` table +
  `IWebhookVerifier` exist, but those are *outbound webhooks* and *inbound provider callbacks*
  respectively — a different concern, not user notifications.)
- **No source-event idempotency on `notifications`.** The table has **no** `source_event_id`
  column and **no** unique constraint tying a notification to the outbox event that caused it. The
  relay is **at-least-once and redelivers on `event.id`**, so a naïve "insert a row when I see
  `ExportJobSucceeded`" consumer would create **duplicate** notifications on every redelivery.
  This is the one place α8.5b.3 may need a schema change — see Fork D.
- **No notification API** (list / mark-read / archive) — the read side the in-app channel implies.
- **No relay/worker daemon** to actually pump delivery in production (pre-existing platform gap,
  shared by every async loop; not α8.5b.3's job to solve, but worth stating).

---

## 4. Where the real architectural tension sits (for the pre-flight)

These are surfaced as **open forks**, not decisions.

- **Fork A — What is a "notification" in this slice?**
  (A1) **In-app record only** — persist a `Notification` row on the trigger event; `read_at`
  /`archived`/`delivered_in_app_at` managed by a small read/patch API later. Smallest, needs no
  new external dependency or secret. (A2) **In-app + email** — also send via a new `INotifier`
  (SMTP/provider adapter), stamping `delivered_email_at`. The schema's two channel columns invite
  both, but email adds a new port, adapter, config, secret, and a whole delivery-failure surface.
  *Consideration:* α8.5b split large concerns into thin slices; email could itself be α8.5b.4.

- **Fork B — Which events trigger a notification?** The unambiguous headline is
  **`ExportJobSucceeded`** ("your video is ready"). Candidates: `ExportJobFailed` (actionable
  failure), and possibly `RenderJobFailed` / `WorkflowRunFailed`. Each non-export trigger needs an
  ownership lookup (Fork E). The pre-flight should pick a **minimal, high-value** trigger set.

- **Fork C — Dispatch mechanism: relay subscriber vs. own worker.** Writing a `Notification` row
  is a lightweight DB insert → fits the **synchronous relay subscriber** precedent (§2.4).
  Sending email is external I/O → argues for a **poll worker** (§2.5) or a two-step design (record
  in-app synchronously; deliver email via a worker that scans `delivered_email_at IS NULL`). Fork C
  is entangled with Fork A.

- **Fork D — Exactly-once under at-least-once (the crux).** Options: (D1) **DB-enforced** — add a
  `source_event_id uuid` column + `UNIQUE (user_id, source_event_id, kind)` (or similar) and let a
  duplicate insert raise `ConflictError` → caught as an already-notified no-op (mirrors ingestion).
  **This is a migration** (additive). (D2) **Query-guarded** — check for an existing notification
  keyed by `payload->>'source_event_id'` before inserting (zero migration, weaker — race-prone
  without the unique index). (D3) store the dedupe key in `payload` + a partial unique index on the
  JSONB expression (migration, but no new column). The pre-flight must choose; it decides the
  **migration verdict**.

- **Fork E — Recipient resolution.** Export triggers carry `requested_by_user_id` (no lookup).
  Render/workflow triggers carry only `project_id` → need `projects.owner_user_id`
  (`IProjectRepository.get_ownership` already exists, additive/system-only). Trigger set (Fork B)
  determines whether any lookup is needed at all.

- **Fork F — Channel-stamp semantics.** What do `delivered_in_app_at` vs `delivered_email_at`
  mean? (e.g. in-app "delivered" = the row is committed and visible; email "delivered" = the send
  adapter accepted it.) Only relevant if Fork A includes email.

- **Gate note (ADR-0042):** adding a **new `EventHandler` to `InProcessPublisher`** is exactly what
  α8.4a did **without** a freeze override — the frozen `PublisherPort`/relay/publisher class are
  *not modified*, only a new consumer is registered. A notification subscriber stays additive iff
  it does **not** touch the relay/publisher contracts. (ADR-0043 render boundary is trivially
  unaffected — notifications are far downstream of composition.)

---

## 5. Open questions the pre-flight must answer

1. **Scope (Fork A):** in-app record only, or in-app + email? Is email deferred to a later slice?
2. **Triggers (Fork B):** exactly which events — just `ExportJobSucceeded`, or also failures?
3. **Dispatch (Fork C):** synchronous relay subscriber, poll worker, or record-now/deliver-later?
4. **Idempotency + migration (Fork D):** DB-enforced dedupe (migration) vs query-guarded
   (zero-migration)? This single choice sets the migration verdict.
5. **Recipient (Fork E):** does the trigger set stay export-only (payload-carried recipient) or
   pull in project-owner lookups?
6. **Read side:** does α8.5b.3 include the in-app list/mark-read/archive API, or only the write
   path (API deferred)?
7. **Ports:** new `INotificationRepository` (certain); new `INotifier` (only if Fork A ⊇ email).

---

## 6. Migration verdict (provisional)

- **Persisting notifications:** **zero migration** — the `notifications` table + indexes already
  exist and are unused.
- **Idempotency:** **conditional** — Fork D2 (query-guarded) is zero-migration; Fork D1/D3
  (DB-enforced dedupe) is a **single additive** column and/or unique index. This is the *only*
  candidate migration in the slice, and it is the pre-flight's call.
- **Email (if in scope):** no schema change (the `delivered_email_at` stamp already exists);
  the cost is a new port + adapter + config/secret, not a migration.

---

## 7. One-line summary

The plumbing is almost entirely present — a modelled `notifications` table, an at-least-once
outbox relay, terminal export events that already carry the recipient, a working subscriber
precedent, and `users.email` for delivery. The pre-flight's real work is **four decisions**:
*what channel(s)* (Fork A), *which triggers* (Fork B), *subscriber vs worker* (Fork C), and — the
crux — *how to be exactly-once under an at-least-once relay* (Fork D), which alone determines
whether this otherwise zero-migration slice takes one additive migration.
