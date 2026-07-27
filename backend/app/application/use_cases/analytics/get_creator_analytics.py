"""``GetCreatorAnalytics`` use case (Slice α9.0 — Creator Analytics Foundation).

Contract:

    GET /api/v1/analytics/summary?since=&until=
      → 200  { data: CreatorAnalyticsSummary, meta }
      → 401  { error: { code: UNAUTHENTICATED, ... } }  (via CurrentUserDep)
      → 422  invalid window (naive datetime / since >= until)

A **read-only**, owner-scoped aggregate over ``analytics_events``: per-``event_name`` counts
for the caller across a half-open ``[since, until)`` window. It reuses the single
:class:`IAnalyticsRepository.summary_for_owner` read inside one Unit of Work and zero-fills
the full :data:`ANALYTICS_EVENT_NAMES` vocabulary, so the response shape is stable regardless
of what the caller has done (a fresh caller sees all-zero). All scope comes from the
authenticated caller; the read is scoped by ``(tenant_id, user_id)``.

The window is resolved by the caller (the API layer defaults to a trailing 30 days) and
passed in as concrete bounds, so this use case is clock-free and deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.application.use_cases.analytics.event_schema import ANALYTICS_EVENT_NAMES


@dataclass(frozen=True, slots=True)
class CreatorAnalyticsSummary:
    """The read-only creator analytics summary over ``[since, until)``.

    ``counts`` always carries every name in :data:`ANALYTICS_EVENT_NAMES` (zero-filled);
    ``total`` is their sum.
    """

    since: datetime
    until: datetime
    counts: dict[str, int]
    total: int


class GetCreatorAnalytics:
    """Assemble the caller's owner-scoped analytics summary from the aggregate read."""

    def __init__(self, uow: IUnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self,
        *,
        tenant_id: UUID,
        owner_user_id: UUID,
        since: datetime,
        until: datetime,
    ) -> CreatorAnalyticsSummary:
        async with self._uow:
            rows = await self._uow.analytics.summary_for_owner(
                tenant_id=tenant_id,
                user_id=owner_user_id,
                since=since,
                until=until,
            )

        counts = {name: 0 for name in ANALYTICS_EVENT_NAMES}
        for row in rows:
            # Ignore any name outside the known vocabulary (defensive; the producer only
            # ever writes names from ANALYTICS_EVENT_NAMES).
            if row.event_name in counts:
                counts[row.event_name] = row.count
        return CreatorAnalyticsSummary(
            since=since,
            until=until,
            counts=counts,
            total=sum(counts.values()),
        )


__all__ = ["GetCreatorAnalytics", "CreatorAnalyticsSummary"]
