"""``/api/v1/projects/{project_id}/scenes/*`` HTTP router (α5c).

Six endpoints, all authenticated via :data:`CurrentUserDep` and all nested
under a project path (the storyboard is implicit — α5c D1):

* ``POST   …/scenes``              → 201, append a scene to the project.
* ``GET    …/scenes``              → 200, list the project's scenes ordered.
* ``GET    …/scenes/{scene_id}``   → 200, fetch one scene.
* ``PATCH  …/scenes/{scene_id}``   → 200, partial version-fenced content update.
* ``POST   …/scenes/{scene_id}/move`` → 200, version-fenced reorder (α5c Q1).
* ``DELETE …/scenes/{scene_id}``   → 204, soft delete (idempotent-by-404).

Every handler resolves ``project_id`` from the path prefix and delegates to
a use case, which runs the two-level visibility gate (project ownership →
scene visibility, α5c D6). The router stays thin: DTO projection + envelope;
ordering, OCC, the 404-before-412 split, and the gap/rebalance live in the
use cases / repository. Errors (``NotFoundError`` → 404,
``VersionConflictError`` → 412, ``ValidationFailedError`` → 422) are rendered
by the centralized handlers in ``app.core.errors``; this module has no
try / except.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Request, Response, status
from fastapi.responses import JSONResponse

from app.api.v1.deps import (
    CreateSceneDep,
    CurrentUserDep,
    DeleteSceneDep,
    GetSceneDep,
    ListScenesDep,
    MoveSceneDep,
    UpdateSceneDep,
)
from app.api.v1.helpers import client_ip, envelope
from app.api.v1.schemas.scenes import (
    SceneCreateRequest,
    SceneMoveRequest,
    ScenePublic,
    SceneUpdateRequest,
)
from app.domain.scenes.scene import Scene

router = APIRouter(prefix="/projects/{project_id}/scenes", tags=["scenes"])


def _to_public(scene: Scene, project_id: UUID, position: int) -> ScenePublic:
    """Project a domain ``Scene`` (+ path project + computed position) into the wire DTO."""
    return ScenePublic(
        id=scene.id,
        project_id=project_id,
        position=position,
        title=scene.title,
        duration_seconds=scene.duration_seconds,
        narration=scene.narration,
        subtitle=scene.subtitle,
        created_at=scene.created_at,
        updated_at=scene.updated_at,
        version=scene.version,
    )


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_scene(
    project_id: UUID,
    body: SceneCreateRequest,
    request: Request,
    current_user: CurrentUserDep,
    use_case: CreateSceneDep,
) -> JSONResponse:
    """Append a scene to the caller's project (auto-creating its storyboard).

    Returns 201 with the created ``ScenePublic`` (``version = 1``,
    ``position`` = last). 404 if the project is missing or not the caller's.
    """
    result = await use_case.execute(
        project_id=project_id,
        owner_user_id=current_user.id,
        tenant_id=current_user.tenant_id,
        title=body.title,
        duration_seconds=body.duration_seconds,
        narration=body.narration,
        subtitle=body.subtitle,
        ip=client_ip(request),
    )
    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content=envelope(_to_public(result.scene, project_id, result.position), request),
    )


@router.get("")
async def list_scenes(
    project_id: UUID,
    request: Request,
    current_user: CurrentUserDep,
    use_case: ListScenesDep,
) -> JSONResponse:
    """List the caller's project's scenes, ordered by position (un-paginated).

    404 if the project is missing or not the caller's. ``position`` is the
    dense 1-based index derived from the ordered list.
    """
    scenes = await use_case.execute(
        project_id=project_id,
        owner_user_id=current_user.id,
        tenant_id=current_user.tenant_id,
    )
    return JSONResponse(
        content=envelope(
            [_to_public(scene, project_id, i + 1) for i, scene in enumerate(scenes)],
            request,
        )
    )


@router.get("/{scene_id}")
async def get_scene(
    project_id: UUID,
    scene_id: UUID,
    request: Request,
    current_user: CurrentUserDep,
    use_case: GetSceneDep,
) -> JSONResponse:
    """Fetch one scene under the caller's project.

    A project or scene that is missing, soft-deleted, or not the caller's
    yields a uniform ``404`` (two-level visibility gate, α5c D6).
    """
    result = await use_case.execute(
        project_id=project_id,
        scene_id=scene_id,
        owner_user_id=current_user.id,
        tenant_id=current_user.tenant_id,
    )
    return JSONResponse(
        content=envelope(_to_public(result.scene, project_id, result.position), request)
    )


@router.patch("/{scene_id}")
async def update_scene(
    project_id: UUID,
    scene_id: UUID,
    body: SceneUpdateRequest,
    request: Request,
    current_user: CurrentUserDep,
    use_case: UpdateSceneDep,
) -> JSONResponse:
    """Partially update one scene's content (version-fenced).

    The body carries the client's last-observed ``version`` plus any subset
    of the content fields (``title`` / ``duration_seconds`` / ``narration``
    / ``subtitle``). Tri-state is resolved via ``exclude_unset``; ``version``
    is the fence, excluded from ``changes``. 404 (project/scene not visible),
    412 (stale version / concurrent bump), 422 (empty patch / bad field).
    Reordering is a separate endpoint (``…/move``) — ``position`` is not a
    field here.
    """
    changes = body.model_dump(exclude_unset=True, exclude={"version"})
    result = await use_case.execute(
        project_id=project_id,
        scene_id=scene_id,
        owner_user_id=current_user.id,
        tenant_id=current_user.tenant_id,
        expected_version=body.version,
        changes=changes,
        ip=client_ip(request),
    )
    return JSONResponse(
        content=envelope(_to_public(result.scene, project_id, result.position), request)
    )


@router.post("/{scene_id}/move")
async def move_scene(
    project_id: UUID,
    scene_id: UUID,
    body: SceneMoveRequest,
    request: Request,
    current_user: CurrentUserDep,
    use_case: MoveSceneDep,
) -> JSONResponse:
    """Reorder one scene to a new 1-based position (version-fenced).

    A dedicated domain action (α5c Q1): ``position`` is clamped into range,
    a move to the current slot is a no-op. 404 (not visible), 412 (stale
    version / concurrent content bump), 422 (bad body).
    """
    result = await use_case.execute(
        project_id=project_id,
        scene_id=scene_id,
        owner_user_id=current_user.id,
        tenant_id=current_user.tenant_id,
        expected_version=body.version,
        position=body.position,
        ip=client_ip(request),
    )
    return JSONResponse(
        content=envelope(_to_public(result.scene, project_id, result.position), request)
    )


@router.delete("/{scene_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_scene(
    project_id: UUID,
    scene_id: UUID,
    request: Request,
    current_user: CurrentUserDep,
    use_case: DeleteSceneDep,
) -> Response:
    """Soft-delete one scene under the caller's project.

    Returns ``204``. Idempotent-by-404 (α5c D13): a second ``DELETE`` — and
    any ``GET``/``PATCH``/``move`` after delete — returns ``404``. Deleting a
    scene under another user's project, or an unknown id, is the same ``404``.
    """
    await use_case.execute(
        project_id=project_id,
        scene_id=scene_id,
        owner_user_id=current_user.id,
        tenant_id=current_user.tenant_id,
        ip=client_ip(request),
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
