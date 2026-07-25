"""Unit tests for the Planner V2 invariants CS-7 and CS-8 (α8.7, Phase 4).

CS-7 — every adjacent shot must differ in >=1 primary AND >=1 secondary cinematic
dimension (no duplicate-scene storyboard).
CS-8 — planner-authored intent must never contain provider/render language (that
belongs solely to the Prompt Builder).
"""

from __future__ import annotations

import pytest

from app.domain.generation.identity import Character, GlobalStyle, IdentityProfile, Location
from app.domain.generation.planner import PlanRequest, plan_from_prompt
from app.domain.generation.shot_intent import (
    PROVIDER_LEXICON,
    Camera,
    CinematicContinuityError,
    Movement,
    ProviderLanguageError,
    ShotIntent,
    ShotType,
    Transition,
    assert_semantic_only,
    provider_language_in,
    select_arc,
    validate_adjacency,
)

pytestmark = pytest.mark.unit


def _intent(**overrides: object) -> ShotIntent:
    base: dict[str, object] = {
        "shot_type": ShotType.MEDIUM,
        "camera": Camera.TRACK,
        "movement": Movement.WALKING,
        "subject_focus": "fox",
        "emotional_purpose": "exploration",
        "transition_from_previous": Transition.CUT,
    }
    base.update(overrides)
    return ShotIntent(**base)  # type: ignore[arg-type]


def _identity() -> IdentityProfile:
    return IdentityProfile(
        seed=70707,
        global_style=GlobalStyle.PIXAR,
        characters=(Character(id="fox", name="a little red fox"),),
        locations=(Location(id="forest", name="a snowy forest"),),
    )


# --- CS-7 validator ---------------------------------------------------------


def test_validate_adjacency_passes_for_healthy_storyboard() -> None:
    a = _intent(shot_type=ShotType.WIDE, movement=Movement.STILL)
    b = _intent(shot_type=ShotType.CLOSE_UP, movement=Movement.LOOKING)
    validate_adjacency((a, b))  # no raise


def test_validate_adjacency_rejects_duplicate_scene() -> None:
    dup = _intent()
    with pytest.raises(CinematicContinuityError, match="CS-7"):
        validate_adjacency((dup, dup))


def test_validate_adjacency_rejects_transition_only_change() -> None:
    a = _intent(transition_from_previous=None)
    b = _intent(transition_from_previous=Transition.DISSOLVE)
    with pytest.raises(CinematicContinuityError, match="primary"):
        validate_adjacency((a, b))


def test_validate_adjacency_trivial_for_single_shot() -> None:
    validate_adjacency((_intent(),))  # no raise
    validate_adjacency(())  # no raise


@pytest.mark.parametrize("shot_count", list(range(1, 13)))
def test_every_arc_passes_the_validator(shot_count: int) -> None:
    intents = tuple(
        ShotIntent(
            shot_type=b.shot_type,
            camera=b.camera,
            movement=b.movement,
            subject_focus=b.focus.value,
            emotional_purpose=b.emotional_purpose,
            transition_from_previous=b.transition_from_previous,
        )
        for b in select_arc(shot_count).beats
    )
    validate_adjacency(intents)  # no raise for any authored/synthesised arc


# --- CS-8 banned lexicon ----------------------------------------------------


def test_provider_language_detects_banned_terms() -> None:
    assert provider_language_in("a moody CLOSE_UP") == ()
    assert "photorealistic" in provider_language_in("ultra photorealistic scene")
    assert provider_language_in("rendered in SDXL via ComfyUI")  # case-insensitive


def test_assert_semantic_only_passes_for_authored_intent() -> None:
    intents = tuple(
        ShotIntent(
            shot_type=b.shot_type,
            camera=b.camera,
            movement=b.movement,
            emotional_purpose=b.emotional_purpose,
        )
        for b in select_arc(6).beats
    )
    assert_semantic_only(intents)  # authored purposes are semantic


def test_assert_semantic_only_rejects_provider_language() -> None:
    bad = _intent(emotional_purpose="masterpiece, 8k photorealistic")
    with pytest.raises(ProviderLanguageError, match="CS-8"):
        assert_semantic_only((bad,))


def test_no_authored_arc_purpose_uses_provider_language() -> None:
    for shot_count in (3, 5, 6):
        for beat in select_arc(shot_count).beats:
            assert provider_language_in(beat.emotional_purpose) == ()


def test_lexicon_is_non_empty_and_lowercase() -> None:
    assert PROVIDER_LEXICON
    assert all(term == term.lower() for term in PROVIDER_LEXICON)


# --- planner enforces both invariants on its own output ---------------------


def test_planner_output_satisfies_both_invariants() -> None:
    plan = plan_from_prompt(PlanRequest(prompt="A fox in a forest.", identity=_identity()))
    intents = tuple(s.intent for s in plan.shots if s.intent is not None)
    validate_adjacency(intents)  # no raise
    assert_semantic_only(intents)  # no raise
