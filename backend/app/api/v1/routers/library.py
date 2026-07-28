"""``/api/v1/library/*`` HTTP router (Slice α9.2 — Media Library).

Owner-scoped folders + library assets over registered ``media_assets`` (ADR-0037 CR-8),
all authenticated via :data:`CurrentUserDep`. The router stays thin: it projects DTOs,
delegates to use cases, and wraps results in the API envelope. Business rules
(ownership, uniqueness, keyset pagination, 404-before-412, cycle guard) live in the use
cases / repository; error mapping is handled by ``app.core.errors``.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Query, Request, Response, status
from fastapi.responses import JSONResponse

from app.api.v1.deps import (
    AddLibraryAssetDep,
    CreateLibraryFolderDep,
    CurrentUserDep,
    DeleteLibraryAssetDep,
    DeleteLibraryFolderDep,
    GetLibraryAssetDep,
    GetLibraryFolderDep,
    ListLibraryAssetsDep,
    ListLibraryFoldersDep,
    RecordLibraryAssetUseDep,
    UpdateLibraryAssetDep,
    UpdateLibraryFolderDep,
)
from app.api.v1.helpers import envelope
from app.api.v1.schemas.library import (
    LibraryAssetCreateRequest,
    LibraryAssetPublic,
    LibraryAssetUpdateRequest,
    LibraryAssetUseRequest,
    LibraryFolderCreateRequest,
    LibraryFolderPublic,
    LibraryFolderUpdateRequest,
)
from app.core.errors import ValidationFailedError

router = APIRouter(prefix="/library", tags=["library"])


# --- folders ----------------------------------------------------------------


@router.post("/folders", status_code=status.HTTP_201_CREATED)
async def create_library_folder(
    body: LibraryFolderCreateRequest,
    request: Request,
    current_user: CurrentUserDep,
    use_case: CreateLibraryFolderDep,
) -> JSONResponse:
    folder = await use_case.execute(
        tenant_id=current_user.tenant_id,
        owner_user_id=current_user.id,
        name=body.name,
        parent_folder_id=body.parent_folder_id,
    )
    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content=envelope(LibraryFolderPublic.from_domain(folder), request),
    )


@router.get("/folders")
async def list_library_folders(
    request: Request,
    current_user: CurrentUserDep,
    use_case: ListLibraryFoldersDep,
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = Query(default=None),
    parent_folder_id: UUID | None = Query(default=None),
    roots: bool = Query(default=False),
) -> JSONResponse:
    if roots and parent_folder_id is not None:
        raise ValidationFailedError(
            "provide either roots=true or parent_folder_id, not both",
            details={"field": "parent_folder_id"},
        )
    filter_by_parent = roots or parent_folder_id is not None
    page = await use_case.execute(
        tenant_id=current_user.tenant_id,
        owner_user_id=current_user.id,
        limit=limit,
        parent_folder_id=parent_folder_id,
        filter_by_parent=filter_by_parent,
        cursor_token=cursor,
    )
    return JSONResponse(
        content=envelope(
            [LibraryFolderPublic.from_domain(f) for f in page.items],
            request,
            next_cursor=page.next_cursor,
        )
    )


@router.get("/folders/{folder_id}")
async def get_library_folder(
    folder_id: UUID,
    request: Request,
    current_user: CurrentUserDep,
    use_case: GetLibraryFolderDep,
) -> JSONResponse:
    folder = await use_case.execute(
        folder_id=folder_id,
        tenant_id=current_user.tenant_id,
        owner_user_id=current_user.id,
    )
    return JSONResponse(content=envelope(LibraryFolderPublic.from_domain(folder), request))


@router.patch("/folders/{folder_id}")
async def update_library_folder(
    folder_id: UUID,
    body: LibraryFolderUpdateRequest,
    request: Request,
    current_user: CurrentUserDep,
    use_case: UpdateLibraryFolderDep,
) -> JSONResponse:
    changes = body.model_dump(exclude_unset=True)
    folder = await use_case.execute(
        folder_id=folder_id,
        tenant_id=current_user.tenant_id,
        owner_user_id=current_user.id,
        changes=changes,
    )
    return JSONResponse(content=envelope(LibraryFolderPublic.from_domain(folder), request))


@router.delete("/folders/{folder_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_library_folder(
    folder_id: UUID,
    current_user: CurrentUserDep,
    use_case: DeleteLibraryFolderDep,
) -> Response:
    await use_case.execute(
        folder_id=folder_id,
        tenant_id=current_user.tenant_id,
        owner_user_id=current_user.id,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- assets -----------------------------------------------------------------


@router.post("/assets", status_code=status.HTTP_201_CREATED)
async def add_library_asset(
    body: LibraryAssetCreateRequest,
    request: Request,
    current_user: CurrentUserDep,
    use_case: AddLibraryAssetDep,
) -> JSONResponse:
    asset = await use_case.execute(
        tenant_id=current_user.tenant_id,
        owner_user_id=current_user.id,
        media_asset_id=body.media_asset_id,
        library_folder_id=body.library_folder_id,
        name=body.name,
        description=body.description,
        tags=body.tags,
    )
    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content=envelope(LibraryAssetPublic.from_domain(asset), request),
    )


@router.get("/assets")
async def list_library_assets(
    request: Request,
    current_user: CurrentUserDep,
    use_case: ListLibraryAssetsDep,
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = Query(default=None),
    folder_id: UUID | None = Query(default=None),
    unfiled: bool = Query(default=False),
    tags: str | None = Query(default=None),
) -> JSONResponse:
    if unfiled and folder_id is not None:
        raise ValidationFailedError(
            "provide either unfiled=true or folder_id, not both",
            details={"field": "folder_id"},
        )
    filter_by_folder = unfiled or folder_id is not None
    tag_tuple: tuple[str, ...] | None = None
    if tags is not None:
        parsed = tuple(t for t in (part.strip() for part in tags.split(",")) if t)
        tag_tuple = parsed or None
    page = await use_case.execute(
        tenant_id=current_user.tenant_id,
        owner_user_id=current_user.id,
        limit=limit,
        folder_id=folder_id,
        filter_by_folder=filter_by_folder,
        tags=tag_tuple,
        cursor_token=cursor,
    )
    return JSONResponse(
        content=envelope(
            [LibraryAssetPublic.from_domain(a) for a in page.items],
            request,
            next_cursor=page.next_cursor,
        )
    )


@router.get("/assets/{asset_id}")
async def get_library_asset(
    asset_id: UUID,
    request: Request,
    current_user: CurrentUserDep,
    use_case: GetLibraryAssetDep,
) -> JSONResponse:
    asset = await use_case.execute(
        asset_id=asset_id,
        tenant_id=current_user.tenant_id,
        owner_user_id=current_user.id,
    )
    return JSONResponse(content=envelope(LibraryAssetPublic.from_domain(asset), request))


@router.patch("/assets/{asset_id}")
async def update_library_asset(
    asset_id: UUID,
    body: LibraryAssetUpdateRequest,
    request: Request,
    current_user: CurrentUserDep,
    use_case: UpdateLibraryAssetDep,
) -> JSONResponse:
    changes = body.model_dump(exclude_unset=True, exclude={"version"})
    asset = await use_case.execute(
        asset_id=asset_id,
        tenant_id=current_user.tenant_id,
        owner_user_id=current_user.id,
        expected_version=body.version,
        changes=changes,
    )
    return JSONResponse(content=envelope(LibraryAssetPublic.from_domain(asset), request))


@router.delete("/assets/{asset_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_library_asset(
    asset_id: UUID,
    current_user: CurrentUserDep,
    use_case: DeleteLibraryAssetDep,
) -> Response:
    await use_case.execute(
        asset_id=asset_id,
        tenant_id=current_user.tenant_id,
        owner_user_id=current_user.id,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/assets/{asset_id}/uses")
async def record_library_asset_use(
    asset_id: UUID,
    body: LibraryAssetUseRequest,
    request: Request,
    current_user: CurrentUserDep,
    use_case: RecordLibraryAssetUseDep,
) -> JSONResponse:
    asset = await use_case.execute(
        asset_id=asset_id,
        tenant_id=current_user.tenant_id,
        owner_user_id=current_user.id,
        project_id=body.project_id,
    )
    return JSONResponse(content=envelope(LibraryAssetPublic.from_domain(asset), request))
