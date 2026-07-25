"""Unit tests for the pure verification policy."""

from __future__ import annotations

import pytest

from app.domain.generation.verification import (
    CheckStatus,
    ObservedImage,
    VerificationExpectation,
    parse_aspect_ratio,
    verify_image,
)

pytestmark = pytest.mark.unit


def _expect(**kw: object) -> VerificationExpectation:
    base = {"min_width": 512, "min_height": 512, "aspect_ratio": "1:1"}
    base.update(kw)
    return VerificationExpectation(**base)  # type: ignore[arg-type]


def _status(report: object, name: str) -> CheckStatus:
    return next(c.status for c in report.checks if c.name == name)  # type: ignore[attr-defined]


def test_not_produced_short_circuits_and_fails() -> None:
    report = verify_image(ObservedImage(produced=False), _expect())
    assert not report.passed
    assert [c.name for c in report.checks] == ["produced"]


def test_all_good_passes() -> None:
    observed = ObservedImage(
        produced=True, width=1024, height=1024, is_blank=False, has_watermark=False
    )
    report = verify_image(observed, _expect())
    assert report.passed
    assert _status(report, "min_dimensions") is CheckStatus.PASS
    assert _status(report, "aspect_ratio") is CheckStatus.PASS


def test_blank_image_fails() -> None:
    observed = ObservedImage(produced=True, width=1024, height=1024, is_blank=True)
    report = verify_image(observed, _expect())
    assert not report.passed
    assert _status(report, "not_blank") is CheckStatus.FAIL


def test_too_small_fails_dimensions() -> None:
    observed = ObservedImage(produced=True, width=256, height=256)
    report = verify_image(observed, _expect(min_width=512, min_height=512))
    assert _status(report, "min_dimensions") is CheckStatus.FAIL


def test_wrong_aspect_ratio_fails() -> None:
    observed = ObservedImage(produced=True, width=1920, height=1080)  # 16:9
    report = verify_image(observed, _expect(min_width=100, min_height=100, aspect_ratio="1:1"))
    assert _status(report, "aspect_ratio") is CheckStatus.FAIL


def test_matching_wide_aspect_ratio_passes() -> None:
    observed = ObservedImage(produced=True, width=1920, height=1080)
    report = verify_image(observed, _expect(min_width=100, min_height=100, aspect_ratio="16:9"))
    assert _status(report, "aspect_ratio") is CheckStatus.PASS


def test_consistency_skipped_without_reference() -> None:
    observed = ObservedImage(produced=True, width=1024, height=1024)
    report = verify_image(observed, _expect())
    assert _status(report, "consistency") is CheckStatus.SKIPPED
    assert report.passed  # skipped never blocks


def test_consistency_fails_below_threshold() -> None:
    observed = ObservedImage(produced=True, width=1024, height=1024, similarity_to_reference=0.4)
    report = verify_image(observed, _expect(min_similarity=0.7))
    assert _status(report, "consistency") is CheckStatus.FAIL
    assert not report.passed


def test_consistency_passes_above_threshold() -> None:
    observed = ObservedImage(produced=True, width=1024, height=1024, similarity_to_reference=0.9)
    report = verify_image(observed, _expect(min_similarity=0.7))
    assert _status(report, "consistency") is CheckStatus.PASS


def test_watermark_fails() -> None:
    observed = ObservedImage(produced=True, width=1024, height=1024, has_watermark=True)
    report = verify_image(observed, _expect())
    assert _status(report, "no_watermark") is CheckStatus.FAIL


def test_unmeasured_features_are_skipped_not_passed() -> None:
    observed = ObservedImage(produced=True)  # nothing measured
    report = verify_image(observed, _expect())
    assert _status(report, "not_blank") is CheckStatus.SKIPPED
    assert _status(report, "min_dimensions") is CheckStatus.SKIPPED
    assert _status(report, "no_watermark") is CheckStatus.SKIPPED
    # No FAILs -> passes, but nothing was silently asserted as good.
    assert report.passed


@pytest.mark.parametrize("value,expected", [("16:9", 16 / 9), ("1:1", 1.0), ("9:16", 9 / 16)])
def test_parse_aspect_ratio(value: str, expected: float) -> None:
    assert parse_aspect_ratio(value) == pytest.approx(expected)


@pytest.mark.parametrize("bad", ["", "16", "16:0", "a:b", "0:9"])
def test_parse_aspect_ratio_rejects_malformed(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_aspect_ratio(bad)
