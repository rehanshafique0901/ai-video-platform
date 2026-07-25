"""Generation plan value objects — the planner's structured output.

A ``GenerationPlan`` is the design-time description of a short video: its
identity, the ordered shots, and their timing. It contains no provider or
execution detail (ADR-0045: the planner requests capabilities, it never chooses
providers or knows how a shot is rendered).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.domain.generation.identity import IdentityProfile


@dataclass(frozen=True, slots=True)
class Shot:
    """One beat of the video: what to show, who is in it, for how long."""

    index: int
    description: str
    character_ids: tuple[str, ...] = ()
    duration_seconds: float = 3.0


@dataclass(frozen=True, slots=True)
class GenerationPlan:
    """The whole plan for a single short video."""

    title: str
    prompt: str
    aspect_ratio: str  # "16:9" | "9:16" | "1:1"
    target_platform: str  # "reel" | "shorts" | "tiktok" | "youtube"
    identity: IdentityProfile
    shots: tuple[Shot, ...]

    @property
    def total_duration_seconds(self) -> float:
        return sum(shot.duration_seconds for shot in self.shots)

    @property
    def shot_count(self) -> int:
        return len(self.shots)
