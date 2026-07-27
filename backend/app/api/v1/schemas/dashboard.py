"""DTOs for ``/api/v1/dashboard/*`` (α8.9c — Creator Dashboard).

The public projection of the read-only :class:`CreatorDashboardSummary`. Scalar counts only —
no analytics, no time-series, no credential/URL/byte material. All numbers are owner-scoped to
the authenticated caller.
"""

from __future__ import annotations

from pydantic import BaseModel

from app.application.use_cases.dashboard.get_creator_dashboard import CreatorDashboardSummary


class PublishJobCountsPublic(BaseModel):
    """Publish-job counts by status (+ total); every status always present."""

    queued: int
    running: int
    succeeded: int
    failed: int
    canceled: int
    total: int


class SocialAccountCountsPublic(BaseModel):
    """Connected vs. total social accounts."""

    connected: int
    total: int


class NotificationCountsPublic(BaseModel):
    """The unread notification badge count."""

    unread: int


class MediaCountsPublic(BaseModel):
    """The caller's live media-asset total."""

    total: int


class DashboardSummaryPublic(BaseModel):
    """The read-only creator dashboard summary."""

    publish_jobs: PublishJobCountsPublic
    social_accounts: SocialAccountCountsPublic
    notifications: NotificationCountsPublic
    media: MediaCountsPublic

    @classmethod
    def from_domain(cls, summary: CreatorDashboardSummary) -> DashboardSummaryPublic:
        return cls(
            publish_jobs=PublishJobCountsPublic(
                queued=summary.publish_jobs.queued,
                running=summary.publish_jobs.running,
                succeeded=summary.publish_jobs.succeeded,
                failed=summary.publish_jobs.failed,
                canceled=summary.publish_jobs.canceled,
                total=summary.publish_jobs.total,
            ),
            social_accounts=SocialAccountCountsPublic(
                connected=summary.social_accounts.connected,
                total=summary.social_accounts.total,
            ),
            notifications=NotificationCountsPublic(unread=summary.unread_notifications),
            media=MediaCountsPublic(total=summary.media.total),
        )


__all__ = [
    "PublishJobCountsPublic",
    "SocialAccountCountsPublic",
    "NotificationCountsPublic",
    "MediaCountsPublic",
    "DashboardSummaryPublic",
]
