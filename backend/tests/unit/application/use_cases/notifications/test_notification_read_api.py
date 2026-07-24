"""Unit tests for the α8.5b.3r notification read/query use cases.

Exercised against ``FakeUnitOfWork`` + ``FakeNotificationRepository``:

* ``ListNotifications`` — newest-first keyset page, over-fetch → ``next_cursor``, owner
  scoping (W8.5b.8), bad cursor → 422, ordering unaffected by read-state (W8.5b.10).
* ``CountUnreadNotifications`` — counts only the caller's unread rows.
* ``MarkNotificationRead`` — marks own unread; already-read is idempotent; foreign/missing
  → 404 (W8.5b.8); identity/provenance untouched (W8.5b.9).
* ``MarkAllNotificationsRead`` — marks all the caller's unread, returns the count;
  idempotent second call.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from app.application.use_cases.notifications.count_unread_notifications import (
    CountUnreadNotifications,
)
from app.application.use_cases.notifications.list_notifications import ListNotifications
from app.application.use_cases.notifications.mark_all_notifications_read import (
    MarkAllNotificationsRead,
)
from app.application.use_cases.notifications.mark_notification_read import (
    MarkNotificationRead,
)
from app.core.errors import NotFoundError, ValidationFailedError
from app.domain.notifications.notification import Notification
from tests.unit.application.use_cases.auth._fakes import FakeUnitOfWork

pytestmark = pytest.mark.unit

_BASE = datetime(2026, 1, 1, tzinfo=UTC)


def _seed(
    uow: FakeUnitOfWork,
    *,
    user_id: UUID,
    title: str,
    created_at: datetime,
    read: bool = False,
) -> UUID:
    nid = uuid4()
    uow._fake_notifications._rows[nid] = Notification(
        id=nid,
        user_id=user_id,
        kind="export.succeeded",
        title=title,
        body=None,
        payload={"k": title},
        source_event_id=uuid4(),
        delivered_in_app_at=created_at,
        read_at=created_at if read else None,
        archived=False,
        created_at=created_at,
        updated_at=created_at,
    )
    return nid


async def test_list_newest_first_keyset_and_owner_scoped() -> None:
    uow = FakeUnitOfWork()
    owner, other = uuid4(), uuid4()
    _seed(uow, user_id=owner, title="n1", created_at=_BASE)
    _seed(uow, user_id=owner, title="n2", created_at=_BASE + timedelta(seconds=1))
    _seed(uow, user_id=owner, title="n3", created_at=_BASE + timedelta(seconds=2))
    _seed(uow, user_id=other, title="foreign", created_at=_BASE + timedelta(seconds=3))

    use_case = ListNotifications(uow=uow)
    page1 = await use_case.execute(user_id=owner, limit=2)

    assert [n.title for n in page1.items] == ["n3", "n2"]  # newest first
    assert all(n.user_id == owner for n in page1.items)  # W8.5b.8
    assert page1.next_cursor is not None

    page2 = await use_case.execute(user_id=owner, limit=2, cursor_token=page1.next_cursor)
    assert [n.title for n in page2.items] == ["n1"]
    assert page2.next_cursor is None  # last page


async def test_list_ordering_unaffected_by_read_state() -> None:
    # W8.5b.10 — marking read does not move a notification in the feed.
    uow = FakeUnitOfWork()
    owner = uuid4()
    _seed(uow, user_id=owner, title="old", created_at=_BASE, read=True)
    _seed(uow, user_id=owner, title="new", created_at=_BASE + timedelta(seconds=1))

    page = await ListNotifications(uow=uow).execute(user_id=owner, limit=10)
    assert [n.title for n in page.items] == ["new", "old"]  # by created_at, not read-state


async def test_list_bad_cursor_is_422() -> None:
    uow = FakeUnitOfWork()
    with pytest.raises(ValidationFailedError):
        await ListNotifications(uow=uow).execute(user_id=uuid4(), limit=20, cursor_token="!!bad!!")


async def test_list_empty_returns_no_items() -> None:
    uow = FakeUnitOfWork()
    page = await ListNotifications(uow=uow).execute(user_id=uuid4(), limit=20)
    assert page.items == []
    assert page.next_cursor is None


async def test_count_unread_scoped_to_caller() -> None:
    uow = FakeUnitOfWork()
    owner, other = uuid4(), uuid4()
    _seed(uow, user_id=owner, title="a", created_at=_BASE)
    _seed(uow, user_id=owner, title="b", created_at=_BASE, read=True)  # read → not counted
    _seed(uow, user_id=other, title="foreign", created_at=_BASE)

    assert await CountUnreadNotifications(uow=uow).execute(user_id=owner) == 1
    assert await CountUnreadNotifications(uow=uow).execute(user_id=other) == 1


async def test_mark_read_marks_own_and_is_idempotent() -> None:
    uow = FakeUnitOfWork()
    owner = uuid4()
    nid = _seed(uow, user_id=owner, title="a", created_at=_BASE)

    result = await MarkNotificationRead(uow=uow).execute(user_id=owner, notification_id=nid)
    assert result.read_at is not None
    assert result.id == nid and result.payload == {"k": "a"}  # identity intact (W8.5b.9)
    assert await CountUnreadNotifications(uow=uow).execute(user_id=owner) == 0

    # Idempotent repeat → still 200 (same row), not a 404.
    again = await MarkNotificationRead(uow=uow).execute(user_id=owner, notification_id=nid)
    assert again.read_at == result.read_at


async def test_mark_read_foreign_or_missing_is_404() -> None:
    uow = FakeUnitOfWork()
    owner, other = uuid4(), uuid4()
    nid = _seed(uow, user_id=owner, title="a", created_at=_BASE)

    with pytest.raises(NotFoundError):  # another principal's row (W8.5b.8)
        await MarkNotificationRead(uow=uow).execute(user_id=other, notification_id=nid)
    with pytest.raises(NotFoundError):  # unknown id
        await MarkNotificationRead(uow=uow).execute(user_id=owner, notification_id=uuid4())


async def test_mark_all_read_returns_count_and_is_idempotent() -> None:
    uow = FakeUnitOfWork()
    owner, other = uuid4(), uuid4()
    _seed(uow, user_id=owner, title="a", created_at=_BASE)
    _seed(uow, user_id=owner, title="b", created_at=_BASE)
    _seed(uow, user_id=other, title="foreign", created_at=_BASE)

    affected = await MarkAllNotificationsRead(uow=uow).execute(user_id=owner)
    assert affected == 2
    assert await CountUnreadNotifications(uow=uow).execute(user_id=owner) == 0
    assert await CountUnreadNotifications(uow=uow).execute(user_id=other) == 1  # untouched

    assert await MarkAllNotificationsRead(uow=uow).execute(user_id=owner) == 0  # idempotent
