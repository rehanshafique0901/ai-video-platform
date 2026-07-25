"""Unit tests for execution-mode selection policy."""

from __future__ import annotations

import pytest

from app.domain.generation.execution import (
    ExecutionMode,
    ExecutionTier,
    constraints_for,
)

pytestmark = pytest.mark.unit


def test_auto_allows_full_cascade_and_stops_at_first() -> None:
    c = constraints_for(ExecutionMode.AUTO)
    assert c.preference == (
        ExecutionTier.LOCAL,
        ExecutionTier.FREE_REMOTE,
        ExecutionTier.COMMERCIAL,
    )
    assert c.stop_at_first_available is True
    assert all(c.allows(t) for t in ExecutionTier)


def test_hybrid_allows_full_cascade_without_stopping() -> None:
    c = constraints_for(ExecutionMode.HYBRID)
    assert c.stop_at_first_available is False
    assert all(c.allows(t) for t in ExecutionTier)


@pytest.mark.parametrize(
    ("mode", "tier"),
    [
        (ExecutionMode.LOCAL_ONLY, ExecutionTier.LOCAL),
        (ExecutionMode.FREE_REMOTE_ONLY, ExecutionTier.FREE_REMOTE),
        (ExecutionMode.COMMERCIAL_ONLY, ExecutionTier.COMMERCIAL),
    ],
)
def test_single_tier_modes_are_exclusive(mode: ExecutionMode, tier: ExecutionTier) -> None:
    c = constraints_for(mode)
    assert c.allowed == (tier,)
    assert c.allows(tier)
    for other in ExecutionTier:
        if other is not tier:
            assert not c.allows(other)
