"""Storyboard: expand a plan into concrete, identity-anchored shot prompts.

Each ``ShotPrompt`` is what the execution runtime feeds to an image-generation
adapter: the shot description with the identity fragment appended, plus the
stable seed. Keeping this pure means the exact prompt sent to any provider is
deterministic and reproducible from the plan alone.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.domain.generation.plan import GenerationPlan
from app.domain.generation.prompt_builder import build_prompt


@dataclass(frozen=True, slots=True)
class ShotPrompt:
    index: int
    prompt_text: str
    duration_seconds: float
    seed: int
    negative_prompt: str | None = None
    reference_image_refs: tuple[str, ...] = ()


def build_storyboard(plan: GenerationPlan) -> tuple[ShotPrompt, ...]:
    """Turn a plan's shots into identity-anchored prompts (ordered by shot index).

    Each shot's prompt is composed through the Prompt Builder so it is anchored to
    the project's world state (Identity Runtime -> Prompt Builder -> Generator).
    The negative prompt and candidate reference images are carried from the
    Reference Asset Store so a reference-capable provider can consume them.
    """
    identity = plan.identity
    return tuple(
        ShotPrompt(
            index=shot.index,
            prompt_text=build_prompt(
                identity,
                description=shot.description,
                character_ids=shot.character_ids,
                location_id=shot.location_id,
            ),
            duration_seconds=shot.duration_seconds,
            seed=identity.seed,
            negative_prompt=identity.negative_prompt,
            reference_image_refs=identity.reference_refs_for(shot.character_ids),
        )
        for shot in sorted(plan.shots, key=lambda s: s.index)
    )
