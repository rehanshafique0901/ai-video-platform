"""``/api/v1/projects/*`` HTTP router (α5a create/read; α5b update/delete).

Five endpoints, all authenticated via :data:`CurrentUserDep` (the sole
authentication seam — no JWT parsing, no ``ITokenIssuer``, no repository
access in this module):

* ``POST   /projects``              → 201, create a project the caller owns.
* ``GET    /projects``              → 200, list the caller's projects
  (newest first, keyset-paginated via ``?limit=`` + opaque ``?cursor=``).
* ``GET    /projects/{project_id}`` → 200, fetch one owned project;
  404 if it is missing, soft-deleted, or owned by someone else (α5a D5).
* ``PATCH  /projects/{project_id}`` → 200, partial version-fenced update
  (α5b). 404-before-412: visibility (missing/not-yours/deleted → 404) is
  decided before the version fence (stale ``version`` → 412); a rename
  collision → 409.
* ``DELETE /projects/{project_id}`` → 204, owner-scoped soft delete (α5b),
  idempotent-by-404 (repeat delete, and GET/PATCH after delete → 404).

The router stays thin: it projects the DTO, delegates to a use case,
and wraps the result in the API_CONTRACT §1.1 envelope. Business rules
(ownership scoping, uniqueness, pagination, the 404-before-412 split)
live in the use cases / repository. Errors raised by the use cases
(``ConflictError`` → 409, ``NotFoundError`` → 404,
``VersionConflictError`` → 412, ``ValidationFailedError`` → 422) are
rendered by the handlers registered in ``app.core.errors``; this module
contains no try / except. See ``docs/api/AUTH_ENDPOINTS.md`` §9 (create/
mutation flow) and the α4 ``PATCH /users/me`` for the version-fence
precedent this PATCH extends to a path-addressed resource.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Query, Request, Response, status
from fastapi.responses import JSONResponse

from app.api.v1.deps import (
    CreateProjectDep,
    CurrentUserDep,
    DeleteProjectDep,
    GetProjectDep,
    ListProjectsDep,
    UpdateProjectDep,
)
from app.api.v1.helpers import client_ip, envelope
from app.api.v1.schemas.projects import (
    ProjectCreateRequest,
    ProjectPublic,
    ProjectUpdateRequest,
)
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


@router.patch("/{project_id}")
async def update_project(
    project_id: UUID,
    body: ProjectUpdateRequest,
    request: Request,
    current_user: CurrentUserDep,
    use_case: UpdateProjectDep,
) -> JSONResponse:
    """Partially update one project owned by the caller (version-fenced).

    The body carries the client's last-observed ``version`` (OCC fence)
    plus any subset of the mutable fields
    (``name``/``description``/``language``/``style``/``settings``).
    Tri-state is resolved here: ``model_dump(exclude_unset=True)`` sends
    only the keys the client actually set (an explicit ``null`` on
    ``description``/``style`` clears the column; an absent field is left
    unchanged), and the ``version`` key is stripped from ``changes``.

    Return semantics (all 200 on success):

    * Persisted change → response ``version`` is incremented by 1;
      ``updated_at`` reflects the DB write.
    * Same-value no-op → ``version``/``updated_at`` unchanged; no DB write.

    Failure modes: 404 (missing / not-yours / soft-deleted — visibility
    before concurrency, α5b D3); 412 ``VERSION_CONFLICT`` (stale
    ``version``, or a concurrent bump/delete race); 409 ``CONFLICT``
    (rename to a name already held by another live project of this owner);
    422 (empty patch, forbidden/mis-typed field, missing ``version``,
    ``null`` for a non-nullable field, non-UUID path) via Pydantic/FastAPI;
    401 via ``CurrentUserDep``.
    """
    # ``exclude_unset`` gives the tri-state ``changes`` mapping; ``version``
    # is the fence, not a mutable field, so it is excluded from ``changes``.
    changes = body.model_dump(exclude_unset=True, exclude={"version"})
    result = await use_case.execute(
        project_id=project_id,
        owner_user_id=current_user.id,
        tenant_id=current_user.tenant_id,
        expected_version=body.version,
        changes=changes,
        ip=client_ip(request),
    )
    return JSONResponse(content=envelope(_to_public(result.project), request))


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(
    project_id: UUID,
    request: Request,
    current_user: CurrentUserDep,
    use_case: DeleteProjectDep,
) -> Response:
    """Soft-delete one project owned by the caller.

    Sets ``deleted_at`` on the caller's own live project and returns
    ``204 No Content`` (α5b D7 — nothing meaningful to return on a soft
    delete; mirrors α2b logout). Idempotent-by-404 (α5b D6): a second
    ``DELETE`` — and any ``GET``/``PATCH`` after delete — returns ``404``.
    Deleting another user's/tenant's project, or an unknown id, is the
    same ``404`` (anti-enumeration). No version fence (α5b D8). Non-UUID
    path → 422; missing/invalid auth → 401.
    """
    await use_case.execute(
        project_id=project_id,
        owner_user_id=current_user.id,
        tenant_id=current_user.tenant_id,
        ip=client_ip(request),
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
