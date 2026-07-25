"""Minimal deterministic planner: prompt -> GenerationPlan.

This is the smallest planner that exercises the architecture: it decomposes a
prompt into evenly-timed shots and attaches the project identity. It is pure and
deterministic (same request -> same plan) and contains **no** provider or
execution logic — richer planners (e.g. a local-LLM adapter that also extracts
characters and scene) can replace it behind the same ``plan_from_prompt`` seam.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.domain.generation.identity import IdentityProfile
from app.domain.generation.plan import GenerationPlan, Shot

# Short-form video envelope; the planner keeps total duration inside this band.
MIN_DURATION_SECONDS = 15.0
MAX_DURATION_SECONDS = 30.0

_SENTENCE_SPLIT = re.compile(r"[.!?;\n]+")
_CLAUSE_SPLIT = re.compile(r"[,]+")


class PlanningError(ValueError):
    """Raised when a prompt cannot be turned into a plan (e.g. empty prompt)."""


@dataclass(frozen=True, slots=True)
class PlanRequest:
    prompt: str
    identity: IdentityProfile
    aspect_ratio: str = "9:16"
    target_platform: str = "reel"
    target_duration_seconds: float = 18.0
    per_shot_seconds: float = 3.0
    title: str | None = None


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _derive_title(prompt: str) -> str:
    words = prompt.split()
    title = " ".join(words[:6]).strip(" .,!?;:")
    return title[:80] if title else "Untitled"


def _beats(prompt: str, count: int) -> tuple[str, ...]:
    """Split ``prompt`` into exactly ``count`` non-empty shot descriptions.

    Sentences first, then clauses, as the source of natural beats. When there are
    more beats than shots they are grouped evenly; when there are fewer they are
    cycled so every shot has a description. Deterministic for a given input.
    """
    parts = [p.strip() for p in _SENTENCE_SPLIT.split(prompt) if p.strip()]
    if len(parts) < count:
        clauses = [c.strip() for c in _CLAUSE_SPLIT.split(prompt) if c.strip()]
        if len(clauses) > len(parts):
            parts = clauses
    if not parts:
        parts = [prompt.strip()]

    if len(parts) >= count:
        # Group contiguous parts into ``count`` roughly-equal buckets.
        per_bucket = len(parts) / count
        beats: list[str] = []
        for i in range(count):
            start = round(i * per_bucket)
            end = round((i + 1) * per_bucket)
            bucket = parts[start:end] or [parts[min(i, len(parts) - 1)]]
            beats.append(" ".join(bucket))
        return tuple(beats)
    # Fewer parts than shots: cycle deterministically.
    return tuple(parts[i % len(parts)] for i in range(count))


def plan_from_prompt(request: PlanRequest) -> GenerationPlan:
    prompt = request.prompt.strip()
    if not prompt:
        raise PlanningError("prompt must be non-empty")
    if request.per_shot_seconds <= 0:
        raise PlanningError("per_shot_seconds must be positive")

    target = _clamp(request.target_duration_seconds, MIN_DURATION_SECONDS, MAX_DURATION_SECONDS)
    shot_count = max(1, round(target / request.per_shot_seconds))
    # Distribute the (clamped) target evenly so total_duration stays in-band.
    per_shot = round(target / shot_count, 2)

    character_ids = tuple(c.id for c in request.identity.characters)
    # Anchor every shot to the project's primary location when one is defined so
    # the setting stays consistent; richer planners can vary this per shot.
    location_id = request.identity.locations[0].id if request.identity.locations else None
    beats = _beats(prompt, shot_count)
    shots = tuple(
        Shot(
            index=i,
            description=beat,
            character_ids=character_ids,
            location_id=location_id,
            duration_seconds=per_shot,
        )
        for i, beat in enumerate(beats)
    )
    return GenerationPlan(
        title=request.title or _derive_title(prompt),
        prompt=prompt,
        aspect_ratio=request.aspect_ratio,
        target_platform=request.target_platform,
        identity=request.identity,
        shots=shots,
    )
