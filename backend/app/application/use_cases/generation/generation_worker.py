"""``GenerationWorker`` — the poll ingress that executes queued generations (α9.7 / ADR-0052 D2).

Mirrors :class:`PublishWorker`: ``run_once()`` is invoked externally, drains a batch, and
isolates failures so one bad generation never stops the rest. What makes generation different
is spend — **one generation execution is one external spend opportunity** — and every deviation
from the publish worker below exists to protect that invariant.

``run_once()`` has two phases:

**Phase 0 — reap.** Terminalise generations a crashed worker abandoned. A row qualifies only if
all three hold: it was claimed (non-terminal but no longer ``queued``), its lease can be
acquired (no live worker holds it), and it has gone quiet past the staleness cutoff. It is
marked ``failed``, **never re-queued**: with ``max_attempts = 1`` a crashed run may already have
been paid for, and re-running it would silently spend again. Without this phase a crashed run
would sit mid-state forever and a polling client would never see a terminal status.

**Phase 1 — claim and run.** For each queued id: take the ``generation:<id>`` lease, CAS
``queued → planning``, then run the pipeline while renewing the lease in the background. The
lease and the CAS are both present and do different jobs — the CAS makes claiming exactly-once,
the lease makes *liveness* observable so phase 0 can tell a dead worker from a slow one. This is
the publish precedent (job lease, then a ``queued → running`` CAS) reused rather than reinvented.

There is no retry path anywhere in this module: nothing ever writes a generation back to
``queued``, so ``max_attempts = 1`` is structural rather than a counter that could drift. The
shot-level repair budget inside the pipeline is untouched — it is intra-run and already paid for.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from app.application.interfaces.generation_job_store import IGenerationJobStore
from app.application.interfaces.generation_runner import IGenerationRunner
from app.application.interfaces.locks import Lease
from app.application.interfaces.unit_of_work import IUnitOfWork
from app.application.use_cases.generation.request import GenerateVideoRequest
from app.application.use_cases.generation.request_codec import to_runtime_request
from app.application.use_cases.generation.results import GenerateVideoResult

logger = logging.getLogger(__name__)

_DEFAULT_BATCH = 5
_DEFAULT_LEASE = timedelta(seconds=300)
_DEFAULT_HEARTBEAT = timedelta(seconds=60)
_DEFAULT_REAP_GRACE = timedelta(seconds=120)

LOST_WORKER_REASON = "worker lost before completion"


@dataclass(frozen=True, slots=True)
class GenerationOutcome:
    """What happened to one generation in this scan."""

    generation_id: UUID
    status: str
    reason: str = ""


@dataclass(frozen=True, slots=True)
class GenerationPollResult:
    """Aggregate outcome of one ``run_once`` scan."""

    scanned: int
    reaped: int = 0
    outcomes: list[GenerationOutcome] = field(default_factory=list)


class GenerationWorker:
    """Reap abandoned generations, then claim and execute queued ones."""

    def __init__(
        self,
        uow: IUnitOfWork,
        store: IGenerationJobStore,
        runner: IGenerationRunner,
        *,
        batch_size: int = _DEFAULT_BATCH,
        lease: timedelta = _DEFAULT_LEASE,
        heartbeat: timedelta = _DEFAULT_HEARTBEAT,
        reap_grace: timedelta = _DEFAULT_REAP_GRACE,
        owner: str | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._uow = uow
        self._store = store
        self._runner = runner
        self._batch_size = batch_size
        self._lease = lease
        self._heartbeat = heartbeat
        self._reap_grace = reap_grace
        self._owner = owner or f"generation-worker:{uuid4()}"
        self._clock = clock or (lambda: datetime.now(UTC))

    async def run_once(self) -> GenerationPollResult:
        reaped = await self._reap()
        outcomes = await self._claim_and_run()
        return GenerationPollResult(scanned=len(outcomes), reaped=reaped, outcomes=outcomes)

    # ---- phase 0 — reap ---------------------------------------------------- #

    async def _reap(self) -> int:
        cutoff = self._clock() - self._reap_grace
        candidates = await self._store.list_reapable(stale_before=cutoff, limit=self._batch_size)
        reaped = 0
        for generation_id in candidates:
            lease = await self._acquire(generation_id)
            if lease is None:
                # A live worker still holds the lease: the run is slow, not abandoned.
                continue
            try:
                if await self._store.mark_lost(
                    generation_id=generation_id, reason=LOST_WORKER_REASON
                ):
                    reaped += 1
                    logger.warning("generation.reaped", extra={"generation_id": str(generation_id)})
            finally:
                await self._release(lease)
        return reaped

    # ---- phase 1 — claim and run ------------------------------------------- #

    async def _claim_and_run(self) -> list[GenerationOutcome]:
        outcomes: list[GenerationOutcome] = []
        for generation_id in await self._store.list_claimable(limit=self._batch_size):
            lease = await self._acquire(generation_id)
            if lease is None:
                continue
            try:
                claimed = await self._store.claim(generation_id=generation_id)
                if claimed is None:
                    # Lost the CAS to a racing worker; it owns the run now.
                    continue
                request = to_runtime_request(claimed.spec, generation_id=generation_id)
                result = await self._run_with_heartbeat(lease, request)
                outcomes.append(
                    GenerationOutcome(
                        generation_id=generation_id,
                        status=str(result.status),
                        reason=result.reason,
                    )
                )
            except Exception as exc:
                # The pipeline settles its own failures; reaching here means something below
                # it broke. Terminalise so the row cannot linger unobservable, and move on.
                logger.exception(
                    "generation.run_failed", extra={"generation_id": str(generation_id)}
                )
                await self._store.mark_lost(generation_id=generation_id, reason=str(exc))
                outcomes.append(
                    GenerationOutcome(generation_id=generation_id, status="failed", reason=str(exc))
                )
            finally:
                await self._release(lease)
        return outcomes

    async def _run_with_heartbeat(
        self, lease: Lease, request: GenerateVideoRequest
    ) -> GenerateVideoResult:
        """Run the pipeline, renewing the lease while it is in flight.

        A lease that expired mid-run would let a second worker start the same generation and
        double the spend — the cost analogue of a duplicate post. Renewal closes that window
        without having to guess a maximum run length up front.
        """
        task = asyncio.create_task(self._runner.run(request))
        current = lease
        while True:
            done, _ = await asyncio.wait({task}, timeout=self._heartbeat.total_seconds())
            if done:
                return await task
            renewed = await self._renew(current)
            if renewed is None:
                # Lost the lease. Cancelling would not refund what has already been spent, so
                # the run continues; the reaper's staleness window makes a competing
                # terminalisation unlikely, and a later completion writes the truthful state.
                logger.warning("generation.lease_lost", extra={"lock_key": current.lock_key})
            else:
                current = renewed

    # ---- lease plumbing ----------------------------------------------------- #

    async def _acquire(self, generation_id: UUID) -> Lease | None:
        async with self._uow:
            lease = await self._uow.locks.acquire(
                key=f"generation:{generation_id}", owner=self._owner, lease=self._lease
            )
            await self._uow.commit()
        return lease

    async def _renew(self, lease: Lease) -> Lease | None:
        async with self._uow:
            renewed = await self._uow.locks.renew(lease, lease_for=self._lease)
            await self._uow.commit()
        return renewed

    async def _release(self, lease: Lease) -> None:
        async with self._uow:
            await self._uow.locks.release(lease)
            await self._uow.commit()
