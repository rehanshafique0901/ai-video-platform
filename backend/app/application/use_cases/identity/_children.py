"""Shared vocabulary for the three kinds of profile child (Slice α10.0).

A character, a location and a prop differ in the fields they carry but not in how they
are governed: each has a stable key that is unique inside its profile, an author-chosen
position, and a cap set by what the Decision plane can honour (PF6). The child use cases
are written once against that shared shape; this module holds the parts that differ.
"""

from __future__ import annotations

from typing import Literal

from app.domain.identity_runtime import (
    MAX_CHARACTERS,
    MAX_LOCATIONS,
    MAX_PROPS,
    Character,
    IdentityProfile,
    Location,
    Prop,
)

ChildKind = Literal["character", "location", "prop"]

ProfileChild = Character | Location | Prop

CHILD_KINDS: tuple[ChildKind, ...] = ("character", "location", "prop")

CAPS: dict[ChildKind, int] = {
    "character": MAX_CHARACTERS,
    "location": MAX_LOCATIONS,
    "prop": MAX_PROPS,
}


def children_of(profile: IdentityProfile, kind: ChildKind) -> tuple[ProfileChild, ...]:
    """The profile's children of one kind, in canonical order."""
    if kind == "character":
        return profile.characters
    if kind == "location":
        return profile.locations
    return profile.props


def child_keys(profile: IdentityProfile, kind: ChildKind) -> tuple[str, ...]:
    return tuple(child.key for child in children_of(profile, kind))
