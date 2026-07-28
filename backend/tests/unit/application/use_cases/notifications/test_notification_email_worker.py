"""Unit tests for ``NotificationEmailWorker`` (α9.5, ADR-0051).

The poll ingress: one ``run_once`` scans deliverable notifications (FIFO) and delegates each to a
``ProcessNotificationEmail``-shaped collaborator. A single misbehaving row is isolated (logged, the
scan continues) — at-least-once, so it is retried next poll.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.application.interfaces.repositories import NotificationEmailDelivery
from app.application.use_cases.notifications.notification_email_worker import (
    NotificationEmailWorker,
)
from app.application.use_cases.notifications.process_notification_email import (
    ProcessNotificationEmailResult,
)
from app.domain.identity.user import User
from tests.unit.application.use_cases.auth._fakes import (
    FakeUnitOfWork,
    FakeUserRepository,
)

pytestmark = pytest.mark.unit

_T0 = datetime(2026, 7, 29, tzinfo=UTC)


class _RecordingProcess:
    """Stands in for ``ProcessNotificationEmail`` — records each claim it is handed."""

    def __init__(self) -> None:
        self.seen: list[NotificationEmailDelivery] = []

    async def process(self, claim: NotificationEmailDelivery) -> ProcessNotificationEmailResult:
        self.seen.append(claim)
        return ProcessNotificationEmailResult(notification_id=claim.id, status="delivered")


class _ExplodingProcess:
    """Raises on the first claim, succeeds after — proves one bad row never aborts the batch."""

    def __init__(self) -> None:
        self.calls = 0

    async def process(self, claim: NotificationEmailDelivery) -> ProcessNotificationEmailResult:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("boom")
        return ProcessNotificationEmailResult(notification_id=claim.id, status="delivered")


def _make_user() -> User:
    return User(
        id=uuid4(),
        tenant_id=uuid4(),
        email="creator@example.com",
        password_hash="hash::pw",
        display_name="Creator",
        email_verified_at=None,
        last_login_at=None,
        created_at=_T0,
        updated_at=_T0,
        version=1,
    )


async def _seed_n(uow: FakeUnitOfWork, user: User, n: int) -> None:
    async with uow:
        for i in range(n):
            await uow.notifications.add(
                user_id=user.id,
                kind="publish.succeeded",
                title=f"Notification {i}",
                body=None,
                payload={},
                source_event_id=uuid4(),
            )


async def test_run_once_drains_all_deliverable() -> None:
    user = _make_user()
    users = FakeUserRepository()
    await users.add(user)
    uow = FakeUnitOfWork(users=users)
    await _seed_n(uow, user, 3)
    process = _RecordingProcess()
    worker = NotificationEmailWorker(uow=uow, process=process, batch_size=10)

    result = await worker.run_once()

    assert result.scanned == 3
    assert len(result.outcomes) == 3
    assert len(process.seen) == 3


async def test_batch_size_limits_scan() -> None:
    user = _make_user()
    users = FakeUserRepository()
    await users.add(user)
    uow = FakeUnitOfWork(users=users)
    await _seed_n(uow, user, 5)
    process = _RecordingProcess()
    worker = NotificationEmailWorker(uow=uow, process=process, batch_size=2)

    result = await worker.run_once()

    assert result.scanned == 2
    assert len(process.seen) == 2


async def test_one_bad_row_does_not_abort_batch() -> None:
    user = _make_user()
    users = FakeUserRepository()
    await users.add(user)
    uow = FakeUnitOfWork(users=users)
    await _seed_n(uow, user, 3)
    process = _ExplodingProcess()
    worker = NotificationEmailWorker(uow=uow, process=process, batch_size=10)

    result = await worker.run_once()

    # All three were attempted; the first raised (isolated), the other two produced outcomes.
    assert result.scanned == 3
    assert process.calls == 3
    assert len(result.outcomes) == 2
