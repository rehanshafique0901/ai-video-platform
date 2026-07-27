"""``GetCreatorDashboard`` use case (Slice α8.9c — Creator Dashboard).

Contract:

    GET /api/v1/dashboard/summary
      → 200  { data: DashboardSummary, meta }
      → 401  { error: { code: UNAUTHENTICATED, ... } }  (via CurrentUserDep)

A **read-only** owner-scoped summary of existing product state — the final increment of the
α8.9 Creator Experience. It composes numbers that already exist as owner-scoped repository
reads (CD3): publish-job counts by status, connected/total social accounts, the unread
notification count, and the caller's media-asset total. It **reuses** those reads inside a
single :class:`IUnitOfWork` and aggregates in the application layer — **no new repository
method, no new SQL, no migration, no analytics**. All scope comes from the authenticated
caller (CD4); the repositories are already owner-scoped, so a fresh caller sees all-zero.

Trade-off (CD3): publish/media/account counts materialise the caller's owner-scoped lists
(``O(owned rows)``). For a per-creator dashboard these sets are small; a dedicated indexed
``COUNT(*) … GROUP BY`` is a deferred, additive optimisation.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.domain.publishing.publish_status import PublishStatus
from app.domain.publishing.social_account import AccountStatus


@dataclass(frozen=True, slots=True)
class PublishJobCounts:
    """Counts of the caller's publish jobs by ``PublishStatus`` (+ total).

    Every status is always present (``0`` when the caller has none) — a stable shape.
    """

    queued: int
    running: int
    succeeded: int
    failed: int
    canceled: int
    total: int


@dataclass(frozen=True, slots=True)
class SocialAccountCounts:
    """Connected vs. total social accounts owned by the caller."""

    connected: int
    total: int


@dataclass(frozen=True, slots=True)
class MediaCounts:
    """The caller's live media-asset total."""

    total: int


@dataclass(frozen=True, slots=True)
class CreatorDashboardSummary:
    """The read-only creator dashboard summary (scalar counts only)."""

    publish_jobs: PublishJobCounts
    social_accounts: SocialAccountCounts
    unread_notifications: int
    media: MediaCounts


class GetCreatorDashboard:
    """Assemble the caller's owner-scoped dashboard summary from existing reads."""

    def __init__(self, uow: IUnitOfWork) -> None:
        self._uow = uow

    async def execute(self, *, tenant_id: UUID, owner_user_id: UUID) -> CreatorDashboardSummary:
        async with self._uow:
            jobs = await self._uow.publish_jobs.list_for_owner(
                tenant_id=tenant_id, owner_user_id=owner_user_id
            )
            accounts = await self._uow.social_accounts.list_for_owner(
                tenant_id=tenant_id, user_id=owner_user_id
            )
            unread = await self._uow.notifications.count_unread(owner_user_id)
            media = await self._uow.media.list_owned(tenant_id, owner_user_id)

        by_status = {status.value: 0 for status in PublishStatus}
        for job in jobs:
            if job.status in by_status:
                by_status[job.status] += 1

        publish_counts = PublishJobCounts(
            queued=by_status[PublishStatus.QUEUED.value],
            running=by_status[PublishStatus.RUNNING.value],
            succeeded=by_status[PublishStatus.SUCCEEDED.value],
            failed=by_status[PublishStatus.FAILED.value],
            canceled=by_status[PublishStatus.CANCELED.value],
            total=len(jobs),
        )
        account_counts = SocialAccountCounts(
            connected=sum(1 for a in accounts if a.status is AccountStatus.CONNECTED),
            total=len(accounts),
        )
        return CreatorDashboardSummary(
            publish_jobs=publish_counts,
            social_accounts=account_counts,
            unread_notifications=unread,
            media=MediaCounts(total=len(media)),
        )
