"""Unit tests for ``GetCreatorDashboard`` (in-memory fakes, no DB).

Prove the read-only aggregation (CD2/CD3): publish jobs grouped by every ``PublishStatus``
(absent → 0, stable shape), connected vs. total social accounts, the unread notification
count passthrough, and the media total — all owner-scoped through the reused reads.
"""

from __future__ import annotations

from types import SimpleNamespace, TracebackType
from typing import Self
from uuid import UUID, uuid4

import pytest

from app.application.use_cases.dashboard.get_creator_dashboard import GetCreatorDashboard
from app.domain.publishing.social_account import AccountStatus

pytestmark = pytest.mark.unit

_TENANT = uuid4()
_USER = uuid4()


class _FakePublishJobs:
    def __init__(self, statuses: list[str]) -> None:
        self._jobs = [SimpleNamespace(status=s) for s in statuses]

    async def list_for_owner(self, *, tenant_id: UUID, owner_user_id: UUID) -> list:
        assert tenant_id == _TENANT and owner_user_id == _USER
        return self._jobs


class _FakeSocialAccounts:
    def __init__(self, statuses: list[AccountStatus]) -> None:
        self._accounts = [SimpleNamespace(status=s) for s in statuses]

    async def list_for_owner(self, *, tenant_id: UUID, user_id: UUID) -> list:
        assert tenant_id == _TENANT and user_id == _USER
        return self._accounts


class _FakeNotifications:
    def __init__(self, unread: int) -> None:
        self._unread = unread

    async def count_unread(self, user_id: UUID) -> int:
        assert user_id == _USER
        return self._unread


class _FakeMedia:
    def __init__(self, count: int) -> None:
        self._assets = [SimpleNamespace(id=uuid4()) for _ in range(count)]

    async def list_owned(self, tenant_id: UUID, owner_user_id: UUID, **_: object) -> list:
        assert tenant_id == _TENANT and owner_user_id == _USER
        return self._assets


class _FakeUoW:
    def __init__(
        self,
        *,
        job_statuses: list[str],
        account_statuses: list[AccountStatus],
        unread: int,
        media_count: int,
    ) -> None:
        self.publish_jobs = _FakePublishJobs(job_statuses)
        self.social_accounts = _FakeSocialAccounts(account_statuses)
        self.notifications = _FakeNotifications(unread)
        self.media = _FakeMedia(media_count)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None


async def _run(uow: _FakeUoW):
    use_case = GetCreatorDashboard(uow=uow)  # type: ignore[arg-type]
    return await use_case.execute(tenant_id=_TENANT, owner_user_id=_USER)


async def test_aggregates_mixed_state() -> None:
    uow = _FakeUoW(
        job_statuses=["queued", "queued", "running", "succeeded", "failed", "canceled"],
        account_statuses=[
            AccountStatus.CONNECTED,
            AccountStatus.CONNECTED,
            AccountStatus.REVOKED,
        ],
        unread=7,
        media_count=4,
    )
    summary = await _run(uow)

    assert summary.publish_jobs.queued == 2
    assert summary.publish_jobs.running == 1
    assert summary.publish_jobs.succeeded == 1
    assert summary.publish_jobs.failed == 1
    assert summary.publish_jobs.canceled == 1
    assert summary.publish_jobs.total == 6

    assert summary.social_accounts.connected == 2
    assert summary.social_accounts.total == 3

    assert summary.unread_notifications == 7
    assert summary.media.total == 4


async def test_empty_owner_is_all_zero() -> None:
    uow = _FakeUoW(job_statuses=[], account_statuses=[], unread=0, media_count=0)
    summary = await _run(uow)

    assert summary.publish_jobs.queued == 0
    assert summary.publish_jobs.running == 0
    assert summary.publish_jobs.succeeded == 0
    assert summary.publish_jobs.failed == 0
    assert summary.publish_jobs.canceled == 0
    assert summary.publish_jobs.total == 0
    assert summary.social_accounts.connected == 0
    assert summary.social_accounts.total == 0
    assert summary.unread_notifications == 0
    assert summary.media.total == 0


async def test_unknown_status_ignored_in_breakdown_but_counts_in_total() -> None:
    # Defensive: an out-of-enum status never breaks the stable 5-key breakdown.
    uow = _FakeUoW(
        job_statuses=["succeeded", "weird-status"],
        account_statuses=[],
        unread=0,
        media_count=0,
    )
    summary = await _run(uow)
    assert summary.publish_jobs.succeeded == 1
    assert summary.publish_jobs.total == 2
