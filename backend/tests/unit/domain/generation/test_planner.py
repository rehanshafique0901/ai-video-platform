"""Unit tests for the deterministic planner."""

from __future__ import annotations

import pytest

from app.domain.generation.identity import Character, GlobalStyle, IdentityProfile
from app.domain.generation.planner import (
    MAX_DURATION_SECONDS,
    MIN_DURATION_SECONDS,
    PlanningError,
    PlanRequest,
    plan_from_prompt,
)

pytestmark = pytest.mark.unit


def _identity() -> IdentityProfile:
    return IdentityProfile(
        seed=7,
        global_style=GlobalStyle.ANIME,
        characters=(Character(id="a", name="Mia"),),
    )


def test_shot_count_derived_from_duration_and_per_shot() -> None:
    plan = plan_from_prompt(
        PlanRequest(
            prompt="A. B. C. D. E. F.",
            identity=_identity(),
            target_duration_seconds=18.0,
            per_shot_seconds=3.0,
        )
    )
    assert plan.shot_count == 6
    assert plan.total_duration_seconds == pytest.approx(18.0)


def test_duration_is_clamped_to_short_form_band() -> None:
    long_plan = plan_from_prompt(
        PlanRequest(prompt="hello world", identity=_identity(), target_duration_seconds=120.0)
    )
    assert long_plan.total_duration_seconds <= MAX_DURATION_SECONDS + 0.01

    short_plan = plan_from_prompt(
        PlanRequest(prompt="hello world", identity=_identity(), target_duration_seconds=2.0)
    )
    assert short_plan.total_duration_seconds >= MIN_DURATION_SECONDS - 0.01


def test_more_sentences_than_shots_are_grouped() -> None:
    prompt = "One. Two. Three. Four. Five. Six. Seven. Eight. Nine."
    plan = plan_from_prompt(
        PlanRequest(
            prompt=prompt, identity=_identity(), target_duration_seconds=15.0, per_shot_seconds=5.0
        )
    )
    assert plan.shot_count == 3
    # Every shot gets a non-empty description; all source beats are represented.
    assert all(shot.description for shot in plan.shots)
    assert "One" in plan.shots[0].description


def test_fewer_sentences_than_shots_are_cycled() -> None:
    plan = plan_from_prompt(
        PlanRequest(
            prompt="Just one idea",
            identity=_identity(),
            target_duration_seconds=18.0,
            per_shot_seconds=3.0,
        )
    )
    assert plan.shot_count == 6
    assert all(shot.description == "Just one idea" for shot in plan.shots)


def test_character_ids_attached_to_every_shot() -> None:
    plan = plan_from_prompt(PlanRequest(prompt="a story", identity=_identity()))
    assert all(shot.character_ids == ("a",) for shot in plan.shots)


def test_title_derived_when_absent() -> None:
    plan = plan_from_prompt(
        PlanRequest(
            prompt="A funny shark learns to swim in the deep blue sea", identity=_identity()
        )
    )
    assert plan.title == "A funny shark learns to swim"


def test_explicit_title_wins() -> None:
    plan = plan_from_prompt(PlanRequest(prompt="whatever", identity=_identity(), title="My Reel"))
    assert plan.title == "My Reel"


def test_deterministic() -> None:
    req = PlanRequest(prompt="A. B. C.", identity=_identity())
    assert plan_from_prompt(req) == plan_from_prompt(req)


def test_empty_prompt_raises() -> None:
    with pytest.raises(PlanningError):
        plan_from_prompt(PlanRequest(prompt="   ", identity=_identity()))


def test_non_positive_per_shot_raises() -> None:
    with pytest.raises(PlanningError):
        plan_from_prompt(PlanRequest(prompt="hi", identity=_identity(), per_shot_seconds=0))
