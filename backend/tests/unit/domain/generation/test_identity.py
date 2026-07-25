"""Unit tests for the Identity Runtime (pure value objects)."""

from __future__ import annotations

import pytest

from app.domain.generation.identity import (
    Character,
    GlobalStyle,
    IdentityProfile,
    ObjectAsset,
    SceneStyle,
)

pytestmark = pytest.mark.unit


def _profile() -> IdentityProfile:
    return IdentityProfile(
        seed=42,
        global_style=GlobalStyle.PIXAR,
        characters=(
            Character(id="a", name="Mia", descriptors=("age 6", "curly red hair", "yellow dress")),
            Character(id="b", name="Rex", descriptors=("green dinosaur",)),
        ),
        scene=SceneStyle(setting="sunny park", lighting="golden hour", camera="wide shot"),
        objects=(ObjectAsset(id="o1", name="red balloon"),),
    )


def test_character_prompt_fragment_includes_descriptors() -> None:
    c = Character(id="a", name="Mia", descriptors=("age 6", "yellow dress"))
    assert c.prompt_fragment() == "Mia (age 6, yellow dress)"


def test_character_prompt_fragment_without_descriptors() -> None:
    assert Character(id="a", name="Mia").prompt_fragment() == "Mia"


def test_scene_fragment_skips_empty_fields() -> None:
    assert SceneStyle(setting="park", camera="wide").prompt_fragment() == "park, wide"


def test_style_suffix_filters_to_named_characters() -> None:
    profile = _profile()
    suffix = profile.style_suffix(character_ids=("a",))
    assert "Mia" in suffix
    assert "Rex" not in suffix  # not named in this shot
    # Scene, object, and global style always included.
    assert "sunny park" in suffix
    assert "red balloon" in suffix
    assert "pixar style" in suffix


def test_style_suffix_is_deterministic() -> None:
    profile = _profile()
    a = profile.style_suffix(character_ids=("a", "b"))
    b = profile.style_suffix(character_ids=("a", "b"))
    assert a == b
    assert a.endswith("pixar style")


def test_style_suffix_ignores_unknown_character_ids() -> None:
    profile = _profile()
    assert profile.character("missing") is None
    suffix = profile.style_suffix(character_ids=("missing",))
    # Unknown ids contribute nothing; scene/object/style still present.
    assert "sunny park" in suffix and "pixar style" in suffix


def test_character_lookup() -> None:
    profile = _profile()
    assert profile.character("b").name == "Rex"  # type: ignore[union-attr]
