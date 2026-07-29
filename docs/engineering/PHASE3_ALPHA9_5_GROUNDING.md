# α9.5 — Notification Delivery (Email) — Grounding (read-only)

> **Status:** Read-only discovery grounding. **Facts only** — no implementation, no design.
> **Outcome: an ADR is genuinely required** (see §4) → **STOP** per the workflow (do not proceed to
> pre-flight/implementation until the ADR is authored + accepted).
>
> **Baseline:** `v0.4.47-phase3-alpha9.4` (frozen). Selected from
> [`NEXT_VERTICAL_SLICES_DISCOVERY.md`](./NEXT_VERTICAL_SLICES_DISCOVERY.md) **§2.7** — this is the
> roadmap's **α8.5b.4**. Every claim below is grounded in a named file / column / event / doc.

---

## 1. Selection rationale (why this slice, why now)

The in-app notification loop is complete (α8.5b.3 projection → α8.5b.3r read API → α8.9a publish
notifications). The next product step is **off-platform reach**: creators learn that a publish /
export succeeded or failed **without polling the app** — essential for a beta where users are not
watching the dashboard. Of the discovery's remaining runners-up, additional destinations (§2.6)
are **external-risk** (platform app-review / API approval — not completable in-repo), so email
delivery (§2.7, the discovery's named companion to publish notifications) is the highest-value
**completable, product-facing** slice.

---

## 2. Decisive grounding facts

**F1 — the email column already exists; no migration needed.** `notifications.delivered_email_at
timestamptz` was created in `0001_baseline` (baseline DDL) and is on the ORM
(`models/notifications.py:47`). It is **dormant** — the write path only stamps `delivered_in_app_at`
(`notification_repository.py:63`); `delivered_email_at` is never written and is intentionally
omitted from `NotificationPublic` and the domain entity.

**F2 — there is NO email infrastructure.** A backend-wide search for
`INotifier` / `Notifier` / `mailer` / `smtp` / `ses` / `sendgrid` / `EmailAdapter` returns **zero
matches**. No port, no adapter dir, no `smtp_*`/`email_*` settings. The recipient address exists
(`User.email`, CITEXT NOT NULL, `models/identity.py:57`) but is not wired to notifications.

**F3 — the docs reserve this exact slice + name the port.** Multiple contracts defer email to
**α8.5b.4** and name **`INotifier`** as the planned port (`PHASE3_ALPHA8_5b3_PREFLIGHT.md` Fork A;
`PHASE3_CREATOR_EXPERIENCE_PREFLIGHT.md`; `PLATFORM_STATUS.md` roadmap line; ADR-0041). The
discovery (`§2.7`) states plainly: *"a small ADR for external-send idempotency/retries is
appropriate."*

**F4 — reusable substrate exists.**
- *Write/idempotency:* the relay + `InProcessPublisher` fan-out already feeds
  `NotificationProjection` / `PublishNotificationProjection` → `CreateNotification`, with DB-enforced
  exactly-once per `(user_id, source_event_id)` (`uq_notifications_user_id_source_event_id`).
- *Poll-worker + lease:* `ExportWorker` / `PublishWorker.run_once()` + `Process*` under
  `SqlAlchemyDistributedLockManager` is a proven poll-ingress template; a Fork-C precedent
  (`PHASE3_ALPHA8_5b3_GROUNDING.md`) already floated "a worker that scans `delivered_email_at IS
  NULL`".
- *Config-gated fail-soft adapter:* `_get_destination_registry()` registers `MockDestination`
  always and the real `YouTubeDestination` **only when configured** (`youtube_oauth_*`), never
  failing boot — the exact pattern for "mock in CI, real provider iff configured" (also
  `openai_api_key` → real vs mock IMAGE provider).

**F5 — read model + repository have no delivery-channel methods.** `INotificationRepository` exposes
only `add` / `list_for_user` / `count_unread` / `mark_read` / `mark_all_read`. There is **no**
method to list/claim/stamp email delivery.

**F6 — CI head stage is 22** (`ci_gate.py`); the next slice earns **Stage 23**.

**F7 — ADR inventory tops out at ADR-0050**; the next ADR is **ADR-0051**.

---

## 3. Missing pieces (what the slice would add — pending ADR)

- *Application:* a new `INotifier` port + an email-delivery use case/worker (dispatch of undelivered
  notifications) + a repository method to list-undelivered + stamp `delivered_email_at`.
- *Infrastructure:* a **mock/log** email adapter (deterministic, CI-safe, always registered) + a
  real config-gated SMTP/provider adapter (fail-soft) + template rendering.
- *Config:* optional `email_*` / `smtp_*` settings (fail-soft, mirroring `youtube_oauth_*`).
- *Persistence:* **none for email** (`delivered_email_at` exists — F1); push would need new columns
  (explicitly out of scope).
- *API:* none required for v1 send (optional channel-preference endpoints are a later concern).
- *Testing:* delivery worker + adapter fakes; a new CI stage.

---

## 4. Architectural-decision check → **ADR REQUIRED (ADR-0051)** → STOP

This slice introduces the platform's **first outbound external-communication channel that is not a
publish destination** (publishing has its own ADR-0047). That is a new cross-cutting subsystem, and
it surfaces genuine, load-bearing decisions that are **not** mere pre-flight rulings:

| # | Decision | Why it is ADR-level |
|---|---|---|
| D1 | **External-send idempotency / effect ordering.** An email send is an external side-effect that **cannot be transactionally rolled back**. Stamp-before-send risks a lost email (stamp commits, send fails); send-then-stamp risks a **duplicate** email (send succeeds, stamp fails, retry re-sends). At-least-once relay/worker delivery makes duplicates the default failure mode. | Same class of "exactly-once external effect" correctness decision as PUB-11 (ambiguous publish) and ADR-0048 (analytics dedup). Must be decided + recorded, not assumed. |
| D2 | **Dispatch mechanism.** A **poll worker** scanning `delivered_email_at IS NULL` (external I/O off the relay's critical path; natural retry) **vs** a **fourth relay projection** (immediate, but couples external I/O to the outbox drain). | Determines where a new external I/O boundary sits relative to the frozen relay/outbox; a structural architectural choice. |
| D3 | **Retry / backoff / terminal-failure semantics** for transient vs permanent send failures, and what "permanently undeliverable" means for a notification row. | A new failure taxonomy for an external channel (mirrors the α8.6b retry ADR discussion). |
| D4 | **PII boundary.** Recipient email + rendered subject/body (potentially containing titles) cross into an external adapter. Which layer resolves the address, and what may cross the boundary. | A new data-egress boundary; needs an explicit, recorded rule (kin to the credential-blindness rule in ADR-0047). |
| D5 | **Template ownership + provider abstraction** (the `INotifier` port shape + neutral message DTO; mock-first, config-gated real provider). | Defines a new port + adapter contract for the bounded concern. |

The repository's own docs (`§6` of the exploration) and the discovery report both explicitly
anticipate an ADR for this slice. Per the workflow — *"Stop only if a genuine architectural
decision requires a new ADR"* — this is a **hard stop**. The proposed ADR is **ADR-0051 —
Notification Delivery Channel (email) idempotency & boundary**.

**No implementation, no pre-flight, and no ADR draft has been produced.** Awaiting direction on
authoring ADR-0051 (options/recommendation to be developed on approval), after which the standard
pre-flight → implementation → gate → release flow resumes.
