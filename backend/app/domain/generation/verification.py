"""Verification policy — generation must never trust itself.

The pipeline is always Generate -> Verify -> (Repair -> Verify)* -> Export, never
Generate -> Export. This module is the *pure policy*: it decides which checks run
and whether a shot passes, given ``ObservedImage`` features that an
infrastructure extractor produces from the raw bytes. The policy never touches
bytes — that keeps it deterministic and unit-testable, and lets richer extractors
(CLIP / face embeddings) plug in later without changing the decision logic.

Checks implemented now are the cheap, model-free ones (produced, not blank,
minimum dimensions, aspect ratio, cross-frame consistency, watermark). Heavier
identity checks (face drift, clothing/object changed, lip-sync) attach as
additional ``ObservedImage`` fields + checks in later slices; unknown/absent
features are reported as ``SKIPPED`` rather than silently passing.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class CheckStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    status: CheckStatus
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ObservedImage:
    """Features extracted from a generated image by an infrastructure extractor.

    ``None`` means "not measured" -> the corresponding check is SKIPPED (never a
    silent pass). ``similarity_to_reference`` is a 0..1 perceptual similarity to
    the previously accepted frame (identity consistency); ``None`` for the first
    shot (no reference yet).
    """

    produced: bool
    width: int | None = None
    height: int | None = None
    is_blank: bool | None = None
    similarity_to_reference: float | None = None
    has_watermark: bool | None = None


@dataclass(frozen=True, slots=True)
class VerificationExpectation:
    min_width: int
    min_height: int
    aspect_ratio: str  # "16:9" | "9:16" | "1:1"
    min_similarity: float | None = None
    aspect_ratio_tolerance: float = 0.08


@dataclass(frozen=True, slots=True)
class VerificationReport:
    checks: tuple[CheckResult, ...]

    @property
    def passed(self) -> bool:
        """A report passes iff no check FAILED (SKIPPED checks do not block)."""
        return all(c.status is not CheckStatus.FAIL for c in self.checks)

    @property
    def failures(self) -> tuple[CheckResult, ...]:
        return tuple(c for c in self.checks if c.status is CheckStatus.FAIL)


def parse_aspect_ratio(value: str) -> float:
    """Return width/height for a ``"W:H"`` string. Raises ValueError if malformed."""
    try:
        w_str, h_str = value.split(":")
        w, h = float(w_str), float(h_str)
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"invalid aspect ratio {value!r}") from exc
    if w <= 0 or h <= 0:
        raise ValueError(f"invalid aspect ratio {value!r}")
    return w / h


def _check(name: str, ok: bool | None, detail_fail: str, detail_ok: str = "") -> CheckResult:
    if ok is None:
        return CheckResult(name, CheckStatus.SKIPPED, "not measured")
    return CheckResult(
        name, CheckStatus.PASS if ok else CheckStatus.FAIL, detail_ok if ok else detail_fail
    )


def verify_image(
    observed: ObservedImage, expectation: VerificationExpectation
) -> VerificationReport:
    """Evaluate one generated shot against expectations. Pure and deterministic."""
    checks: list[CheckResult] = []

    # produced — hard gate; if nothing was produced the rest is moot.
    checks.append(_check("produced", observed.produced, "generator returned no image"))
    if not observed.produced:
        return VerificationReport(tuple(checks))

    # not_blank
    not_blank = None if observed.is_blank is None else (not observed.is_blank)
    checks.append(_check("not_blank", not_blank, "image is blank / near-uniform"))

    # min_dimensions
    if observed.width is None or observed.height is None:
        checks.append(CheckResult("min_dimensions", CheckStatus.SKIPPED, "not measured"))
    else:
        big_enough = (
            observed.width >= expectation.min_width and observed.height >= expectation.min_height
        )
        checks.append(
            _check(
                "min_dimensions",
                big_enough,
                f"{observed.width}x{observed.height} < "
                f"{expectation.min_width}x{expectation.min_height}",
            )
        )

    # aspect_ratio
    if observed.width is None or observed.height is None or observed.height == 0:
        checks.append(CheckResult("aspect_ratio", CheckStatus.SKIPPED, "not measured"))
    else:
        want = parse_aspect_ratio(expectation.aspect_ratio)
        got = observed.width / observed.height
        within = abs(got - want) <= expectation.aspect_ratio_tolerance * want
        checks.append(_check("aspect_ratio", within, f"ratio {got:.3f} != {want:.3f} (±tol)"))

    # consistency — only when a reference exists and a threshold is set.
    if observed.similarity_to_reference is None or expectation.min_similarity is None:
        checks.append(CheckResult("consistency", CheckStatus.SKIPPED, "no reference frame"))
    else:
        consistent = observed.similarity_to_reference >= expectation.min_similarity
        checks.append(
            _check(
                "consistency",
                consistent,
                f"similarity {observed.similarity_to_reference:.3f} < "
                f"{expectation.min_similarity:.3f}",
            )
        )

    # no_watermark
    no_watermark = None if observed.has_watermark is None else (not observed.has_watermark)
    checks.append(_check("no_watermark", no_watermark, "watermark detected"))

    return VerificationReport(tuple(checks))
