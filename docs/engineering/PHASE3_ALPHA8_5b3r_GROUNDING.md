# Phase 3 — α8.5b.3r Grounding: Notifications Read API

> Status: **GROUNDING — evidence gathered from the codebase; awaiting fork rulings before a
> pre-flight.** The read/query half of the notifications bounded context, landing on the stable
> α8.5b.3 write projection. Baseline: `v0.4.33-phase3-alpha8.5b3`. Companion:
> `PHASE3_ALPHA8_5b3_GROUNDING.md` + `PHASE3_ALPHA8_5b3_PREFLIGHT.md` (the write projection).
>
> **The one question this slice answers:** *Given the stable notification projection, how do users
> safely and efficiently read and manage it?* Nothing about delivery channels (α8.5b.4) or
> publishing (α8.6) is in scope. This is a **pure query + read-state surface** over rows that
> already exist.

---

## 1. Gate 1 — ADR-0042 (orchestration freeze): **PASS (query-only)**

The slice is read/read-state only. Evidence that it touches **none** of the frozen surface:

- **No orchestration paths.** `notifications` / `Notification` appear in **zero** orchestration
  files (grep across `backend/app`): the only references are the α8.5b.3 write path
  (`domain/notifications/`, `application/use_cases/notifications/`,
  `infrastructure/repositories/notification_repository.py`, the UoW wiring, the model) plus the
  container/interfaces. The runner, `AdvanceWorkflowRun`, `CompletionEngine`, dispatcher, and
  registry are untouched.
- **No provider ports.** No `ProviderPort` / dispatcher / registry / provider-adapter change — the
  read side never dispatches or resolves provider work.
- **No render/export lifecycle changes.** `RenderJob` / `ExportJob` state machines, their workers,
  and `IRenderer`/`IExporter` are not referenced.
- **No outbox changes.** `event_outbox`, `IEventOutboxRepository`, and the event emitters are not
  touched — the read side never reads or writes the outbox.
- **No relay changes.** `RelayService` / `PublisherPort` / `InProcessPublisher` /
  `NotificationProjection` are not modified. The read API is *decoupled* from the projection: it
  reads the rows the projection already committed.

**What the slice actually adds (all additive growth-surface):** additive **query/read-state methods**
on `INotificationRepository`, new **read use cases**, a new **router** (`/notifications`), new
**DTOs**, and DI wiring. Registering a new router + use cases in the composition root is exactly the
growth pattern every α8.5x slice used. **Expected: PASS, zero freeze overrides.**

> **Gate 2 in the pre-flight sense (ADR-0043 render boundary): PASS trivially** — notifications are
> far downstream of composition; no `IRenderer`/`IExporter`/timeline/mix code is in scope.

---

## 2. Gate 2 — the existing projection contract (what α8.5b.3 actually stores)

Grounding the read model against the real schema + code, **not** an invented one.

### 2.1 Notification aggregate fields

Two shapes exist today:

**ORM row** — `backend/app/infrastructure/db/models/notifications.py` (`Notification`):

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | `UUIDPrimaryKeyMixin` |
| `user_id` | uuid NOT NULL | FK → `users.id` ON DELETE CASCADE — **the owner** |
| `kind` | text NOT NULL | `export.succeeded` / `export.failed` (α8.5b.3) |
| `title` | text NOT NULL | |
| `body` | text NULL | |
| `payload` | jsonb NOT NULL DEFAULT `'{}'` | delivery-identity subset |
| `source_event_id` | uuid NULL | α8.5b.3 dedupe key (no FK) |
| `delivered_in_app_at` | timestamptz NULL | stamped at insert (in-app delivery) |
| `delivered_email_at` | timestamptz NULL | **unused** (email → α8.5b.4) |
| `read_at` | timestamptz NULL | **mutable read-state — already present** |
| `archived` | boolean NOT NULL DEFAULT false | **mutable read-state — already present** |
| `created_at` / `updated_at` | timestamptz | `TimestampMixin` (trigger-owned `updated_at`) |

**Domain entity** — `backend/app/domain/notifications/notification.py` (`Notification`): the *slim*
write-path projection. It carries `id, user_id, kind, title, body, payload, source_event_id,
delivered_in_app_at, created_at, updated_at` — but **not** `read_at` or `archived` (the write path
never needed them). The read side will need those two fields on the domain entity (a **code change,
not a migration** — the columns already exist).

### 2.2 Indexes (all already implemented — `docs/database/INDEX_STRATEGY.md` §183–184, §266)

| Index | Shape | Purpose (per INDEX_STRATEGY) |
|---|---|---|
| `ix_notifications_user_id_created_at` | `(user_id, created_at)` | **"Notification feed"** → serves `GET /notifications` |
| `ix_notifications_user_id_unread` | partial `(user_id, created_at) WHERE read_at IS NULL AND archived = false` | **"Badge counts"** → serves unread-count |
| `uq_notifications_user_id_source_event_id` | partial unique `(user_id, source_event_id) WHERE source_event_id IS NOT NULL` | α8.5b.3 exactly-once (write side) |

The two read indexes the API needs **already exist and are named for exactly these queries.**

### 2.3 Ownership model

Ownership is **direct and single-axis**: a notification belongs to one `user_id` (FK → `users.id`).
Unlike `media_assets` (owner + `tenant_id`) or `projects` (owner + tenant), `notifications` has **no
`tenant_id` and no `project_id`** — the recipient is the sole scoping axis. So every read/mutation
is scoped by `user_id = current_user.id`, nothing more.

### 2.4 Uniqueness invariant

`(user_id, source_event_id)` partial-unique (W8.5b.7) — a **write-side** invariant. The read side
must **never mutate `source_event_id`** (it is projection identity), which motivates proposed
**W8.5b.9** below.

### 2.5 Read / update methods already present

**None.** `INotificationRepository` (`backend/app/application/interfaces/repositories.py`) exposes
**only** `add(...)`. There is no list, no get-by-id, no count, no mark-read, no update of any kind.
There is **no notifications router** and **no read use case** (grep confirms zero `/notifications`
routes). The read surface is a greenfield addition over an existing, populated table.

---

## 3. Grounding questions — answered with code evidence

### 3.1 Repository surface
- **What exists:** `INotificationRepository.add(...)` only (write path).
- **What is actually needed (all additive, all pure queries/scoped-CAS — no projection change):**
  1. `list_for_user(user_id, *, limit, after)` — keyset page (mirrors
     `ProjectRepository.list_owned`, §3.3).
  2. `count_unread(user_id)` — scalar `func.count()` over the partial-unread predicate (§3.4).
  3. `mark_read(user_id, notification_id)` — scoped CAS (§3.5).
  4. `mark_all_read(user_id)` — scoped bulk update (§3.5).
- These sit alongside `add` on the same port; no existing method changes.

### 3.2 Ownership precedent
- **Which endpoints establish the pattern:** `media` (`backend/app/api/v1/routers/media.py`) is the
  closest analog — a **top-level, owner-scoped** resource (`/media`, not nested under a project),
  authed via `CurrentUserDep`, every call scoped by `current_user.id`. `GetMedia`/`ListMedia` pass
  `owner_user_id=current_user.id`; `MediaRepository` filters on it, and a foreign/missing row
  returns `None` → the use case raises `NotFoundError` → uniform **404** (anti-enumeration).
  `projects` `get_owned` uses the identical "return None → 404, indistinguishable" gate (α5a D5).
- **Should notifications follow it verbatim?** **Yes, simplified**: same `CurrentUserDep` + repo
  scoping + uniform-404 gate, but scoped by **`user_id` only** (no `tenant_id` — the table has no
  such column). `/notifications` is top-level like `/media`.

### 3.3 Pagination
- **Existing convention:** **keyset (cursor)**, not offset. `GET /projects`
  (`backend/app/api/v1/routers/projects.py`) uses `?limit=` (default 20, `ge=1, le=100`) + opaque
  `?cursor=` → `meta.next_cursor`. The primitive is `app/application/pagination.py`
  (`Cursor(created_at, id)`, `Page`, `encode_cursor`/`decode_cursor`; a bad token → 422). The repo
  keyset predicate is a row-value comparison `tuple_(created_at, id) < (after_created_at, after_id)`
  with `ORDER BY created_at DESC, id DESC` (`ProjectRepository.list_owned`).
- **Offset?** No — the codebase deliberately avoids it (pagination.py docstring: keyset "so pages
  stay stable under concurrent inserts"). Notifications are **insert-heavy**, so keyset is doubly
  correct here.
- **Stable ordering field:** `(created_at DESC, id DESC)` — the same total order, reusing `Cursor`
  verbatim. The existing `ix_notifications_user_id_created_at (user_id, created_at)` backs the
  `user_id` equality + `created_at` ordering; the `id` tie-break resolves within equal-timestamp
  rows (cheap, no new index needed). **Do not introduce a second pagination style.**

### 3.4 Unread counts
- **Dedicated query or aggregate-during-list?** **Dedicated.** A badge count is needed independently
  of any list page (and often when no list is loaded), so folding it into list meta would couple two
  concerns and mis-count under pagination. Precedent for a scalar count exists
  (`UserRepository.count` / `SceneRepository` use `select(func.count())`).
- **Existing index support:** **Yes** — `ix_notifications_user_id_unread` is the partial index whose
  documented purpose is literally *"Badge counts"* (`WHERE read_at IS NULL AND archived = false`).
  The count query's predicate matches the index predicate exactly.

### 3.5 Mark-read semantics
- **Single:** a scoped CAS `UPDATE notifications SET read_at = now() WHERE id = :id AND user_id =
  :uid AND read_at IS NULL RETURNING …`. Precedent: `IExportJobRepository.record_download` (scoped
  update returning the row or `None` when no row matched) and `ProjectRepository.update_owned`
  (owner-scoped `RETURNING`). A missing/foreign id → `None` → uniform **404**; an
  already-read row → idempotent success (no-op).
- **Bulk mark-all:** `UPDATE notifications SET read_at = now() WHERE user_id = :uid AND read_at IS
  NULL` — returns the count affected (0 is a valid no-op). Served by the same unread partial index.
- **Archive / soft-delete:** the `archived` column **exists** but is **out of scope for 3r** (see
  Fork C + §6). No delete endpoint.
- **Which belong in 3r vs later:** **3r = single read + bulk mark-all-read.** Archive/delete → a
  later slice only if a product need appears.

### 3.6 State model — is another migration unnecessary?
- **Mutable fields already present:** `read_at` (nullable timestamptz) and `archived` (bool). Both
  ship in the α8.5b.3 baseline schema. Mark-read writes `read_at`; archive (deferred) would write
  `archived`.
- **Verdict: zero migration.** The read API needs no new column and no new index — the feed index,
  the unread partial index, and the two read-state columns all pre-date this slice. The only code
  change to the aggregate is **adding `read_at` + `archived` to the domain `Notification` entity**
  (so it is a faithful read DTO) — not a schema change.

### 3.7 HTTP surface (grounded against router conventions, not invented)
Proposed, consistent with the established conventions:

| Method + path | Maps to | Convention grounded in |
|---|---|---|
| `GET /notifications?limit=&cursor=` | `ListNotifications` → keyset `Page` | `GET /projects` (keyset list) + `GET /media` (top-level owner list) |
| `GET /notifications/unread-count` | `CountUnreadNotifications` → `{count}` | scalar-count precedent; `ix_notifications_user_id_unread` |
| `POST /notifications/{id}/read` | `MarkNotificationRead` | `POST /…/render-jobs/{id}/cancel` (action-verb state transition) |
| `POST /notifications/read-all` | `MarkAllNotificationsRead` | same action-verb precedent |

Notes: state-transition **actions** use `POST /{id}/<verb>` in this codebase (render-job
`cancel`), so `POST /{id}/read` + `POST /read-all` are consistent (rather than a `PATCH` with a
body). All responses use the standard envelope (`app/api/v1/helpers.py::envelope`); the list
response carries `meta.next_cursor`. **No API_CONTRACT.md currently pins notification endpoints**
(grep: none), so this surface is grounded purely on router precedent, as requested.

### 3.8 Authorization
Every query/mutation is constrained by `requested user → owned notifications only`:

```
CurrentUserDep (current_user.id)
        │
        ▼
use case passes user_id = current_user.id
        │
        ▼
repository filters WHERE user_id = :uid   ← the single enforcement point
```

The **repository methods are the enforcement point** (mirrors `MediaRepository`/`ProjectRepository`
owner scoping): `list_for_user` / `count_unread` filter by `user_id`; `mark_read` includes
`user_id = :uid` in the CAS `WHERE` so a foreign id can neither be read nor mutated (returns `None`
→ 404). No cross-user read or write is expressible.

---

## 4. Forks for the pre-flight (grounded recommendations)

### Fork A — Query scope
**Recommend:** `list` + `unread-count` + `mark-read` (single) + `mark-all-read`. **Archive/delete
deferred.** (Matches the reviewer's expected A.)

### Fork B — Pagination
**Recommend:** reuse `app/application/pagination.py` keyset verbatim (`?limit=`/`?cursor=` →
`meta.next_cursor`, order `created_at DESC, id DESC`). **No second pagination style.** (Matches B.)

### Fork C — Mark-read
**Recommend:** single + bulk mark-all-read. **No archive, no delete** in 3r. (Matches C.)

### Fork D — Repository
**Recommend:** pure additive query/scoped-CAS methods on `INotificationRepository`; **no projection
change**, no change to `add` or the write path. (Matches D.)

### Fork E — Ordering/index (the one genuinely open sub-question)
The existing `ix_notifications_user_id_created_at` is `(user_id, created_at)` — it backs the feed
scan, with the `id` tie-break resolved within equal-timestamp rows. **Recommend: ship zero-migration
and rely on the existing index** (notification volume per user is low; equal-microsecond collisions
are rare and cheap to sort). *If* a strict composite `(user_id, created_at, id)` is ever wanted for
perfectly index-only keyset, that is an additive index in a later slice — **not** needed for 3r.

---

## 5. Proposed invariants (for the pre-flight to ratify)

- **W8.5b.8 — Notification queries never expose notifications outside the requesting principal.**
  Every read/read-state method is scoped by `user_id = current_user.id` at the repository layer;
  a foreign or missing id is indistinguishable (uniform 404). No endpoint can enumerate or mutate
  another user's notifications. *(Grounded in the media/project owner-visibility gate.)*
- **W8.5b.9 — Read-state mutations affect only notification metadata and never alter projection
  identity or source-event linkage.** `mark_read` / `mark_all_read` write only `read_at` (and, if
  ever added, `archived`); they never touch `id`, `user_id`, `kind`, `title`, `body`, `payload`, or
  `source_event_id`. The read side cannot re-project, re-key, or break the α8.5b.7 exactly-once
  linkage. *(Keeps the read model strictly downstream of the write projection — the read/write
  split established at α8.5b.3 sign-off.)*

---

## 6. Explicitly out of scope (not an inbox product)

Per the reviewer's guardrail, 3r exposes the projection and nothing more. **Deferred/excluded:**
folders, labels, filtering DSL, full-text search, priority, pinning, snooze, archive, delete, email
/ push / websocket delivery (α8.5b.4), and any cross-context aggregation. These are product
features, not platform capabilities; adding them here would turn a projection read-surface into an
inbox app.

---

## 7. Migration verdict

**Zero migration.** The feed index, the unread partial index, and the `read_at` / `archived`
read-state columns all pre-date this slice (α8.5b.3 baseline). The only aggregate change is a
**code-level** extension of the domain `Notification` entity to carry `read_at` + `archived`.

---

## 8. Open questions for sign-off (fork rulings)

1. **Fork A/C scope** — confirm 3r = list + unread-count + mark-read + mark-all-read; archive/delete
   deferred?
2. **Fork E (index)** — accept zero-migration on the existing `(user_id, created_at)` index, or
   pre-emptively add a composite `(user_id, created_at, id)` (would make it one small additive
   migration)?
3. **HTTP verbs** — accept `POST /{id}/read` + `POST /read-all` (action-verb precedent), or prefer
   `PATCH /notifications/{id}` with a `{read: true}` body?
4. **Unread-count shape** — dedicated `GET /notifications/unread-count` returning `{ "count": N }`
   (recommended), or also surface `unread_count` in the list `meta`?
5. **W8.5b.8 / W8.5b.9** — adopt as worded, or adjust?
6. **Version** — `0.4.34-phase3-alpha8.5b3r` (runtime capability). Confirm the `3r` suffix scheme.
