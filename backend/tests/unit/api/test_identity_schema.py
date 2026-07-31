"""Unit tests for the Identity Runtime API DTOs (Slice α10.0)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.api.v1.schemas.identity import (
    CharacterCreateRequest,
    CharacterUpdateRequest,
    IdentityProfileCreateRequest,
    IdentityProfilePublic,
    IdentityProfileUpdateRequest,
    LocationCreateRequest,
    PropUpdateRequest,
)
from app.domain.generation.identity import GlobalStyle
from app.domain.identity_runtime import Character, IdentityProfile, Location, Prop

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def test_create_strips_and_requires_a_name() -> None:
    dto = IdentityProfileCreateRequest(name="  Bedtime  ")
    assert dto.name == "Bedtime"
    assert dto.global_style is GlobalStyle.PIXAR
    assert dto.seed is None
    with pytest.raises(ValidationError):
        IdentityProfileCreateRequest(name="   ")


def test_create_forbids_fields_the_deployment_cannot_honour() -> None:
    # Reference images, voice and personality are deferred (PF5): not "ignored", refused.
    with pytest.raises(ValidationError):
        IdentityProfileCreateRequest(name="X", reference_image_url="http://x")  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        IdentityProfileCreateRequest(name="X", voice="warm")  # type: ignore[call-arg]


def test_create_forbids_ownership_in_the_body() -> None:
    with pytest.raises(ValidationError):
        IdentityProfileCreateRequest(name="X", owner_user_id=uuid4())  # type: ignore[call-arg]


def test_create_caps_the_inline_children() -> None:
    with pytest.raises(ValidationError):
        IdentityProfileCreateRequest(
            name="X",
            characters=[{"character_key": f"c{i}", "name": f"C{i}"} for i in range(5)],
        )
    with pytest.raises(ValidationError):
        IdentityProfileCreateRequest(
            name="X",
            locations=[
                {"location_key": "a", "name": "A"},
                {"location_key": "b", "name": "B"},
            ],
        )
    ok = IdentityProfileCreateRequest(
        name="X", props=[{"prop_key": f"p{i}", "name": f"P{i}"} for i in range(6)]
    )
    assert len(ok.props) == 6


def test_seed_must_fit_the_bound_ingress_can_carry() -> None:
    assert IdentityProfileCreateRequest(name="X", seed=0).seed == 0
    with pytest.raises(ValidationError):
        IdentityProfileCreateRequest(name="X", seed=-1)
    with pytest.raises(ValidationError):
        IdentityProfileCreateRequest(name="X", seed=2**31)


def test_descriptor_lists_are_bounded_in_both_axes() -> None:
    with pytest.raises(ValidationError):
        LocationCreateRequest(
            version=1, location_key="home", name="Home", descriptors=[f"d{i}" for i in range(21)]
        )
    with pytest.raises(ValidationError):
        LocationCreateRequest(version=1, location_key="home", name="Home", descriptors=["x" * 201])
    with pytest.raises(ValidationError):
        LocationCreateRequest(version=1, location_key="home", name="Home", descriptors=["  "])


def test_profile_update_requires_a_version_and_one_mutable_field() -> None:
    with pytest.raises(ValidationError):
        IdentityProfileUpdateRequest(name="X")  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        IdentityProfileUpdateRequest(version=1)
    dto = IdentityProfileUpdateRequest(version=3, lighting="golden hour")
    assert dto.model_fields_set == {"version", "lighting"}


def test_a_child_write_is_fenced_on_the_profile_version() -> None:
    with pytest.raises(ValidationError):
        CharacterCreateRequest(character_key="zoe", name="Zoe")  # type: ignore[call-arg]
    dto = CharacterCreateRequest(version=2, character_key="zoe", name="Zoe")
    assert dto.version == 2
    assert dto.appearance == []


def test_a_child_update_cannot_rename_the_stable_key() -> None:
    with pytest.raises(ValidationError):
        CharacterUpdateRequest(version=1, character_key="other")  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        PropUpdateRequest(version=1, prop_key="other")  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        CharacterUpdateRequest(version=1)
    dto = CharacterUpdateRequest(version=1, clothing="red coat")
    assert dto.model_fields_set == {"version", "clothing"}


def test_public_projection_carries_the_whole_world() -> None:
    pid = uuid4()
    profile = IdentityProfile(
        id=pid,
        tenant_id=uuid4(),
        owner_user_id=uuid4(),
        name="Bedtime",
        seed=42,
        version=3,
        created_at=_NOW,
        updated_at=_NOW,
        global_style=GlobalStyle.ANIME,
        characters=(
            Character(id=uuid4(), profile_id=pid, character_key="zoe", name="Zoe", position=1),
            Character(id=uuid4(), profile_id=pid, character_key="ben", name="Ben", position=0),
        ),
        locations=(Location(id=uuid4(), profile_id=pid, location_key="home", name="Home"),),
        props=(
            Prop(
                id=uuid4(),
                profile_id=pid,
                prop_key="kite",
                name="Kite",
                descriptors=("red",),
            ),
        ),
    )
    public = IdentityProfilePublic.from_domain(profile)
    assert [c.character_key for c in public.characters] == ["ben", "zoe"]
    assert public.props[0].descriptors == ["red"]
    assert public.version == 3
    assert public.model_dump(mode="json")["global_style"] == "anime"
