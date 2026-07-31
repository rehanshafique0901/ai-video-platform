"""Unit tests for the authored Identity Runtime aggregate (α10.0 step 1)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from app.domain.generation.identity import GlobalStyle
from app.domain.identity_runtime import (
    MAX_CHARACTERS,
    MAX_LOCATIONS,
    MAX_PROPS,
    SEED_BOUND,
    Character,
    IdentityProfile,
    IdentityValidationError,
    Location,
    Prop,
    ensure_unique_keys,
    ensure_within_caps,
    in_canonical_order,
)

pytestmark = pytest.mark.unit

PROFILE_ID = UUID("00000000-0000-0000-0000-0000000000aa")
NOW = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)


def _character(key: str, *, position: int = 0, profile_id: UUID = PROFILE_ID) -> Character:
    return Character(
        id=uuid4(),
        profile_id=profile_id,
        character_key=key,
        name=key.title(),
        position=position,
    )


def _location(key: str, *, position: int = 0) -> Location:
    return Location(
        id=uuid4(), profile_id=PROFILE_ID, location_key=key, name=key.title(), position=position
    )


def _prop(key: str, *, position: int = 0) -> Prop:
    return Prop(
        id=uuid4(), profile_id=PROFILE_ID, prop_key=key, name=key.title(), position=position
    )


def _profile(**overrides: object) -> IdentityProfile:
    fields: dict[str, object] = {
        "id": PROFILE_ID,
        "tenant_id": uuid4(),
        "owner_user_id": uuid4(),
        "name": "Bedtime world",
        "seed": 42,
        "version": 1,
        "created_at": NOW,
        "updated_at": NOW,
    }
    fields.update(overrides)
    return IdentityProfile(**fields)  # type: ignore[arg-type]


class TestProfileInvariants:
    def test_a_minimal_profile_is_valid_and_keeps_its_values(self) -> None:
        profile = _profile()

        assert profile.name == "Bedtime world"
        assert profile.seed == 42
        assert profile.global_style is GlobalStyle.PIXAR
        assert profile.characters == ()
        assert profile.locations == ()
        assert profile.props == ()

    @pytest.mark.parametrize("name", ["", "   "])
    def test_a_blank_profile_name_is_rejected(self, name: str) -> None:
        with pytest.raises(IdentityValidationError, match="profile name"):
            _profile(name=name)

    @pytest.mark.parametrize("seed", [-1, SEED_BOUND, SEED_BOUND + 1])
    def test_a_seed_ingress_could_not_carry_is_rejected(self, seed: int) -> None:
        with pytest.raises(IdentityValidationError, match="seed"):
            _profile(seed=seed)

    @pytest.mark.parametrize("seed", [0, SEED_BOUND - 1])
    def test_the_seed_bounds_themselves_are_accepted(self, seed: int) -> None:
        assert _profile(seed=seed).seed == seed

    def test_version_starts_at_one(self) -> None:
        with pytest.raises(IdentityValidationError, match="version"):
            _profile(version=0)

    def test_a_child_of_another_profile_cannot_hang_off_this_root(self) -> None:
        foreign = _character("mia", profile_id=uuid4())

        with pytest.raises(IdentityValidationError, match="belongs to profile"):
            _profile(characters=(foreign,))


class TestCaps:
    def test_the_cap_is_the_limit_not_the_first_rejection(self) -> None:
        profile = _profile(
            characters=tuple(_character(f"c{i}") for i in range(MAX_CHARACTERS)),
            locations=(_location("home"),),
            props=tuple(_prop(f"p{i}") for i in range(MAX_PROPS)),
        )

        assert len(profile.characters) == MAX_CHARACTERS
        assert len(profile.locations) == MAX_LOCATIONS
        assert len(profile.props) == MAX_PROPS

    def test_a_fifth_character_is_rejected(self) -> None:
        with pytest.raises(IdentityValidationError, match="at most 4 characters"):
            _profile(characters=tuple(_character(f"c{i}") for i in range(MAX_CHARACTERS + 1)))

    def test_a_second_location_is_rejected(self) -> None:
        with pytest.raises(IdentityValidationError, match="at most 1 locations"):
            _profile(locations=(_location("home"), _location("park")))

    def test_a_seventh_prop_is_rejected(self) -> None:
        with pytest.raises(IdentityValidationError, match="at most 6 props"):
            _profile(props=tuple(_prop(f"p{i}") for i in range(MAX_PROPS + 1)))

    def test_ensure_within_caps_is_callable_before_a_profile_exists(self) -> None:
        ensure_within_caps(characters=MAX_CHARACTERS, locations=MAX_LOCATIONS, props=MAX_PROPS)

        with pytest.raises(IdentityValidationError, match="at most 4 characters"):
            ensure_within_caps(characters=MAX_CHARACTERS + 1)


class TestChildKeys:
    @pytest.mark.parametrize("key", ["", "  "])
    def test_a_blank_child_key_is_rejected(self, key: str) -> None:
        with pytest.raises(IdentityValidationError, match="character key"):
            _character(key)

    def test_a_blank_child_name_is_rejected(self) -> None:
        with pytest.raises(IdentityValidationError, match="character name"):
            Character(id=uuid4(), profile_id=PROFILE_ID, character_key="mia", name=" ")

    def test_a_negative_position_is_rejected(self) -> None:
        with pytest.raises(IdentityValidationError, match="position"):
            _character("mia", position=-1)

    def test_duplicate_keys_within_one_kind_are_rejected(self) -> None:
        with pytest.raises(IdentityValidationError, match="duplicate character key 'mia'"):
            _profile(characters=(_character("mia"), _character("mia", position=1)))

    def test_the_same_key_in_two_kinds_is_allowed(self) -> None:
        profile = _profile(characters=(_character("teddy"),), props=(_prop("teddy"),))

        assert profile.characters[0].key == profile.props[0].key == "teddy"

    def test_ensure_unique_keys_is_callable_before_a_profile_exists(self) -> None:
        ensure_unique_keys(["a", "b"], kind="prop")

        with pytest.raises(IdentityValidationError, match="duplicate prop key 'a'"):
            ensure_unique_keys(["a", "a"], kind="prop")


class TestDeterministicOrdering:
    def test_children_are_ordered_by_position(self) -> None:
        profile = _profile(
            characters=(
                _character("zoe", position=2),
                _character("mia", position=0),
                _character("ben", position=1),
            )
        )

        assert [c.key for c in profile.characters] == ["mia", "ben", "zoe"]

    def test_a_shared_position_is_broken_by_key_so_the_order_is_total(self) -> None:
        profile = _profile(
            characters=(_character("zoe"), _character("ben"), _character("mia")),
            props=(_prop("kite"), _prop("balloon")),
        )

        assert [c.key for c in profile.characters] == ["ben", "mia", "zoe"]
        assert [p.key for p in profile.props] == ["balloon", "kite"]

    def test_two_profiles_built_from_the_same_children_in_any_order_serialise_alike(self) -> None:
        forwards = (_character("mia", position=1), _character("ben", position=0))
        backwards = tuple(reversed(forwards))

        assert [c.key for c in _profile(characters=forwards).characters] == [
            c.key for c in _profile(characters=backwards).characters
        ]

    def test_in_canonical_order_is_usable_on_a_bare_sequence(self) -> None:
        ordered = in_canonical_order([_prop("kite", position=1), _prop("balloon", position=1)])

        assert [p.key for p in ordered] == ["balloon", "kite"]
