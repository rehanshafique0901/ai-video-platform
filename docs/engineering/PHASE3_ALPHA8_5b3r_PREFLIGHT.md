# Phase 3 — α8.5b.3r Pre-Flight: Notifications Read API

> Status: **SIGNED OFF — all forks ruled; proceeding to implement.** The read/query half of the
> notifications bounded context, landing on the stable α8.5b.3 write projection. Input:
> `PHASE3_ALPHA8_5b3r_GROUNDING.md`. Baseline: `v0.4.33-phase3-alpha8.5b3`.
>
> **Rulings (SIGNED OFF):** Gate 1 **PASS** · Gate 2 **PASS** · **A1** (list + unread-count +
> mark-read + mark-all-read; everything else out) · **B1** (reuse existing keyset pagination) ·
> **C1** (`POST /{id}/read` + `POST /read-all` action verbs) · **D1** (pure additive repo methods) ·
> **E1** (use the existing `(user_id, created_at)` index; **zero migration**) · **W8.5b.8**,
> **W8.5b.9**, **W8.5b.10** (new) adopted · domain `Notification` entity extended with
> `read_at` + `archived` · version `0.4.34-phase3-alpha8.5b3r`.

---

## 1. Gates

- **Gate 1 — ADR-0042 (orchestration freeze): PASS.** Read-model slice only. `notifications`
  touches **no** orchestration path, provider port, render/export lifecycle, outbox, or relay
  (grounding §1). The slice adds additive repo query methods, read use cases, a router, and DTOs —
  a composition-root growth-surface change. Freeze guard expected green, **zero overrides**.
- **Gate 2 — projection contract intact: PASS.** The slice only **exposes and updates notification
  metadata** (`read_at`) without changing how notifications are projected or created (`add`, the
  projection, the write path, and `source_event_id` linkage are untouched).

---

## 2. The shape of the slice

α8.5b.3 established `Immutable Event → Exactly-once Projection → Notification row`. α8.5b.3r adds the
**query surface** on top of that stable foundation — nothing more:

```
Notification row  ──►  Query model (list / count / mark-read)  ──►  User API
```

The read API is **decoupled** from the projection: it reads the rows the projection already
committed and mutates only their read-state metadata. No new architectural concept is introduced.

---

## 3. Forks (all ruled)

### Fork A — Query scope *(RULED: A1)*
Ship exactly four endpoints:

| Method + path | Use case |
|---|---|
| `GET /notifications?limit=&cursor=` | `ListNotifications` (keyset page, newest first) |
| `GET /notifications/unread-count` | `CountUnreadNotifications` → `{ "count": N }` |
| `POST /notifications/{id}/read` | `MarkNotificationRead` (idempotent) |
| `POST /notifications/read-all` | `MarkAllNotificationsRead` → `{ "updated": N }` |

**Out of scope (explicitly):** archive, delete, search, filters, folders, labels, pinning, snooze,
priority, email/push/websocket. This is a read-model **completion**, not an inbox product.

### Fork B — Pagination *(RULED: B1)*
Reuse `app/application/pagination.py` verbatim (`Cursor(created_at, id)`, `Page`,
`encode_cursor`/`decode_cursor`; `?limit=` default 20 / `1..100`, opaque `?cursor=` →
`meta.next_cursor`; bad token → 422). Ordering `created_at DESC, id DESC`. **No** offset, **no**
notification-specific cursor format, **no** alternate paging. Notifications become another consumer
of the existing abstraction.

### Fork C — Verb *(RULED: C1)*
`POST /notifications/{id}/read` + `POST /notifications/read-all` (action semantics), matching the
`POST /…/render-jobs/{id}/cancel` convention — not `PATCH`. Internally consistent with the platform.

### Fork D — Repository *(RULED: D1)*
Pure additive methods on `INotificationRepository`; the write path (`add`) and projection code are
untouched:
- `list_for_user(user_id, *, limit, after)` — keyset page (mirrors `ProjectRepository.list_owned`).
- `count_unread(user_id)` — scalar `func.count()` over the unread predicate.
- `mark_read(user_id, notification_id)` — scoped CAS `… SET read_at = now() WHERE id = :id AND
  user_id = :uid AND read_at IS NULL RETURNING …`; returns the row, or `None` when no row matched
  (missing/foreign → 404; already-read → idempotent success via a follow-up scoped read).
- `mark_all_read(user_id)` — scoped bulk `… SET read_at = now() WHERE user_id = :uid AND read_at IS
  NULL`; returns the affected count.

### Fork E — Index *(RULED: E1 — existing index, zero migration)*
Use the existing `ix_notifications_user_id_created_at (user_id, created_at)` (feed) and
`ix_notifications_user_id_unread` (partial, badge counts). The `id` keyset tie-break resolves within
equal-timestamp rows (cheap). **Composite keyset index `(user_id, created_at, id)` intentionally
deferred pending observed production need** — it is an optimization, not a capability; the platform's
cadence is prove-correctness-first, optimize-when-justified, not speculative indexing.

---

## 4. New surface (all additive)

| Layer | Addition |
|---|---|
| **Domain** | Extend `Notification` with `read_at: datetime \| None` + `archived: bool` (now legitimate domain state, not repo-only details). |
| **Application (port)** | `INotificationRepository`: `list_for_user`, `count_unread`, `mark_read`, `mark_all_read` (additive; `add` unchanged). |
| **Application (use cases)** | `ListNotifications` (keyset `Page[Notification]`), `CountUnreadNotifications`, `MarkNotificationRead` (404 on missing/foreign; idempotent on already-read), `MarkAllNotificationsRead` (returns count). |
| **API** | `/notifications` router (4 endpoints, `CurrentUserDep`); `NotificationPublic` DTO + `{count}` / `{updated}` response models; 4 `*Dep` aliases in `deps.py`; router registered in `routers/__init__.py`. |
| **Composition root** | 4 `get_*_use_case()` factories (fresh UoW per call). |
| **Migration** | **None** (E1). |
| **Config** | **None.** |

**`NotificationPublic` wire shape:** `id, kind, title, body, payload, source_event_id, read_at,
created_at` (+ `updated_at`). `delivered_email_at` / `archived` are internal for now (archived is
deferred; email is α8.5b.4) — kept off the wire to avoid implying capabilities that do not exist.

---

## 5. Invariants

- **W8.5b.8 (new) — Notification queries never expose notifications belonging to another
  principal.** Every read/read-state method is scoped by `user_id = current_user.id` at the
  repository layer; a foreign or missing id is indistinguishable (uniform 404). The read-side
  ownership invariant.
- **W8.5b.9 (new) — Read-state mutations modify only notification metadata and never alter
  projection identity, source-event linkage, or delivery provenance.** `mark_read` /
  `mark_all_read` write only `read_at`; they never touch `id`, `user_id`, `kind`, `title`, `body`,
  `payload`, `source_event_id`, or `delivered_in_app_at`. Mirrors W8.5b.6/7 — the read side cannot
  re-project or re-key.
- **W8.5b.10 (new) — Notification ordering is observational only; read-state mutations must not
  affect feed ordering.** The feed is ordered by creation/projection time (`created_at DESC, id
  DESC`); marking one read, or marking all read, never moves a notification or reshuffles the feed.
  Order is a pure function of `(created_at, id)`, independent of `read_at`. Keeps the feed stable and
  predictable across reads.

---

## 6. Authorization

```
CurrentUserDep (current_user.id)  ──►  use case (user_id = current_user.id)  ──►
        repository WHERE user_id = :uid   (the single enforcement point)
```

The repository methods are the enforcement point (mirrors `MediaRepository`/`ProjectRepository`
owner scoping). `mark_read`'s CAS includes `user_id = :uid`, so a foreign id can neither be read nor
mutated. No cross-user read or write is expressible (W8.5b.8).

---

## 7. Migration verdict

**Zero migration.** The feed index, the unread partial index, and the `read_at` / `archived`
columns all pre-date this slice (α8.5b.3 baseline). The only aggregate change is **code-level**:
extending the domain `Notification` entity.

---

## 8. Versioning & deliverable

- Version bump: **`0.4.34-phase3-alpha8.5b3r`** (runtime capability).
- Deliverable: the notifications bounded context is complete — the projection written by α8.5b.3 is
  now safely and efficiently **readable and manageable** (list, unread count, mark-read, mark-all)
  behind owner-scoped, keyset-paginated endpoints, with the export/render/orchestration contracts
  and the write projection entirely unchanged.

---

## 9. Sign-off checklist

- [x] **Gate 1** ADR-0042 PASS (read-model; no frozen surface) · **Gate 2** PASS (projection intact)
- [x] **A1** four endpoints only; archive/delete/search/… out
- [x] **B1** reuse keyset pagination (no offset / no new cursor format)
- [x] **C1** `POST /{id}/read` + `POST /read-all` (action verbs)
- [x] **D1** additive repo methods only; projection untouched
- [x] **E1** existing index; **zero migration**; composite index deferred pending observed need
- [x] **W8.5b.8 / W8.5b.9 / W8.5b.10** adopted
- [x] Domain `Notification` extended with `read_at` + `archived`
- [x] Version `0.4.34-phase3-alpha8.5b3r`

---

## 10. Test plan (on sign-off)

- **`ListNotifications`** — newest-first keyset page; `limit + 1` over-fetch → `next_cursor` set iff
  a further page exists; last page → `next_cursor` None; owner-scoped (another user's rows never
  appear — W8.5b.8); bad cursor → 422; empty → `200 []`.
- **`CountUnreadNotifications`** — counts only `read_at IS NULL AND archived = false` for the caller;
  0 when none; unaffected by other users' rows.
- **`MarkNotificationRead`** — marks own unread → `read_at` set; already-read → idempotent success;
  missing/foreign id → 404 (W8.5b.8); never alters identity/payload/`source_event_id` (W8.5b.9).
- **`MarkAllNotificationsRead`** — marks all the caller's unread, returns count; second call → 0
  (idempotent); scoped to the caller.
- **Ordering (W8.5b.10)** — marking read / mark-all does not change list order (order is a pure
  function of `(created_at, id)`).
- **Repository (integration)** — `list_for_user` keyset correctness + owner isolation;
  `count_unread` matches the partial-index predicate; `mark_read` scoped CAS (foreign → None);
  `mark_all_read` affected count.
- **Router (integration)** — the four endpoints wired, `CurrentUserDep` 401, envelope +
  `meta.next_cursor` on list; `POST` verbs return the updated resource / counts.
- **Freeze guard** green; full gate (ruff/black/mypy/import-linter/pytest/coverage + live-DB stages)
  green. **No migration** (stages 5–7 unchanged from baseline).
