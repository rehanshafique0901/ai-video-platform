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

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.application.use_cases.export.process_export_job import (
    ProcessExportJob,
    ProcessExportJobResult,
)

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
            outcomes.append(
                await self._process.process(
                    project_id=claim.project_id, export_job_id=claim.export_job_id
                )
            )
        return ExportPollResult(scanned=len(claims), outcomes=outcomes)
