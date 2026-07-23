"""``MediaEnrichmentWorker`` — the poll ingress that enriches generated videos (α8.4c).

Mirrors ``CompletionEngine.poll_once`` / ``RenderWorker.run_once`` (Fork B → B2):
FFmpeg belongs to workers, never to relay subscribers, so enrichment runs behind a
**poller**, keeping the relay deterministic. One ``run_once()`` scans the oldest
un-enriched generated video ``MediaAsset``s and hands each to
:class:`EnrichGeneratedMedia`, which settles it independently under its own
``media_enrichment:<id>`` lease.

W8.4c.3: the worker discovers work by scanning the **media table** (the parent asset
is the sole source of truth) — it never reads render-job history or reacts to a
``RenderJobSucceeded`` payload. Any producer of a generated video simply creates an
un-enriched asset the worker later finds.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.application.use_cases.media.enrich_generated_media import (
    CURRENT_ENRICHMENT_VERSION,
    EnrichGeneratedMedia,
    EnrichGeneratedMediaResult,
)

_DEFAULT_BATCH = 10


@dataclass(frozen=True, slots=True)
class MediaEnrichmentPollResult:
    """Aggregate outcome of one ``run_once`` scan."""

    scanned: int
    outcomes: list[EnrichGeneratedMediaResult] = field(default_factory=list)


class MediaEnrichmentWorker:
    """Enrich every currently un-enriched generated video once per invocation."""

    def __init__(
        self,
        uow: IUnitOfWork,
        enrich: EnrichGeneratedMedia,
        *,
        batch_size: int = _DEFAULT_BATCH,
    ) -> None:
        self._uow = uow
        self._enrich = enrich
        self._batch_size = batch_size

    async def run_once(self) -> MediaEnrichmentPollResult:
        """Claim + enrich the oldest below-target-version generated videos in the batch."""
        async with self._uow:
            assets = await self._uow.media.list_enrichable_generated_videos(
                target_version=CURRENT_ENRICHMENT_VERSION, limit=self._batch_size
            )

        outcomes: list[EnrichGeneratedMediaResult] = []
        for asset in assets:
            outcomes.append(await self._enrich.execute(asset=asset))
        return MediaEnrichmentPollResult(scanned=len(assets), outcomes=outcomes)
