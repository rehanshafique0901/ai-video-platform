"""Timeline verification — a cheap gate *before* ffmpeg assembly.

Once every shot is individually verified, the ordered set still has to form a
valid timeline. Detecting problems here (missing/duplicated/out-of-order frames,
zero-length durations, aspect-ratio drift) is far cheaper than discovering them
after an ffmpeg render, so this policy runs between per-shot verification and
assembly. Pure and deterministic; it inspects observed features only, never bytes.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import pairwise

from app.domain.generation.verification import CheckResult, CheckStatus, parse_aspect_ratio


@dataclass(frozen=True, slots=True)
class TimelineFrame:
    """One assembled frame's observed properties, in intended playback order."""

    index: int
    duration_seconds: float
    width: int | None = None
    height: int | None = None
    content_hash: str | None = None


@dataclass(frozen=True, slots=True)
class TimelineReport:
    checks: tuple[CheckResult, ...]

    @property
    def passed(self) -> bool:
        return all(c.status is not CheckStatus.FAIL for c in self.checks)

    @property
    def failures(self) -> tuple[CheckResult, ...]:
        return tuple(c for c in self.checks if c.status is CheckStatus.FAIL)


def _result(name: str, ok: bool, detail_fail: str) -> CheckResult:
    return CheckResult(
        name, CheckStatus.PASS if ok else CheckStatus.FAIL, "" if ok else detail_fail
    )


def verify_timeline(
    frames: tuple[TimelineFrame, ...],
    *,
    expected_count: int,
    aspect_ratio: str,
    aspect_ratio_tolerance: float = 0.08,
    min_duration_seconds: float = 0.01,
) -> TimelineReport:
    """Validate an ordered timeline before rendering. Pure and deterministic."""
    checks: list[CheckResult] = []

    # complete — no missing frames.
    checks.append(
        _result(
            "complete",
            len(frames) == expected_count,
            f"{len(frames)} frames != expected {expected_count}",
        )
    )

    if not frames:
        return TimelineReport(tuple(checks))

    # ordered — indices strictly increasing, contiguous from the first index.
    indices = [f.index for f in frames]
    ordered = indices == sorted(indices) and all(b - a == 1 for a, b in pairwise(indices))
    checks.append(_result("ordered", ordered, f"non-contiguous/out-of-order indices {indices}"))

    # no_duplicate_frames — identical content hashes mean a frame was repeated.
    hashes = [f.content_hash for f in frames if f.content_hash is not None]
    if hashes:
        dupes = len(hashes) != len(set(hashes))
        checks.append(_result("no_duplicate_frames", not dupes, "duplicate frame content detected"))
    else:
        checks.append(CheckResult("no_duplicate_frames", CheckStatus.SKIPPED, "no hashes"))

    # durations — every frame must have a positive, non-trivial duration.
    bad_durations = [f.index for f in frames if f.duration_seconds < min_duration_seconds]
    checks.append(
        _result("durations", not bad_durations, f"zero/negative duration at shots {bad_durations}")
    )

    # aspect_ratio — every measured frame must match the target within tolerance.
    want = parse_aspect_ratio(aspect_ratio)
    measured = [f for f in frames if f.width and f.height]
    if not measured:
        checks.append(CheckResult("aspect_ratio", CheckStatus.SKIPPED, "not measured"))
    else:
        offenders = [
            f.index
            for f in measured
            if abs((f.width / f.height) - want) > aspect_ratio_tolerance * want  # type: ignore[operator]
        ]
        checks.append(_result("aspect_ratio", not offenders, f"aspect drift at shots {offenders}"))

    return TimelineReport(tuple(checks))
