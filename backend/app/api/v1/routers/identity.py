"""``/api/v1/identities/*`` HTTP router (Slice α10.0 — Identity Runtime).

Lets a creator author and keep a *world* — named characters, the place they are in,
recurring props, the project look, and a stable seed — so the shots of a video are about
the same people in the same place. Everything downstream of this resource already
consumed a profile; until now there was no way to write one.

The router stays thin: it projects DTOs, delegates to use cases, and wraps results in the
API envelope. Ownership, 404-before-412, caps and key uniqueness live in the use cases;
error mapping is ``app.core.errors``'.

**Closed to** (ADR-0055 frozen decision 18): no endpoint here ranks, scores, annotates or
recommends identity options, and none reports what "works well". The API offers controls;
preference belongs to the Decision plane and arrives, if ever, through its own contract.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Query, Request, Response, status
from fastapi.responses import JSONResponse

from app.api.v1.deps import (
    AddIdentityChildDep,
    CreateIdentityProfileDep,
    CurrentUserDep,
    DeleteIdentityProfileDep,
    GetIdentityProfileDep,
    ListIdentityProfilesDep,
    RemoveIdentityChildDep,
    UpdateIdentityChildDep,
    UpdateIdentityProfileDep,
)
from app.api.v1.helpers import envelope
from app.api.v1.schemas.identity import (
    CharacterCreateRequest,
    CharacterUpdateRequest,
    IdentityProfileCreateRequest,
    IdentityProfilePublic,
    IdentityProfileUpdateRequest,
    LocationCreateRequest,
    LocationUpdateRequest,
    PropCreateRequest,
    PropUpdateRequest,
)

router = APIRouter(prefix="/identities", tags=["identities"])


# --- profile ------------------------------------------------------------------


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_identity_profile(
    body: IdentityProfileCreateRequest,
    request: Request,
    current_user: CurrentUserDep,
    use_case: CreateIdentityProfileDep,
) -> JSONResponse:
    profile = await use_case.execute(
        tenant_id=current_user.tenant_id,
        owner_user_id=current_user.id,
        name=body.name,
        seed=body.seed,
        global_style=body.global_style,
        camera_style=body.camera_style,
        lighting=body.lighting,
        color_palette=body.color_palette,
        negative_prompt=body.negative_prompt,
        characters=[c.model_dump() for c in body.characters],
        locations=[loc.model_dump() for loc in body.locations],
        props=[p.model_dump() for p in body.props],
    )
    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content=envelope(IdentityProfilePublic.from_domain(profile), request),
    )


@router.get("")
async def list_identity_profiles(
    request: Request,
    current_user: CurrentUserDep,
    use_case: ListIdentityProfilesDep,
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = Query(default=None),
) -> JSONResponse:
    page = await use_case.execute(
        tenant_id=current_user.tenant_id,
        owner_user_id=current_user.id,
        limit=limit,
        cursor_token=cursor,
    )
    return JSONResponse(
        content=envelope(
            [IdentityProfilePublic.from_domain(p) for p in page.items],
            request,
            next_cursor=page.next_cursor,
        )
    )


@router.get("/{identity_id}")
async def get_identity_profile(
    identity_id: UUID,
    request: Request,
    current_user: CurrentUserDep,
    use_case: GetIdentityProfileDep,
) -> JSONResponse:
    profile = await use_case.execute(
        profile_id=identity_id,
        tenant_id=current_user.tenant_id,
        owner_user_id=current_user.id,
    )
    return JSONResponse(content=envelope(IdentityProfilePublic.from_domain(profile), request))


@router.patch("/{identity_id}")
async def update_identity_profile(
    identity_id: UUID,
    body: IdentityProfileUpdateRequest,
    request: Request,
    current_user: CurrentUserDep,
    use_case: UpdateIdentityProfileDep,
) -> JSONResponse:
    changes = body.model_dump(exclude_unset=True, exclude={"version"})
    profile = await use_case.execute(
        profile_id=identity_id,
        tenant_id=current_user.tenant_id,
        owner_user_id=current_user.id,
        expected_version=body.version,
        changes=changes,
    )
    return JSONResponse(content=envelope(IdentityProfilePublic.from_domain(profile), request))


@router.delete("/{identity_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_identity_profile(
    identity_id: UUID,
    current_user: CurrentUserDep,
    use_case: DeleteIdentityProfileDep,
) -> Response:
    """Hard-delete a world (PF10).

    Generations already bound to it are unaffected — they hold a snapshot taken at
    acceptance, not a reference (IDENT-1) — and their ``identity_id`` remains as the
    honest record that this world existed and no longer does.
    """
    await use_case.execute(
        profile_id=identity_id,
        tenant_id=current_user.tenant_id,
        owner_user_id=current_user.id,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- characters ----------------------------------------------------------------


@router.post("/{identity_id}/characters", status_code=status.HTTP_201_CREATED)
async def add_character(
    identity_id: UUID,
    body: CharacterCreateRequest,
    request: Request,
    current_user: CurrentUserDep,
    use_case: AddIdentityChildDep,
) -> JSONResponse:
    profile = await use_case.execute(
        profile_id=identity_id,
        tenant_id=current_user.tenant_id,
        owner_user_id=current_user.id,
        expected_version=body.version,
        kind="character",
        key=body.character_key,
        name=body.name,
        attributes=body.model_dump(exclude={"version", "character_key", "name"}),
    )
    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content=envelope(IdentityProfilePublic.from_domain(profile), request),
    )


@router.patch("/{identity_id}/characters/{character_key}")
async def update_character(
    identity_id: UUID,
    character_key: str,
    body: CharacterUpdateRequest,
    request: Request,
    current_user: CurrentUserDep,
    use_case: UpdateIdentityChildDep,
) -> JSONResponse:
    profile = await use_case.execute(
        profile_id=identity_id,
        tenant_id=current_user.tenant_id,
        owner_user_id=current_user.id,
        expected_version=body.version,
        kind="character",
        key=character_key,
        changes=body.model_dump(exclude_unset=True, exclude={"version"}),
    )
    return JSONResponse(content=envelope(IdentityProfilePublic.from_domain(profile), request))


@router.delete("/{identity_id}/characters/{character_key}")
async def remove_character(
    identity_id: UUID,
    character_key: str,
    request: Request,
    current_user: CurrentUserDep,
    use_case: RemoveIdentityChildDep,
    version: int = Query(ge=1),
) -> JSONResponse:
    """Remove a character. The profile ``version`` is a required query parameter."""
    profile = await use_case.execute(
        profile_id=identity_id,
        tenant_id=current_user.tenant_id,
        owner_user_id=current_user.id,
        expected_version=version,
        kind="character",
        key=character_key,
    )
    return JSONResponse(content=envelope(IdentityProfilePublic.from_domain(profile), request))


# --- locations -----------------------------------------------------------------


@router.post("/{identity_id}/locations", status_code=status.HTTP_201_CREATED)
async def add_location(
    identity_id: UUID,
    body: LocationCreateRequest,
    request: Request,
    current_user: CurrentUserDep,
    use_case: AddIdentityChildDep,
) -> JSONResponse:
    profile = await use_case.execute(
        profile_id=identity_id,
        tenant_id=current_user.tenant_id,
        owner_user_id=current_user.id,
        expected_version=body.version,
        kind="location",
        key=body.location_key,
        name=body.name,
        attributes=body.model_dump(exclude={"version", "location_key", "name"}),
    )
    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content=envelope(IdentityProfilePublic.from_domain(profile), request),
    )


@router.patch("/{identity_id}/locations/{location_key}")
async def update_location(
    identity_id: UUID,
    location_key: str,
    body: LocationUpdateRequest,
    request: Request,
    current_user: CurrentUserDep,
    use_case: UpdateIdentityChildDep,
) -> JSONResponse:
    profile = await use_case.execute(
        profile_id=identity_id,
        tenant_id=current_user.tenant_id,
        owner_user_id=current_user.id,
        expected_version=body.version,
        kind="location",
        key=location_key,
        changes=body.model_dump(exclude_unset=True, exclude={"version"}),
    )
    return JSONResponse(content=envelope(IdentityProfilePublic.from_domain(profile), request))


@router.delete("/{identity_id}/locations/{location_key}")
async def remove_location(
    identity_id: UUID,
    location_key: str,
    request: Request,
    current_user: CurrentUserDep,
    use_case: RemoveIdentityChildDep,
    version: int = Query(ge=1),
) -> JSONResponse:
    """Remove a location. The profile ``version`` is a required query parameter."""
    profile = await use_case.execute(
        profile_id=identity_id,
        tenant_id=current_user.tenant_id,
        owner_user_id=current_user.id,
        expected_version=version,
        kind="location",
        key=location_key,
    )
    return JSONResponse(content=envelope(IdentityProfilePublic.from_domain(profile), request))


# --- props ---------------------------------------------------------------------


@router.post("/{identity_id}/props", status_code=status.HTTP_201_CREATED)
async def add_prop(
    identity_id: UUID,
    body: PropCreateRequest,
    request: Request,
    current_user: CurrentUserDep,
    use_case: AddIdentityChildDep,
) -> JSONResponse:
    profile = await use_case.execute(
        profile_id=identity_id,
        tenant_id=current_user.tenant_id,
        owner_user_id=current_user.id,
        expected_version=body.version,
        kind="prop",
        key=body.prop_key,
        name=body.name,
        attributes=body.model_dump(exclude={"version", "prop_key", "name"}),
    )
    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content=envelope(IdentityProfilePublic.from_domain(profile), request),
    )


@router.patch("/{identity_id}/props/{prop_key}")
async def update_prop(
    identity_id: UUID,
    prop_key: str,
    body: PropUpdateRequest,
    request: Request,
    current_user: CurrentUserDep,
    use_case: UpdateIdentityChildDep,
) -> JSONResponse:
    profile = await use_case.execute(
        profile_id=identity_id,
        tenant_id=current_user.tenant_id,
        owner_user_id=current_user.id,
        expected_version=body.version,
        kind="prop",
        key=prop_key,
        changes=body.model_dump(exclude_unset=True, exclude={"version"}),
    )
    return JSONResponse(content=envelope(IdentityProfilePublic.from_domain(profile), request))


@router.delete("/{identity_id}/props/{prop_key}")
async def remove_prop(
    identity_id: UUID,
    prop_key: str,
    request: Request,
    current_user: CurrentUserDep,
    use_case: RemoveIdentityChildDep,
    version: int = Query(ge=1),
) -> JSONResponse:
    """Remove a prop. The profile ``version`` is a required query parameter."""
    profile = await use_case.execute(
        profile_id=identity_id,
        tenant_id=current_user.tenant_id,
        owner_user_id=current_user.id,
        expected_version=version,
        kind="prop",
        key=prop_key,
    )
    return JSONResponse(content=envelope(IdentityProfilePublic.from_domain(profile), request))
