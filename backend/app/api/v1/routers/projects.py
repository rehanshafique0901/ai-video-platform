"""``/api/v1/projects/*`` HTTP router (Slice α5a — create + read).

Three endpoints, all authenticated via :data:`CurrentUserDep` (the sole
authentication seam — no JWT parsing, no ``ITokenIssuer``, no repository
access in this module):

* ``POST /projects``            → 201, create a project the caller owns.
* ``GET  /projects``            → 200, list the caller's projects
  (newest first, keyset-paginated via ``?limit=`` + opaque ``?cursor=``).
* ``GET  /projects/{project_id}`` → 200, fetch one owned project;
  404 if it is missing, soft-deleted, or owned by someone else (α5a D5).

The router stays thin: it projects the DTO, delegates to a use case,
and wraps the result in the API_CONTRACT §1.1 envelope. Business rules
(ownership scoping, uniqueness, pagination) live in the use cases /
repository. Errors raised by the use cases (``ConflictError`` → 409,
``NotFoundError`` → 404, ``ValidationFailedError`` → 422) are rendered
by the handlers registered in ``app.core.errors``; this module contains
no try / except. See ``docs/api/AUTH_ENDPOINTS.md`` §9 for the mutation
flow this create endpoint follows.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Query, Request, status
from fastapi.responses import JSONResponse

from app.api.v1.deps import (
    CreateProjectDep,
    CurrentUserDep,
    GetProjectDep,
    ListProjectsDep,
)
from app.api.v1.helpers import client_ip, envelope
from app.api.v1.schemas.projects import ProjectCreateRequest, ProjectPublic
from app.domain.projects.project import Project

router = APIRouter(prefix="/projects", tags=["projects"])


def _to_public(project: Project) -> ProjectPublic:
    """Project a domain ``Project`` into the wire DTO ``ProjectPublic``.

    Single source of truth for the projection — used by all three
    endpoints so their bodies are identical for the same row.
    """
    return ProjectPublic(
        id=project.id,
        tenant_id=project.tenant_id,
        owner_user_id=project.owner_user_id,
        folder_id=project.folder_id,
        name=project.name,
        description=project.description,
        aspect_ratio=project.aspect_ratio,
        language=project.language,
        style=project.style,
        settings=project.settings,
        created_at=project.created_at,
        updated_at=project.updated_at,
        version=project.version,
    )


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_project(
    body: ProjectCreateRequest,
    request: Request,
    current_user: CurrentUserDep,
    use_case: CreateProjectDep,
) -> JSONResponse:
    """Create a project owned by the authenticated caller.

    Ownership (``owner_user_id``) and tenancy (``tenant_id``) are taken
    from ``current_user`` — the body cannot set them (``extra="forbid"``
    on the DTO). Returns 201 with the created ``ProjectPublic``
    (``version = 1``). A duplicate live name for this owner raises
    ``ConflictError`` → 409.
    """
    result = await use_case.execute(
        owner_user_id=current_user.id,
        tenant_id=current_user.tenant_id,
        name=body.name,
        aspect_ratio=body.aspect_ratio,
        description=body.description,
        language=body.language,
        style=body.style,
        settings=body.settings,
        ip=client_ip(request),
    )
    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content=envelope(_to_public(result.project), request),
    )


@router.get("")
async def list_projects(
    request: Request,
    current_user: CurrentUserDep,
    use_case: ListProjectsDep,
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = Query(default=None),
) -> JSONResponse:
    """List the caller's projects, newest first, keyset-paginated.

    ``limit`` is clamped to ``1..100`` by FastAPI (a value outside the
    range is a 422 before the handler runs — α5a A14). ``cursor`` is the
    opaque token from a prior response's ``meta.next_cursor``; a
    malformed token is a 422 (``ValidationFailedError`` from the use
    case). The response ``meta.next_cursor`` is present iff a further
    page exists.
    """
    page = await use_case.execute(
        owner_user_id=current_user.id,
        tenant_id=current_user.tenant_id,
        limit=limit,
        cursor_token=cursor,
    )
    return JSONResponse(
        content=envelope(
            [_to_public(p) for p in page.items],
            request,
            next_cursor=page.next_cursor,
        )
    )


@router.get("/{project_id}")
async def get_project(
    project_id: UUID,
    request: Request,
    current_user: CurrentUserDep,
    use_case: GetProjectDep,
) -> JSONResponse:
    """Fetch one project owned by the caller.

    ``project_id`` is validated as a UUID by FastAPI (a non-UUID path is
    a 422). A project that is missing, soft-deleted, or owned by another
    user/tenant yields a uniform ``404 NOT_FOUND`` (α5a D5) — the caller
    cannot distinguish the three cases.
    """
    project = await use_case.execute(
        project_id=project_id,
        owner_user_id=current_user.id,
        tenant_id=current_user.tenant_id,
    )
    return JSONResponse(content=envelope(_to_public(project), request))
