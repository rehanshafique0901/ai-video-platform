"""Unit tests for ``RenderWorker.run_once`` (Slice α8.4b).

The render poll ingress: one scan claims the oldest ``queued`` jobs (FIFO, capped
by ``batch_size``) and delegates each to ``ProcessRenderJob`` under its own lease.
Reuses the ``ProcessRenderJob`` fixtures so the worker is exercised end-to-end
against the real use case + in-memory fakes (no stubbing of the delegate).
"""

from __future__ import annotations

import pytest

from app.application.use_cases.render.process_render_job import ProcessRenderJob
from app.application.use_cases.render.render_worker import RenderWorker
from tests.unit.application.use_cases.render.test_process_render_job import (
    FakeRenderer,
    _Fixture,
)

pytestmark = pytest.mark.unit


def _worker(fx: _Fixture, renderer: FakeRenderer, *, batch_size: int = 10) -> RenderWorker:
    process = ProcessRenderJob(uow=fx.uow, storage=fx.storage, renderer=renderer)
    return RenderWorker(uow=fx.uow, process=process, batch_size=batch_size)


async def test_run_once_drains_all_queued_jobs() -> None:
    fx = _Fixture()
    asset_id = await fx.seed_video_asset("srcA")
    timeline_id = await fx.seed_timeline([asset_id])
    await fx.seed_job(timeline_id)
    await fx.seed_job(timeline_id)
    renderer = FakeRenderer()

    result = await _worker(fx, renderer).run_once()

    assert result.scanned == 2
    assert [o.status for o in result.outcomes] == ["rendered", "rendered"]
    # Both jobs settled succeeded.
    remaining = await fx.render_jobs.list_claimable(limit=10)
    assert remaining == []


async def test_run_once_respects_batch_size() -> None:
    fx = _Fixture()
    asset_id = await fx.seed_video_asset("srcA")
    timeline_id = await fx.seed_timeline([asset_id])
    await fx.seed_job(timeline_id)
    await fx.seed_job(timeline_id)
    renderer = FakeRenderer()

    result = await _worker(fx, renderer, batch_size=1).run_once()

    assert result.scanned == 1
    # One job remains queued for the next scan.
    remaining = await fx.render_jobs.list_claimable(limit=10)
    assert len(remaining) == 1


async def test_run_once_empty_scan_is_noop() -> None:
    fx = _Fixture()
    renderer = FakeRenderer()

    result = await _worker(fx, renderer).run_once()

    assert result.scanned == 0
    assert result.outcomes == []
