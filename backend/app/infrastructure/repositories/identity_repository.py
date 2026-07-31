"""SQLAlchemy implementation of ``IIdentityRepository`` (Slice α10.0 — Identity Runtime).

The creator's authored world, persisted relationally (PF1) and read back as one
aggregate. Mirrors :class:`LibraryRepository` in every respect that matters:

* **Owner-scoped.** Every read and write filters ``tenant_id`` + ``owner_user_id``, so
  another owner's world is invisible rather than forbidden (anti-enumeration).
* **OCC on the root.** ``update_profile`` and *every* child write are version-fenced CAS
  statements that hand-set ``version = version + 1``, so the guarded
  ``tg_identity_profiles_biu_version_bump`` trigger no-ops and the net increment stays +1.
  Children carry no version of their own (PF8): a world must never be half-edited when a
  generation snapshots it.
* **Constraints are the arbiter.** ``uq_identity_profiles_owner_name`` and the three
  ``uq_identity_<kind>_profile_key`` indexes surface as ``ConflictError`` → ``409``; there
  is no pre-check SELECT to race against.
* **Hard delete** (PF10) — children cascade in the database. Generations that bound this
  world are untouched: they hold a snapshot, not a reference (IDENT-1).

Every child write bumps the root *first*. If the child statement then fails or matches
nothing, the caller gets ``None``/an exception and never commits, so the bump does not
survive the transaction the Unit of Work rolls back.

Nothing here reads or writes execution state: a profile is Knowledge-plane world state,
never a record of what happened (ADR-0055 D1, IDENT-4).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import Any, NoReturn, TypeVar
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select, tuple_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.interfaces.repositories import IIdentityRepository
from app.core.errors import ConflictError
from app.domain.generation.identity import GlobalStyle
from app.domain.identity_runtime import (
    Character as CharacterEntity,
    IdentityProfile as IdentityProfileEntity,
    Location as LocationEntity,
    Prop as PropEntity,
)
from app.infrastructure.db.models.identity_runtime import (
    IdentityCharacter as CharacterRow,
    IdentityLocation as LocationRow,
    IdentityProfile as ProfileRow,
    IdentityProp as PropRow,
)

ChildRowT = TypeVar("ChildRowT", CharacterRow, LocationRow, PropRow)


def _extract_constraint_name(exc: IntegrityError) -> str | None:
    """Best-effort constraint name from psycopg's diagnostics."""
    diag = getattr(getattr(exc, "orig", None), "diag", None)
    name = getattr(diag, "constraint_name", None)
    return str(name) if name else None


# The only integrity failures this repository can explain to a caller. Anything else —
# a foreign key, a not-null, a check — is a defect rather than a creator's conflict, and
# is re-raised untranslated rather than reported as a 409 it is not.
_CONFLICT_MESSAGES = {
    "uq_identity_profiles_owner_name": "identity profile name already in use",
    "uq_identity_characters_profile_key": "character key already used in this profile",
    "uq_identity_locations_profile_key": "location key already used in this profile",
    "uq_identity_props_profile_key": "prop key already used in this profile",
}


def _raise_conflict(exc: IntegrityError) -> NoReturn:
    constraint = _extract_constraint_name(exc)
    message = _CONFLICT_MESSAGES.get(constraint or "")
    if message is None:
        raise exc
    raise ConflictError(message, details={"constraint": constraint}) from exc


def _character_to_entity(row: CharacterRow) -> CharacterEntity:
    return CharacterEntity(
        id=row.id,
        profile_id=row.profile_id,
        character_key=row.character_key,
        name=row.name,
        age=row.age,
        appearance=tuple(row.appearance),
        clothing=row.clothing,
        accessories=tuple(row.accessories),
        position=row.position,
    )


def _location_to_entity(row: LocationRow) -> LocationEntity:
    return LocationEntity(
        id=row.id,
        profile_id=row.profile_id,
        location_key=row.location_key,
        name=row.name,
        descriptors=tuple(row.descriptors),
        position=row.position,
    )


def _prop_to_entity(row: PropRow) -> PropEntity:
    return PropEntity(
        id=row.id,
        profile_id=row.profile_id,
        prop_key=row.prop_key,
        name=row.name,
        descriptors=tuple(row.descriptors),
        position=row.position,
    )


def _to_entity(
    row: ProfileRow,
    characters: Iterable[CharacterRow],
    locations: Iterable[LocationRow],
    props: Iterable[PropRow],
) -> IdentityProfileEntity:
    """Rebuild the aggregate. Ordering and caps are re-established by the domain."""
    return IdentityProfileEntity(
        id=row.id,
        tenant_id=row.tenant_id,
        owner_user_id=row.owner_user_id,
        name=row.name,
        seed=row.seed,
        version=row.version,
        created_at=row.created_at,
        updated_at=row.updated_at,
        global_style=GlobalStyle(row.global_style),
        camera_style=row.camera_style,
        lighting=row.lighting,
        color_palette=row.color_palette,
        negative_prompt=row.negative_prompt,
        characters=tuple(_character_to_entity(c) for c in characters),
        locations=tuple(_location_to_entity(loc) for loc in locations),
        props=tuple(_prop_to_entity(p) for p in props),
    )


class IdentityRepository(IIdentityRepository):
    """Identity Runtime persistence adapter."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

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
    ) -> IdentityProfileEntity:
        profile_id = uuid4()
        row = ProfileRow(
            id=profile_id,
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            name=name,
            seed=seed,
            global_style=global_style,
            camera_style=camera_style,
            lighting=lighting,
            color_palette=color_palette,
            negative_prompt=negative_prompt,
        )
        self._session.add(row)
        # Flushed before the children: the models declare no ``relationship()``, so the
        # session has no parent-before-child ordering to infer and would otherwise emit a
        # child INSERT against a profile row that does not exist yet.
        await self._flush_or_conflict()
        for spec in characters:
            self._session.add(CharacterRow(id=uuid4(), profile_id=profile_id, **_arrays(spec)))
        for spec in locations:
            self._session.add(LocationRow(id=uuid4(), profile_id=profile_id, **_arrays(spec)))
        for spec in props:
            self._session.add(PropRow(id=uuid4(), profile_id=profile_id, **_arrays(spec)))
        await self._flush_or_conflict()
        created = await self._load(profile_id, tenant_id, owner_user_id)
        assert created is not None, "the profile just written must be readable in this session"
        return created

    async def get_profile(
        self, profile_id: UUID, tenant_id: UUID, owner_user_id: UUID
    ) -> IdentityProfileEntity | None:
        return await self._load(profile_id, tenant_id, owner_user_id)

    async def list_profiles(
        self,
        tenant_id: UUID,
        owner_user_id: UUID,
        *,
        limit: int,
        after: tuple[datetime, UUID] | None = None,
    ) -> list[IdentityProfileEntity]:
        stmt = (
            select(ProfileRow)
            .where(ProfileRow.tenant_id == tenant_id)
            .where(ProfileRow.owner_user_id == owner_user_id)
        )
        if after is not None:
            stmt = stmt.where(tuple_(ProfileRow.created_at, ProfileRow.id) < after)
        stmt = stmt.order_by(ProfileRow.created_at.desc(), ProfileRow.id.desc()).limit(limit)
        rows = list((await self._session.execute(stmt)).scalars().all())
        if not rows:
            return []

        ids = [row.id for row in rows]
        characters = await self._children(CharacterRow, ids)
        locations = await self._children(LocationRow, ids)
        props = await self._children(PropRow, ids)
        return [
            _to_entity(
                row,
                characters.get(row.id, ()),
                locations.get(row.id, ()),
                props.get(row.id, ()),
            )
            for row in rows
        ]

    async def update_profile(
        self,
        profile_id: UUID,
        tenant_id: UUID,
        owner_user_id: UUID,
        *,
        expected_version: int,
        changes: Mapping[str, Any],
    ) -> IdentityProfileEntity | None:
        assert changes, "update_profile requires at least one changed column"
        bumped = await self._bump_root(
            profile_id,
            tenant_id,
            owner_user_id,
            expected_version=expected_version,
            changes=changes,
        )
        if not bumped:
            return None
        return await self._load(profile_id, tenant_id, owner_user_id)

    async def delete_profile(self, profile_id: UUID, tenant_id: UUID, owner_user_id: UUID) -> bool:
        stmt = (
            delete(ProfileRow)
            .where(ProfileRow.id == profile_id)
            .where(ProfileRow.tenant_id == tenant_id)
            .where(ProfileRow.owner_user_id == owner_user_id)
            .returning(ProfileRow.id)
        )
        removed = (await self._session.execute(stmt)).scalar_one_or_none()
        return removed is not None

    # ---- characters ----------------------------------------------------

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
    ) -> IdentityProfileEntity | None:
        return await self._add_child(
            CharacterRow(
                id=uuid4(),
                profile_id=profile_id,
                character_key=character_key,
                name=name,
                age=age,
                appearance=list(appearance),
                clothing=clothing,
                accessories=list(accessories),
                position=position,
            ),
            profile_id,
            tenant_id,
            owner_user_id,
            expected_version=expected_version,
        )

    async def update_character(
        self,
        profile_id: UUID,
        tenant_id: UUID,
        owner_user_id: UUID,
        *,
        expected_version: int,
        character_key: str,
        changes: Mapping[str, Any],
    ) -> IdentityProfileEntity | None:
        return await self._update_child(
            CharacterRow,
            CharacterRow.character_key == character_key,
            profile_id,
            tenant_id,
            owner_user_id,
            expected_version=expected_version,
            changes=changes,
        )

    async def remove_character(
        self,
        profile_id: UUID,
        tenant_id: UUID,
        owner_user_id: UUID,
        *,
        expected_version: int,
        character_key: str,
    ) -> IdentityProfileEntity | None:
        return await self._remove_child(
            CharacterRow,
            CharacterRow.character_key == character_key,
            profile_id,
            tenant_id,
            owner_user_id,
            expected_version=expected_version,
        )

    # ---- locations -----------------------------------------------------

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
    ) -> IdentityProfileEntity | None:
        return await self._add_child(
            LocationRow(
                id=uuid4(),
                profile_id=profile_id,
                location_key=location_key,
                name=name,
                descriptors=list(descriptors),
                position=position,
            ),
            profile_id,
            tenant_id,
            owner_user_id,
            expected_version=expected_version,
        )

    async def update_location(
        self,
        profile_id: UUID,
        tenant_id: UUID,
        owner_user_id: UUID,
        *,
        expected_version: int,
        location_key: str,
        changes: Mapping[str, Any],
    ) -> IdentityProfileEntity | None:
        return await self._update_child(
            LocationRow,
            LocationRow.location_key == location_key,
            profile_id,
            tenant_id,
            owner_user_id,
            expected_version=expected_version,
            changes=changes,
        )

    async def remove_location(
        self,
        profile_id: UUID,
        tenant_id: UUID,
        owner_user_id: UUID,
        *,
        expected_version: int,
        location_key: str,
    ) -> IdentityProfileEntity | None:
        return await self._remove_child(
            LocationRow,
            LocationRow.location_key == location_key,
            profile_id,
            tenant_id,
            owner_user_id,
            expected_version=expected_version,
        )

    # ---- props ---------------------------------------------------------

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
    ) -> IdentityProfileEntity | None:
        return await self._add_child(
            PropRow(
                id=uuid4(),
                profile_id=profile_id,
                prop_key=prop_key,
                name=name,
                descriptors=list(descriptors),
                position=position,
            ),
            profile_id,
            tenant_id,
            owner_user_id,
            expected_version=expected_version,
        )

    async def update_prop(
        self,
        profile_id: UUID,
        tenant_id: UUID,
        owner_user_id: UUID,
        *,
        expected_version: int,
        prop_key: str,
        changes: Mapping[str, Any],
    ) -> IdentityProfileEntity | None:
        return await self._update_child(
            PropRow,
            PropRow.prop_key == prop_key,
            profile_id,
            tenant_id,
            owner_user_id,
            expected_version=expected_version,
            changes=changes,
        )

    async def remove_prop(
        self,
        profile_id: UUID,
        tenant_id: UUID,
        owner_user_id: UUID,
        *,
        expected_version: int,
        prop_key: str,
    ) -> IdentityProfileEntity | None:
        return await self._remove_child(
            PropRow,
            PropRow.prop_key == prop_key,
            profile_id,
            tenant_id,
            owner_user_id,
            expected_version=expected_version,
        )

    # ---- internals -----------------------------------------------------

    async def _load(
        self, profile_id: UUID, tenant_id: UUID, owner_user_id: UUID
    ) -> IdentityProfileEntity | None:
        stmt = (
            select(ProfileRow)
            .where(ProfileRow.id == profile_id)
            .where(ProfileRow.tenant_id == tenant_id)
            .where(ProfileRow.owner_user_id == owner_user_id)
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is None:
            return None
        characters = await self._children(CharacterRow, [profile_id])
        locations = await self._children(LocationRow, [profile_id])
        props = await self._children(PropRow, [profile_id])
        return _to_entity(
            row,
            characters.get(profile_id, ()),
            locations.get(profile_id, ()),
            props.get(profile_id, ()),
        )

    async def _children(
        self, row_cls: type[ChildRowT], profile_ids: Sequence[UUID]
    ) -> dict[UUID, list[ChildRowT]]:
        """One query per kind for a whole page — never one per profile."""
        stmt = select(row_cls).where(row_cls.profile_id.in_(profile_ids))
        grouped: dict[UUID, list[ChildRowT]] = {}
        for child in (await self._session.execute(stmt)).scalars().all():
            grouped.setdefault(child.profile_id, []).append(child)
        return grouped

    async def _bump_root(
        self,
        profile_id: UUID,
        tenant_id: UUID,
        owner_user_id: UUID,
        *,
        expected_version: int,
        changes: Mapping[str, Any] | None = None,
    ) -> bool:
        """Version-fenced CAS on the root. ``False`` when the fence or the owner missed."""
        upd = (
            update(ProfileRow)
            .where(ProfileRow.id == profile_id)
            .where(ProfileRow.tenant_id == tenant_id)
            .where(ProfileRow.owner_user_id == owner_user_id)
            .where(ProfileRow.version == expected_version)
            .values(
                **dict(changes or {}),
                version=ProfileRow.version + 1,
                updated_at=func.now(),
            )
            .returning(ProfileRow.id)
        )
        try:
            fenced = (await self._session.execute(upd)).scalar_one_or_none()
        except IntegrityError as e:
            _raise_conflict(e)
        return fenced is not None

    async def _add_child(
        self,
        child: ChildRowT,
        profile_id: UUID,
        tenant_id: UUID,
        owner_user_id: UUID,
        *,
        expected_version: int,
    ) -> IdentityProfileEntity | None:
        if not await self._bump_root(
            profile_id, tenant_id, owner_user_id, expected_version=expected_version
        ):
            return None
        self._session.add(child)
        await self._flush_or_conflict()
        return await self._load(profile_id, tenant_id, owner_user_id)

    async def _update_child(
        self,
        row_cls: type[ChildRowT],
        key_match: Any,
        profile_id: UUID,
        tenant_id: UUID,
        owner_user_id: UUID,
        *,
        expected_version: int,
        changes: Mapping[str, Any],
    ) -> IdentityProfileEntity | None:
        assert changes, "a child update requires at least one changed column"
        if not await self._bump_root(
            profile_id, tenant_id, owner_user_id, expected_version=expected_version
        ):
            return None
        upd = (
            update(row_cls)
            .where(row_cls.profile_id == profile_id)
            .where(key_match)
            .values(**_arrays(changes))
            .returning(row_cls.id)
        )
        try:
            touched = (await self._session.execute(upd)).scalar_one_or_none()
        except IntegrityError as e:
            _raise_conflict(e)
        if touched is None:
            return None
        return await self._load(profile_id, tenant_id, owner_user_id)

    async def _remove_child(
        self,
        row_cls: type[ChildRowT],
        key_match: Any,
        profile_id: UUID,
        tenant_id: UUID,
        owner_user_id: UUID,
        *,
        expected_version: int,
    ) -> IdentityProfileEntity | None:
        if not await self._bump_root(
            profile_id, tenant_id, owner_user_id, expected_version=expected_version
        ):
            return None
        stmt = (
            delete(row_cls)
            .where(row_cls.profile_id == profile_id)
            .where(key_match)
            .returning(row_cls.id)
        )
        removed = (await self._session.execute(stmt)).scalar_one_or_none()
        if removed is None:
            return None
        return await self._load(profile_id, tenant_id, owner_user_id)

    async def _flush_or_conflict(self) -> None:
        try:
            await self._session.flush()
        except IntegrityError as e:
            _raise_conflict(e)


def _arrays(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Coerce the domain's immutable tuples into lists for the ``text[]`` columns."""
    return {key: list(value) if isinstance(value, tuple) else value for key, value in spec.items()}
