"""``/api/v1/media/*`` HTTP router (α6.2).

Five **top-level** endpoints (owner-scoped, NOT nested under a project — media is
an owner-level artefact, α6.2 Q1), all authenticated via :data:`CurrentUserDep`:

* ``POST   /media``               → 201, register a media asset (metadata only).
* ``GET    /media``               → 200, list the caller's media (filters).
* ``GET    /media/{media_id}``    → 200, fetch one asset.
* ``PATCH  /media/{media_id}``    → 200, narrow partial update (no OCC).
* ``DELETE /media/{media_id}``    → 204, soft delete (idempotent-by-404).

Media assets are **generation outputs** (ADR-0037): there is no ``version`` on
the wire and no ``412`` — a PATCH is last-writer-wins. The router stays thin: DTO
projection + envelope; the owner 404 gate, optional-link validation, and the
register-by-metadata boundary live in the use cases. Errors (``NotFoundError`` →
404, ``ValidationFailedError`` → 422, ``ConflictError`` → 409) are rendered by
the centralized handlers in ``app.core.errors``.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Request, Response, status
from fastapi.responses import JSONResponse

from app.api.v1.deps import (
    CurrentUserDep,
    DeleteMediaDep,
    GetMediaDep,
    ListMediaDep,
    RegisterMediaDep,
    UpdateMediaDep,
)
from app.api.v1.helpers import client_ip, envelope
from app.api.v1.schemas.media import (
    MediaKind,
    MediaPublic,
    MediaRegisterRequest,
    MediaUpdateRequest,
)
from app.domain.media.media_asset import MediaAsset

router = APIRouter(prefix="/media", tags=["media"])


def _to_public(media: MediaAsset) -> MediaPublic:
    """Project a domain ``MediaAsset`` into the wire DTO."""
    return MediaPublic(
        id=media.id,
        kind=media.kind,
        source=media.source,
        project_id=media.project_id,
        scene_id=media.scene_id,
        prompt_id=media.prompt_id,
        model_id=media.model_id,
        provider=media.provider,
        storage_backend=media.storage_backend,
        storage_bucket=media.storage_bucket,
        storage_key=media.storage_key,
        mime_type=media.mime_type,
        size_bytes=media.size_bytes,
        width=media.width,
        height=media.height,
        duration_seconds=media.duration_seconds,
        checksum_sha256=media.checksum_sha256,
        source_metadata=media.source_metadata,
        created_at=media.created_at,
        updated_at=media.updated_at,
    )


@router.post("", status_code=status.HTTP_201_CREATED)
async def register_media(
    body: MediaRegisterRequest,
    request: Request,
    current_user: CurrentUserDep,
    use_case: RegisterMediaDep,
) -> JSONResponse:
    """Register a media asset (metadata only) owned by the caller.

    Returns 201 with the created ``MediaPublic``. 422 for a foreign/unknown
    ``project_id`` / ``scene_id`` / ``prompt_id`` / ``model_id`` (or a
    scene/prompt without a project); 409 if the ``(storage_backend,
    storage_bucket, storage_key)`` coordinates are already registered.
    """
    media = await use_case.execute(
        owner_user_id=current_user.id,
        tenant_id=current_user.tenant_id,
        kind=body.kind,
        source=body.source,
        storage_backend=body.storage_backend,
        storage_bucket=body.storage_bucket,
        storage_key=body.storage_key,
        mime_type=body.mime_type,
        size_bytes=body.size_bytes,
        checksum_sha256=body.checksum_bytes(),
        project_id=body.project_id,
        scene_id=body.scene_id,
        prompt_id=body.prompt_id,
        model_id=body.model_id,
        provider=body.provider,
        width=body.width,
        height=body.height,
        duration_seconds=body.duration_seconds,
        source_metadata=body.source_metadata,
        ip=client_ip(request),
    )
    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content=envelope(_to_public(media), request),
    )


@router.get("")
async def list_media(
    request: Request,
    current_user: CurrentUserDep,
    use_case: ListMediaDep,
    kind: MediaKind | None = None,
    source: str | None = None,
    project_id: UUID | None = None,
    scene_id: UUID | None = None,
) -> JSONResponse:
    """List the caller's media assets, newest-first, optionally filtered.

    ``?kind=`` (enum), ``?source=`` (string), ``?project_id=`` and ``?scene_id=``
    (uuid) narrow the result (combined = AND); a bad enum / non-UUID is a 422.
    Owner-scoped — no project route gate. Empty → ``200 []``.
    """
    media = await use_case.execute(
        owner_user_id=current_user.id,
        tenant_id=current_user.tenant_id,
        kind=kind,
        source=source,
        project_id=project_id,
        scene_id=scene_id,
    )
    return JSONResponse(content=envelope([_to_public(m) for m in media], request))


@router.get("/{media_id}")
async def get_media(
    media_id: UUID,
    request: Request,
    current_user: CurrentUserDep,
    use_case: GetMediaDep,
) -> JSONResponse:
    """Fetch one media asset owned by the caller.

    A missing / soft-deleted / other-owner asset yields a uniform ``404``
    (owner visibility gate, α6.2 D2).
    """
    media = await use_case.execute(
        media_id=media_id,
        owner_user_id=current_user.id,
        tenant_id=current_user.tenant_id,
    )
    return JSONResponse(content=envelope(_to_public(media), request))


@router.patch("/{media_id}")
async def update_media(
    media_id: UUID,
    body: MediaUpdateRequest,
    request: Request,
    current_user: CurrentUserDep,
    use_case: UpdateMediaDep,
) -> JSONResponse:
    """Narrowly update one media asset (no version fence).

    The body carries any subset of the mutable fields (the four links +
    ``provider`` + ``source_metadata``). Tri-state is resolved via
    ``exclude_unset``; an explicit ``project_id: null`` clears the link. 404
    (asset not visible), 422 (empty patch / immutable field / bad link). No
    ``412`` — media is last-writer-wins (ADR-0037).
    """
    changes = body.model_dump(exclude_unset=True)
    media = await use_case.execute(
        media_id=media_id,
        owner_user_id=current_user.id,
        tenant_id=current_user.tenant_id,
        changes=changes,
        ip=client_ip(request),
    )
    return JSONResponse(content=envelope(_to_public(media), request))


@router.delete("/{media_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_media(
    media_id: UUID,
    request: Request,
    current_user: CurrentUserDep,
    use_case: DeleteMediaDep,
) -> Response:
    """Soft-delete one media asset owned by the caller.

    Returns ``204``. Idempotent-by-404 (α6.2 D4): a second ``DELETE`` — and any
    ``GET``/``PATCH`` after delete — returns ``404``. Deleting another owner's
    asset, or an unknown id, is the same ``404``.
    """
    await use_case.execute(
        media_id=media_id,
        owner_user_id=current_user.id,
        tenant_id=current_user.tenant_id,
        ip=client_ip(request),
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
