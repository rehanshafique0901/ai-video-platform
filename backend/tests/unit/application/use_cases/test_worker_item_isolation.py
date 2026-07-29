"""Unit tests — α9.8 PF8: per-item isolation in the four workers that lacked it.

Render, export, enrichment, and publish let an unclassified exception propagate straight out of
``run_once()``, silently discarding every item behind the failing one. Relay, email, and generation
already isolated per item; these tests bring the other four into conformance with that pattern and
lock it in.

The shape is identical in all four: three items, the **second** raises something the inner use case
does not classify, and the assertion is that the **third** still gets its turn. Each of these fails
against the pre-α9.8 implementation.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import TracebackType
from typing import Self
from uuid import UUID, uuid4

import pytest

from app.application.use_cases.export.export_worker import ExportWorker
from app.application.use_cases.media.media_enrichment_worker import MediaEnrichmentWorker
from app.application.use_cases.publishing.publish_worker import PublishWorker
from app.application.use_cases.render.render_worker import RenderWorker

pytestmark = pytest.mark.unit


@dataclass(frozen=True, slots=True)
class _Claim:
    """Stands in for the several claim shapes the workers scan; only ids are read."""

    id: UUID
    project_id: UUID

    @property
    def export_job_id(self) -> UUID:
        return self.id

    @property
    def publish_job_id(self) -> UUID:
        return self.id


class _Scan:
    """A repository stub whose one job is to return the seeded claims."""

    def __init__(self, claims: list[_Claim]) -> None:
        self._claims = claims

    async def list_claimable(self, **_kwargs: object) -> list[_Claim]:
        return self._claims

    async def list_enrichable_generated_videos(self, **_kwargs: object) -> list[_Claim]:
        return self._claims


class _FakeUnitOfWork:
    """Exposes the same scan stub under every repository name the four workers reach for."""

    def __init__(self, claims: list[_Claim]) -> None:
        scan = _Scan(claims)
        self.render_jobs = scan
        self.export_jobs = scan
        self.publish_jobs = scan
        self.media = scan

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None


class _FailsOnSecond:
    """Processes each item, raising an unclassified error for the second one only."""

    def __init__(self) -> None:
        self.seen: list[UUID] = []

    async def _handle(self, item_id: UUID) -> str:
        self.seen.append(item_id)
        if len(self.seen) == 2:
            raise RuntimeError("unclassified failure the inner use case never anticipated")
        return "ok"

    async def process(self, **kwargs: object) -> str:
        item_id = kwargs.get("render_job_id") or kwargs.get("export_job_id")
        item_id = item_id or kwargs.get("publish_job_id")
        assert isinstance(item_id, UUID)
        return await self._handle(item_id)

    async def execute(self, *, asset: _Claim) -> str:
        return await self._handle(asset.id)


def _three_claims() -> list[_Claim]:
    project_id = uuid4()
    return [_Claim(id=uuid4(), project_id=project_id) for _ in range(3)]


async def test_render_worker_isolates_a_failing_job() -> None:
    claims = _three_claims()
    process = _FailsOnSecond()
    worker = RenderWorker(uow=_FakeUnitOfWork(claims), process=process)  # type: ignore[arg-type]

    result = await worker.run_once()

    assert [c.id for c in claims] == process.seen, "the third job never got its turn"
    assert result.scanned == 3
    assert len(result.outcomes) == 2, "only the successful jobs contribute outcomes"


async def test_export_worker_isolates_a_failing_job() -> None:
    claims = _three_claims()
    process = _FailsOnSecond()
    worker = ExportWorker(uow=_FakeUnitOfWork(claims), process=process)  # type: ignore[arg-type]

    result = await worker.run_once()

    assert [c.id for c in claims] == process.seen
    assert result.scanned == 3
    assert len(result.outcomes) == 2


async def test_publish_worker_isolates_a_failing_job() -> None:
    claims = _three_claims()
    process = _FailsOnSecond()
    worker = PublishWorker(uow=_FakeUnitOfWork(claims), process=process)  # type: ignore[arg-type]

    result = await worker.run_once()

    assert [c.id for c in claims] == process.seen
    assert result.scanned == 3
    assert len(result.outcomes) == 2


async def test_enrichment_worker_isolates_a_failing_asset() -> None:
    claims = _three_claims()
    enrich = _FailsOnSecond()
    worker = MediaEnrichmentWorker(uow=_FakeUnitOfWork(claims), enrich=enrich)  # type: ignore[arg-type]

    result = await worker.run_once()

    assert [c.id for c in claims] == enrich.seen
    assert result.scanned == 3
    assert len(result.outcomes) == 2
