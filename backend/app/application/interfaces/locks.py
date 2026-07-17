"""Port: distributed lock manager (Slice α7.3).

The first consumer of the baseline ``distributed_locks`` table (CR-DB-2,
``schema.md`` §32) and its ``chk_distributed_locks_lease_until_after_acquired_at``
CHECK (ADR-0032). Implements the lease contract fixed in ADR-0041 D8:

* **acquire** — take a free key, or **steal** one whose lease has expired
  (``lease_until < now()``), in a single atomic round-trip. An active lease held
  by a live owner is **never** stolen.
* **renew** (heartbeat) — extend a lease the caller still holds. **Owner-fenced**
  (``lock_key`` + ``owner``) *and* live-fenced (the lease must not have expired):
  a holder that missed its heartbeat window has lost the lock and must re-acquire.
* **release** — free a key the caller holds. **Owner-fenced**, idempotent.
* **reclaim_expired** — the janitor: delete abandoned expired rows. An explicit
  maintenance operation, **not** a daemon; correctness comes from steal-on-acquire
  (α7.3 sign-off Q5), not from this cleanup.

The manager owns *lease bookkeeping only* — it never knows what the lock
protects. Ownership is a **caller-supplied** opaque identity string (e.g.
``worker:<uuid4>``); the caller is responsible for using a stable, unique token
per holder (α7.3 sign-off Q4). Per ADR-0041 D8, a background loop that *holds* a
lease across real work (heartbeating on a timer) is a later slice (α8.1) — α7.3
ships the primitive, not the loop.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True, slots=True)
class Lease:
    """An owned distributed lock lease — the value returned by a successful acquire/renew.

    Immutable snapshot of the ``distributed_locks`` row the caller holds.
    ``owner`` is the fencing identity: ``renew`` and ``release`` only affect a row
    whose ``owner`` still matches. ``lease_until`` is when the lease expires (a
    live lease has ``lease_until > now()``); ``acquired_at`` is when *this* holder
    took the key (reset on a steal). Invariant (DB-enforced, ADR-0032):
    ``lease_until > acquired_at``.
    """

    lock_key: str
    owner: str
    lease_until: datetime
    heartbeat_at: datetime
    acquired_at: datetime


class IDistributedLockManager(ABC):
    """Acquire / renew / release / reclaim leases over ``distributed_locks`` (ADR-0041 D8)."""

    @abstractmethod
    async def acquire(self, *, key: str, owner: str, lease: timedelta) -> Lease | None:
        """Acquire ``key`` for ``owner`` for ``lease`` duration; ``None`` if held live.

        Atomically inserts a fresh lease for a free key, or **steals** a key whose
        ``lease_until`` is already in the past (the prior holder abandoned it).
        Returns the resulting :class:`Lease`, or ``None`` when the key is currently
        held by a **live** lease (``lease_until > now()``) — the caller did not get
        the lock. ``lease`` must be strictly positive (a zero/negative lease is a
        ``ValueError``; the DB CHECK ``lease_until > acquired_at`` is the backstop).
        """
        ...

    @abstractmethod
    async def renew(self, lease: Lease, *, lease_for: timedelta) -> Lease | None:
        """Extend a lease the caller still holds; ``None`` if it can no longer renew.

        **Owner-fenced and live-fenced**: the update matches only a row with the
        same ``lock_key`` + ``owner`` whose current ``lease_until`` is still in the
        future. Returns the refreshed :class:`Lease`, or ``None`` if the lease was
        lost (stolen after expiry, released, or the heartbeat window was missed) —
        the caller must treat ``None`` as "you no longer hold this lock".
        """
        ...

    @abstractmethod
    async def release(self, lease: Lease) -> bool:
        """Release a lease the caller holds. **Owner-fenced**, idempotent.

        Deletes the row iff its ``owner`` still matches ``lease.owner``. Returns
        ``True`` if a row was freed, ``False`` if there was nothing to free (already
        released, or stolen by another owner). Not live-fenced — a holder may clean
        up its own expired-but-unstolen key.
        """
        ...

    @abstractmethod
    async def reclaim_expired(self, *, now: datetime | None = None) -> int:
        """Janitor: delete rows whose lease has expired; return the count reclaimed.

        Cleanup only — an explicit maintenance call (no daemon, no timer in α7.3).
        Correctness does not depend on it (steal-on-acquire already frees expired
        keys); this reclaims abandoned rows nobody re-acquires. ``now`` defaults to
        the database clock when omitted.
        """
        ...
