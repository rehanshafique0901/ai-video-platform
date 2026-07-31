"""The authored profile aggregate: the root, its children, the caps, the invariants.

Shape mirrors migration ``0017`` (α10.0 pre-flight §3): one parent table plus a
character, location and prop child table, each child carrying a stable key that
is unique inside its profile. The root owns the OCC ``version``; children are
written through it (PF8), because a snapshot must never straddle two states.

**Caps are capability, not taste** (PF6, IDENT-2). The planner casts every
character into every shot (``planner.py:120``) and anchors on the *first*
location (``:123``); the prompt builder emits every prop
(``prompt_builder.py:95``). So a fifth character makes every prompt worse and a
second location is inert — the numbers below are what the current Decision
plane can honour, and they move only when it does. Nothing here expresses
whether a world is *good*, only whether it can be executed at all.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, TypeVar
from uuid import UUID

from app.domain.generation.identity import GlobalStyle

MAX_CHARACTERS = 4
MAX_LOCATIONS = 1
MAX_PROPS = 6

# Mirrors ingress's own bound for a drawn seed: comfortably inside
# ``generations.seed`` (bigint) and every provider's accepted range. Authoring
# refuses what ingress could not carry (IDENT-2).
SEED_BOUND = 2**31


class IdentityValidationError(ValueError):
    """An authored world breaks a rule this deployment must be able to honour."""


def ensure_within_caps(*, characters: int = 0, locations: int = 0, props: int = 0) -> None:
    """Raise if any child count exceeds what the Decision plane can honour."""
    for kind, count, cap in (
        ("characters", characters, MAX_CHARACTERS),
        ("locations", locations, MAX_LOCATIONS),
        ("props", props, MAX_PROPS),
    ):
        if count > cap:
            raise IdentityValidationError(f"at most {cap} {kind} per profile, got {count}")


def ensure_unique_keys(keys: Sequence[str], *, kind: str) -> None:
    """Raise on a repeated child key — the in-memory half of ``uq_..._profile_key``."""
    seen: set[str] = set()
    for key in keys:
        if key in seen:
            raise IdentityValidationError(f"duplicate {kind} key {key!r}")
        seen.add(key)


class _OrderedChild(Protocol):
    """A profile child: a stable key and an author-chosen position."""

    @property
    def key(self) -> str: ...

    @property
    def position(self) -> int: ...


ChildT = TypeVar("ChildT", bound=_OrderedChild)


def in_canonical_order(children: Iterable[ChildT]) -> tuple[ChildT, ...]:
    """Order children by position, then key.

    The tie-break on key is what makes the order *total*: two children may share
    a position, and a world that serialised differently on two reads would make
    a snapshot non-reproducible.
    """
    return tuple(sorted(children, key=lambda child: (child.position, child.key)))


def _require_text(value: str, *, label: str) -> None:
    if not value.strip():
        raise IdentityValidationError(f"{label} must not be blank")


def _validate_child(*, key: str, name: str, position: int, kind: str) -> None:
    _require_text(key, label=f"{kind} key")
    _require_text(name, label=f"{kind} name")
    if position < 0:
        raise IdentityValidationError(f"{kind} position must not be negative, got {position}")


@dataclass(frozen=True, slots=True)
class Character:
    """A person the creator declares exists, carried into every shot unchanged.

    ``character_key`` is the stable id the planner and shot records carry, so it
    outlives renames of ``name``.
    """

    id: UUID
    profile_id: UUID
    character_key: str
    name: str
    age: str | None = None
    appearance: tuple[str, ...] = ()
    clothing: str | None = None
    accessories: tuple[str, ...] = ()
    position: int = 0

    @property
    def key(self) -> str:
        return self.character_key

    def __post_init__(self) -> None:
        _validate_child(
            key=self.character_key, name=self.name, position=self.position, kind="character"
        )


@dataclass(frozen=True, slots=True)
class Location:
    """The place the world happens in. One per profile, until per-shot casting ships."""

    id: UUID
    profile_id: UUID
    location_key: str
    name: str
    descriptors: tuple[str, ...] = ()
    position: int = 0

    @property
    def key(self) -> str:
        return self.location_key

    def __post_init__(self) -> None:
        _validate_child(
            key=self.location_key, name=self.name, position=self.position, kind="location"
        )


@dataclass(frozen=True, slots=True)
class Prop:
    """A recurring object that must stay consistent across shots."""

    id: UUID
    profile_id: UUID
    prop_key: str
    name: str
    descriptors: tuple[str, ...] = ()
    position: int = 0

    @property
    def key(self) -> str:
        return self.prop_key

    def __post_init__(self) -> None:
        _validate_child(key=self.prop_key, name=self.name, position=self.position, kind="prop")


@dataclass(frozen=True, slots=True)
class IdentityProfile:
    """One creator's world: the aggregate root.

    Owned by ``(tenant_id, owner_user_id)`` and not by a project (PF9), because
    generations are not project-scoped either. ``version`` is the OCC handle
    every mutation bumps, including a mutation of a child.

    Mutable creator state — which is exactly why a generation binds a *snapshot*
    of it and never a reference (ADR-0055 D2, IDENT-1). Editing or deleting a
    profile can never reach a generation that already accepted one.
    """

    id: UUID
    tenant_id: UUID
    owner_user_id: UUID
    name: str
    seed: int
    version: int
    created_at: datetime
    updated_at: datetime
    global_style: GlobalStyle = GlobalStyle.PIXAR
    camera_style: str | None = None
    lighting: str | None = None
    color_palette: str | None = None
    negative_prompt: str | None = None
    characters: tuple[Character, ...] = ()
    locations: tuple[Location, ...] = ()
    props: tuple[Prop, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.name, label="profile name")
        if not 0 <= self.seed < SEED_BOUND:
            raise IdentityValidationError(f"seed must be in [0, {SEED_BOUND}), got {self.seed}")
        if self.version < 1:
            raise IdentityValidationError(f"version must be >= 1, got {self.version}")
        ensure_within_caps(
            characters=len(self.characters),
            locations=len(self.locations),
            props=len(self.props),
        )
        for kind, children in (
            ("character", self.characters),
            ("location", self.locations),
            ("prop", self.props),
        ):
            ensure_unique_keys([child.key for child in children], kind=kind)
            for child in children:
                if child.profile_id != self.id:
                    raise IdentityValidationError(
                        f"{kind} {child.key!r} belongs to profile {child.profile_id}, "
                        f"not {self.id}"
                    )
        object.__setattr__(self, "characters", in_canonical_order(self.characters))
        object.__setattr__(self, "locations", in_canonical_order(self.locations))
        object.__setattr__(self, "props", in_canonical_order(self.props))
