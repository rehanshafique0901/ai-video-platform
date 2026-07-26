"""Unit tests for ``PublishStatus`` — mirrors the export lifecycle (α8.6b, DQ8)."""

from __future__ import annotations

import pytest

from app.domain.publishing.publish_status import PublishStatus


@pytest.mark.unit
def test_values_match_the_publish_status_enum() -> None:
    assert {s.value for s in PublishStatus} == {
        "queued",
        "running",
        "succeeded",
        "failed",
        "canceled",
    }


@pytest.mark.unit
def test_terminal_partitioning() -> None:
    assert PublishStatus.SUCCEEDED.is_terminal
    assert PublishStatus.FAILED.is_terminal
    assert PublishStatus.CANCELED.is_terminal
    assert not PublishStatus.QUEUED.is_terminal
    assert not PublishStatus.RUNNING.is_terminal
