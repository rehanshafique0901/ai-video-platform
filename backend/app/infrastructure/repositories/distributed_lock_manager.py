"""SQLAlchemy implementation of ``IDistributedLockManager`` (Slice α7.3).

The first application consumer of ``distributed_locks`` (CR-DB-2, ``schema.md``
§32; introduced inert by ADR-0022, given its lease CHECK by ADR-0032). Implements
the ADR-0041 D8 lease contract entirely in SQL so acquire/steal is a **single
atomic round-trip** with no read-then-write race:

* **acquire** — ``INSERT … ON CONFLICT (lock_key) DO UPDATE … WHERE
  distributed_locks.lease_until < now()``. A free key inserts; an *expired* key is
  stolen by the ``DO UPDATE``; a *live* key fails the ``WHERE`` predicate, updates
  nothing, and returns no row → the caller gets ``None``. Postgres evaluates the
  conflict + predicate atomically, so two racers cannot both win.
* **renew** — owner-fenced + live-fenced ``UPDATE``.
* **release** — owner-fenced ``DELETE`` (idempotent).
* **reclaim_expired** — ``DELETE … WHERE lease_until < :now`` janitor.

Every path relies on the DB clock (``now()``) rather than the app clock, so lease
arithmetic is consistent across processes. The ``chk_distributed_locks_lease_until_after_acquired_at``
CHECK (ADR-0032) is the backstop for a non-positive lease; :meth:`acquire` also
guards it in Python for a clean ``ValueError`` at the call site.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.interfaces.locks import IDistributedLockManager, Lease

# Acquire-or-steal. ``:lease_seconds`` is applied via ``now() + make_interval`` so
# both the fresh-INSERT and the steal-UPDATE compute the lease against the DB
# clock. The ``WHERE`` on the DO UPDATE is what makes stealing safe: it only fires
# for an already-expired lease, so a live holder is never displaced.
_ACQUIRE_SQL = text(
    """
    INSERT INTO distributed_locks
        (lock_key, owner, lease_until, heartbeat_at, acquired_at, metadata)
    VALUES
        (:key, :owner, now() + make_interval(secs => :lease_seconds), now(), now(), '{}'::jsonb)
    ON CONFLICT (lock_key) DO UPDATE
        SET owner        = EXCLUDED.owner,
            lease_until  = EXCLUDED.lease_until,
            heartbeat_at = EXCLUDED.heartbeat_at,
            acquired_at  = now()
        WHERE distributed_locks.lease_until < now()
    RETURNING lock_key, owner, lease_until, heartbeat_at, acquired_at
    """
).bindparams(bindparam("lease_seconds"))

# Owner-fenced + live-fenced renew: only a lease the caller still holds AND that
# has not yet expired can be extended (a missed heartbeat window = lost lock).
_RENEW_SQL = text(
    """
    UPDATE distributed_locks
        SET lease_until  = now() + make_interval(secs => :lease_seconds),
            heartbeat_at = now()
    WHERE lock_key = :key
      AND owner    = :owner
      AND lease_until > now()
    RETURNING lock_key, owner, lease_until, heartbeat_at, acquired_at
    """
).bindparams(bindparam("lease_seconds"))

# Owner-fenced release (idempotent): a holder can free its own key even if the
# lease has lapsed, but cannot delete a key another owner has since stolen.
_RELEASE_SQL = text(
    """
    DELETE FROM distributed_locks
    WHERE lock_key = :key AND owner = :owner
    RETURNING lock_key
    """
)


class SqlAlchemyDistributedLockManager(IDistributedLockManager):
    """Lease bookkeeping over ``distributed_locks`` — atomic acquire/steal in one statement."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def acquire(self, *, key: str, owner: str, lease: timedelta) -> Lease | None:
        lease_seconds = lease.total_seconds()
        if lease_seconds <= 0:
            # ADR-0032: a zero/negative lease would violate
            # ``lease_until > acquired_at``. Fail fast at the call site with a
            # clean domain error rather than a psycopg IntegrityError; the CHECK
            # remains the durable backstop for any path that bypasses this guard.
            raise ValueError("lease must be strictly positive")
        row = (
            await self._session.execute(
                _ACQUIRE_SQL,
                {"key": key, "owner": owner, "lease_seconds": lease_seconds},
            )
        ).one_or_none()
        return _row_to_lease(row) if row is not None else None

    async def renew(self, lease: Lease, *, lease_for: timedelta) -> Lease | None:
        lease_seconds = lease_for.total_seconds()
        if lease_seconds <= 0:
            raise ValueError("lease must be strictly positive")
        row = (
            await self._session.execute(
                _RENEW_SQL,
                {"key": lease.lock_key, "owner": lease.owner, "lease_seconds": lease_seconds},
            )
        ).one_or_none()
        return _row_to_lease(row) if row is not None else None

    async def release(self, lease: Lease) -> bool:
        row = (
            await self._session.execute(
                _RELEASE_SQL,
                {"key": lease.lock_key, "owner": lease.owner},
            )
        ).one_or_none()
        return row is not None

    async def reclaim_expired(self, *, now: datetime | None = None) -> int:
        # RETURNING + row count (consistent with acquire/release) so the count is
        # exact and typed without reaching for a driver-specific ``rowcount``.
        if now is None:
            stmt = text(
                "DELETE FROM distributed_locks WHERE lease_until < now() RETURNING lock_key"
            )
            params: dict[str, object] = {}
        else:
            stmt = text("DELETE FROM distributed_locks WHERE lease_until < :now RETURNING lock_key")
            params = {"now": now}
        rows = (await self._session.execute(stmt, params)).all()
        return len(rows)


def _row_to_lease(row: Row[Any]) -> Lease:
    return Lease(
        lock_key=row.lock_key,
        owner=row.owner,
        lease_until=row.lease_until,
        heartbeat_at=row.heartbeat_at,
        acquired_at=row.acquired_at,
    )
