"""``PublishWorker`` — the poll ingress that drains due ``queued`` publish jobs (α8.6b).

A faithful adaptation of :class:`app.application.use_cases.export.export_worker.ExportWorker`
(DQ8): uploading is I/O-bound + serialised per project, so it runs behind a **poller**. One
``run_once()`` scans the oldest due ``queued`` jobs (FIFO; ``scheduled_at <= now`` so retries
wait for their backoff) and hands each to :class:`ProcessPublishJob`, which settles it
independently under its own ``publish_job:<id>`` + ``project_publish:<project_id>`` leases —
so one slow/blocked publish never blocks the others.

No trigger, endpoint, or cron is added in α8.6b — the worker is invoked externally, exactly
as ``ExportWorker`` is (PUB-7).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

import structlog

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.application.use_cases.publishing.process_publish_job import (
    ProcessPublishJob,
    ProcessPublishJobResult,
)

_LOGGER = structlog.get_logger(__name__)

_DEFAULT_BATCH = 10


@dataclass(frozen=True, slots=True)
class PublishPollResult:
    """Aggregate outcome of one ``run_once`` scan."""

    scanned: int
    outcomes: list[ProcessPublishJobResult] = field(default_factory=list)


class PublishWorker:
    """Drain due queued publish jobs once per invocation (the α8.6b publish ingress)."""

    def __init__(
        self,
        uow: IUnitOfWork,
        process: ProcessPublishJob,
        *,
        batch_size: int = _DEFAULT_BATCH,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._uow = uow
        self._process = process
        self._batch_size = batch_size
        self._clock = clock or (lambda: datetime.now(UTC))

    async def run_once(self) -> PublishPollResult:
        """Claim + publish every currently-due ``queued`` job in the batch, oldest first."""
        now = self._clock()
        async with self._uow:
            claims = await self._uow.publish_jobs.list_claimable(now=now, limit=self._batch_size)

        outcomes: list[ProcessPublishJobResult] = []
        for claim in claims:
            try:
                outcomes.append(
                    await self._process.process(
                        project_id=claim.project_id, publish_job_id=claim.publish_job_id
                    )
                )
            except Exception:
                # α9.8 PF8: isolate one unclassified failure (credentials, storage, destination
                # HTTP) so it cannot discard the batch. PUB-11 is unaffected — this changes nothing
                # about how an ambiguous upload outcome is classified, only whether the *other*
                # jobs in the batch get their turn.
                _LOGGER.warning(
                    "publish.process_error",
                    publish_job_id=str(claim.publish_job_id),
                    exc_info=True,
                )
        return PublishPollResult(scanned=len(claims), outcomes=outcomes)
