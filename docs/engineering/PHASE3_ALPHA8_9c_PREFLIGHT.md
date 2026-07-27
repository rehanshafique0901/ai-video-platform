# α8.9c — Creator Dashboard · Pre-flight (design decisions)

**Baseline:** `v0.4.41-phase3-alpha8.9b` · **Milestone:** α8.9c (final increment of the α8.9
Creator Experience). Grounded by [`PHASE3_ALPHA8_9c_GROUNDING.md`](./PHASE3_ALPHA8_9c_GROUNDING.md).

**Goal:** expose a **read-only** owner-scoped creator dashboard that summarises existing product
state. **Strictly additive; no analytics subsystem, no charts/reporting, no runtime change.**

## Decisions

### CD1 — Scope: one read-only summary endpoint
Ship a single authenticated `GET /api/v1/dashboard/summary` returning scalar counts assembled from
existing owner-scoped reads. No list/detail/pagination, no time-series, no charts, no export.

### CD2 — Response shape (deterministic scalar summary)
```
DashboardSummaryPublic:
  publish_jobs:     { queued, running, succeeded, failed, canceled, total }
  social_accounts:  { connected, total }
  notifications:    { unread }
  media:            { total }
```
- `publish_jobs` covers all five `PublishStatus` values + `total` (a status the caller has none of
  reports `0`, never absent — a stable shape).
- `social_accounts.connected` counts `AccountStatus.CONNECTED`; `total` is all of the caller's
  accounts.
- `notifications.unread` is the native `count_unread`.
- `media.total` is the caller's live media-asset count. Per-kind media breakdown is **deferred**
  (grounding §1 — no existing media-stat method; keep the surface minimal).

### CD3 — Aggregation strategy: reuse existing owner-scoped reads, aggregate in-app
A new read-only use case `GetCreatorDashboard` opens **one** `IUnitOfWork` and calls the existing
repository reads directly (mirroring `CountUnreadNotifications`):
- `uow.publish_jobs.list_for_owner(tenant_id, owner_user_id)` → group by `status`.
- `uow.social_accounts.list_for_owner(tenant_id, user_id)` → count `CONNECTED` + total.
- `uow.notifications.count_unread(user_id)` → unread.
- `uow.media.list_owned(tenant_id, owner_user_id)` → total.

**No new repository method, no new SQL, no new port.** This directly honours "reuse existing
owner-scoped queries" (task scope). Trade-off: counting materialises the owner's publish/media/account
lists (`O(owned rows)`); for a per-creator dashboard these sets are small (grounding §5). A dedicated
indexed `COUNT(*) … GROUP BY` is the future optimisation and is **explicitly deferred** — adding it
later is itself additive (read-only methods on existing repositories, no migration).

### CD4 — Ownership / auth
All scope derives from `CurrentUserDep` (`user.id`, `user.tenant_id`) — never from body/query.
Notifications scope by `user_id` alone (consistent with `count_unread`). No cross-tenant read.

### CD5 — No migration, no new port, no ADR
Every number is a read over already-migrated tables (`publish_jobs`, `social_accounts`,
`notifications`, `media_assets`). No frozen boundary crossed (orchestration/execution/generation/
render/export/publish runtime, Asset Promotion Bridge, AI, Planner, verification all untouched). If
implementation surfaces a contradiction, stop and propose an ADR — none is expected.

### CD6 — Analytics explicitly out of scope
No analytics subsystem, no `analytics_events` writes/reads (the dormant baseline table stays
untouched), no charts, reporting, email, push, scheduler, AI, Planner, generation/render/export/
publish runtime changes, and no second destination.

## Files (additive)

| File | Change |
|---|---|
| `application/use_cases/dashboard/__init__.py` + `get_creator_dashboard.py` | new read-only use case + result dataclasses |
| `api/v1/schemas/dashboard.py` | `DashboardSummaryPublic` (+ nested count models) |
| `api/v1/routers/dashboard.py` | `GET /dashboard/summary` (thin: `envelope(...)`) |
| `core/container.py` | `get_creator_dashboard_use_case()` factory |
| `api/v1/deps.py` | `CreatorDashboardDep` |
| `app/main.py` | include `dashboard.router`; version → `0.4.42-phase3-alpha8.9c-dev` |
| `CHANGELOG.md` | α8.9c entry |
| tests (unit + integration API) | coverage (below) |

## Testing
- **Unit — use case:** a fake UoW returns seeded publish jobs (mixed statuses), social accounts
  (mixed statuses), an unread count, and media assets; assert the summary groups/counts exactly and
  reports `0`s for absent statuses (stable shape). Owner isolation is implicit (repositories are
  owner-scoped; the use case passes the authenticated scope through).
- **Integration — API (new Stage 17):** register a user, seed **committed** owner-scoped rows across
  publish jobs / social accounts / notifications / media, call `GET /api/v1/dashboard/summary` with
  the minted token, assert the envelope counts; assert a second fresh user sees all-zero (owner
  isolation); assert `401` unauthenticated. Clean up committed rows on teardown.

**CI:** the dashboard is a **new read surface composed across bounded contexts**, so — following the
established "each new slice earns its own stage" discipline (Stage 15 promotion, Stage 16
notifications) — its DB-backed integration test runs as a new **Stage 17**. Unit aggregation logic
runs in Stage 4.

## Release
Hold at `0.4.42-phase3-alpha8.9c-dev`; one feature commit; push; open release-review PR; stop.
