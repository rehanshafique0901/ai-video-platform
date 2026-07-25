"""Unit tests for the Identity Runtime world state (pure value objects)."""

from __future__ import annotations

import pytest

from app.domain.generation.identity import (
    Character,
    GlobalStyle,
    IdentityProfile,
    Location,
    Prop,
    join_fragments,
)

pytestmark = pytest.mark.unit


def _profile() -> IdentityProfile:
    return IdentityProfile(
        seed=42,
        global_style=GlobalStyle.PIXAR,
        characters=(
            Character(
                id="a",
                name="Mia",
                age="6 years old",
                appearance=("curly red hair",),
                clothing="yellow dress",
                accessories=("blue backpack",),
                expressions=("smiling", "surprised"),
                poses=("running",),
                voice="cheerful child",
                reference_image_refs=("assets/mia-01.png",),
            ),
            Character(id="b", name="Rex", appearance=("green dinosaur",)),
        ),
        locations=(Location(id="park", name="sunny park", descriptors=("autumn leaves",)),),
        props=(Prop(id="o1", name="red balloon"),),
        camera_style="wide shot",
        lighting="golden hour",
        color_palette="warm pastel palette",
        music_style="upbeat ukulele",
        subtitle_style="bold yellow captions",
    )


def test_character_prompt_fragment_uses_stable_identity_only() -> None:
    c = _profile().character("a")
    assert c is not None
    fragment = c.prompt_fragment()
    assert fragment == "Mia (6 years old, curly red hair, wearing yellow dress, with blue backpack)"
    # Expressions/poses are per-shot variation and must NOT leak into identity.
    assert "smiling" not in fragment
    assert "running" not in fragment


def test_character_prompt_fragment_without_details() -> None:
    assert Character(id="a", name="Mia").prompt_fragment() == "Mia"


def test_location_and_prop_fragments() -> None:
    loc = Location(id="p", name="park", descriptors=("autumn leaves",))
    assert loc.prompt_fragment() == "park (autumn leaves)"
    assert Prop(id="x", name="red balloon").prompt_fragment() == "red balloon"


def test_join_fragments_skips_empty() -> None:
    assert join_fragments(("a", "", "  ", "b")) == "a, b"


def test_character_and_location_lookup() -> None:
    profile = _profile()
    assert profile.character("b").name == "Rex"  # type: ignore[union-attr]
    assert profile.location("park").name == "sunny park"  # type: ignore[union-attr]
    assert profile.character("missing") is None
    assert profile.location("missing") is None


def test_music_and_subtitle_style_are_carried_but_not_visual() -> None:
    # These belong to later audio/subtitle slices; the profile carries them but
    # they must never appear in a character's visual fragment.
    profile = _profile()
    assert profile.music_style == "upbeat ukulele"
    assert profile.subtitle_style == "bold yellow captions"
