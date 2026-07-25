"""Unit tests for the pure timeline verifier (pre-ffmpeg gate)."""

from __future__ import annotations

import pytest

from app.domain.generation.timeline_verification import TimelineFrame, verify_timeline

pytestmark = pytest.mark.unit


def _frames(n: int, *, w: int = 720, h: int = 1280, dur: float = 3.0) -> tuple[TimelineFrame, ...]:
    return tuple(
        TimelineFrame(index=i, duration_seconds=dur, width=w, height=h, content_hash=f"h{i}")
        for i in range(n)
    )


def _names(report) -> dict[str, str]:
    return {c.name: c.status.value for c in report.checks}


def test_valid_timeline_passes_all_checks() -> None:
    report = verify_timeline(_frames(3), expected_count=3, aspect_ratio="9:16")
    assert report.passed
    assert _names(report)["complete"] == "pass"
    assert _names(report)["ordered"] == "pass"
    assert _names(report)["no_duplicate_frames"] == "pass"


def test_missing_frame_fails_complete() -> None:
    report = verify_timeline(_frames(2), expected_count=3, aspect_ratio="9:16")
    assert not report.passed
    assert "complete" in {c.name for c in report.failures}


def test_out_of_order_fails_ordered() -> None:
    frames = (
        TimelineFrame(index=1, duration_seconds=3.0, width=720, height=1280),
        TimelineFrame(index=0, duration_seconds=3.0, width=720, height=1280),
    )
    report = verify_timeline(frames, expected_count=2, aspect_ratio="9:16")
    assert not report.passed
    assert "ordered" in {c.name for c in report.failures}


def test_gap_in_indices_fails_ordered() -> None:
    frames = (
        TimelineFrame(index=0, duration_seconds=3.0, width=720, height=1280),
        TimelineFrame(index=2, duration_seconds=3.0, width=720, height=1280),
    )
    report = verify_timeline(frames, expected_count=2, aspect_ratio="9:16")
    assert "ordered" in {c.name for c in report.failures}


def test_duplicate_content_hash_fails() -> None:
    frames = (
        TimelineFrame(index=0, duration_seconds=3.0, width=720, height=1280, content_hash="same"),
        TimelineFrame(index=1, duration_seconds=3.0, width=720, height=1280, content_hash="same"),
    )
    report = verify_timeline(frames, expected_count=2, aspect_ratio="9:16")
    assert not report.passed
    assert "no_duplicate_frames" in {c.name for c in report.failures}


def test_zero_duration_fails() -> None:
    frames = (
        TimelineFrame(index=0, duration_seconds=0.0, width=720, height=1280),
        TimelineFrame(index=1, duration_seconds=3.0, width=720, height=1280),
    )
    report = verify_timeline(frames, expected_count=2, aspect_ratio="9:16")
    assert "durations" in {c.name for c in report.failures}


def test_aspect_drift_fails() -> None:
    frames = (
        TimelineFrame(index=0, duration_seconds=3.0, width=1920, height=1080),  # 16:9, not 9:16
    )
    report = verify_timeline(frames, expected_count=1, aspect_ratio="9:16")
    assert "aspect_ratio" in {c.name for c in report.failures}


def test_missing_dims_skip_aspect() -> None:
    frames = (TimelineFrame(index=0, duration_seconds=3.0),)
    report = verify_timeline(frames, expected_count=1, aspect_ratio="9:16")
    aspect = next(c for c in report.checks if c.name == "aspect_ratio")
    assert aspect.status.value == "skipped"


def test_empty_timeline_only_reports_complete() -> None:
    report = verify_timeline((), expected_count=3, aspect_ratio="9:16")
    assert not report.passed  # complete fails
    assert {c.name for c in report.checks} == {"complete"}
