"""Integration tests for ``NotificationRepository`` (Slice α8.5b.3).

Runs against the live database; each test is wrapped in a SAVEPOINT that rolls back on
teardown, so no rows persist. Covers the write path and the exactly-once guarantee
(W8.5b.7):

* N1 — ``add`` inserts a row, stamps ``delivered_in_app_at`` (in-app delivery), leaves
  ``delivered_email_at`` / ``read_at`` NULL.
* N2 — duplicate ``(user_id, source_event_id)`` → ``ConflictError`` (the partial-unique
  ``uq_notifications_user_id_source_event_id`` backstop).
* N3 — the same ``source_event_id`` for a **different** recipient is allowed (per-recipient
  uniqueness, future fan-out).
* N4 — multiple ``source_event_id IS NULL`` rows for one user coexist (partial index).
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError
from app.infrastructure.db.models.identity import Tenant, User
from app.infrastructure.repositories.notification_repository import NotificationRepository

pytestmark = pytest.mark.integration


async def _seed_user(session: AsyncSession) -> UUID:
    tenant_id = uuid4()
    await session.execute(
        insert(Tenant).values(id=tenant_id, name="NR Test", slug=f"nr-{tenant_id}")
    )
    user_id = uuid4()
    await session.execute(
        insert(User).values(
            id=user_id,
            tenant_id=tenant_id,
            email=f"nr-{user_id}@example.com",
            display_name="NR Owner",
        )
    )
    await session.flush()
    return user_id


async def test_n1_add_inserts_and_marks_in_app_delivered(session: AsyncSession) -> None:
    user_id = await _seed_user(session)
    repo = NotificationRepository(session)

    notification = await repo.add(
        user_id=user_id,
        kind="export.succeeded",
        title="Your video is ready",
        body="Your hd_1080p mp4 export is ready to download.",
        payload={"export_job_id": str(uuid4())},
        source_event_id=uuid4(),
    )

    assert notification.user_id == user_id
    assert notification.kind == "export.succeeded"
    assert notification.delivered_in_app_at is not None
    assert notification.source_event_id is not None


async def test_n2_duplicate_source_event_raises_conflict(session: AsyncSession) -> None:
    user_id = await _seed_user(session)
    repo = NotificationRepository(session)
    source_event_id = uuid4()

    await repo.add(
        user_id=user_id,
        kind="export.succeeded",
        title="t",
        body=None,
        payload={},
        source_event_id=source_event_id,
    )
    with pytest.raises(ConflictError):
        await repo.add(
            user_id=user_id,
            kind="export.succeeded",
            title="t",
            body=None,
            payload={},
            source_event_id=source_event_id,  # same recipient + event
        )


async def test_n3_same_event_different_recipient_allowed(session: AsyncSession) -> None:
    user_a = await _seed_user(session)
    user_b = await _seed_user(session)
    repo = NotificationRepository(session)
    source_event_id = uuid4()

    a = await repo.add(
        user_id=user_a,
        kind="export.succeeded",
        title="t",
        body=None,
        payload={},
        source_event_id=source_event_id,
    )
    b = await repo.add(
        user_id=user_b,
        kind="export.succeeded",
        title="t",
        body=None,
        payload={},
        source_event_id=source_event_id,  # same event, different recipient → allowed
    )
    assert a.id != b.id


async def test_n4_multiple_null_source_event_rows_coexist(session: AsyncSession) -> None:
    user_id = await _seed_user(session)
    repo = NotificationRepository(session)

    first = await repo.add(
        user_id=user_id,
        kind="system.welcome",
        title="Welcome",
        body=None,
        payload={},
        source_event_id=None,
    )
    second = await repo.add(
        user_id=user_id,
        kind="system.welcome",
        title="Welcome again",
        body=None,
        payload={},
        source_event_id=None,  # partial index excludes NULLs → both persist
    )
    assert first.id != second.id
