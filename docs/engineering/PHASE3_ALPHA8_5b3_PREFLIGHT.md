# Phase 3 — α8.5b.3 Pre-Flight: Notifications (in-app)

> Status: **SIGNED OFF — all forks ruled; proceeding to implement.** Third and final slice
> of the **distribution** context: once an export finishes, project the terminal event into an
> in-app notification. Input: `PHASE3_ALPHA8_5b3_GROUNDING.md`. Companions:
> `PHASE3_ALPHA8_5b2_PREFLIGHT.md` (storage backends) and `PHASE3_ALPHA8_5b_PREFLIGHT.md`
> (download serving). Baseline: `v0.4.32-phase3-alpha8.5b2`.
>
> **Rulings (SIGNED OFF):** **A — in-app only** (email deferred to α8.5b.4) · **B —
> triggers = `ExportJobSucceeded` + `ExportJobFailed`** · **C — relay subscriber** (no poll
> worker) · **D — DB-enforced idempotency** via `source_event_id` + partial `UNIQUE (user_id,
> source_event_id)` (the one accepted migration) · **W8.5b.6** adopted (notification creation is a
> pure projection of immutable events) · **W8.5b.7** adopted (exactly-once per recipient per source
> event, persistence-enforced) · **read side deferred to α8.5b.3r** · the reacting component is a
> **projection** (`NotificationProjection`), not a service/workflow · email
> adapter/templates/provider/retries **explicitly deferred** · version `0.4.33-phase3-alpha8.5b3`.

---

## 1. Gates (must pass before any design)

- **Gate 1 — ADR-0042 (orchestration freeze).** **PASS (additive consumer).** The slice registers
  a **new `EventHandler`** on `InProcessPublisher` in the composition root — exactly what α8.4a did
  for ingestion **without** a freeze override. The frozen contracts (`PublisherPort`, the relay,
  the `InProcessPublisher` class, the event emitters, the outbox schema/semantics) are **not
  modified**. Registering a second handler in `container.init()` is a composition-root (growth
  surface) change, not a frozen-contract change. Freeze guard expected green, **zero overrides**.
- **Gate 2 — ADR-0043 (render composition boundary).** **PASS (trivially).** Notifications live far
  downstream of composition — no `IRenderer`/`IExporter`/timeline/mix code is touched; RC1–RC6 and
  RP1–RP9 are unaffected.

---

## 2. The shape of the slice

α8.5b.3 is **not** "building notifications" — it is **safely connecting an existing event stream to
an existing notification model**. The entire runtime addition is one downstream projection:

```
ExportJobSucceeded / ExportJobFailed   (already emitted → event_outbox)
        ↓  RelayService.relay_once() → InProcessPublisher.publish(event)   (at-least-once)
        ↓
NotificationProjection           (new EventHandler; reacts to those two event types only)
        ↓  maps event → (recipient, kind, title, body, payload, source_event_id=event.id)
CreateNotification                (new use case; own UoW per event)
        ↓
INotificationRepository.add(...)  (new port + adapter)
        ↓  INSERT notifications row
   ── UNIQUE (user_id, source_event_id) ──►  duplicate ⇒ ConflictError ⇒ idempotent no-op
```

**Exactly-once = at-least-once relay + a DB uniqueness invariant.** The relay may redeliver; the
database refuses the second write; the use case treats that refusal as an already-notified no-op.
This is the *same* idempotency shape as α8.4a ingestion (deterministic key + `ConflictError`
recovery), applied to notifications.

---

## 3. Forks (all ruled)

### Fork A — Channel scope *(RULED: in-app only; email deferred)*
Persist a `Notification` row; set `delivered_in_app_at = now()` at insert (in-app "delivery" = the
committed, visible row). **`delivered_email_at` stays NULL** — no `INotifier`, no SMTP/provider
adapter, no templates, no send-retries, no credentials/secret enter this slice. Email is a
future slice (α8.5b.4) with its own external-dependency + deliverability surface.

### Fork B — Trigger set *(RULED: export success + failure)*
React to **`ExportJobSucceeded`** and **`ExportJobFailed`** only; every other event is a clean
no-op (the relay still marks it published). Both already carry **`requested_by_user_id`** in their
payload, so **no ownership lookup** is needed (recipient resolution is trivial — Grounding Fork E
avoided). Render/workflow triggers are **out of scope** (they'd need a project-owner lookup and
add noise; revisit only if a product need appears).

### Fork C — Dispatch *(RULED: relay subscriber, no poll worker)*
`NotificationProjection` runs **synchronously inside the relay's `publish()`** (mirroring
`GeneratedMediaIngestionSubscriber`), because writing one row is a fast local DB insert — not the
expensive/slow/externally-dependent work that justifies a poll worker. It builds a fresh
`CreateNotification` via a container factory with its **own UoW per event** (separate transaction
from the relay's `mark_published`, so at-least-once + DB-dedupe compose correctly). **No new worker,
no new `*_batch_size`/lease config.** It is named a **projection**, not a service/workflow, to
reflect that it derives read state from immutable events — it never orchestrates.

### Fork D — Idempotency *(RULED: DB-enforced via `source_event_id`)*
The database owns the "one event → one notification (per recipient)" invariant. **No**
`SELECT … IF NOT EXISTS … INSERT` (race-prone). Concretely:
- Add column **`notifications.source_event_id uuid NULL`** — the outbox `event.id` that produced
  the row (the relay's own idempotency coordinate). **Nullable** so non-event notifications (future
  welcome/system messages) are still expressible; **no FK** to `event_outbox` (the outbox is a
  transient delivery log that may be pruned/parked — the id is a logical dedupe key, not a
  lifetime coupling).
- Add **partial unique index** `uq_notifications_user_id_source_event_id ON notifications
  (user_id, source_event_id) WHERE source_event_id IS NOT NULL`. `(user_id, source_event_id)`
  (not `source_event_id` alone) keeps the door open for one event fanning out to multiple
  recipients later, while guaranteeing exactly-once **per recipient per event** now.
- `INotificationRepository.add(...)` raises **`ConflictError`** on the uniqueness violation;
  `CreateNotification` catches it → **already-notified no-op** (mirrors `IMediaRepository.add`).

---

## 4. New surface (all additive)

| Layer | Addition |
|---|---|
| **Domain** | `app/domain/notifications/notification.py` — minimal frozen `Notification` value (id, user_id, kind, title, body, payload, source_event_id, timestamps) as the repo return type (keeps layering; no ORM leak). |
| **Application (port)** | `INotificationRepository.add(*, user_id, kind, title, body, payload, source_event_id) -> Notification` (raises `ConflictError` on dup). Wired onto the UoW alongside the other repos. |
| **Application (use case)** | `CreateNotification.execute(*, user_id, kind, title, body, payload, source_event_id)` — idempotent insert in its own UoW txn; `ConflictError` → no-op; a `user_id` FK violation (recipient deleted) → clean no-op (can't notify a gone user). |
| **Application (projection)** | `NotificationProjection(EventHandler)` — reacts to the two export events; maps each to notification content; malformed payload → log + clean return (not retryable → never parks); genuine DB failure → raise (relay retries). Named a *projection*, not a service. |
| **Infrastructure** | `NotificationRepository` (SQLAlchemy) implementing the port; unique-violation → `ConflictError`. |
| **Composition root** | Register `NotificationProjection` on the existing `InProcessPublisher` list in `container.init()`; a `get_create_notification_use_case()` factory; repo on the UoW. |
| **Migration** | One additive Alembic revision: `ADD COLUMN source_event_id` + the partial unique index (+ matching `downgrade`). |
| **Config** | **None** (no batch size, no lease, no email settings — reinforces the minimalism). |

**Notification content mapping (owned by the subscriber):**

| Event | `kind` | `title` | `body` | `user_id` | `payload` (subset) |
|---|---|---|---|---|---|
| `ExportJobSucceeded` | `export.succeeded` | "Your video is ready" | e.g. "Your {quality} {format} export is ready to download." | `requested_by_user_id` | export_job_id, render_job_id, output_media_asset_id, format, quality, orientation |
| `ExportJobFailed` | `export.failed` | "Your video export failed" | neutral `error.message` (no internals — W8.5.2 already guarantees the event error is neutral) | `requested_by_user_id` | export_job_id, render_job_id, format, quality, orientation, error |

`source_event_id = event.id` in both cases.

---

## 5. Invariants

- **W8.5b.6 (new, adopted) — Notification creation is a pure projection of immutable events.** The
  subscriber + use case only **read** a terminal, already-committed event and **write** notification
  state. They never mutate export/render/orchestration state, never retry or re-drive the export,
  never dispatch provider/render work, and never call back into the frozen pipeline. Notifications
  are strictly observational and downstream (kin to W8.4.2 / W8.4c.1 / W8.5b.1).
- **W8.5b.7 (new, adopted) — A notification is projected exactly once per recipient per source
  event. This invariant is enforced by the persistence layer, not by subscriber control flow.**
  The relay may deliver more than once; the projection may execute more than once; the database
  guarantees the projection exists **at most once** (partial `UNIQUE (user_id, source_event_id)`),
  and the use case treats the refused duplicate as a successful no-op. This wording deliberately
  keeps the invariant resilient even if the projection implementation changes later — correctness
  never depends on application-level control flow.

---

## 6. Migration verdict

**One small additive migration** — accepted deliberately (your ruling): it encodes an invariant
that already exists conceptually ("one event → one notification"), rather than adding capability.
Additive and safe: a new **nullable** column + a **partial** unique index on a currently **empty**
table (zero existing rows to backfill or conflict). `downgrade` drops both. No other schema change.
Everything else (the table, its indexes, the relay, the events, `users.email`) is pre-existing.

---

## 7. Read side *(RULED: deferred to α8.5b.3r)*

This slice is scoped as the **write projection only** (event → notification row). Reading
notifications is a different responsibility with its own shape:

```
   Immutable Event → Exactly-once Projection → Notification row     ← α8.5b.3 (this slice)
   Notification row → Query model → User API                        ← α8.5b.3r (follow-up)
```

Splitting them yields **one release proving projection correctness** and **another proving query
behaviour** — the same milestone discipline used across the α8.5 series. `GET /notifications`,
unread counts, mark-read, and archive (all served by the existing `ix_notifications_user_id_unread`
partial index) are **out of scope** here and land in α8.5b.3r on this stable foundation.

---

## 8. Versioning & deliverable

- Version bump: **`0.4.33-phase3-alpha8.5b3`** (runtime capability).
- Deliverable: export terminal events are projected into per-recipient in-app notification rows,
  **exactly-once**, behind a new relay subscriber — with the download/export/render/orchestration
  contracts entirely unchanged.

---

## 9. Sign-off checklist

- [ ] **Gate 1** ADR-0042 PASS (additive consumer; no frozen contract modified) ·
      **Gate 2** ADR-0043 PASS (trivial)
- [ ] **Fork A** in-app only; `delivered_in_app_at` set; email deferred (no `INotifier`)
- [ ] **Fork B** triggers = `ExportJobSucceeded` + `ExportJobFailed` (export-only; recipient from
      payload)
- [ ] **Fork C** relay subscriber, own UoW per event; **no** poll worker / batch / lease config
- [ ] **Fork D** `source_event_id` + partial `UNIQUE (user_id, source_event_id)`; `ConflictError`
      → idempotent no-op; no `SELECT-then-INSERT`
- [ ] **W8.5b.6** adopted; **W8.5b.7** adopted (persistence-enforced exactly-once wording)
- [ ] **Read API** deferred to α8.5b.3r (this slice = write projection only)
- [ ] Reacting component named `NotificationProjection` (projection, not service/workflow)
- [ ] Migration = one additive column + partial unique index (with `downgrade`); nothing else
- [ ] Version `0.4.33-phase3-alpha8.5b3`

---

## 10. Test plan (on sign-off)

- **`NotificationProjection`** — maps `ExportJobSucceeded` → correct `(user_id, kind, title, body,
  payload, source_event_id=event.id)`; maps `ExportJobFailed` likewise; **ignores** other event
  types (clean no-op, relay still publishes); **malformed payload** → log + clean return (not
  retryable → no parking); **duplicate event** (redelivery) → `ConflictError` swallowed → no-op;
  **genuine DB error** → raises (relay retry).
- **`CreateNotification`** — inserts a row; duplicate `(user_id, source_event_id)` → no-op;
  deleted recipient (FK violation) → clean no-op.
- **`NotificationRepository`** (integration) — `add` inserts + stamps `delivered_in_app_at`;
  duplicate `(user_id, source_event_id)` → `ConflictError`; same event / different recipient
  allowed; the partial index permits multiple `source_event_id IS NULL` rows.
- **Idempotency (layered exactly-once proof, W8.5b.7)** — the projection unit test drives a
  redelivery whose `CreateNotification` reports `duplicate` (swallowed, no raise); the
  `CreateNotification` unit test proves a repeated `(user_id, source_event_id)` yields exactly one
  row; the repository integration test proves the DB constraint refuses the second write. Together:
  at-least-once relay + DB uniqueness ⇒ at-most-one persisted row.
- **Migration** — `upgrade`/`downgrade` roundtrip (ci_gate stages 5–7, live DB).
- **Freeze guard** green; full gate (ruff/black/mypy/import-linter/pytest/coverage) green.
