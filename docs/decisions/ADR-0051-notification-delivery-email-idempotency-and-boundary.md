# ADR-0051 — Notification Delivery Channel (Email): Idempotency, Dispatch & Boundary

**Status:** **Accepted** (Phase 3, α9.5 — Notification Delivery (Email), 2026-07-29). Governance that
**precedes** implementation (like ADR-0044/0045/0047/0048/0049/0050): it fixes how the platform
performs its **first outbound external-communication side-effect that is not a publish destination**
before any email code exists. Drafted at the α9.5 grounding stop
([`PHASE3_ALPHA9_5_GROUNDING.md`](../engineering/PHASE3_ALPHA9_5_GROUNDING.md)), **amended** after
empirical provider verification (see below), and **Accepted** with the amendment. The α9.5 pre-flight
follows; **no implementation** accompanies this ADR.

**Accepted decisions (summary).** **D1-C** — per-notification lease + send-then-stamp →
**at-least-once** delivery with a **bounded, explicitly accepted rare-duplicate window**; delivery is
**never** sacrificed to avoid a duplicate. **Provider-native deduplication is optional, provider-
specific, and never part of the platform correctness proof**; the deterministic notification-derived
identifier is primarily a **correlation/reconciliation** identifier and may *additionally* serve as a
provider idempotency key **only where a provider is independently verified to honour it**.
**Appendix A is non-normative** — future provider selection must **re-verify** capabilities at
implementation time, not rely on today's documentation. **D2-C** (dedicated poll worker), **D3**
(bounded retry with terminal failure), **D4** (PII-minimal adapter boundary), and **D5**
(application-owned, mock-first `INotifier` port) are accepted unchanged.

**Amendment (2026-07-29, pre-acceptance).** Empirical provider verification (**Appendix A**)
established that none of the realistic starting transports (raw SMTP, Amazon SES, SendGrid, Postmark,
Mailgun) offer native deterministic email-send dedup. The recommendation was reworked so it is
**correct with zero provider dedup**, and provider-specific dedup is documented **separately**
(Appendix A) as an **optional optimisation**, never a core invariant. This amendment **removed an
unsupported assumption** and introduced **no new architectural question**; the decision was Accepted
with it in place.

**Builds on:**
- **ADR-0041** (provider runtime contract + the **event-projection pattern**: immutable events fan
  out to independent downstream projections, each owning its own persistence/idempotency; a
  projection must never chain into another).
- **ADR-0042** (orchestration/platform freeze — a new channel plugs in **additively**, never by
  editing the frozen relay/outbox/runner).
- **ADR-0047** (publishing-credential ownership — **credential-blind leaves**; configuration is
  injected, never fetched by the adapter; the PII/credential-boundary discipline this ADR mirrors
  for recipient data).
- **ADR-0048** (DB/contract-owned idempotency & replay-safety for a downstream outbox consumer —
  the precedent this ADR must extend to an effect that the DB **cannot** make exactly-once).
- **ADR-0049** (application-owned port + one-way dependency + neutral DTO + **mock-first,
  config-gated** adapter — the port/adapter shape this ADR reuses).
- **ADR-0050** (a **best-effort, non-fatal external second-effect** that never corrupts the primary
  path — the failure-posture precedent).
- The notification stack (**W8.5b.6/W8.5b.7**: a projection only reads a terminal event + writes
  notification state; exactly-once per `(user_id, source_event_id)` is DB-enforced) and the
  **poll-worker + lease** pattern (`ExportWorker`/`PublishWorker` + `Process*` under
  `SqlAlchemyDistributedLockManager`).

`PUBLISHING_RUNTIME_CONTRACT`-adjacent docs (`PHASE3_ALPHA8_5b3_PREFLIGHT.md` Fork A,
`PHASE3_CREATOR_EXPERIENCE_PREFLIGHT.md`, `PLATFORM_STATUS.md` roadmap) **explicitly deferred**
email to **α8.5b.4** and named **`INotifier`** as the planned port; this ADR is that slice's
governance.

---

## Context

The in-app notification loop is complete: a relay drain fans terminal `ExportJob*` / `PublishJob*`
events to `NotificationProjection` / `PublishNotificationProjection` → `CreateNotification` →
`INotificationRepository.add`, which writes one row and stamps `delivered_in_app_at`. Exactly-once
per recipient per source event is **DB-owned** by the partial unique index
`uq_notifications_user_id_source_event_id`.

Grounding (`PHASE3_ALPHA9_5_GROUNDING.md`) established, cited to `file:line`:

- **The email column already exists and is dormant.** `notifications.delivered_email_at timestamptz`
  (baseline `0001`, `models/notifications.py:47`) is never written today; the table's own docstring
  is `"Notifications (in-app + email queue)."`
- **There is zero email infrastructure.** No `INotifier`, no adapter, no `smtp_*`/`email_*`
  settings (backend-wide search: zero matches). The recipient exists (`User.email`, CITEXT NOT NULL,
  `models/identity.py:57`) but is unconnected to notifications.
- **Reusable substrate exists:** the relay fan-out; the poll-worker+lease pattern; the config-gated
  **fail-soft** adapter pattern (`_get_destination_registry()` registers `MockDestination` always
  and `YouTubeDestination` only when `youtube_oauth_*` is set; `openai_api_key` → real vs mock).

### The decisive new fact

Every downstream effect the platform has shipped so far is a **database write** — a `MediaAsset`
row, a `Notification` row, an `analytics_events` row — which the DB makes **exactly-once** via a
unique constraint, and which a transaction can **roll back**. **Sending an email is different in
kind:** it is an **external, non-transactional, non-rollback-able side-effect** whose outcome may be
**ambiguous** (the SMTP/API call can succeed at the provider while the connection drops before we
learn it). Therefore:

> **Exactly-once email delivery is impossible in general.** We must consciously choose between
> **at-most-once** (never double-send, risk a *lost* email) and **at-least-once** (never lose an
> email, risk a *duplicate* send) — and mitigate the residual risk. This is the same class of
> "exactly-once external effect" problem as **PUB-11** (ambiguous publish upload), and it is exactly
> why the DB-owned idempotency of **ADR-0048** does **not** transfer directly.

### Decision points

- **D1 — External-send idempotency & effect ordering** (send-then-stamp / stamp-then-send /
  outbox-claim-token; provider idempotency; crash-replay; duplicate vs lost-email risk).
- **D2 — Dispatch mechanism** (poll worker / relay projection / alternatives; operational
  complexity; consistency with the frozen relay/outbox).
- **D3 — Retry semantics** (transient vs permanent; backoff; terminal state; observability).
- **D4 — PII boundary** (ownership; adapter responsibility; data crossing into providers; logging
  constraints).
- **D5 — Port ownership** (application-owned interface; infrastructure adapter; mock-first;
  configuration gating).

D3–D5 are largely shared regardless of the D1/D2 choice; D1 and D2 are the load-bearing decisions.

---

## D1 — External-send idempotency & effect ordering

The durable state we can observe per notification is `delivered_email_at` (NULL = not yet
delivered). The question is **when** we set it relative to the external send, and how we bound the
duplicate/lost risk.

### Options

- **D1-A — Stamp-then-send.** Set `delivered_email_at = now()` (commit), *then* call the provider.
  - *Crash/replay:* if the process dies after the stamp commits but before/without a confirmed send,
    the row looks delivered and is **never retried** → **lost email**.
  - *Guarantee:* **at-most-once.** Zero duplicates; non-zero lost-mail probability on every crash
    window.
- **D1-B — Send-then-stamp.** Call the provider; on a confirmed success, set `delivered_email_at`
  (commit).
  - *Crash/replay:* if the process dies after the provider accepted but before the stamp commits, the
    row stays NULL and is **re-sent** → **duplicate email**.
  - *Guarantee:* **at-least-once.** Zero lost mail; non-zero duplicate probability only in the
    narrow crash-after-send window.
- **D1-C — Send-then-stamp under a per-notification claim/lease** (the "outbox/claim-token" variant
  applied to the notifications table), with a **deterministic correlation key** that is *optionally*
  used as a provider dedup input **only where a specific provider is independently verified to
  support it** (Appendix A).
  - The notifications table **is** the durable outbox for email (`delivered_email_at IS NULL` = the
    pending queue). A worker **claims** a pending row via the existing distributed lease
    (`email:<notification_id>`) so no two workers send concurrently, sends, then stamps. A **stable
    key derived from `notification.id`** (e.g. `blake2b(notification.id)`) is attached to every send
    for **log correlation and event-webhook reconciliation** — and, *only for a provider verified to
    honour it*, as a native dedup key.
  - *Crash/replay:* the lease prevents concurrent double-sends; a re-send after a crash-after-accept
    carries the **same** key. **Correctness does not depend on the provider deduping:** the baseline
    guarantee is exactly **D1-B** (a rare duplicate in the crash-after-accept window, never a lost
    email). Where a provider is verified to honour the key, that residual duplicate is additionally
    suppressed — a **bonus, not a load-bearing property**.
  - *Guarantee:* **at-least-once** (never lost; a bounded, accepted rare-duplicate window),
    **regardless of provider capabilities**; tightened toward effectively-exactly-once **only** where
    a provider's dedup is verified.
- **D1-D — Dedicated `email_outbox` table + relay/claim token.** A separate durable queue table with
  its own claim-token column, parallel to the notifications row.
  - Adds a table + write path that **duplicates** what `delivered_email_at IS NULL` already
    expresses. No consumer needs a second queue.

### Provider idempotency does not underpin correctness (verified — see Appendix A)

Provider-side deduplication is **not assumed**. Empirical verification (Appendix A) found that
**none of the realistic starting transports offer native, deterministic email-send dedup**; where a
"key" exists (SendGrid `X-Entity-Ref-ID`, Mailgun `v:` variables) it is for **tracking/correlation**,
not dedup, and Postmark/SES/raw SMTP have no such feature at all. The D1 recommendation is therefore
designed to be correct **without any provider dedup, including raw SMTP**. Provider dedup — where a
specific provider is later verified to support it — is an **optional optimisation** documented
separately in **Appendix A**, never a core invariant.

### Crash / replay scenarios (matrix)

| Failure window | D1-A stamp-then-send | D1-B send-then-stamp | D1-C claim + key |
|---|---|---|---|
| Crash **before** send | Row marked delivered, never sent → **lost** | Row NULL → retried → **sent once** ✅ | Row NULL, lease expires → retried → **sent once** ✅ |
| Crash **after** provider accept, **before** stamp | (n/a — already stamped) | Row NULL → **re-sent (duplicate)** | **Re-sent (duplicate)** — same key attached; suppressed **only if** provider dedup is verified (Appendix A) |
| Two workers race the same row | Possible double-send | Possible double-send | **Lease** prevents concurrency; key covers residual |
| Transient send failure | (already stamped → lost) | Row NULL → retried ✅ | Row NULL → retried with backoff ✅ |

### D1 evaluation

| Criterion | D1-A stamp-then-send | D1-B send-then-stamp | D1-C claim + key | D1-D email_outbox table |
|---|---|---|---|---|
| **Lost-email risk** | **High** (every crash window) | **None** | **None** | None |
| **Duplicate-send risk** | None | Low (crash-after-send only) | **Low = D1-B baseline** (bounded, accepted); further suppressed only where provider dedup is verified (Appendix A) | Low |
| **Concurrency safety** | none | none (needs a lease anyway) | **Lease-guarded** | lease-guarded |
| **Migration** | none | none | none (reuses `delivered_email_at`; key derived, not stored) | **new table** |
| **Consistency w/ ADR-0048 posture** | — | reuse-existing-storage | **reuse-existing-storage** | duplicates storage |
| **Product harm of residual failure** | a creator **never hears** their publish failed | a rare **duplicate** email (annoying, not harmful) | a rare **duplicate** email (bounded, accepted) | rare duplicate |

**Product framing:** a *lost* success/failure notification is materially worse than a *duplicate*
one — the entire point of the channel is off-platform reach, so silently dropping mail defeats it; a
second copy is a minor annoyance. This asymmetry favours **at-least-once**.

---

## D2 — Dispatch mechanism

### Options

- **D2-A — Dedicated poll worker** scanning `notifications WHERE delivered_email_at IS NULL` (+ due),
  claiming each under a lease and sending — mirroring `ExportWorker`/`PublishWorker.run_once()`.
  External I/O runs **off** the relay's critical path; retry is a natural re-scan.
- **D2-B — Relay projection** (a new `EmailNotificationProjection` subscriber on `InProcessPublisher`
  that sends email inline during the outbox drain).
  Couples **external I/O + its latency + its failure** to the outbox drain that also feeds the
  in-app/analytics projections; a slow or failing SMTP call would stall or fail sibling projections.
  Contradicts the ADR-0041 posture that handlers are fast, idempotent, DB-only reactions.
- **D2-C — Hybrid (recommended shape): keep the existing projection unchanged; add a separate send
  worker.** The in-app notification row is already written exactly-once by the existing projection;
  a **new poll worker** drains `delivered_email_at IS NULL`. This reuses the write path untouched and
  isolates external I/O.  *(D2-C is D2-A plus the explicit statement that the row-creation projection
  is not modified.)*
- **D2-D — Enqueue to an external queue/broker** (SQS/Celery/etc.) from a relay handler.
  Introduces new infrastructure the platform does not have; the notifications table already provides
  a durable queue.

### D2 evaluation

| Criterion | D2-A/C poll worker | D2-B relay projection | D2-D external broker |
|---|---|---|---|
| **External I/O off the frozen relay path** | **Yes** | **No** (couples send to drain) | Yes |
| **Retry model** | Re-scan + backoff (natural) | Would need in-handler retry (blocks drain) | Broker redelivery |
| **Blast radius of a slow/failed send** | Isolated to the worker | **Hits sibling projections** | Isolated |
| **Consistency w/ existing patterns** | **High** (Export/PublishWorker twins) | Low (violates handler = fast/DB-only) | New infra, none today |
| **Operational complexity** | Low (one more `run_once` cadence, as Export/Publish already require) | Low code, **high coupling risk** | **High** (new broker to run) |
| **Supporting index** | May want a partial index on `delivered_email_at IS NULL` (small additive migration) — pre-flight detail | n/a | n/a |

The notifications table is explicitly *"in-app + email queue"*; a poll worker treats
`delivered_email_at IS NULL` as that queue, exactly as `ExportJob`/`PublishJob` claimable scans do.

---

## D3 — Retry semantics

| Concern | Ruling (recommended) |
|---|---|
| **Transient failure** (network, timeout, SMTP `4xx`, `429`, provider `5xx`) | Leave `delivered_email_at` NULL; re-scanned on a later tick with **capped exponential backoff** (mirrors the α8.6b/Q4 retry posture). |
| **Permanent failure** (invalid address, hard bounce, auth/policy rejection) | Classified by the adapter into a **neutral, non-retryable** error; the notification moves to a **terminal "email-failed" state** and is **not** re-scanned. |
| **Bounded attempts** | A **max attempt count** guarantees the loop terminates even for mis-classified transients (no infinite re-send). |
| **Terminal state storage** | A terminal marker + attempt count must be **persisted**. *Where* is a pre-flight detail — either a JSONB marker on the existing `payload`/a status field (**no migration**, ADR-0048's reuse-existing-storage posture) or a small additive column/index (migration). The ADR fixes only that a **terminal state exists** so retries are bounded; it does not mandate the storage mechanism. |
| **Observability** | Every attempt emits a structured log (`notification.email.sent` / `.retry` / `.failed_permanent`) with `user_id`, `notification_id`, attempt, provider status, and a **masked** recipient (never the full address/body). An optional terminal outbox event is a pre-flight/PUB-8 concern, **not** decided here. |

---

## D4 — PII boundary

| Concern | Ruling (recommended) |
|---|---|
| **Recipient ownership** | The **application** send use case resolves the recipient from `User.email`, owner-scoped by the notification's `user_id`. The adapter never queries users or notifications. |
| **Adapter responsibility** | The adapter is a **config-blind, PII-minimal leaf** (mirrors ADR-0047 credential-blindness): it receives a ready, neutral message (recipient, subject, body) + injected transport config; it performs the send and returns a neutral result/error. It resolves nothing and stores nothing. |
| **Data crossing into providers** | Only the **minimum** required: recipient address + rendered subject/body. **No** access/refresh tokens, no internal ids beyond an opaque idempotency key, no credential material. |
| **Logging constraints** | Structured logs **never** contain the full recipient address or the rendered body; only `user_id`, `notification_id`, a **masked** address (e.g. `a***@d***`), status, and attempt — consistent with the anti-enumeration/PII discipline already used at the auth boundary. |

---

## D5 — Port ownership

| Concern | Ruling (recommended) |
|---|---|
| **Port location** | An **application-owned** `INotifier` port (`application/interfaces/`) with a **neutral DTO** (`EmailMessage`/`NotificationDelivery`) — no infrastructure or provider type leaks into the application (ADR-0049 shape). |
| **Adapter** | An **infrastructure** adapter implementing `INotifier`; dependency is **one-way** (infra → application port), never the reverse. |
| **Mock-first** | A deterministic, network-free `MockNotifier`/`LoggingNotifier` is **always** registered (the CI default) so the gate stays offline and deterministic (mirrors `MockDestination`/`MockLLMProvider`). |
| **Configuration gating** | The real SMTP/provider adapter is registered **only when configured** (`email_*`/`smtp_*` settings present) and is **fail-soft** (unconfigured → mock/no-op, **no boot failure**) — the exact `_get_destination_registry()` + `youtube_oauth_*` pattern. |

---

## Consistency with prior ADRs & existing architecture

| Dimension | ADR-0048 (analytics) | ADR-0049 (AI metadata) | ADR-0050 (thumbnail) | **ADR-0051 (email) — proposed** |
|---|---|---|---|---|
| **Effect type** | DB write (rollback-able) | In-request advisory call | External second upload | **External send (non-rollback-able)** |
| **Idempotency owner** | **DB** unique index (exactly-once) | n/a (advisory, fallback) | job key + PUB-11 (no double-post) | **at-least-once + lease** (never lost; bounded rare duplicate); provider dedup optional, **not assumed** (exactly-once impossible) |
| **Dispatch** | Relay projection (DB-only handler) | Synchronous use case | Publish worker (in-band, best-effort) | **Dedicated poll worker** (off relay path) |
| **Port shape** | Repository | App-owned port + neutral DTO + mock | Additive DTO handle | **App-owned `INotifier` + neutral DTO + mock, config-gated** |
| **Failure posture** | Refuse duplicate = no-op | Graceful fallback | Best-effort, non-fatal | **Bounded retry + terminal state; never blocks in-app** |
| **Relay coupling** | Handler is fast/DB-only | none | none | **None** — external I/O kept off the relay (why not D2-B) |

The proposed shape is a faithful continuation: reuse the existing exactly-once **in-app** projection
untouched; add an **isolated** external-send worker; keep the provider a **mock-first, config-gated,
PII-minimal leaf**; and make the one thing that is genuinely new — an effect the DB cannot make
exactly-once — an **explicit at-least-once-with-dedup** decision rather than a silent assumption.

---

## Decision (Accepted)

Accepted 2026-07-29 (with the provider-verification amendment):

- **D1 → D1-C.** **Send-then-stamp under a per-notification lease.** The core, **provider-independent**
  guarantee is **at-least-once, never lost mail**, with a **bounded, accepted rare-duplicate window**
  (the crash-after-accept case) — correct even on **raw SMTP with zero provider dedup**. A
  deterministic key derived from `notification.id` is attached for **log/webhook correlation** and,
  **only where a provider is independently verified to honour it** (Appendix A), as an optional dedup
  input that further suppresses that residual duplicate. **Provider dedup is an optimisation, not a
  correctness dependency.** **Reject D1-A** (lost-email risk defeats the channel's purpose), **reject
  D1-D** (a second queue duplicating `delivered_email_at IS NULL`).
- **D2 → D2-C.** A **dedicated poll worker** draining `delivered_email_at IS NULL` under a lease,
  mirroring `ExportWorker`/`PublishWorker`; the existing row-creating projection is **unchanged**.
  **Reject D2-B** (couples external I/O to the frozen relay drain), **reject D2-D** (new broker
  infra the platform does not need).
- **D3.** Capped exponential backoff for transient failures; adapter-classified permanent failures →
  a **bounded, terminal** email-failed state; full structured observability with masked recipients.
- **D4.** Application resolves the owner-scoped recipient; the adapter is a config-blind, PII-minimal
  leaf; logs never carry the full address or body.
- **D5.** Application-owned `INotifier` + neutral DTO; **mock-first**, **config-gated fail-soft** real
  adapter; one-way dependency.

This decision deliberately does **not** fix: the exact terminal-state storage (JSONB marker vs
additive column/index), the supporting scan index, the concrete provider (SMTP vs a specific ESP),
template ownership/rendering specifics, whether a terminal-failure **outbox event** is emitted, the
batch-size/backoff constants, and the CI stage/test wiring — **all deferred to the α9.5 pre-flight**
(now proceeding). None of these are architectural decisions; if the pre-flight surfaces one, it stops
for a superseding ADR.

---

## Rejected alternatives (summary)

1. **Stamp-then-send (D1-A).** *Rejected.* Trades the channel's whole purpose (reliable off-platform
   reach) for zero duplicates; a lost failure/success email is the worst outcome.
2. **Relay projection sends email inline (D2-B).** *Rejected.* Couples external I/O latency + failure
   to the frozen outbox drain and its sibling projections (violates the ADR-0041 handler posture).
3. **Dedicated `email_outbox` table (D1-D) / external broker (D2-D).** *Rejected.* Duplicate durable
   queue / new infrastructure; the notifications table already **is** the email queue
   (`delivered_email_at IS NULL`).
4. **Email as a hard prerequisite / blocking the in-app notification.** *Rejected.* In-app delivery
   must remain exactly as today; email is an **additional** channel that never blocks or alters the
   existing write path (W8.5b.6/7 preserved).
5. **Adapter resolves the recipient or reads notifications/users.** *Rejected.* Breaks the
   credential-/config-blind leaf discipline (ADR-0047); recipient resolution is an application
   concern.
6. **Infinite retry with no terminal state.** *Rejected.* An un-bounded re-send loop for a permanent
   failure risks provider abuse flags and repeated duplicates; a terminal state is mandatory.

---

## Consequences (if accepted)

- **Positive.** Creators receive off-platform email on publish/export success/failure; the existing
  exactly-once in-app path is untouched; external I/O is isolated from the frozen relay; the provider
  is a mock-first, config-gated, PII-minimal leaf (offline-deterministic CI); the "exactly-once is
  impossible" reality is made explicit and mitigated (lease + send-then-stamp; provider dedup treated
  as an optional, separately-documented optimisation — Appendix A) rather than assumed.
- **Cost.** A new port + adapter + send worker + settings + CI stage; a genuinely new **at-least-once
  external-effect** posture the platform must own (duplicates are possible, by design, and must be
  minimised); a terminal-failure state must be persisted (storage mechanism TBD at pre-flight); PII
  handling (recipient addresses, masked logging) becomes a standing concern.
- **Boundary proposed (a future slice may not cross without its own ADR).** *Email is an additive,
  isolated, at-least-once delivery channel: it never blocks or alters the exactly-once in-app write
  path; external sends run in a dedicated worker off the relay drain, under a per-notification lease
  (with a stable correlation key; provider dedup is optional and never assumed — Appendix A); the
  provider adapter is a config-blind, PII-minimal leaf that resolves nothing; and every send path has
  a bounded, terminal failure state.* Push/websocket
  channels (new columns/tokens) remain **out of scope** and would need their own grounding/ADR.

---

## Appendix A — Provider idempotency: verified capabilities (NON-NORMATIVE reference material)

**Non-normative.** This appendix documents provider-specific behaviour and is deliberately separated
from the core architecture above. **Nothing in the accepted Decision depends on it**, and it does not
constrain any implementation obligation. Verified **2026-07-29** against current provider
documentation; provider behaviour can change, so **future provider selection must re-verify these
capabilities at implementation time** rather than relying on this snapshot.

| Transport | Native deterministic send-dedup? | Verified notes |
|---|---|---|
| **Raw SMTP** | **No** | No idempotency concept; a resend is simply a new message. |
| **Amazon SES** (`SendEmail` / `SendRawEmail`) | **No** | No dedup-ID request parameter; on a timeout there is **no way to know** whether SES accepted the message. (SES/SNS "message deduplication" docs apply to **SQS/SNS FIFO**, *not* email sends.) |
| **SendGrid** (Mail Send v3) | **No** | API is not idempotent — duplicate POSTs are all dispatched. `X-Entity-Ref-ID` / unique-args / custom-args are **tracking/correlation only**, not dedup; dedup must be built app-side. |
| **Postmark** | **No** | Support docs state plainly: *"Postmark does not currently support an idempotency key feature."* Recommends app-side dedup + webhooks. |
| **Mailgun** | **No** | No native idempotency key; `v:` variables are **custom tracking metadata**, not dedup. |
| Resend / Stripe-style APIs | **Yes** (request header) | Support an `Idempotency-Key` header with server-side dedup — **not** a currently planned provider; listed only to show the capability exists in the ecosystem. |

**Conclusion of verification.** **None of the transports the platform is realistically likely to
start with (raw SMTP, SES, SendGrid, Postmark, Mailgun) provide native, deterministic email-send
deduplication.** The deterministic `notification.id`-derived key is therefore treated as a
**correlation / reconciliation identifier** (structured logs + event webhooks) in the core design; it
becomes a **dedup** input **only** if and when a specific provider (e.g. Resend) is independently
verified to honour it. This is exactly why D1-C's correctness is defined **entirely** by the lease +
send-then-stamp (at-least-once, bounded rare duplicate), **independent of any provider feature** — the
recommendation is unchanged whether the chosen transport is raw SMTP or an idempotent HTTP API.

---

## Change log

| Date | Change |
| --- | --- |
| 2026-07-29 | **Proposed (draft).** Drafted at the α9.5 grounding stop for review. Frames the decisive new fact (email is a non-transactional, non-rollback-able external effect ⇒ exactly-once is impossible; choose at-most-once vs at-least-once). Evaluates D1 external-send idempotency (stamp-then-send / send-then-stamp / claim-token + provider key / dedicated outbox table) with crash-replay matrix and provider-support table; D2 dispatch (poll worker / relay projection / hybrid / external broker); D3 retry (transient/permanent/backoff/terminal/observability); D4 PII boundary (ownership/adapter/data crossing/logging); D5 port ownership (app-owned interface / infra adapter / mock-first / config gating); and consistency vs ADR-0048/0049/0050 + the relay/outbox + worker patterns. **Recommends D1-C + D2-C + bounded retry + PII-minimal leaf + app-owned mock-first config-gated port — explicitly as a recommendation pending approval, NOT accepted.** No pre-flight, implementation, branch, commit, or PR produced. |
| 2026-07-29 | **Accepted.** Status → Accepted with the provider-verification amendment in place. Confirmed accepted decisions: **D1-C** (per-notification lease + send-then-stamp → at-least-once, bounded/accepted rare-duplicate window; delivery never sacrificed to avoid a duplicate); **provider-native dedup is optional, provider-specific, and never part of the correctness proof** (the deterministic notification-derived id is primarily a correlation/reconciliation id, and a provider dedup key only where independently verified); **Appendix A is non-normative** (re-verify at implementation time); **D2-C / D3 / D4 / D5** accepted unchanged. The amendment removed an unsupported assumption and introduced **no new architectural question**. α9.5 proceeds to the pre-flight; no implementation accompanies this ADR. |
| 2026-07-29 | **Amended (pre-acceptance).** Empirically verified provider idempotency against current docs (SendGrid Mail Send v3, Amazon SES `SendEmail`/`SendRawEmail`, Postmark, Mailgun, raw SMTP) and found **none offer native deterministic email-send dedup** (SendGrid `X-Entity-Ref-ID` / Mailgun `v:` vars are tracking-only; Postmark states no idempotency-key feature; SES/SMTP have none). Reworked D1-C so that (1) provider dedup is an **optional optimisation unless verified**, (2) the recommendation is **correct with zero provider dedup, including raw SMTP** — its correctness rests solely on the per-notification **lease + send-then-stamp** (at-least-once, bounded rare-duplicate window), and (3) provider-specific capabilities are documented **separately in Appendix A**, isolated from the core invariant. Updated the D1-C option, crash/replay matrix, D1 evaluation table, the Decision D1 bullet, the consistency table, and the Consequences/boundary clauses accordingly. Recommendation shape unchanged (D1-C + D2-C + bounded retry + PII-minimal leaf + app-owned mock-first config-gated port); the deterministic key is reframed from an assumed dedup mechanism to a correlation identifier that is *optionally* a dedup input only where a provider is verified. |
