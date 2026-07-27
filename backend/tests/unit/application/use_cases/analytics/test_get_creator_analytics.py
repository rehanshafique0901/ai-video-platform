"""Unit tests for ``GetCreatorAnalytics`` (Slice α9.0 — in-memory fakes, no DB).

Prove the read-only aggregation: the full ``ANALYTICS_EVENT_NAMES`` vocabulary is always
present (zero-filled), present counts are surfaced, ``total`` is their sum, unknown names are
ignored, and the resolved window is echoed back — owner-scoped through the reused read.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Self
from uuid import UUID, uuid4

import pytest

from app.application.use_cases.analytics.event_schema import ANALYTICS_EVENT_NAMES
from app.application.use_cases.analytics.get_creator_analytics import GetCreatorAnalytics
from app.domain.analytics.analytics_event import AnalyticsEventCount

pytestmark = pytest.mark.unit

_TENANT = uuid4()
_USER = uuid4()


class _FakeAnalytics:
    def __init__(self, rows: list[AnalyticsEventCount]) -> None:
        self._rows = rows
        self.seen: dict[str, object] = {}

    async def summary_for_owner(
        self, *, tenant_id: UUID, user_id: UUID, since: datetime, until: datetime
    ) -> list[AnalyticsEventCount]:
        self.seen = {"tenant_id": tenant_id, "user_id": user_id, "since": since, "until": until}
        return self._rows


class _FakeUoW:
    def __init__(self, rows: list[AnalyticsEventCount]) -> None:
        self.analytics = _FakeAnalytics(rows)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None


async def _run(uow: _FakeUoW, *, since: datetime, until: datetime):
    use_case = GetCreatorAnalytics(uow=uow)  # type: ignore[arg-type]
    return await use_case.execute(tenant_id=_TENANT, owner_user_id=_USER, since=since, until=until)


async def test_zero_fills_full_vocabulary_and_totals() -> None:
    until = datetime.now(UTC)
    since = until - timedelta(days=30)
    rows = [
        AnalyticsEventCount(event_name="publish.succeeded", count=3),
        AnalyticsEventCount(event_name="export.created", count=5),
    ]
    uow = _FakeUoW(rows)
    summary = await _run(uow, since=since, until=until)

    # Every known name is present (zero-filled), plus the reported counts.
    assert set(summary.counts) == set(ANALYTICS_EVENT_NAMES)
    assert summary.counts["publish.succeeded"] == 3
    assert summary.counts["export.created"] == 5
    assert summary.counts["publish.failed"] == 0
    assert summary.total == 8
    # Window echoed + scope forwarded to the repository.
    assert summary.since == since and summary.until == until
    assert uow.analytics.seen == {
        "tenant_id": _TENANT,
        "user_id": _USER,
        "since": since,
        "until": until,
    }


async def test_empty_owner_is_all_zero() -> None:
    until = datetime.now(UTC)
    since = until - timedelta(days=7)
    summary = await _run(_FakeUoW([]), since=since, until=until)
    assert summary.total == 0
    assert all(v == 0 for v in summary.counts.values())
    assert set(summary.counts) == set(ANALYTICS_EVENT_NAMES)


async def test_unknown_event_name_is_ignored() -> None:
    until = datetime.now(UTC)
    since = until - timedelta(days=1)
    rows = [AnalyticsEventCount(event_name="mystery.event", count=9)]
    summary = await _run(_FakeUoW(rows), since=since, until=until)
    assert "mystery.event" not in summary.counts
    assert summary.total == 0
