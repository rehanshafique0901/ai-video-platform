"""Deterministic cinematic planner (Planner V2, α8.7): prompt -> GenerationPlan.

The planner decomposes a prompt into evenly-timed shots, selects a **story arc
template** (data-driven, see ``shot_intent``), and instantiates a ``ShotIntent``
per shot along with a stable semantic ``shot_id`` and a derived per-shot ``seed``.
It is pure and deterministic (same request -> same plan) and contains **no**
provider or execution logic. Per CS-8 it emits *semantic cinematic intent only* —
never provider/render wording; translating intent into generator-facing text is the
Prompt Builder's job.

Richer planners (e.g. a local-LLM adapter that also extracts characters and scene)
can replace this behind the same ``plan_from_prompt`` seam and the same
``ShotIntent`` contract.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.domain.generation.identity import IdentityProfile
from app.domain.generation.plan import GenerationPlan, Shot
from app.domain.generation.shot_intent import (
    FocusHint,
    ShotIntent,
    assert_semantic_only,
    assign_shot_ids,
    derive_shot_seed,
    select_arc,
    validate_adjacency,
)

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


def _resolve_focus(identity: IdentityProfile, focus: FocusHint) -> str | None:
    """Map a template's abstract focus to a concrete identity id (or None)."""
    if focus is FocusHint.SUBJECT:
        return identity.characters[0].id if identity.characters else None
    if focus is FocusHint.ENVIRONMENT:
        return identity.locations[0].id if identity.locations else None
    # DETAIL: prefer a prop, fall back to the subject so the field is never empty
    # when the project has a character.
    if identity.props:
        return identity.props[0].id
    return identity.characters[0].id if identity.characters else None


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

    arc = select_arc(shot_count)
    shot_ids = assign_shot_ids(arc.beats)
    beats = _beats(prompt, shot_count)
    shots = tuple(
        Shot(
            index=i,
            description=beat_text,
            character_ids=character_ids,
            location_id=location_id,
            duration_seconds=per_shot,
            shot_id=shot_ids[i],
            seed=derive_shot_seed(request.identity.seed, shot_ids[i]),
            intent=ShotIntent(
                shot_type=beat.shot_type,
                camera=beat.camera,
                movement=beat.movement,
                subject_focus=_resolve_focus(request.identity, beat.focus),
                emotional_purpose=beat.emotional_purpose,
                transition_from_previous=beat.transition_from_previous,
            ),
        )
        for i, (beat, beat_text) in enumerate(zip(arc.beats, beats, strict=True))
    )

    # Defensive enforcement of the Planner V2 invariants before the plan escapes:
    # CS-7 (no duplicate-scene storyboard) and CS-8 (no provider language in intent).
    # Authored/synthesised arcs always satisfy these; this guarantees a bad future
    # template can never silently reintroduce the α8.6 duplicate-frame defect.
    intents = tuple(shot.intent for shot in shots if shot.intent is not None)
    validate_adjacency(intents)
    assert_semantic_only(intents)

    return GenerationPlan(
        title=request.title or _derive_title(prompt),
        prompt=prompt,
        aspect_ratio=request.aspect_ratio,
        target_platform=request.target_platform,
        identity=request.identity,
        shots=shots,
    )
