"""``CreateIdentityProfile`` use case (Slice α10.0).

Creates a world owned by the caller, optionally with its characters, locations and props
inline (§4). Caps and per-kind key uniqueness are checked before the write so the caller
gets a ``422``/``409`` that names the problem rather than a database error; the unique
indexes remain the race-safe backstop and surface as ``409`` from the repository.

A profile always has a seed, because a world that renders differently every time is not
the same world. When the creator does not choose one, one is drawn here and kept — the
same bound ingress uses for a drawn generation seed.
"""

from __future__ import annotations

import secrets
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.application.use_cases.identity._children import CAPS
from app.core.errors import ValidationFailedError
from app.domain.generation.identity import GlobalStyle
from app.domain.identity_runtime import (
    SEED_BOUND,
    IdentityProfile,
    IdentityValidationError,
    ensure_unique_keys,
    ensure_within_caps,
)


class CreateIdentityProfile:
    def __init__(self, uow: IUnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self,
        *,
        tenant_id: UUID,
        owner_user_id: UUID,
        name: str,
        seed: int | None = None,
        global_style: GlobalStyle = GlobalStyle.PIXAR,
        camera_style: str | None = None,
        lighting: str | None = None,
        color_palette: str | None = None,
        negative_prompt: str | None = None,
        characters: Sequence[Mapping[str, Any]] = (),
        locations: Sequence[Mapping[str, Any]] = (),
        props: Sequence[Mapping[str, Any]] = (),
    ) -> IdentityProfile:
        try:
            ensure_within_caps(
                characters=len(characters), locations=len(locations), props=len(props)
            )
            ensure_unique_keys([c["character_key"] for c in characters], kind="character")
            ensure_unique_keys([loc["location_key"] for loc in locations], kind="location")
            ensure_unique_keys([p["prop_key"] for p in props], kind="prop")
        except IdentityValidationError as e:
            raise ValidationFailedError(str(e), details={"caps": dict(CAPS)}) from e

        async with self._uow:
            try:
                profile = await self._uow.identities.add_profile(
                    tenant_id=tenant_id,
                    owner_user_id=owner_user_id,
                    name=name,
                    seed=seed if seed is not None else secrets.randbelow(SEED_BOUND),
                    global_style=global_style.value,
                    camera_style=camera_style,
                    lighting=lighting,
                    color_palette=color_palette,
                    negative_prompt=negative_prompt,
                    characters=characters,
                    locations=locations,
                    props=props,
                )
            except IdentityValidationError as e:
                raise ValidationFailedError(str(e)) from e
            await self._uow.commit()
        return profile
