"""DTOs for ``/api/v1/identities/*`` endpoints (Slice α10.0 — Identity Runtime).

The creator's authored world: a profile root with characters, locations and props. The
field whitelist is enforced by ``extra="forbid"`` — ownership and tenancy come from the
authenticated caller, never the body, and a field the deployment cannot honour is not
offered at all (IDENT-2). That is why there is no reference image, no voice, no
personality, no music or subtitle style here: no v1 path consumes them (PF5).

Mutation DTOs carry the profile's ``version``; a stale one is a ``412``. Children are
edited through their own endpoints but fence on the *root's* version (PF8), because a
snapshot must never straddle two states.

Not the authentication context — that is ``schemas/auth.py`` / ``schemas/users.py``. The
resource is named ``identities`` because it is the creator-facing name for a world; the
bounded context is ``identity_runtime``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domain.generation.identity import GlobalStyle
from app.domain.identity_runtime import (
    MAX_CHARACTERS,
    MAX_LOCATIONS,
    MAX_PROPS,
    SEED_BOUND,
    Character,
    IdentityProfile,
    Location,
    Prop,
)

_MAX_PHRASES = 20
_MAX_PHRASE_LEN = 200
_MAX_NAME_LEN = 200
_MAX_KEY_LEN = 64
_MAX_TEXT_LEN = 2000


def _validate_phrases(value: list[str]) -> list[str]:
    """Descriptor lists go straight into a prompt, so they are bounded in both axes."""
    if len(value) > _MAX_PHRASES:
        raise ValueError(f"at most {_MAX_PHRASES} entries are allowed")
    for phrase in value:
        if not phrase.strip():
            raise ValueError("entries must not be blank")
        if len(phrase) > _MAX_PHRASE_LEN:
            raise ValueError(f"each entry must be at most {_MAX_PHRASE_LEN} characters")
    return value


class _Body(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")


# --- children: inputs --------------------------------------------------------


class CharacterInput(_Body):
    """A character as supplied inline when a world is created."""

    character_key: str = Field(min_length=1, max_length=_MAX_KEY_LEN)
    name: str = Field(min_length=1, max_length=_MAX_NAME_LEN)
    age: str | None = Field(default=None, max_length=_MAX_NAME_LEN)
    appearance: list[str] = Field(default_factory=list)
    clothing: str | None = Field(default=None, max_length=_MAX_PHRASE_LEN)
    accessories: list[str] = Field(default_factory=list)
    position: int = Field(default=0, ge=0)

    _check_appearance = field_validator("appearance")(_validate_phrases)
    _check_accessories = field_validator("accessories")(_validate_phrases)


class LocationInput(_Body):
    """A location as supplied inline when a world is created."""

    location_key: str = Field(min_length=1, max_length=_MAX_KEY_LEN)
    name: str = Field(min_length=1, max_length=_MAX_NAME_LEN)
    descriptors: list[str] = Field(default_factory=list)
    position: int = Field(default=0, ge=0)

    _check_descriptors = field_validator("descriptors")(_validate_phrases)


class PropInput(_Body):
    """A prop as supplied inline when a world is created."""

    prop_key: str = Field(min_length=1, max_length=_MAX_KEY_LEN)
    name: str = Field(min_length=1, max_length=_MAX_NAME_LEN)
    descriptors: list[str] = Field(default_factory=list)
    position: int = Field(default=0, ge=0)

    _check_descriptors = field_validator("descriptors")(_validate_phrases)


# --- profile -----------------------------------------------------------------


class IdentityProfileCreateRequest(_Body):
    """POST /api/v1/identities body.

    Children are optional and inline. The caps are the planner's, not a product
    preference (PF6): it casts every character into every shot and anchors on the first
    location, so a fifth character makes every prompt worse and a second is inert.
    """

    name: str = Field(min_length=1, max_length=_MAX_NAME_LEN)
    seed: int | None = Field(default=None, ge=0, lt=SEED_BOUND)
    global_style: GlobalStyle = GlobalStyle.PIXAR
    camera_style: str | None = Field(default=None, max_length=_MAX_PHRASE_LEN)
    lighting: str | None = Field(default=None, max_length=_MAX_PHRASE_LEN)
    color_palette: str | None = Field(default=None, max_length=_MAX_PHRASE_LEN)
    negative_prompt: str | None = Field(default=None, max_length=_MAX_TEXT_LEN)
    characters: list[CharacterInput] = Field(default_factory=list, max_length=MAX_CHARACTERS)
    locations: list[LocationInput] = Field(default_factory=list, max_length=MAX_LOCATIONS)
    props: list[PropInput] = Field(default_factory=list, max_length=MAX_PROPS)


class IdentityProfileUpdateRequest(_Body):
    """PATCH /api/v1/identities/{id} body — partial, version-fenced.

    Root fields only. Children have their own endpoints; both fence on this version.
    """

    version: int = Field(
        ge=1,
        description=(
            "The ``version`` the client last observed on the profile. A stale value "
            "yields 412 VERSION_CONFLICT."
        ),
    )
    name: str = Field(default="", min_length=1, max_length=_MAX_NAME_LEN)
    seed: int = Field(default=0, ge=0, lt=SEED_BOUND)
    global_style: GlobalStyle = GlobalStyle.PIXAR
    camera_style: str | None = Field(default=None, max_length=_MAX_PHRASE_LEN)
    lighting: str | None = Field(default=None, max_length=_MAX_PHRASE_LEN)
    color_palette: str | None = Field(default=None, max_length=_MAX_PHRASE_LEN)
    negative_prompt: str | None = Field(default=None, max_length=_MAX_TEXT_LEN)

    @model_validator(mode="after")
    def _require_mutable_field(self) -> Self:
        if not (set(self.model_fields_set) - {"version"}):
            raise ValueError("at least one mutable field is required")
        return self


# --- children: requests ------------------------------------------------------


class _FencedBody(_Body):
    version: int = Field(
        ge=1,
        description=(
            "The ``version`` the client last observed on the *profile*. Every child "
            "write bumps it (PF8); a stale value yields 412 VERSION_CONFLICT."
        ),
    )


class CharacterCreateRequest(_FencedBody, CharacterInput):
    """POST /api/v1/identities/{id}/characters body."""


class LocationCreateRequest(_FencedBody, LocationInput):
    """POST /api/v1/identities/{id}/locations body."""


class PropCreateRequest(_FencedBody, PropInput):
    """POST /api/v1/identities/{id}/props body."""


class _ChildUpdateRequest(_FencedBody):
    """A child edit never includes the stable key: the planner and shot records carry it."""

    @model_validator(mode="after")
    def _require_mutable_field(self) -> Self:
        if not (set(self.model_fields_set) - {"version"}):
            raise ValueError("at least one mutable field is required")
        return self


class CharacterUpdateRequest(_ChildUpdateRequest):
    """PATCH /api/v1/identities/{id}/characters/{key} body."""

    name: str = Field(default="", min_length=1, max_length=_MAX_NAME_LEN)
    age: str | None = Field(default=None, max_length=_MAX_NAME_LEN)
    appearance: list[str] = Field(default_factory=list)
    clothing: str | None = Field(default=None, max_length=_MAX_PHRASE_LEN)
    accessories: list[str] = Field(default_factory=list)
    position: int = Field(default=0, ge=0)

    _check_appearance = field_validator("appearance")(_validate_phrases)
    _check_accessories = field_validator("accessories")(_validate_phrases)


class LocationUpdateRequest(_ChildUpdateRequest):
    """PATCH /api/v1/identities/{id}/locations/{key} body."""

    name: str = Field(default="", min_length=1, max_length=_MAX_NAME_LEN)
    descriptors: list[str] = Field(default_factory=list)
    position: int = Field(default=0, ge=0)

    _check_descriptors = field_validator("descriptors")(_validate_phrases)


class PropUpdateRequest(_ChildUpdateRequest):
    """PATCH /api/v1/identities/{id}/props/{key} body."""

    name: str = Field(default="", min_length=1, max_length=_MAX_NAME_LEN)
    descriptors: list[str] = Field(default_factory=list)
    position: int = Field(default=0, ge=0)

    _check_descriptors = field_validator("descriptors")(_validate_phrases)


# --- projections -------------------------------------------------------------


class CharacterPublic(BaseModel):
    id: UUID
    character_key: str
    name: str
    age: str | None
    appearance: list[str]
    clothing: str | None
    accessories: list[str]
    position: int

    @classmethod
    def from_domain(cls, child: Character) -> CharacterPublic:
        return cls(
            id=child.id,
            character_key=child.character_key,
            name=child.name,
            age=child.age,
            appearance=list(child.appearance),
            clothing=child.clothing,
            accessories=list(child.accessories),
            position=child.position,
        )


class LocationPublic(BaseModel):
    id: UUID
    location_key: str
    name: str
    descriptors: list[str]
    position: int

    @classmethod
    def from_domain(cls, child: Location) -> LocationPublic:
        return cls(
            id=child.id,
            location_key=child.location_key,
            name=child.name,
            descriptors=list(child.descriptors),
            position=child.position,
        )


class PropPublic(BaseModel):
    id: UUID
    prop_key: str
    name: str
    descriptors: list[str]
    position: int

    @classmethod
    def from_domain(cls, child: Prop) -> PropPublic:
        return cls(
            id=child.id,
            prop_key=child.prop_key,
            name=child.name,
            descriptors=list(child.descriptors),
            position=child.position,
        )


class IdentityProfilePublic(BaseModel):
    """Public projection of the whole world, children in canonical order."""

    id: UUID
    tenant_id: UUID
    owner_user_id: UUID
    name: str
    seed: int
    global_style: GlobalStyle
    camera_style: str | None
    lighting: str | None
    color_palette: str | None
    negative_prompt: str | None
    characters: list[CharacterPublic]
    locations: list[LocationPublic]
    props: list[PropPublic]
    version: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_domain(cls, profile: IdentityProfile) -> IdentityProfilePublic:
        return cls(
            id=profile.id,
            tenant_id=profile.tenant_id,
            owner_user_id=profile.owner_user_id,
            name=profile.name,
            seed=profile.seed,
            global_style=profile.global_style,
            camera_style=profile.camera_style,
            lighting=profile.lighting,
            color_palette=profile.color_palette,
            negative_prompt=profile.negative_prompt,
            characters=[CharacterPublic.from_domain(c) for c in profile.characters],
            locations=[LocationPublic.from_domain(loc) for loc in profile.locations],
            props=[PropPublic.from_domain(p) for p in profile.props],
            version=profile.version,
            created_at=profile.created_at,
            updated_at=profile.updated_at,
        )
