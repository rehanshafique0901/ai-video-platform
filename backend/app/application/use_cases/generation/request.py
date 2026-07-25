"""Input request for the ``GenerateVideo`` use case."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from app.domain.generation.execution import ExecutionMode
from app.domain.generation.identity import IdentityProfile
from app.domain.generation.repair import DEFAULT_MAX_ATTEMPTS


@dataclass(frozen=True, slots=True)
class GenerateVideoRequest:
    """A single short-video generation request.

    ``identity`` is the project world state every shot references. ``execution_mode``
    selects which adapter tiers may run (capability-first; never a named provider).
    ``min_similarity`` is the cross-shot consistency threshold applied from the
    second accepted frame onward.
    """

    prompt: str
    identity: IdentityProfile
    generation_id: UUID | None = None
    execution_mode: ExecutionMode = ExecutionMode.AUTO
    aspect_ratio: str = "9:16"
    target_platform: str = "reel"
    target_duration_seconds: float = 18.0
    per_shot_seconds: float = 3.0
    title: str | None = None
    width: int = 720
    height: int = 1280
    fps: int = 30
    min_width: int = 512
    min_height: int = 512
    min_similarity: float | None = 0.6
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    budget: float | None = None
