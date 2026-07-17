"""Integration tests for ``SqlAlchemyDistributedLockManager`` (Slice α7.3).

Runs against the live database inside a SAVEPOINT that rolls back on teardown.
Exercises the ADR-0041 D8 lease contract over ``distributed_locks`` + its
``chk_distributed_locks_lease_until_after_acquired_at`` CHECK (ADR-0032).

**Transaction-clock note.** Postgres freezes ``now()`` at transaction start, and a
lease acquired in *this* savepoint has ``acquired_at == now()`` — so it can never
appear "expired" within the same transaction. Expiry / steal / reclaim scenarios
therefore seed a **past-dated** row directly (``acquired_at`` + ``lease_until``
both in the year 2000, so the lease CHECK holds and the lease is already expired
relative to the live clock). "Live lease" scenarios use the manager's own
``acquire`` (lease_until = now()+lease > now()).

Coverage map (α7.3 sign-off Q4/Q5):

* L1  — acquire a free key returns a lease owned by the caller.
* L2  — acquire a key held by a **live** lease returns ``None`` (no steal).
* L3  — acquire **steals** an expired lease (past-dated row) for the new owner.
* L4  — acquire with a non-positive lease raises ``ValueError``.
* L5  — renew (owner + live) extends ``lease_until`` and bumps ``heartbeat_at``.
* L6  — renew with the wrong owner returns ``None`` (owner-fenced).
* L7  — renew an expired lease returns ``None`` (live-fenced).
* L8  — release (owner-fenced) frees the key; a second release is ``False``.
* L9  — release with the wrong owner returns ``False`` (no delete).
* L10 — reclaim_expired deletes only expired rows and returns the count.
* L11 — the DB CHECK rejects a lease with ``lease_until <= acquired_at``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.interfaces.locks import Lease
from app.infrastructure.repositories.distributed_lock_manager import (
    SqlAlchemyDistributedLockManager,
)

_PAST_ACQUIRED = datetime(2000, 1, 1, tzinfo=UTC)
_PAST_LEASE_UNTIL = datetime(2000, 1, 2, tzinfo=UTC)  # > acquired_at (CHECK ok), < now()


def _key() -> str:
    return f"lock-{uuid4()}"


async def _seed_expired(session: AsyncSession, *, key: str, owner: str) -> None:
    """Insert a past-dated (already-expired) lock row directly (bypassing acquire)."""
    await session.execute(
        text(
            "INSERT INTO distributed_locks "
            "(lock_key, owner, lease_until, heartbeat_at, acquired_at, metadata) "
            "VALUES (:k, :o, :lu, :hb, :aq, '{}'::jsonb)"
        ),
        {
            "k": key,
            "o": owner,
            "lu": _PAST_LEASE_UNTIL,
            "hb": _PAST_ACQUIRED,
            "aq": _PAST_ACQUIRED,
        },
    )
    await session.flush()


# ---- L1 — acquire a free key ------------------------------------------


@pytest.mark.integration
async def test_l1_acquire_free_key(session: AsyncSession) -> None:
    mgr = SqlAlchemyDistributedLockManager(session)
    key = _key()

    lease = await mgr.acquire(key=key, owner="worker-a", lease=timedelta(seconds=30))
    assert lease is not None
    assert lease.lock_key == key
    assert lease.owner == "worker-a"
    assert lease.lease_until > lease.acquired_at


# ---- L2 — acquire a live key → None (no steal) ------------------------


@pytest.mark.integration
async def test_l2_acquire_live_key_returns_none(session: AsyncSession) -> None:
    mgr = SqlAlchemyDistributedLockManager(session)
    key = _key()
    first = await mgr.acquire(key=key, owner="worker-a", lease=timedelta(seconds=30))
    assert first is not None

    # A second, different owner cannot take a live lease.
    second = await mgr.acquire(key=key, owner="worker-b", lease=timedelta(seconds=30))
    assert second is None


# ---- L3 — acquire steals an expired lease -----------------------------


@pytest.mark.integration
async def test_l3_acquire_steals_expired(session: AsyncSession) -> None:
    mgr = SqlAlchemyDistributedLockManager(session)
    key = _key()
    await _seed_expired(session, key=key, owner="worker-old")

    stolen = await mgr.acquire(key=key, owner="worker-new", lease=timedelta(seconds=30))
    assert stolen is not None
    assert stolen.owner == "worker-new"
    assert stolen.lease_until > stolen.acquired_at


# ---- L4 — non-positive lease → ValueError -----------------------------


@pytest.mark.integration
async def test_l4_non_positive_lease_raises(session: AsyncSession) -> None:
    mgr = SqlAlchemyDistributedLockManager(session)
    with pytest.raises(ValueError, match="strictly positive"):
        await mgr.acquire(key=_key(), owner="worker-a", lease=timedelta(0))


# ---- L5 — renew (owner + live) extends --------------------------------


@pytest.mark.integration
async def test_l5_renew_extends(session: AsyncSession) -> None:
    mgr = SqlAlchemyDistributedLockManager(session)
    key = _key()
    lease = await mgr.acquire(key=key, owner="worker-a", lease=timedelta(seconds=10))
    assert lease is not None

    renewed = await mgr.renew(lease, lease_for=timedelta(seconds=60))
    assert renewed is not None
    assert renewed.owner == "worker-a"
    assert renewed.lease_until >= lease.lease_until


# ---- L6 — renew wrong owner → None ------------------------------------


@pytest.mark.integration
async def test_l6_renew_wrong_owner(session: AsyncSession) -> None:
    mgr = SqlAlchemyDistributedLockManager(session)
    key = _key()
    lease = await mgr.acquire(key=key, owner="worker-a", lease=timedelta(seconds=30))
    assert lease is not None

    impostor = Lease(
        lock_key=key,
        owner="worker-b",
        lease_until=lease.lease_until,
        heartbeat_at=lease.heartbeat_at,
        acquired_at=lease.acquired_at,
    )
    assert await mgr.renew(impostor, lease_for=timedelta(seconds=60)) is None


# ---- L7 — renew an expired lease → None (live-fenced) -----------------


@pytest.mark.integration
async def test_l7_renew_expired_returns_none(session: AsyncSession) -> None:
    mgr = SqlAlchemyDistributedLockManager(session)
    key = _key()
    await _seed_expired(session, key=key, owner="worker-old")

    stale = Lease(
        lock_key=key,
        owner="worker-old",
        lease_until=_PAST_LEASE_UNTIL,
        heartbeat_at=_PAST_ACQUIRED,
        acquired_at=_PAST_ACQUIRED,
    )
    assert await mgr.renew(stale, lease_for=timedelta(seconds=60)) is None


# ---- L8 — release owner-fenced + idempotent ---------------------------


@pytest.mark.integration
async def test_l8_release_owner_then_idempotent(session: AsyncSession) -> None:
    mgr = SqlAlchemyDistributedLockManager(session)
    key = _key()
    lease = await mgr.acquire(key=key, owner="worker-a", lease=timedelta(seconds=30))
    assert lease is not None

    assert await mgr.release(lease) is True
    # Second release finds nothing to free.
    assert await mgr.release(lease) is False


# ---- L9 — release wrong owner → False ---------------------------------


@pytest.mark.integration
async def test_l9_release_wrong_owner(session: AsyncSession) -> None:
    mgr = SqlAlchemyDistributedLockManager(session)
    key = _key()
    lease = await mgr.acquire(key=key, owner="worker-a", lease=timedelta(seconds=30))
    assert lease is not None

    impostor = Lease(
        lock_key=key,
        owner="worker-b",
        lease_until=lease.lease_until,
        heartbeat_at=lease.heartbeat_at,
        acquired_at=lease.acquired_at,
    )
    assert await mgr.release(impostor) is False
    # The real owner can still release it.
    assert await mgr.release(lease) is True


# ---- L10 — reclaim_expired deletes only expired -----------------------


@pytest.mark.integration
async def test_l10_reclaim_expired_counts_only_expired(session: AsyncSession) -> None:
    mgr = SqlAlchemyDistributedLockManager(session)
    expired_key = _key()
    live_key = _key()
    await _seed_expired(session, key=expired_key, owner="worker-old")
    live = await mgr.acquire(key=live_key, owner="worker-a", lease=timedelta(seconds=60))
    assert live is not None

    reclaimed = await mgr.reclaim_expired()
    assert reclaimed >= 1

    # The expired row is gone; the live one survives.
    remaining = (
        await session.execute(
            text("SELECT owner FROM distributed_locks WHERE lock_key = :k"),
            {"k": expired_key},
        )
    ).one_or_none()
    assert remaining is None
    still_live = (
        await session.execute(
            text("SELECT owner FROM distributed_locks WHERE lock_key = :k"),
            {"k": live_key},
        )
    ).one_or_none()
    assert still_live is not None


# ---- L11 — DB CHECK rejects lease_until <= acquired_at ----------------


@pytest.mark.integration
async def test_l11_check_rejects_non_positive_lease(session: AsyncSession) -> None:
    key = _key()
    now = datetime(2001, 1, 1, tzinfo=UTC)
    with pytest.raises(IntegrityError):
        await session.execute(
            text(
                "INSERT INTO distributed_locks "
                "(lock_key, owner, lease_until, heartbeat_at, acquired_at, metadata) "
                "VALUES (:k, :o, :lu, :hb, :aq, '{}'::jsonb)"
            ),
            {"k": key, "o": "w", "lu": now, "hb": now, "aq": now},  # lease_until == acquired_at
        )
        await session.flush()
