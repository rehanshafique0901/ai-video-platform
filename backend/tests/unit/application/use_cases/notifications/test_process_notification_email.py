"""Unit tests for ``ProcessNotificationEmail`` (α9.5, ADR-0051).

Exercises the per-notification delivery logic against ``FakeUnitOfWork`` + a spy/failing
:class:`INotifier`: send-then-stamp on success, bounded backed-off retry on a transient failure,
terminal failure on a permanent error or the attempt ceiling, a permanent failure for an unresolved
recipient, and a clean skip when the per-notification lease is already held.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.application.interfaces.notifier import (
    EmailMessage,
    INotifier,
    NotifierDeliveryError,
)
from app.application.interfaces.repositories import NotificationEmailDelivery
from app.application.use_cases.notifications.process_notification_email import (
    ProcessNotificationEmail,
)
from app.domain.identity.user import User
from tests.unit.application.use_cases.auth._fakes import (
    FakeUnitOfWork,
    FakeUserRepository,
)

pytestmark = pytest.mark.unit

_T0 = datetime(2026, 7, 29, tzinfo=UTC)


class _SpyNotifier(INotifier):
    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []

    async def send(self, message: EmailMessage) -> None:
        self.sent.append(message)


class _FailingNotifier(INotifier):
    def __init__(self, *, permanent: bool, code: str = "boom") -> None:
        self._permanent = permanent
        self._code = code
        self.calls = 0

    async def send(self, message: EmailMessage) -> None:
        self.calls += 1
        raise NotifierDeliveryError("send failed", permanent=self._permanent, code=self._code)


def _make_user(email: str = "creator@example.com") -> User:
    return User(
        id=uuid4(),
        tenant_id=uuid4(),
        email=email,
        password_hash="hash::pw",
        display_name="Creator",
        email_verified_at=None,
        last_login_at=None,
        created_at=_T0,
        updated_at=_T0,
        version=1,
    )


async def _seed(uow: FakeUnitOfWork, user: User) -> NotificationEmailDelivery:
    async with uow:
        notification = await uow.notifications.add(
            user_id=user.id,
            kind="publish.succeeded",
            title="Your video is live",
            body="Your YouTube upload succeeded.",
            payload={"platform": "youtube"},
            source_event_id=uuid4(),
        )
    return NotificationEmailDelivery(
        id=notification.id,
        user_id=user.id,
        title=notification.title,
        body=notification.body,
        attempts=0,
    )


async def test_delivered_sends_then_stamps() -> None:
    user = _make_user()
    users = FakeUserRepository()
    await users.add(user)
    uow = FakeUnitOfWork(users=users)
    claim = await _seed(uow, user)
    notifier = _SpyNotifier()
    use_case = ProcessNotificationEmail(uow=uow, notifier=notifier)

    result = await use_case.process(claim)

    assert result.status == "delivered"
    # Exactly one send, carrying the resolved recipient + a deterministic idempotency key.
    (message,) = notifier.sent
    assert message.recipient == "creator@example.com"
    assert message.subject == "Your video is live"
    assert message.body_text == "Your YouTube upload succeeded."
    assert message.idempotency_key == f"notification-email:{claim.id}"
    # Send-then-stamp: delivered_email_at recorded, no failure bookkeeping.
    assert claim.id in uow._fake_notifications._email_delivered
    assert claim.id not in uow._fake_notifications._email_state


async def test_transient_failure_schedules_backed_off_retry() -> None:
    user = _make_user()
    users = FakeUserRepository()
    await users.add(user)
    uow = FakeUnitOfWork(users=users)
    claim = await _seed(uow, user)
    notifier = _FailingNotifier(permanent=False, code="smtp_451")
    use_case = ProcessNotificationEmail(uow=uow, notifier=notifier, max_attempts=5)

    result = await use_case.process(claim)

    assert result.status == "retry"
    assert claim.id not in uow._fake_notifications._email_delivered
    state = uow._fake_notifications._email_state[claim.id]
    assert state["state"] == "pending"
    assert state["attempts"] == 1
    assert state["last_error"] == "smtp_451"
    assert isinstance(state["next_attempt_at"], datetime)


async def test_permanent_failure_is_terminal() -> None:
    user = _make_user()
    users = FakeUserRepository()
    await users.add(user)
    uow = FakeUnitOfWork(users=users)
    claim = await _seed(uow, user)
    notifier = _FailingNotifier(permanent=True, code="address_refused")
    use_case = ProcessNotificationEmail(uow=uow, notifier=notifier, max_attempts=5)

    result = await use_case.process(claim)

    assert result.status == "failed"
    state = uow._fake_notifications._email_state[claim.id]
    assert state["state"] == "failed"
    assert state["attempts"] == 1
    # A terminal failure has no next attempt — it is never re-scanned.
    assert state["next_attempt_at"] is None


async def test_attempt_ceiling_makes_transient_terminal() -> None:
    user = _make_user()
    users = FakeUserRepository()
    await users.add(user)
    uow = FakeUnitOfWork(users=users)
    claim = await _seed(uow, user)
    notifier = _FailingNotifier(permanent=False)
    # max_attempts=1 → the first (transient) failure already reaches the ceiling → terminal.
    use_case = ProcessNotificationEmail(uow=uow, notifier=notifier, max_attempts=1)

    result = await use_case.process(claim)

    assert result.status == "failed"
    state = uow._fake_notifications._email_state[claim.id]
    assert state["state"] == "failed"
    assert state["attempts"] == 1


async def test_unresolved_recipient_is_permanent_failure() -> None:
    # No user seeded → recipient cannot be resolved → permanent, never sent.
    uow = FakeUnitOfWork()
    claim = NotificationEmailDelivery(id=uuid4(), user_id=uuid4(), title="t", body=None, attempts=0)
    notifier = _SpyNotifier()
    use_case = ProcessNotificationEmail(uow=uow, notifier=notifier)

    result = await use_case.process(claim)

    assert result.status == "failed"
    assert result.reason == "recipient_unresolved"
    assert notifier.sent == []
    assert uow._fake_notifications._email_state[claim.id]["state"] == "failed"


async def test_lease_held_skips_cleanly() -> None:
    user = _make_user()
    users = FakeUserRepository()
    await users.add(user)
    uow = FakeUnitOfWork(users=users)
    claim = await _seed(uow, user)
    # Pre-hold the per-notification lease under a different owner.
    async with uow:
        await uow.locks.acquire(
            key=f"notification_email:{claim.id}", owner="other-worker", lease=timedelta(seconds=60)
        )
        await uow.commit()
    notifier = _SpyNotifier()
    use_case = ProcessNotificationEmail(uow=uow, notifier=notifier)

    result = await use_case.process(claim)

    assert result.status == "skipped"
    assert result.reason == "locked"
    assert notifier.sent == []
