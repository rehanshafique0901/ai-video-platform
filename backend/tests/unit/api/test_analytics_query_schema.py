"""Unit tests for ``AnalyticsSummaryQuery`` validation (α9.0 — the analytics window).

Both bounds are optional; when present they must be timezone-aware (normalised to UTC) and,
if both are given, ``since`` must be strictly before ``until``. A rejected value raises
``ValidationError`` at the schema boundary (which FastAPI renders as a 422). Pure, DB-free.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.api.v1.schemas.analytics import AnalyticsSummaryQuery

pytestmark = pytest.mark.unit


def test_both_absent_is_valid() -> None:
    q = AnalyticsSummaryQuery()
    assert q.since is None and q.until is None


def test_ordered_tz_aware_window_is_accepted() -> None:
    until = datetime.now(UTC)
    since = until - timedelta(days=30)
    q = AnalyticsSummaryQuery(since=since, until=until)
    assert q.since == since and q.until == until


def test_non_utc_offset_is_normalised_to_utc() -> None:
    tz = timezone(timedelta(hours=5))
    since = datetime.now(tz) - timedelta(days=1)
    q = AnalyticsSummaryQuery(since=since)
    assert q.since is not None
    assert q.since.utcoffset() == timedelta(0)


def test_naive_since_is_rejected() -> None:
    naive = datetime.now(UTC).replace(tzinfo=None)
    with pytest.raises(ValidationError, match="timezone-aware"):
        AnalyticsSummaryQuery(since=naive)


def test_naive_until_is_rejected() -> None:
    naive = datetime.now(UTC).replace(tzinfo=None)
    with pytest.raises(ValidationError, match="timezone-aware"):
        AnalyticsSummaryQuery(until=naive)


def test_inverted_window_is_rejected() -> None:
    until = datetime.now(UTC)
    since = until + timedelta(days=1)  # since after until
    with pytest.raises(ValidationError, match="strictly before"):
        AnalyticsSummaryQuery(since=since, until=until)


def test_equal_bounds_are_rejected() -> None:
    now = datetime.now(UTC)
    with pytest.raises(ValidationError, match="strictly before"):
        AnalyticsSummaryQuery(since=now, until=now)
