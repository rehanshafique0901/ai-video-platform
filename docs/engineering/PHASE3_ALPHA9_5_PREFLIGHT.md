# α9.5 — Notification Delivery (Email) — Pre-flight (design blueprint)

> **Status:** Design blueprint for review. **No implementation** accompanies this document.
> **Baseline:** `v0.4.47-phase3-alpha9.4` (frozen). **Target version:** `v0.4.48-phase3-alpha9.5`.
> **Governed by:** [`ADR-0051`](../decisions/ADR-0051-notification-delivery-email-idempotency-and-boundary.md)
> (**Accepted**) — D1-C send-then-stamp (at-least-once, bounded rare-duplicate; delivery never
> sacrificed to avoid a duplicate), D2-C dedicated poll worker, D3 bounded retry + terminal state,
> D4 PII-minimal leaf, D5 application-owned mock-first `INotifier`. Provider-native dedup is optional
> and never a correctness dependency (Appendix A, non-normative).
> **Grounded by:** [`PHASE3_ALPHA9_5_GROUNDING.md`](./PHASE3_ALPHA9_5_GROUNDING.md).
>
> **Architectural-decision check (up front):** this pre-flight surfaced **no** new architectural
> decision beyond ADR-0051 (see §11). Every choice below is an implementation ruling *within* the
> accepted ADR. Per the workflow, **STOP after this pre-flight for review** before implementation.

---

## 1. Scope

Deliver the platform's first outbound email: when a `notifications` row already exists (written by
the frozen in-app projection), a **new, independent poll worker** sends it to the recipient's
`User.email` and stamps `delivered_email_at`. Strictly additive; the in-app write/read paths and the
relay/outbox are untouched.

**In scope:** `INotifier` port + neutral DTO; a mock/logging adapter (CI default) + a config-gated
real SMTP adapter (fail-soft); repository read/claim/stamp methods; a `NotificationEmailWorker`
(`run_once`) + per-row `ProcessNotificationEmail` under a per-notification lease; bounded retry +
terminal failure; settings; DI wiring; an import-linter leaf contract; CI Stage 23; tests.

**Out of scope (unchanged / deferred):** push/websocket channels (need new columns/tokens — their own
slice); channel-preference API; a terminal-failure **outbox event** (logs only in v1 — ADR-0051 left
this deferred); HTML templating beyond a plain-text body; any change to the relay, `InProcessPublisher`,
the three existing projections, or the in-app read API contract.

---

## 2. Decisive grounding facts (cited)

- **The email column exists; no migration for the happy path.** `notifications.delivered_email_at`
  (`infrastructure/db/models/notifications.py:47`) — dormant; only `delivered_in_app_at` is written
  today (`notification_repository.py:63`).
- **`payload` is JSONB and is *exposed*.** `NotificationPublic.payload` returns `dict(row.payload)`
  (`api/v1/schemas/notifications.py:42`); the read entity carries it too
  (`notification_repository.py:167` `_row_to_entity`). → any reserved bookkeeping written into
  `payload` **must be excluded** from the user-facing read model (§5).
- **The poll-worker + lease template is proven.** `PublishWorker.run_once()` scans
  `list_claimable(now, limit)` then delegates to `ProcessPublishJob.process()`, which takes a
  `publish_job:<id>` lease via `uow.locks.acquire(...)` (`publish_worker.py:53`,
  `process_publish_job.py:98-119`); the lease contract is `IDistributedLockManager`
  (`application/interfaces/locks.py`), backed by the baseline `distributed_locks` table.
- **The application-owned mock-first port template is proven (ADR-0049).**
  `IPublishMetadataGenerator` + neutral frozen DTOs in `application/interfaces/publish_metadata_generator.py`;
  the adapter `LlmPublishMetadataGenerator` (`infrastructure/ai/metadata/...`) is cancellation-safe
  (`asyncio.wait_for`), logs only a coarse `reason`, and never leaks the Publishing context.
- **The config-gated fail-soft adapter template is proven.** `_get_destination_registry()` registers
  `MockDestination` always and `YouTubeDestination` only when `youtube_oauth_*` is set
  (`container.py:1829-1850`); `youtube_oauth_client_secret: SecretStr | None`
  (`config.py:354`), `publish_batch_size` (`config.py:307`), `llm_metadata_timeout_seconds`
  (`config.py:395`) are the shapes to mirror.
- **Recipient exists.** `User.email` (CITEXT NOT NULL); resolvable via `uow.users.get_by_id(user_id)`
  (`IUserRepository.get_by_id`, `repositories.py:95`).
- **CI head is Stage 22** (`ci_gate.py:28`); **ADR head is 0051**; next stage = **23**.

---

## 3. The interface (D5) — `INotifier` + neutral DTO

New file `app/application/interfaces/notifier.py` (mirrors `publish_metadata_generator.py`):

```python
@dataclass(frozen=True, slots=True)
class EmailMessage:
    """Neutral, ready-to-send message. The adapter resolves nothing (D4)."""
    recipient: str          # the resolved User.email (the only PII that crosses)
    subject: str
    body_text: str
    idempotency_key: str    # deterministic, notification-derived; correlation-first (ADR-0051)

class NotifierDeliveryError(RuntimeError):
    """Raised by an adapter when a send fails. ``permanent`` classifies the failure
    (invalid address / hard bounce / auth-policy reject = permanent; network / timeout /
    4xx-5xx transient = transient) so the worker can choose retry vs terminal (D3)."""
    def __init__(self, message: str, *, permanent: bool, code: str) -> None: ...

class INotifier(ABC):
    @abstractmethod
    async def send(self, message: EmailMessage) -> None:
        """Send one email or raise ``NotifierDeliveryError``. MUST be cancellation-safe
        (wrap the transport in ``asyncio.wait_for``; let ``CancelledError`` propagate) and
        MUST NOT log the full recipient address or ``body_text`` (D4)."""
```

- **Neutral & one-way:** the port + DTO reference no infrastructure and no provider type; the adapter
  depends only on this port + a transport library (D5, mirroring ADR-0049).
- **`idempotency_key`** = `blake2b(str(notification.id).encode(), digest_size=16).hexdigest()` —
  minted by the worker, stable across retries. Per ADR-0051 it is **correlation-first**; an adapter
  MAY additionally pass it to a provider **only where independently verified** to dedup (none of the
  starting transports do — Appendix A). SMTP: emit it as a stable `Message-ID`/custom header.

---

## 4. Adapters (D5) — mock-first + config-gated real

New package `app/infrastructure/notifications/`:

- **`LoggingNotifier`** (always available; the CI/dev default): deterministic, network-free; "sends"
  by emitting a structured log with a **masked** recipient (`a***@d***`) + `idempotency_key`, never
  the address or body (D4). This is the email analogue of `MockDestination` / `MockLLMProvider`.
- **`SmtpNotifier`** (config-gated real; fail-soft): sends over SMTP via **`aiosmtplib`** (the one new
  dependency — an established async SMTP client; added to `pyproject.toml` dependencies). It is
  **config-blind** (ADR-0047 posture): host/port/TLS/username/password/from-address are injected at
  the composition root, never read from `Settings` inside the adapter. It classifies SMTP outcomes
  into transient vs permanent `NotifierDeliveryError` (e.g. `550/551/553` recipient-rejected →
  permanent; timeouts / `421` / `4xx` / connection errors → transient).

*Dependency-selection note:* SMTP (not a specific ESP) is chosen for v1 because it needs no account
provisioning/app-review and works with any provider. This is a pre-flight ruling within ADR-0051 D5,
not an architectural decision; Appendix A's re-verify-at-adoption rule applies if an ESP is chosen
later.

---

## 5. Persistence & the terminal/attempt state — **NO migration** (D3 storage decision)

ADR-0051 D3 requires a **persisted, bounded** retry state and a **terminal** state that is *not
re-scanned*, and left the storage mechanism to this pre-flight (JSONB reuse vs additive columns).

**Decision: reuse the existing `payload` JSONB under a reserved `_email` namespace — no migration.**
This matches ADR-0048's reuse-existing-storage posture and the project's additive discipline.

```jsonc
payload["_email"] = {
  "attempts": 2,
  "state": "pending" | "failed",     // "failed" = terminal (permanent, or attempts exhausted)
  "next_attempt_at": "2026-07-29T…Z",// backoff gate for the next transient retry
  "failed_at": "…", "last_error": "smtp_transient"   // observability (coarse code only)
}
```

- **Exposure is contained (decisive fact §2).** Because `NotificationPublic.payload` is exposed, the
  reserved `_email` key **must not leak**: `_row_to_entity` / the read methods strip any key prefixed
  `_` before building the user-facing `Notification` entity, so the feed/read API is byte-for-byte
  unchanged. The email worker reads `_email` through a **dedicated** claim DTO (below), never the
  user-facing entity.
- **Success uses the existing column.** Delivery stamps `delivered_email_at = now()` (never encoded in
  `payload`), so "delivered" stays unambiguous and a future email-status read is honest.
- **Rejected alternative — additive columns + partial index (a migration).** Cleaner separation and an
  indexable scan, but a migration is **not genuinely necessary** (the payload namespace is correct and
  additive), so per the workflow we do not take it now. See §9.

**New repository methods on `INotificationRepository`** (additive; the write/read paths untouched):

- `list_email_deliverable(*, now, limit) -> list[NotificationEmailDelivery]` — scans
  `delivered_email_at IS NULL AND COALESCE(payload#>>'{_email,state}','pending') <> 'failed' AND
  COALESCE((payload#>>'{_email,next_attempt_at}')::timestamptz, created_at) <= :now`, oldest first;
  returns a small DTO `(id, user_id, title, body, attempts)`.
- `mark_email_delivered(*, notification_id) -> None` — sets `delivered_email_at = now()`.
- `record_email_delivery_failure(*, notification_id, permanent, code, attempts, next_attempt_at) ->
  None` — merges the `_email` namespace (`payload = payload || :patch`), setting `state='failed'` when
  `permanent` or `attempts >= max`.

*Scan-index note:* there is no index on `delivered_email_at IS NULL` today, and adding one is a
migration. At beta volume the partial scan is acceptable; a **supporting partial index is deferred to
a future additive migration** — exactly the α9.2 keyset-index deferral precedent. Documented in §9.

---

## 6. The worker (D2-C) + leasing model + ordering (D1-C)

Two new use cases under `app/application/use_cases/notifications/` (mirroring
`PublishWorker`/`ProcessPublishJob`):

- **`NotificationEmailWorker.run_once() -> EmailPollResult`** — the dedicated poll ingress (NOT a
  relay projection). In a short read UoW it calls `list_email_deliverable(now, batch)`, then delegates
  each id to `ProcessNotificationEmail.process(notification_id)`. Library-only, invoked externally,
  exactly like `ExportWorker`/`PublishWorker` (no cron/endpoint added).
- **`ProcessNotificationEmail.process(notification_id)`** — per-row, under a single lease:
  1. `lease = uow.locks.acquire(key=f"notification_email:{notification_id}", owner, lease=…)`; if
     `None`, another worker holds it → skip.
  2. Re-read the row (still deliverable? already stamped? → release + no-op) and resolve the recipient
     via `uow.users.get_by_id(user_id)`; a missing user/blank email → **permanent** failure.
  3. Build `EmailMessage` (subject ← `notification.title`; `body_text` ← `notification.body` or a
     minimal template; `idempotency_key` ← blake2b of the id).
  4. **Send-then-stamp (D1-C):** `await notifier.send(msg)` **outside** any DB transaction (like
     `ProcessPublishJob` materialises outside a txn); on success → `mark_email_delivered` in a fresh
     short txn; on `NotifierDeliveryError` → `record_email_delivery_failure(...)`.
  5. `uow.locks.release(lease)`.

**Crash/replay (D1-C):** a crash after `send` but before `mark_email_delivered` leaves the row
deliverable → re-scanned → re-sent with the **same** `idempotency_key` (at-least-once; a bounded,
accepted rare duplicate; never lost). The lease prevents two workers sending concurrently.

---

## 7. Retry / backoff / terminal semantics (D3) + observability

- **Transient failure:** `attempts += 1`; if `attempts >= email_max_attempts` → terminal
  (`state='failed'`); else `next_attempt_at = now + min(base * 2**(attempts-1), cap)`
  (capped exponential backoff, mirroring the α8.6b publish retry posture).
- **Permanent failure:** `state='failed'` immediately (not re-scanned).
- **Bounded:** `email_max_attempts` guarantees termination even for mis-classified transients.
- **Observability:** structured logs per attempt — `notification.email.sent` / `.retry` /
  `.failed_permanent` / `.failed_exhausted` with `notification_id`, `user_id`, `attempts`, coarse
  `code`, and a **masked** recipient — never the address, subject, or body (D4). A terminal **outbox
  event** is intentionally **not** emitted in v1 (ADR-0051 deferred it).

---

## 8. Configuration model + fail-soft when disabled (D5)

New `Settings` fields (all optional; mirror the `youtube_oauth_*` / batch / timeout shapes):

| Setting | Default | Purpose |
|---|---|---|
| `email_delivery_enabled` | `False` | Master gate. When `False`, the worker no-ops (claims/stamps nothing → `delivered_email_at` stays NULL, so enabling later can still deliver). |
| `email_from_address` | `None` | Envelope/from; required for the real SMTP adapter. |
| `email_smtp_host` / `email_smtp_port` | `None` / `587` | Real transport endpoint. |
| `email_smtp_username` | `None` | SMTP auth user. |
| `email_smtp_password` | `SecretStr \| None` | Injected into the adapter; never read by it. |
| `email_smtp_use_tls` | `True` | STARTTLS/TLS. |
| `email_send_timeout_seconds` | `30.0` | Per-send `asyncio.wait_for` bound. |
| `email_batch_size` | `10` | Per `run_once` scan size (mirrors `publish_batch_size`). |
| `email_delivery_lease_seconds` | `120` | Per-notification lease duration. |
| `email_max_attempts` | `6` | Bounded retry ceiling. |
| `email_backoff_base_seconds` / `email_backoff_cap_seconds` | `60` / `3600` | Backoff schedule. |

**Fail-soft (mirrors `youtube_oauth_*`):**
- `_get_notifier()` returns `SmtpNotifier` **iff** `email_smtp_host` + `email_from_address` are set,
  else `LoggingNotifier`. Never a boot failure.
- `get_notification_email_worker()` builds the worker with `_get_notifier()`; when
  `email_delivery_enabled` is `False`, `run_once` returns immediately (`scanned=0`).
- CI/tests set `email_delivery_enabled=True` with **no** SMTP config → the deterministic
  `LoggingNotifier` exercises the full path offline.

---

## 9. Migration assessment → **NO migration required**

- **Success state:** `delivered_email_at` already exists (§2).
- **Leasing:** the baseline `distributed_locks` table already backs `uow.locks` (§2).
- **Retry/terminal state:** stored in the existing `payload` JSONB under `_email` (§5) — additive,
  no DDL.
- **Recipient:** `User.email` already exists (§2).

∴ **α9.5 adds no migration.** A migration is *not genuinely necessary*: the payload-namespace design
is correct and additive, so per the workflow we proceed without one. **Accepted documented
limitations** (recorded like the α9.2 root-folder/keyset precedents):
1. The `delivered_email_at IS NULL` scan is **unindexed** at beta volume; a supporting **partial index
   is a deferred future additive migration**.
2. Retry/terminal bookkeeping lives in `payload._email` (reserved, stripped from the read model)
   rather than dedicated columns; a future slice may migrate it to columns + index if volume warrants.

---

## 10. Ownership boundaries (D4) + import-linter (§ contract)

- **API/domain ownership:** no new API endpoint in v1 (delivery is a background worker). The
  **application** worker resolves the owner-scoped recipient (`uow.users.get_by_id`) and constructs the
  neutral `EmailMessage`; the **adapter** only sends. Only the recipient address + rendered
  subject/body cross into the provider — no tokens, no internal ids beyond the opaque
  `idempotency_key`.
- **Import-linter (new contract), mirroring "Destination adapters are credential-blind leaves":**

```toml
[[tool.importlinter.contracts]]
name = "Notifier adapters are PII-minimal leaves (ADR-0051 D4)"
type = "forbidden"
source_modules = ["app.infrastructure.notifications"]
forbidden_modules = [
    "app.application.use_cases",
    "app.api",
    "app.domain",
]
allow_indirect_imports = "True"
```

  The port + DTO live in `app.application.interfaces.notifier`; the worker in
  `app.application.use_cases.notifications` imports only the port + `uow` (satisfies the existing
  "use_cases never import infrastructure" contract); the adapter implements the port and imports only
  `aiosmtplib` + the neutral DTO. The composition root (`app.core.container`) is the sole wiring point.

---

## 11. Architectural-decision check → none beyond ADR-0051

Every choice here is fixed by, or a routine ruling within, the accepted ADR-0051: send-then-stamp +
lease (D1-C), dedicated poll worker (D2-C), bounded retry + terminal state (D3), PII-minimal leaf
(D4), application-owned mock-first config-gated port (D5). The storage mechanism (payload namespace),
transport choice (SMTP/`aiosmtplib`), the gate flag, and backoff constants are implementation details
ADR-0051 explicitly deferred to pre-flight. **No new architectural decision surfaced → no new ADR.**

---

## 12. Test strategy + CI Stage 23

**Unit (`-m unit`, no DB):**
- `LoggingNotifier` masks the recipient and never logs the body; `SmtpNotifier` classifies SMTP
  outcomes into transient vs permanent `NotifierDeliveryError` (against a fake `aiosmtplib` transport).
- `ProcessNotificationEmail`: send-then-stamp ordering; success stamps; transient → attempts++ +
  backoff; permanent → terminal; attempts-exhausted → terminal; missing user → permanent;
  crash-after-send re-sends with the same `idempotency_key` (over fakes); lease contention skips.
- `NotificationEmailWorker.run_once`: batch fan-out; disabled gate → `scanned=0`.
- Schema/read-model: the reserved `_email` key is stripped from `NotificationPublic.payload`.

**Integration (`-m integration`, DB at head) — new file
`tests/integration/infrastructure/notifications/test_email_delivery.py`:**
- Seed a committed notification (unique user per test — teardown deletes its rows), run the worker
  with a `LoggingNotifier`: `delivered_email_at` is stamped exactly once; a second `run_once` is a
  no-op (already delivered).
- Transient-then-success across two `run_once` calls (respecting `next_attempt_at`); permanent →
  terminal (`state='failed'`, never re-scanned); the reserved bookkeeping never appears in the read
  API.
- Owner isolation + auth of the read API remain unchanged (regression).

**CI Stage 23** (append to `ci_gate.py`; each new slice earns its own stage, Stages 15-22 precedent):

```
23. email_delivery  (α9.5 — notification email dispatch worker: send-then-stamp, bounded retry, terminal)
```

Tests roll back inside a SAVEPOINT (or delete their rows) on teardown, so the destructive-migration
guard is untouched.

---

## 13. Implementation order (for the implementation step — not started)

1. Port + DTO (`application/interfaces/notifier.py`).
2. Adapters (`infrastructure/notifications/logging_notifier.py`, `smtp_notifier.py`); add `aiosmtplib`.
3. Repository methods + `_email` read-model stripping (interface + `NotificationRepository` + fakes).
4. `ProcessNotificationEmail` + `NotificationEmailWorker`.
5. Settings (§8).
6. DI wiring (`_get_notifier`, `get_process_notification_email_use_case`,
   `get_notification_email_worker`) — no relay/projection change.
7. Import-linter contract (§10).
8. Unit + integration tests; CI Stage 23.
9. Version bump `0.4.48-phase3-alpha9.5-dev`; CHANGELOG.
10. Full ephemeral-DB CI gate → green → push branch → open `-dev` release-review PR.

---

## 14. Mandatory constraints (for implementation)

- **No migration** (§9); no change to the relay, `InProcessPublisher`, the three projections, or the
  in-app read API contract.
- **Send-then-stamp only**; delivery is never sacrificed to avoid a duplicate (ADR-0051 D1-C).
- **Provider dedup is never relied upon** for correctness (ADR-0051 / Appendix A).
- **Adapter is a config-blind, PII-minimal leaf**; logs never carry the address/subject/body.
- **Bounded retry + a terminal state** that is never re-scanned (D3).
- **Additive only**; behaviour with `email_delivery_enabled=False` is exactly today's (no email, no
  stamping).
