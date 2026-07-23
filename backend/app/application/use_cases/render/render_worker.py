"""``RenderWorker`` — the poll ingress that drains ``queued`` render jobs (α8.4b).

Mirrors ``CompletionEngine.poll_once`` (Fork A): rendering is long-running and
CPU-bound, so it runs behind a **poller**, not the relay fan-out — the relay stays
fast for the lightweight ingestion/notification subscribers. One ``run_once()``
scans the oldest ``queued`` jobs (FIFO) and hands each to :class:`ProcessRenderJob`,
which settles it independently under its own ``render_job:<id>`` lease — so one
slow/stuck render never blocks the others.

W8.4b.1 / W8.4b.2: the worker only reads ``render_jobs`` (the claimable scan) and
delegates; it touches no orchestration state.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.application.use_cases.render.process_render_job import (
    ProcessRenderJob,
    ProcessRenderJobResult,
)

_DEFAULT_BATCH = 10


@dataclass(frozen=True, slots=True)
class RenderPollResult:
    """Aggregate outcome of one ``run_once`` scan."""

    scanned: int
    outcomes: list[ProcessRenderJobResult] = field(default_factory=list)


class RenderWorker:
    """Drain queued render jobs once per invocation (the α8.4b render ingress)."""

    def __init__(
        self,
        uow: IUnitOfWork,
        process: ProcessRenderJob,
        *,
        batch_size: int = _DEFAULT_BATCH,
    ) -> None:
        self._uow = uow
        self._process = process
        self._batch_size = batch_size

    async def run_once(self) -> RenderPollResult:
        """Claim + render every currently-``queued`` job in the batch, oldest first."""
        async with self._uow:
            jobs = await self._uow.render_jobs.list_claimable(limit=self._batch_size)

        outcomes: list[ProcessRenderJobResult] = []
        for job in jobs:
            outcomes.append(
                await self._process.process(project_id=job.project_id, render_job_id=job.id)
            )
        return RenderPollResult(scanned=len(jobs), outcomes=outcomes)
