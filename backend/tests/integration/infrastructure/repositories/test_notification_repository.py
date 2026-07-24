"""Integration tests for ``NotificationRepository`` (Slice α8.5b.3 write + α8.5b.3r read).

Runs against the live database; each test is wrapped in a SAVEPOINT that rolls back on
teardown, so no rows persist. Covers the write path + exactly-once (W8.5b.7) and the
α8.5b.3r read/query surface (owner scoping W8.5b.8, metadata-only mutation W8.5b.9,
order-stability W8.5b.10):

* N1 — ``add`` inserts a row, stamps ``delivered_in_app_at`` (in-app delivery), leaves
  ``delivered_email_at`` / ``read_at`` NULL and ``archived`` false.
* N2 — duplicate ``(user_id, source_event_id)`` → ``ConflictError`` (the partial-unique
  ``uq_notifications_user_id_source_event_id`` backstop).
* N3 — the same ``source_event_id`` for a **different** recipient is allowed (per-recipient
  uniqueness, future fan-out).
* N4 — multiple ``source_event_id IS NULL`` rows for one user coexist (partial index).
* N5 — ``list_for_user`` returns the user's rows newest-first, keyset-paged, owner-scoped.
* N6 — ``count_unread`` counts only unread rows for the caller.
* N7 — ``mark_read`` marks an owned unread row; a foreign id returns ``None`` (W8.5b.8);
  a repeat is idempotent; identity/provenance untouched (W8.5b.9).
* N8 — ``mark_all_read`` marks all the caller's unread rows and returns the count.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError
from app.infrastructure.db.models.identity import Tenant, User
from app.infrastructure.db.models.notifications import Notification as NotificationRow
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
    assert notification.read_at is None
    assert notification.archived is False


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


async def _add(repo: NotificationRepository, user_id: UUID, title: str) -> UUID:
    n = await repo.add(
        user_id=user_id,
        kind="export.succeeded",
        title=title,
        body=None,
        payload={"k": title},
        source_event_id=uuid4(),
    )
    return n.id


async def _insert_at(
    session: AsyncSession, *, user_id: UUID, title: str, created_at: datetime
) -> UUID:
    """Insert a notification with an explicit ``created_at`` (bypasses repo.add).

    The fixture's transaction-constant ``now()`` gives every row the same ``created_at``,
    so the ordering/pagination tests set distinct timestamps to exercise the
    ``created_at DESC`` primary sort honestly (the α5a project-repo precedent).
    """
    nid = uuid4()
    await session.execute(
        insert(NotificationRow).values(
            id=nid,
            user_id=user_id,
            kind="export.succeeded",
            title=title,
            payload={},
            created_at=created_at,
        )
    )
    return nid


async def test_n5_list_for_user_newest_first_keyset_and_owner_scoped(
    session: AsyncSession,
) -> None:
    owner = await _seed_user(session)
    other = await _seed_user(session)
    repo = NotificationRepository(session)

    base = datetime(2026, 1, 1, tzinfo=UTC)
    # Three for the owner (distinct timestamps), one for another principal (W8.5b.8).
    await _insert_at(session, user_id=owner, title="n1", created_at=base)
    await _insert_at(session, user_id=owner, title="n2", created_at=base + timedelta(seconds=1))
    await _insert_at(session, user_id=owner, title="n3", created_at=base + timedelta(seconds=2))
    await _insert_at(session, user_id=other, title="foreign", created_at=base)
    await session.flush()

    first_page = await repo.list_for_user(owner, limit=2)
    assert [n.title for n in first_page] == ["n3", "n2"]  # newest first
    assert all(n.user_id == owner for n in first_page)

    # Keyset next page from the last returned row.
    last = first_page[-1]
    second_page = await repo.list_for_user(owner, limit=2, after=(last.created_at, last.id))
    assert [n.title for n in second_page] == ["n1"]


async def test_n6_count_unread_scoped_to_caller(session: AsyncSession) -> None:
    owner = await _seed_user(session)
    other = await _seed_user(session)
    repo = NotificationRepository(session)

    await _add(repo, owner, "a")
    await _add(repo, owner, "b")
    await _add(repo, other, "foreign")

    assert await repo.count_unread(owner) == 2
    assert await repo.count_unread(other) == 1


async def test_n7_mark_read_scoped_idempotent_metadata_only(session: AsyncSession) -> None:
    owner = await _seed_user(session)
    other = await _seed_user(session)
    repo = NotificationRepository(session)
    nid = await _add(repo, owner, "a")

    # A foreign principal cannot read/mutate it → None (W8.5b.8).
    assert await repo.mark_read(other, nid) is None

    read = await repo.mark_read(owner, nid)
    assert read is not None
    assert read.read_at is not None
    # Identity / provenance untouched (W8.5b.9).
    assert read.id == nid and read.kind == "export.succeeded" and read.payload == {"k": "a"}
    assert read.source_event_id is not None

    # Repeat is idempotent — same read_at, still the same row.
    again = await repo.mark_read(owner, nid)
    assert again is not None and again.read_at == read.read_at

    assert await repo.count_unread(owner) == 0


async def test_n8_mark_all_read_returns_affected_count(session: AsyncSession) -> None:
    owner = await _seed_user(session)
    other = await _seed_user(session)
    repo = NotificationRepository(session)
    await _add(repo, owner, "a")
    await _add(repo, owner, "b")
    await _add(repo, other, "foreign")

    affected = await repo.mark_all_read(owner)
    assert affected == 2
    assert await repo.count_unread(owner) == 0
    # The other principal is untouched (W8.5b.8).
    assert await repo.count_unread(other) == 1
    # Idempotent second call.
    assert await repo.mark_all_read(owner) == 0
