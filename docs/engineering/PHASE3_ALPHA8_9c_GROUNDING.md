# α8.9c — Creator Dashboard · Grounding (read-only)

**Baseline:** `v0.4.41-phase3-alpha8.9b` · **Status:** facts only, no design.

Final increment of the α8.9 Creator Experience milestone. Verifies, against the source at the
baseline, exactly which owner-scoped reads already exist to compose a **read-only** creator
dashboard, and whether any migration / new port / ADR is required. Narrows the umbrella
[`PHASE3_CREATOR_EXPERIENCE_GROUNDING.md`](./PHASE3_CREATOR_EXPERIENCE_GROUNDING.md) (§ dashboard).

## 1. Requested dashboard fields → existing reads

| Dashboard field | Existing owner-scoped read (verified) | Reuse shape |
|---|---|---|
| **Publish job counts by status** | `IPublishJobRepository.list_for_owner(tenant_id, owner_user_id)` (`repositories.py:2405`), surfaced by `ListPublishJobs.execute(tenant_id, owner_user_id)` (`list_publish_jobs.py`) — returns all of the caller's `PublishJob`s newest-first | group in-app by `job.status` over the 5 `PublishStatus` values (`queued`/`running`/`succeeded`/`failed`/`canceled`) |
| **Connected social account count** | `ISocialAccountRepository.list_for_owner(tenant_id, user_id)` (`repositories.py:2293`), surfaced by `ListSocialAccounts.execute(user=…)` (`list_social_accounts.py`) — returns the caller's `SocialAccount`s | count where `account.status is AccountStatus.CONNECTED` (`social_account.py:32`); a total count is also trivially available |
| **Unread notification count** | `INotificationRepository.count_unread(user_id)` (`repositories.py:2211`), surfaced by `CountUnreadNotifications.execute(user_id=…)` (`count_unread_notifications.py`) — an **index-only** scan on `ix_notifications_user_id_unread` | direct `int`, no aggregation |
| **Media statistics (where available)** | `IMediaAssetRepository.list_owned(tenant_id, owner_user_id, kind?, source?, …)` (`repositories.py:2405`… `:792`), surfaced by `ListMedia.execute(owner_user_id, tenant_id, …)` (`list_media.py`) — returns the caller's live media assets | count total (and optionally by `kind`) in-app; no dedicated media-stat method exists today |

**Fact:** every requested number is derivable from an **already-existing owner-scoped read**. Only
`count_unread` is a native aggregate; the other three are lists that would be counted/grouped in the
application layer.

## 2. Ownership / auth seam (verified)

- `CurrentUserDep` (`api/v1/deps.py`) yields the authenticated `User` (carrying `id` and
  `tenant_id`). Every read above is scoped by `(tenant_id, owner_user_id)` or `user_id` — the same
  discipline used by the existing list endpoints. A dashboard endpoint would derive all scope from
  `CurrentUserDep`, never from the request body/query.
- Notifications are scoped by `user_id` alone (no `tenant_id`), as documented in the notifications
  router — consistent with `count_unread(user_id)`.

## 3. API + wiring conventions (verified, reusable)

- **Router pattern:** thin routers project a DTO and wrap it in `envelope(...)` (`api/v1/helpers.py`);
  e.g. `notifications.py` `GET /unread-count` returns `envelope(UnreadCountPublic(count=…), request)`.
  Routers are registered in `main.py` (`app.include_router(<r>.router, prefix="/api/v1")`, lines
  112–127).
- **Use-case pattern:** a read use case takes `IUnitOfWork`, opens `async with self._uow:` and calls
  repository reads (e.g. `CountUnreadNotifications`). A dashboard use case would call several reads
  within one UoW and assemble a result DTO.
- **DI pattern:** `core/container.py` exposes `get_<x>_use_case()` factories built on
  `get_unit_of_work()`; `api/v1/deps.py` exposes `XDep = Annotated[X, Depends(container.get_x_use_case)]`.
- **Pagination primitive** (`application/pagination.py`) exists but is **not** needed here — a summary
  returns scalar counts, not a page.

## 4. What does NOT exist today (the gap)

- **No dashboard/summary use case, endpoint, DTO, or router.** There is no aggregate read composing
  these owner-scoped numbers.
- **No dedicated COUNT/GROUP BY repository methods** for publish jobs, social accounts, or media.
  Today those are list-returning reads; counting is by materialising the list.
- No `count_for_owner`-style method on `IPublishJobRepository` / `IMediaAssetRepository` /
  `ISocialAccountRepository`.

## 5. Performance note (fact, not design)

`list_for_owner` (publish jobs) and `list_owned` (media) are **unbounded** owner-scoped lists (α6.2
Q10 explicitly notes media is "not paginated"). Counting by materialising them is `O(rows the caller
owns)`. For a per-creator dashboard the owned-row counts are small; a dedicated indexed `COUNT(*) …
GROUP BY status` would be the optimisation if it ever matters. This is a **design trade-off for the
pre-flight**, not a blocker.

## 6. Persistence / migration

Every field is a read over **already-migrated** tables (`publish_jobs`, `social_accounts`,
`notifications`, `media_assets`). **No new column, no migration.** The dormant `analytics_events`
table (baseline, unused by app logic) stays untouched — the dashboard is **not** analytics.

## 7. Ports / ADRs

- **No new port required** if the dashboard aggregates existing reads (recommended). If the pre-flight
  instead chooses dedicated count queries, those are **additive read-only methods on existing
  repositories** — still no new port, no interface removed.
- **No ADR.** No frozen boundary is crossed: orchestration/execution/generation/render/export/publish
  runtime, the Asset Promotion Bridge, AI providers, Planner, and the verification pipeline are all
  untouched. A read-only summary composed from owner-scoped reads is additive.

## 8. Test assets available to reuse

- Integration API patterns: `tests/integration/api/test_notifications.py`,
  `tests/integration/api/test_publish_jobs.py` (register → auth → call → assert envelope).
- The DB-backed seeding pattern (commit owner + rows, assert, clean up) is established in the
  publishing/notifications integration suites.

## 9. Conclusion (facts)

α8.9c is implementable as a **strictly additive read-only aggregation**: one new use case composing
`ListPublishJobs`-equivalent, `ListSocialAccounts`-equivalent, `CountUnreadNotifications`, and
`ListMedia`-equivalent owner-scoped reads into a summary DTO, exposed at one new authenticated
endpoint. **No migration, no new port, no ADR, no analytics subsystem, no runtime change.** The only
open design question (pre-flight) is aggregate-in-app over existing lists vs. add dedicated read-only
count queries.
