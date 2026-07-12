"""``/api/v1/projects/{project_id}/prompts/*`` HTTP router (α6.1).

Five endpoints, all authenticated via :data:`CurrentUserDep` and all nested
under a project path (two-level ownership gate — α6.1 D2):

* ``POST   …/prompts``               → 201, create a prompt.
* ``GET    …/prompts``               → 200, list the project's prompts (filters).
* ``GET    …/prompts/{prompt_id}``   → 200, fetch one prompt.
* ``PATCH  …/prompts/{prompt_id}``   → 200, partial content update (no OCC).
* ``DELETE …/prompts/{prompt_id}``   → 204, soft delete (idempotent-by-404).

Prompts are **generation inputs** (ADR-0036): there is no ``version`` on the
wire and no ``412`` — a PATCH is last-writer-wins. The router stays thin: DTO
projection + envelope; the two-level 404 gate and scene/model link validation
live in the use cases. Errors (``NotFoundError`` → 404, ``ValidationFailedError``
→ 422) are rendered by the centralized handlers in ``app.core.errors``.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Request, Response, status
from fastapi.responses import JSONResponse

from app.api.v1.deps import (
    CreatePromptDep,
    CurrentUserDep,
    DeletePromptDep,
    GetPromptDep,
    ListPromptsDep,
    UpdatePromptDep,
)
from app.api.v1.helpers import client_ip, envelope
from app.api.v1.schemas.prompts import (
    PromptCreateRequest,
    PromptKind,
    PromptPublic,
    PromptUpdateRequest,
)
from app.domain.prompts.prompt import Prompt

router = APIRouter(prefix="/projects/{project_id}/prompts", tags=["prompts"])


def _to_public(prompt: Prompt) -> PromptPublic:
    """Project a domain ``Prompt`` into the wire DTO."""
    return PromptPublic(
        id=prompt.id,
        project_id=prompt.project_id,
        scene_id=prompt.scene_id,
        kind=prompt.kind,
        text_content=prompt.text_content,
        model_id=prompt.model_id,
        extra=prompt.extra,
        created_at=prompt.created_at,
        updated_at=prompt.updated_at,
    )


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_prompt(
    project_id: UUID,
    body: PromptCreateRequest,
    request: Request,
    current_user: CurrentUserDep,
    use_case: CreatePromptDep,
) -> JSONResponse:
    """Create a prompt under the caller's project.

    Returns 201 with the created ``PromptPublic``. 404 if the project is
    missing or not the caller's; 422 for a foreign/unknown ``scene_id`` or an
    unknown/retired ``model_id``.
    """
    prompt = await use_case.execute(
        project_id=project_id,
        owner_user_id=current_user.id,
        tenant_id=current_user.tenant_id,
        kind=body.kind,
        text_content=body.text_content,
        scene_id=body.scene_id,
        model_id=body.model_id,
        extra=body.extra,
        ip=client_ip(request),
    )
    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content=envelope(_to_public(prompt), request),
    )


@router.get("")
async def list_prompts(
    project_id: UUID,
    request: Request,
    current_user: CurrentUserDep,
    use_case: ListPromptsDep,
    kind: PromptKind | None = None,
    scene_id: UUID | None = None,
) -> JSONResponse:
    """List the caller's project's prompts, newest-first, optionally filtered.

    ``?kind=`` (enum) and ``?scene_id=`` (uuid) narrow the result (combined =
    AND); a bad enum / non-UUID is a 422. 404 if the project is missing or not
    the caller's. Empty → ``200 []``.
    """
    prompts = await use_case.execute(
        project_id=project_id,
        owner_user_id=current_user.id,
        tenant_id=current_user.tenant_id,
        kind=kind,
        scene_id=scene_id,
    )
    return JSONResponse(content=envelope([_to_public(p) for p in prompts], request))


@router.get("/{prompt_id}")
async def get_prompt(
    project_id: UUID,
    prompt_id: UUID,
    request: Request,
    current_user: CurrentUserDep,
    use_case: GetPromptDep,
) -> JSONResponse:
    """Fetch one prompt under the caller's project.

    A project or prompt that is missing, soft-deleted, or not the caller's
    yields a uniform ``404`` (two-level visibility gate, α6.1 D2).
    """
    prompt = await use_case.execute(
        project_id=project_id,
        prompt_id=prompt_id,
        owner_user_id=current_user.id,
        tenant_id=current_user.tenant_id,
    )
    return JSONResponse(content=envelope(_to_public(prompt), request))


@router.patch("/{prompt_id}")
async def update_prompt(
    project_id: UUID,
    prompt_id: UUID,
    body: PromptUpdateRequest,
    request: Request,
    current_user: CurrentUserDep,
    use_case: UpdatePromptDep,
) -> JSONResponse:
    """Partially update one prompt's content (no version fence).

    The body carries any subset of the mutable fields (``text_content`` /
    ``kind`` / ``model_id`` / ``extra``). Tri-state is resolved via
    ``exclude_unset``; an explicit ``model_id: null`` clears the link. 404
    (project/prompt not visible), 422 (empty patch / bad field / unknown
    model). No ``412`` — prompts are last-writer-wins (ADR-0036).
    """
    changes = body.model_dump(exclude_unset=True)
    prompt = await use_case.execute(
        project_id=project_id,
        prompt_id=prompt_id,
        owner_user_id=current_user.id,
        tenant_id=current_user.tenant_id,
        changes=changes,
        ip=client_ip(request),
    )
    return JSONResponse(content=envelope(_to_public(prompt), request))


@router.delete("/{prompt_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_prompt(
    project_id: UUID,
    prompt_id: UUID,
    request: Request,
    current_user: CurrentUserDep,
    use_case: DeletePromptDep,
) -> Response:
    """Soft-delete one prompt under the caller's project.

    Returns ``204``. Idempotent-by-404 (α6.1 D4): a second ``DELETE`` — and any
    ``GET``/``PATCH`` after delete — returns ``404``. Deleting a prompt under
    another user's project, or an unknown id, is the same ``404``.
    """
    await use_case.execute(
        project_id=project_id,
        prompt_id=prompt_id,
        owner_user_id=current_user.id,
        tenant_id=current_user.tenant_id,
        ip=client_ip(request),
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
