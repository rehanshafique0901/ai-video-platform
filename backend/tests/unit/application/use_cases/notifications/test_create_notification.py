"""Unit tests for ``CreateNotification`` (Slice α8.5b.3).

The idempotent write half of the projection: persist one notification per
``(user_id, source_event_id)``, treating the DB's uniqueness refusal (relay
redelivery) as a successful no-op (W8.5b.7). Exercised against ``FakeUnitOfWork`` +
``FakeNotificationRepository``.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.application.use_cases.notifications.create_notification import CreateNotification
from tests.unit.application.use_cases.auth._fakes import FakeUnitOfWork

pytestmark = pytest.mark.unit


async def test_creates_notification_and_commits() -> None:
    uow = FakeUnitOfWork()
    use_case = CreateNotification(uow=uow)
    user_id = uuid4()
    source_event_id = uuid4()

    result = await use_case.execute(
        user_id=user_id,
        kind="export.succeeded",
        title="Your video is ready",
        body="Your hd_1080p mp4 export is ready to download.",
        payload={"export_job_id": str(uuid4())},
        source_event_id=source_event_id,
    )

    assert result.status == "created"
    assert result.notification_id is not None
    assert uow.commits == 1
    (row,) = uow._fake_notifications._rows.values()
    assert row.user_id == user_id
    assert row.source_event_id == source_event_id
    assert row.delivered_in_app_at is not None  # in-app delivery = committed row


async def test_duplicate_source_event_is_idempotent_noop() -> None:
    uow = FakeUnitOfWork()
    use_case = CreateNotification(uow=uow)
    user_id = uuid4()
    source_event_id = uuid4()

    async def _run() -> str:
        result = await use_case.execute(
            user_id=user_id,
            kind="export.succeeded",
            title="Your video is ready",
            body="ready",
            payload={},
            source_event_id=source_event_id,
        )
        return result.status

    first = await _run()
    second = await _run()

    assert first == "created"
    assert second == "duplicate"
    # Exactly one row persisted despite two executions (W8.5b.7).
    assert len(uow._fake_notifications._rows) == 1
    # The refused write did not add a spurious commit.
    assert uow.commits == 1


async def test_same_event_different_recipients_both_persist() -> None:
    # (user_id, source_event_id) uniqueness — a future fan-out to two recipients of the
    # same event both succeed (today's single-recipient semantics are unchanged).
    uow = FakeUnitOfWork()
    use_case = CreateNotification(uow=uow)
    source_event_id = uuid4()

    r1 = await use_case.execute(
        user_id=uuid4(),
        kind="export.succeeded",
        title="t",
        body=None,
        payload={},
        source_event_id=source_event_id,
    )
    r2 = await use_case.execute(
        user_id=uuid4(),
        kind="export.succeeded",
        title="t",
        body=None,
        payload={},
        source_event_id=source_event_id,
    )

    assert r1.status == "created"
    assert r2.status == "created"
    assert len(uow._fake_notifications._rows) == 2


async def test_null_source_event_id_permits_multiple_rows() -> None:
    # Non-event notifications (source_event_id NULL) are not deduped (partial index).
    uow = FakeUnitOfWork()
    use_case = CreateNotification(uow=uow)
    user_id = uuid4()

    for _ in range(2):
        result = await use_case.execute(
            user_id=user_id,
            kind="system.welcome",
            title="Welcome",
            body=None,
            payload={},
            source_event_id=None,
        )
        assert result.status == "created"

    assert len(uow._fake_notifications._rows) == 2
