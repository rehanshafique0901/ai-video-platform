"""DTOs for ``/api/v1/analytics/*`` (α9.0 — Creator Analytics Foundation).

The wire contract for the read-only creator analytics summary. :class:`AnalyticsSummaryQuery`
validates the optional ``[since, until)`` window (timezone-aware, ordered); the API layer
resolves absent bounds to a trailing 30 days before calling the use case.
:class:`AnalyticsSummaryPublic` projects per-``event_name`` counts + total — scalar numbers
only, no credential/URL/byte material, all owner-scoped to the authenticated caller.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, field_validator, model_validator

from app.application.use_cases.analytics.get_creator_analytics import CreatorAnalyticsSummary


class AnalyticsSummaryQuery(BaseModel):
    """``GET /analytics/summary`` query params — the optional analytics window.

    Both bounds are optional; the router defaults ``until`` to now and ``since`` to 30 days
    before ``until``. When provided they must be timezone-aware (normalised to UTC) and, if
    both are given, ``since`` must be strictly before ``until`` — else a 422.
    """

    since: datetime | None = None
    until: datetime | None = None

    @field_validator("since", "until")
    @classmethod
    def _require_aware(cls, value: datetime | None) -> datetime | None:
        """Timezone-aware only; normalised to UTC (a naive datetime is a 422)."""
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("must be timezone-aware (include a UTC offset)")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def _ordered(self) -> AnalyticsSummaryQuery:
        if self.since is not None and self.until is not None and self.since >= self.until:
            raise ValueError("since must be strictly before until")
        return self


class AnalyticsWindowPublic(BaseModel):
    """The resolved ``[since, until)`` window echoed back to the caller."""

    since: datetime
    until: datetime


class AnalyticsSummaryPublic(BaseModel):
    """Public projection of :class:`CreatorAnalyticsSummary` — counts + total over a window."""

    window: AnalyticsWindowPublic
    counts: dict[str, int]
    total: int

    @classmethod
    def from_domain(cls, summary: CreatorAnalyticsSummary) -> AnalyticsSummaryPublic:
        return cls(
            window=AnalyticsWindowPublic(since=summary.since, until=summary.until),
            counts=dict(summary.counts),
            total=summary.total,
        )


__all__ = [
    "AnalyticsSummaryQuery",
    "AnalyticsWindowPublic",
    "AnalyticsSummaryPublic",
]
