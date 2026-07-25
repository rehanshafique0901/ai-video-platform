"""Unit tests for storyboard expansion (plan -> identity-anchored shot prompts)."""

from __future__ import annotations

import pytest

from app.domain.generation.identity import Character, GlobalStyle, IdentityProfile, SceneStyle
from app.domain.generation.plan import GenerationPlan, Shot
from app.domain.generation.storyboard import build_storyboard

pytestmark = pytest.mark.unit


def _plan() -> GenerationPlan:
    identity = IdentityProfile(
        seed=99,
        global_style=GlobalStyle.PIXAR,
        characters=(Character(id="a", name="Mia", descriptors=("yellow dress",)),),
        scene=SceneStyle(setting="sunny park"),
    )
    return GenerationPlan(
        title="t",
        prompt="p",
        aspect_ratio="9:16",
        target_platform="reel",
        identity=identity,
        shots=(
            Shot(index=1, description="Mia runs", character_ids=("a",), duration_seconds=3.0),
            Shot(index=0, description="Mia waves", character_ids=("a",), duration_seconds=3.0),
        ),
    )


def test_storyboard_is_ordered_by_shot_index() -> None:
    board = build_storyboard(_plan())
    assert [s.index for s in board] == [0, 1]


def test_prompt_text_anchors_identity_and_seed() -> None:
    board = build_storyboard(_plan())
    first = board[0]
    assert first.prompt_text.startswith("Mia waves")
    assert "Mia (yellow dress)" in first.prompt_text
    assert "sunny park" in first.prompt_text
    assert "pixar style" in first.prompt_text
    # Stable seed carried from the identity to every shot for consistency.
    assert all(s.seed == 99 for s in board)


def test_deterministic() -> None:
    assert build_storyboard(_plan()) == build_storyboard(_plan())
