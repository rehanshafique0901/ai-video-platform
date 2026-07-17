"""Integration tests for the α7.3 relay surface of ``EventOutboxRepository``.

Covers ``fetch_unpublished`` / ``mark_published`` / ``mark_failed`` against the
live ``event_outbox`` table. Most tests run inside the SAVEPOINT ``session``
fixture and assert **relative to a baseline** (the shared DB may already hold
unpublished rows), keying every assertion on this test's own inserted ids so
pre-existing rows never perturb the result. The ``FOR UPDATE SKIP LOCKED`` claim
is proven with two real concurrent connections (committed rows + explicit
cleanup), since row-locking cannot be observed within a single transaction.

Coverage map (α7.3 sign-off Q3/Q6 + ADR-0041 D9):

* O1 — fetch returns unpublished rows in ``occurred_at`` order (relative).
* O2 — fetch excludes rows with ``attempts >= max_attempts`` (parked).
* O3 — fetch excludes already-published rows.
* O4 — mark_published stamps ``published_at`` (row drops out of the next fetch).
* O5 — mark_failed bumps ``attempts`` and records ``last_error`` (stays unpublished).
* O6 — two concurrent relays claim disjoint rows (FOR UPDATE SKIP LOCKED).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import insert, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.infrastructure.db.models.events import EventOutbox as EventOutboxRow
from app.infrastructure.repositories.event_outbox_repository import EventOutboxRepository

# Far-past so this test's rows sort ahead of any recent baseline rows in the
# shared table — used by the ordering + concurrency tests for determinism.
_BASE = datetime(2000, 1, 1, tzinfo=UTC)
_BIG = 100_000  # a limit large enough to return every unpublished row


async def _add(
    repo: EventOutboxRepository,
    *,
    aggregate_type: str,
    occurred_at: datetime,
) -> None:
    await repo.add(
        aggregate_type=aggregate_type,
        aggregate_id=uuid4(),
        event_type="RelayTestEvent",
        payload={"n": 1},
        occurred_at=occurred_at,
    )


async def _ids_for(session: AsyncSession, aggregate_type: str) -> list[UUID]:
    """Committed-order-independent: fetch this test's rows by its unique marker."""
    rows = (
        await session.execute(
            select(EventOutboxRow.id, EventOutboxRow.occurred_at)
            .where(EventOutboxRow.aggregate_type == aggregate_type)
            .order_by(EventOutboxRow.occurred_at.asc(), EventOutboxRow.id.asc())
        )
    ).all()
    return [r.id for r in rows]


# ---- O1 — fetch ordering (relative to this test's rows) ---------------


@pytest.mark.integration
async def test_o1_fetch_orders_by_occurred_at(session: AsyncSession) -> None:
    marker = f"relay-o1-{uuid4()}"
    repo = EventOutboxRepository(session)
    # Insert newest-first so a naive query would return them out of order.
    await _add(repo, aggregate_type=marker, occurred_at=_BASE + timedelta(days=2))
    await _add(repo, aggregate_type=marker, occurred_at=_BASE + timedelta(days=1))
    await session.flush()

    fetched = await repo.fetch_unpublished(limit=_BIG, max_attempts=10)
    mine = [e.id for e in fetched if e.aggregate_type == marker]
    expected = await _ids_for(session, marker)  # occurred_at asc
    assert mine == expected
    assert len(mine) == 2


# ---- O2 — fetch excludes parked (attempts >= max_attempts) ------------


@pytest.mark.integration
async def test_o2_fetch_excludes_parked(session: AsyncSession) -> None:
    marker = f"relay-o2-{uuid4()}"
    repo = EventOutboxRepository(session)
    await _add(repo, aggregate_type=marker, occurred_at=_BASE)
    await _add(repo, aggregate_type=marker, occurred_at=_BASE + timedelta(days=1))
    await session.flush()
    ids = await _ids_for(session, marker)
    parked_id, live_id = ids[0], ids[1]

    # Park the first row at the cap.
    await session.execute(
        text("UPDATE event_outbox SET attempts = 10 WHERE id = :id"), {"id": parked_id}
    )
    await session.flush()

    fetched = await repo.fetch_unpublished(limit=_BIG, max_attempts=10)
    mine = {e.id for e in fetched if e.aggregate_type == marker}
    assert parked_id not in mine
    assert live_id in mine


# ---- O3 — fetch excludes already-published ----------------------------


@pytest.mark.integration
async def test_o3_fetch_excludes_published(session: AsyncSession) -> None:
    marker = f"relay-o3-{uuid4()}"
    repo = EventOutboxRepository(session)
    await _add(repo, aggregate_type=marker, occurred_at=_BASE)
    await session.flush()
    published_id = (await _ids_for(session, marker))[0]

    await session.execute(
        text("UPDATE event_outbox SET published_at = now() WHERE id = :id"),
        {"id": published_id},
    )
    await session.flush()

    fetched = await repo.fetch_unpublished(limit=_BIG, max_attempts=10)
    mine = {e.id for e in fetched if e.aggregate_type == marker}
    assert mine == set()


# ---- O4 — mark_published drops the row from the next fetch ------------


@pytest.mark.integration
async def test_o4_mark_published(session: AsyncSession) -> None:
    marker = f"relay-o4-{uuid4()}"
    repo = EventOutboxRepository(session)
    await _add(repo, aggregate_type=marker, occurred_at=_BASE)
    await session.flush()
    event_id = (await _ids_for(session, marker))[0]

    stamp = datetime.now(UTC)
    await repo.mark_published(event_id=event_id, published_at=stamp)
    await session.flush()

    row = (
        await session.execute(
            select(EventOutboxRow.published_at).where(EventOutboxRow.id == event_id)
        )
    ).scalar_one()
    assert row is not None

    fetched = await repo.fetch_unpublished(limit=_BIG, max_attempts=10)
    assert event_id not in {e.id for e in fetched}


# ---- O5 — mark_failed bumps attempts + records last_error -------------


@pytest.mark.integration
async def test_o5_mark_failed(session: AsyncSession) -> None:
    marker = f"relay-o5-{uuid4()}"
    repo = EventOutboxRepository(session)
    await _add(repo, aggregate_type=marker, occurred_at=_BASE)
    await session.flush()
    event_id = (await _ids_for(session, marker))[0]

    await repo.mark_failed(event_id=event_id, error="RuntimeError: boom")
    await session.flush()

    row = (
        await session.execute(
            select(
                EventOutboxRow.attempts, EventOutboxRow.last_error, EventOutboxRow.published_at
            ).where(EventOutboxRow.id == event_id)
        )
    ).one()
    assert row.attempts == 1
    assert row.last_error == "RuntimeError: boom"
    assert row.published_at is None  # still eligible for retry


# ---- O6 — FOR UPDATE SKIP LOCKED: concurrent relays don't collide -----


@pytest.mark.integration
async def test_o6_skip_locked_disjoint_claims(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    marker = f"relay-skiplocked-{uuid4()}"
    # Seed two COMMITTED, far-past rows so they are the two globally-oldest
    # unpublished events (visible to both connections).
    async with session_factory() as seed:
        for i in range(2):
            await seed.execute(
                insert(EventOutboxRow).values(
                    aggregate_type=marker,
                    aggregate_id=uuid4(),
                    event_type="RelayTestEvent",
                    event_version="1.0",
                    payload={},
                    metadata_json={},
                    occurred_at=_BASE + timedelta(days=i),
                )
            )
        await seed.commit()

    sess_a = session_factory()
    sess_b = session_factory()
    try:
        # Relay A claims (and locks) the oldest row; its transaction stays open.
        claimed_a = await EventOutboxRepository(sess_a).fetch_unpublished(limit=1, max_attempts=10)
        assert len(claimed_a) == 1
        assert claimed_a[0].aggregate_type == marker

        # Relay B, concurrently, skips A's locked row and claims the next.
        claimed_b = await EventOutboxRepository(sess_b).fetch_unpublished(limit=1, max_attempts=10)
        assert len(claimed_b) == 1
        assert claimed_b[0].aggregate_type == marker
        assert claimed_a[0].id != claimed_b[0].id
    finally:
        await sess_a.rollback()
        await sess_b.rollback()
        await sess_a.close()
        await sess_b.close()
        async with session_factory() as cleanup:
            await cleanup.execute(
                text("DELETE FROM event_outbox WHERE aggregate_type = :m"), {"m": marker}
            )
            await cleanup.commit()
