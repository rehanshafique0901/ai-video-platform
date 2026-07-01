"""Unit tests for ``SystemClock`` (Slice α2b)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.infrastructure.clock import SystemClock


@pytest.mark.unit
def test_now_returns_tz_aware_utc_within_a_second_of_wall_clock() -> None:
    clock = SystemClock()
    before = datetime.now(UTC)
    now = clock.now()
    after = datetime.now(UTC)

    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)
    assert before <= now <= after
