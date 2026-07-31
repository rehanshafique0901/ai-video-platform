"""In-memory fakes for the Identity Runtime use-case unit tests (Slice α10.0).

A dependency-free reimplementation of the persistence semantics the real
``IdentityRepository`` provides: owner scoping, per-owner name uniqueness, per-profile
child-key uniqueness, the root-fenced version bump that every child write performs (PF8),
keyset ordering, and the cascade on delete. The real adapter is exercised against a live
database by Stage 27.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Any, Self, cast
from uuid import UUID, uuid4

from app.application.interfaces.repositories import IIdentityRepository
from app.application.interfaces.unit_of_work import IUnitOfWork
from app.core.errors import ConflictError
from app.domain.generation.identity import GlobalStyle
from app.domain.identity_runtime import Character, IdentityProfile, Location, Prop

_BASE = datetime(2026, 1, 1, tzinfo=UTC)


class FakeIdentityRepository(IIdentityRepository):
    """In-memory ``IIdentityRepository`` with faithful semantics."""

    def __init__(self) -> None:
        self.profiles: dict[UUID, IdentityProfile] = {}
        self._seq = 0

    def _next_ts(self) -> datetime:
        self._seq += 1
        return _BASE + timedelta(seconds=self._seq)

    # ---- profile -------------------------------------------------------

    async def add_profile(
        self,
        *,
        tenant_id: UUID,
        owner_user_id: UUID,
        name: str,
        seed: int,
        global_style: str,
        camera_style: str | None = None,
        lighting: str | None = None,
        color_palette: str | None = None,
        negative_prompt: str | None = None,
        characters: Sequence[Mapping[str, Any]] = (),
        locations: Sequence[Mapping[str, Any]] = (),
        props: Sequence[Mapping[str, Any]] = (),
    ) -> IdentityProfile:
        for existing in self.profiles.values():
            if existing.owner_user_id == owner_user_id and existing.name == name:
                raise ConflictError(
                    "identity profile name already in use",
                    details={"constraint": "uq_identity_profiles_owner_name"},
                )
        profile_id = uuid4()
        ts = self._next_ts()
        profile = IdentityProfile(
            id=profile_id,
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            name=name,
            seed=seed,
            version=1,
            created_at=ts,
            updated_at=ts,
            global_style=GlobalStyle(global_style),
            camera_style=camera_style,
            lighting=lighting,
            color_palette=color_palette,
            negative_prompt=negative_prompt,
            characters=tuple(
                Character(id=uuid4(), profile_id=profile_id, **_tuples(spec)) for spec in characters
            ),
            locations=tuple(
                Location(id=uuid4(), profile_id=profile_id, **_tuples(spec)) for spec in locations
            ),
            props=tuple(Prop(id=uuid4(), profile_id=profile_id, **_tuples(spec)) for spec in props),
        )
        self.profiles[profile_id] = profile
        return profile

    async def get_profile(
        self, profile_id: UUID, tenant_id: UUID, owner_user_id: UUID
    ) -> IdentityProfile | None:
        p = self.profiles.get(profile_id)
        if p is None or p.tenant_id != tenant_id or p.owner_user_id != owner_user_id:
            return None
        return p

    async def list_profiles(
        self,
        tenant_id: UUID,
        owner_user_id: UUID,
        *,
        limit: int,
        after: tuple[datetime, UUID] | None = None,
    ) -> list[IdentityProfile]:
        rows = [
            p
            for p in self.profiles.values()
            if p.tenant_id == tenant_id and p.owner_user_id == owner_user_id
        ]
        rows.sort(key=lambda p: (p.created_at, p.id), reverse=True)
        if after is not None:
            rows = [p for p in rows if (p.created_at, p.id) < after]
        return rows[:limit]

    async def update_profile(
        self,
        profile_id: UUID,
        tenant_id: UUID,
        owner_user_id: UUID,
        *,
        expected_version: int,
        changes: Mapping[str, Any],
    ) -> IdentityProfile | None:
        p = await self.get_profile(profile_id, tenant_id, owner_user_id)
        if p is None or p.version != expected_version:
            return None
        resolved = dict(changes)
        if "global_style" in resolved:
            resolved["global_style"] = GlobalStyle(resolved["global_style"])
        if "name" in resolved:
            for other in self.profiles.values():
                if (
                    other.id != profile_id
                    and other.owner_user_id == owner_user_id
                    and other.name == resolved["name"]
                ):
                    raise ConflictError(
                        "identity profile name already in use",
                        details={"constraint": "uq_identity_profiles_owner_name"},
                    )
        updated = replace(p, **resolved, version=p.version + 1, updated_at=self._next_ts())
        self.profiles[profile_id] = updated
        return updated

    async def delete_profile(self, profile_id: UUID, tenant_id: UUID, owner_user_id: UUID) -> bool:
        p = await self.get_profile(profile_id, tenant_id, owner_user_id)
        if p is None:
            return False
        del self.profiles[profile_id]
        return True

    # ---- children ------------------------------------------------------

    async def add_character(
        self,
        profile_id: UUID,
        tenant_id: UUID,
        owner_user_id: UUID,
        *,
        expected_version: int,
        character_key: str,
        name: str,
        age: str | None = None,
        appearance: Sequence[str] = (),
        clothing: str | None = None,
        accessories: Sequence[str] = (),
        position: int = 0,
    ) -> IdentityProfile | None:
        p = await self._fenced(profile_id, tenant_id, owner_user_id, expected_version)
        if p is None:
            return None
        child = Character(
            id=uuid4(),
            profile_id=profile_id,
            character_key=character_key,
            name=name,
            age=age,
            appearance=tuple(appearance),
            clothing=clothing,
            accessories=tuple(accessories),
            position=position,
        )
        self._assert_free(p.characters, character_key, "characters")
        return self._store(replace(p, characters=(*p.characters, child)))

    async def update_character(
        self,
        profile_id: UUID,
        tenant_id: UUID,
        owner_user_id: UUID,
        *,
        expected_version: int,
        character_key: str,
        changes: Mapping[str, Any],
    ) -> IdentityProfile | None:
        p = await self._fenced(profile_id, tenant_id, owner_user_id, expected_version)
        if p is None:
            return None
        children = _replace_child(p.characters, character_key, changes)
        if children is None:
            return None
        return self._store(replace(p, characters=children))

    async def remove_character(
        self,
        profile_id: UUID,
        tenant_id: UUID,
        owner_user_id: UUID,
        *,
        expected_version: int,
        character_key: str,
    ) -> IdentityProfile | None:
        p = await self._fenced(profile_id, tenant_id, owner_user_id, expected_version)
        if p is None:
            return None
        remaining = tuple(c for c in p.characters if c.key != character_key)
        if len(remaining) == len(p.characters):
            return None
        return self._store(replace(p, characters=remaining))

    async def add_location(
        self,
        profile_id: UUID,
        tenant_id: UUID,
        owner_user_id: UUID,
        *,
        expected_version: int,
        location_key: str,
        name: str,
        descriptors: Sequence[str] = (),
        position: int = 0,
    ) -> IdentityProfile | None:
        p = await self._fenced(profile_id, tenant_id, owner_user_id, expected_version)
        if p is None:
            return None
        child = Location(
            id=uuid4(),
            profile_id=profile_id,
            location_key=location_key,
            name=name,
            descriptors=tuple(descriptors),
            position=position,
        )
        self._assert_free(p.locations, location_key, "locations")
        return self._store(replace(p, locations=(*p.locations, child)))

    async def update_location(
        self,
        profile_id: UUID,
        tenant_id: UUID,
        owner_user_id: UUID,
        *,
        expected_version: int,
        location_key: str,
        changes: Mapping[str, Any],
    ) -> IdentityProfile | None:
        p = await self._fenced(profile_id, tenant_id, owner_user_id, expected_version)
        if p is None:
            return None
        children = _replace_child(p.locations, location_key, changes)
        if children is None:
            return None
        return self._store(replace(p, locations=children))

    async def remove_location(
        self,
        profile_id: UUID,
        tenant_id: UUID,
        owner_user_id: UUID,
        *,
        expected_version: int,
        location_key: str,
    ) -> IdentityProfile | None:
        p = await self._fenced(profile_id, tenant_id, owner_user_id, expected_version)
        if p is None:
            return None
        remaining = tuple(c for c in p.locations if c.key != location_key)
        if len(remaining) == len(p.locations):
            return None
        return self._store(replace(p, locations=remaining))

    async def add_prop(
        self,
        profile_id: UUID,
        tenant_id: UUID,
        owner_user_id: UUID,
        *,
        expected_version: int,
        prop_key: str,
        name: str,
        descriptors: Sequence[str] = (),
        position: int = 0,
    ) -> IdentityProfile | None:
        p = await self._fenced(profile_id, tenant_id, owner_user_id, expected_version)
        if p is None:
            return None
        child = Prop(
            id=uuid4(),
            profile_id=profile_id,
            prop_key=prop_key,
            name=name,
            descriptors=tuple(descriptors),
            position=position,
        )
        self._assert_free(p.props, prop_key, "props")
        return self._store(replace(p, props=(*p.props, child)))

    async def update_prop(
        self,
        profile_id: UUID,
        tenant_id: UUID,
        owner_user_id: UUID,
        *,
        expected_version: int,
        prop_key: str,
        changes: Mapping[str, Any],
    ) -> IdentityProfile | None:
        p = await self._fenced(profile_id, tenant_id, owner_user_id, expected_version)
        if p is None:
            return None
        children = _replace_child(p.props, prop_key, changes)
        if children is None:
            return None
        return self._store(replace(p, props=children))

    async def remove_prop(
        self,
        profile_id: UUID,
        tenant_id: UUID,
        owner_user_id: UUID,
        *,
        expected_version: int,
        prop_key: str,
    ) -> IdentityProfile | None:
        p = await self._fenced(profile_id, tenant_id, owner_user_id, expected_version)
        if p is None:
            return None
        remaining = tuple(c for c in p.props if c.key != prop_key)
        if len(remaining) == len(p.props):
            return None
        return self._store(replace(p, props=remaining))

    # ---- internals -----------------------------------------------------

    async def _fenced(
        self, profile_id: UUID, tenant_id: UUID, owner_user_id: UUID, expected_version: int
    ) -> IdentityProfile | None:
        p = await self.get_profile(profile_id, tenant_id, owner_user_id)
        if p is None or p.version != expected_version:
            return None
        return replace(p, version=p.version + 1, updated_at=self._next_ts())

    def _assert_free(self, children: tuple[Any, ...], key: str, kind: str) -> None:
        if any(c.key == key for c in children):
            raise ConflictError(
                f"{kind[:-1]} key already used in this profile",
                details={"constraint": f"uq_identity_{kind}_profile_key"},
            )

    def _store(self, profile: IdentityProfile) -> IdentityProfile:
        self.profiles[profile.id] = profile
        return profile


def _tuples(spec: Mapping[str, Any]) -> dict[str, Any]:
    return {k: tuple(v) if isinstance(v, list) else v for k, v in spec.items()}


def _replace_child(
    children: tuple[Any, ...], key: str, changes: Mapping[str, Any]
) -> tuple[Any, ...] | None:
    found = False
    out = []
    for child in children:
        if child.key == key:
            out.append(replace(child, **_tuples(changes)))
            found = True
        else:
            out.append(child)
    return tuple(out) if found else None


class FakeIdentityUnitOfWork(IUnitOfWork):
    """Minimal UoW exposing the one port the identity use cases touch."""

    def __init__(self, *, identities: FakeIdentityRepository | None = None) -> None:
        self.identities = cast(IIdentityRepository, identities or FakeIdentityRepository())
        self.committed = False

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        return None
