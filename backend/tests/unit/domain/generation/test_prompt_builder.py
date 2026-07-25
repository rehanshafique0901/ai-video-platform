"""Unit tests for the Prompt Builder (Identity Runtime -> prompt text)."""

from __future__ import annotations

import pytest

from app.domain.generation.identity import (
    Character,
    GlobalStyle,
    IdentityProfile,
    Location,
    Prop,
)
from app.domain.generation.prompt_builder import build_prompt

pytestmark = pytest.mark.unit


def _identity() -> IdentityProfile:
    return IdentityProfile(
        seed=7,
        global_style=GlobalStyle.PIXAR,
        characters=(
            Character(id="a", name="Mia", clothing="yellow dress"),
            Character(id="b", name="Rex", appearance=("green dinosaur",)),
        ),
        locations=(Location(id="park", name="sunny park"),),
        props=(Prop(id="o1", name="red balloon"),),
        camera_style="wide shot",
        lighting="golden hour",
        color_palette="warm pastel palette",
    )


def test_prompt_starts_with_description_and_ends_with_global_style() -> None:
    text = build_prompt(_identity(), description="Mia waves", character_ids=("a",))
    assert text.startswith("Mia waves")
    assert text.endswith("pixar style")


def test_prompt_only_includes_named_characters() -> None:
    text = build_prompt(_identity(), description="a shot", character_ids=("a",))
    assert "Mia (wearing yellow dress)" in text
    assert "Rex" not in text


def test_prompt_includes_location_props_and_look() -> None:
    text = build_prompt(
        _identity(),
        description="a shot",
        character_ids=("a",),
        location_id="park",
    )
    assert "sunny park" in text
    assert "red balloon" in text  # recurring prop always present
    assert "wide shot" in text
    assert "golden hour" in text
    assert "warm pastel palette" in text


def test_unknown_ids_are_ignored() -> None:
    text = build_prompt(
        _identity(),
        description="a shot",
        character_ids=("missing",),
        location_id="nowhere",
    )
    assert text.startswith("a shot")
    assert "red balloon" in text  # props/look still applied


def test_modifiers_are_appended() -> None:
    text = build_prompt(
        _identity(),
        description="a shot",
        character_ids=("a",),
        modifiers=("low angle", "smiling"),
    )
    assert "low angle" in text
    assert "smiling" in text


def test_deterministic() -> None:
    a = build_prompt(_identity(), description="x", character_ids=("a", "b"), location_id="park")
    b = build_prompt(_identity(), description="x", character_ids=("a", "b"), location_id="park")
    assert a == b
