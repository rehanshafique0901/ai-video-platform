"""Storyboard: expand a plan into concrete, identity-anchored shot prompts.

Each ``ShotPrompt`` is what the execution runtime feeds to an image-generation
adapter: the shot description with the identity fragment appended, plus the
stable seed. Keeping this pure means the exact prompt sent to any provider is
deterministic and reproducible from the plan alone.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.domain.generation.plan import GenerationPlan


@dataclass(frozen=True, slots=True)
class ShotPrompt:
    index: int
    prompt_text: str
    duration_seconds: float
    seed: int


def _compose(description: str, suffix: str) -> str:
    description = description.strip()
    return f"{description}, {suffix}" if suffix else description


def build_storyboard(plan: GenerationPlan) -> tuple[ShotPrompt, ...]:
    """Turn a plan's shots into identity-anchored prompts (ordered by shot index)."""
    seed = plan.identity.seed
    return tuple(
        ShotPrompt(
            index=shot.index,
            prompt_text=_compose(
                shot.description,
                plan.identity.style_suffix(character_ids=shot.character_ids),
            ),
            duration_seconds=shot.duration_seconds,
            seed=seed,
        )
        for shot in sorted(plan.shots, key=lambda s: s.index)
    )
