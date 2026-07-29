"""``ExportWorker`` — the poll ingress that drains ``queued`` export jobs (α8.5a).

Mirrors :class:`app.application.use_cases.render.render_worker.RenderWorker` (Fork B):
transcoding is CPU-bound, so it runs behind a **poller**, not the relay fan-out. One
``run_once()`` scans the oldest ``queued`` export jobs (FIFO) and hands each to
:class:`ProcessExportJob`, which settles it independently under its own ``export_job:<id>``
lease — so one slow/stuck export never blocks the others.

W8.5.1 / W8.5.2: the worker only reads ``export_jobs`` (the claimable scan, which resolves
each job's owning ``project_id`` via ``render_jobs``) and delegates; it touches no
orchestration state.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import structlog

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.application.use_cases.export.process_export_job import (
    ProcessExportJob,
    ProcessExportJobResult,
)

_LOGGER = structlog.get_logger(__name__)

_DEFAULT_BATCH = 10


@dataclass(frozen=True, slots=True)
class ExportPollResult:
    """Aggregate outcome of one ``run_once`` scan."""

    scanned: int
    outcomes: list[ProcessExportJobResult] = field(default_factory=list)


class ExportWorker:
    """Drain queued export jobs once per invocation (the α8.5a export ingress)."""

    def __init__(
        self,
        uow: IUnitOfWork,
        process: ProcessExportJob,
        *,
        batch_size: int = _DEFAULT_BATCH,
    ) -> None:
        self._uow = uow
        self._process = process
        self._batch_size = batch_size

    async def run_once(self) -> ExportPollResult:
        """Claim + export every currently-``queued`` job in the batch, oldest first."""
        async with self._uow:
            claims = await self._uow.export_jobs.list_claimable(limit=self._batch_size)

        outcomes: list[ProcessExportJobResult] = []
        for claim in claims:
            try:
                outcomes.append(
                    await self._process.process(
                        project_id=claim.project_id, export_job_id=claim.export_job_id
                    )
                )
            except Exception:
                # α9.8 PF8: isolate one unclassified failure (ffmpeg, storage) so it cannot discard
                # the batch. The job's lease expires and a later pass retries it.
                _LOGGER.warning(
                    "export.process_error",
                    export_job_id=str(claim.export_job_id),
                    exc_info=True,
                )
        return ExportPollResult(scanned=len(claims), outcomes=outcomes)
