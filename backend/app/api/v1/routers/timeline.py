"""``/api/v1/projects/{project_id}/timeline/*`` HTTP router (α6.3a).

The Timeline aggregate is **nested under a project** (Q4 — the ownership model
stays obvious) and is a **self-contained OCC aggregate** (ADR-0038):
``timelines.version`` is the single token fencing the whole tree. α6.3a ships the
timeline root + tracks (clips are α6.3b):

* ``POST   …/timeline``                    → 201, provision the single timeline.
* ``GET    …/timeline``                    → 200, timeline + ordered tracks.
* ``PATCH  …/timeline``                    → 200, version-fenced root update.
* ``POST   …/timeline/tracks``             → 201, add a track (version optional).
* ``GET    …/timeline/tracks``             → 200, list tracks (z_index ASC).
* ``PATCH  …/timeline/tracks/{track_id}``  → 200, version-fenced track update.
* ``DELETE …/timeline/tracks/{track_id}``  → 204, version-fenced soft delete.

Every handler resolves ``project_id`` from the path and delegates to a use case,
which runs the two-level gate (project ownership → timeline resolution) and, for
child mutations, advances ``timelines.version``. The child (track) OCC token
travels in the response ``meta`` as ``timeline_version``. The router stays thin:
DTO projection + envelope; errors (``NotFoundError`` → 404, ``ConflictError`` →
409, ``VersionConflictError`` → 412, ``ValidationFailedError`` → 422) are rendered
by the centralized handlers in ``app.core.errors``.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Query, Request, Response, status
from fastapi.responses import JSONResponse

from app.api.v1.deps import (
    CreateTrackDep,
    CurrentUserDep,
    DeleteTrackDep,
    GetTimelineDep,
    ListTracksDep,
    ProvisionTimelineDep,
    UpdateTimelineDep,
    UpdateTrackDep,
)
from app.api.v1.helpers import client_ip, envelope
from app.api.v1.schemas.timeline import (
    TimelineProvisionRequest,
    TimelinePublic,
    TimelineUpdateRequest,
    TrackCreateRequest,
    TrackPublic,
    TrackUpdateRequest,
)
from app.application.use_cases.timeline.results import TimelineResult
from app.domain.timeline.track import Track

router = APIRouter(prefix="/projects/{project_id}/timeline", tags=["timeline"])


def _track_to_public(track: Track) -> TrackPublic:
    """Project a domain ``Track`` into the wire DTO."""
    return TrackPublic(
        id=track.id,
        timeline_id=track.timeline_id,
        kind=track.kind,
        z_index=track.z_index,
        name=track.name,
        locked=track.locked,
        muted=track.muted,
        created_at=track.created_at,
        updated_at=track.updated_at,
    )


def _timeline_to_public(result: TimelineResult) -> TimelinePublic:
    """Project a ``TimelineResult`` (root + ordered tracks) into the wire DTO."""
    timeline = result.timeline
    return TimelinePublic(
        id=timeline.id,
        project_id=timeline.project_id,
        project_version_id=timeline.project_version_id,
        aspect_ratio=timeline.aspect_ratio,
        frame_rate=timeline.frame_rate,
        background_color=timeline.background_color,
        duration_seconds=timeline.duration_seconds,
        version=timeline.version,
        created_at=timeline.created_at,
        updated_at=timeline.updated_at,
        tracks=[_track_to_public(t) for t in result.tracks],
    )


# ---- timeline root ----------------------------------------------------


@router.post("", status_code=status.HTTP_201_CREATED)
async def provision_timeline(
    project_id: UUID,
    body: TimelineProvisionRequest,
    request: Request,
    current_user: CurrentUserDep,
    use_case: ProvisionTimelineDep,
) -> JSONResponse:
    """Create the caller's project timeline (one per project).

    Returns 201 with the ``TimelinePublic`` (``version = 1``, ``tracks = []``).
    404 if the project is missing/not the caller's; 409 if a timeline already
    exists. ``aspect_ratio`` defaults from the project orientation when omitted.
    """
    result = await use_case.execute(
        project_id=project_id,
        owner_user_id=current_user.id,
        tenant_id=current_user.tenant_id,
        aspect_ratio=body.aspect_ratio,
        frame_rate=body.frame_rate,
        background_color=body.background_color,
        ip=client_ip(request),
    )
    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content=envelope(_timeline_to_public(result), request),
    )


@router.get("")
async def get_timeline(
    project_id: UUID,
    request: Request,
    current_user: CurrentUserDep,
    use_case: GetTimelineDep,
) -> JSONResponse:
    """Fetch the caller's project timeline with its ordered tracks.

    404 if the project or its timeline is missing/not the caller's.
    """
    result = await use_case.execute(
        project_id=project_id,
        owner_user_id=current_user.id,
        tenant_id=current_user.tenant_id,
    )
    return JSONResponse(content=envelope(_timeline_to_public(result), request))


@router.patch("")
async def update_timeline(
    project_id: UUID,
    body: TimelineUpdateRequest,
    request: Request,
    current_user: CurrentUserDep,
    use_case: UpdateTimelineDep,
) -> JSONResponse:
    """Partially update the timeline root (version-fenced).

    The body carries the aggregate ``version`` plus any subset of the mutable
    root fields. 404 (project/timeline not visible), 412 (stale version), 422
    (empty patch / bad field).
    """
    changes = body.model_dump(exclude_unset=True, exclude={"version"})
    result = await use_case.execute(
        project_id=project_id,
        owner_user_id=current_user.id,
        tenant_id=current_user.tenant_id,
        expected_version=body.version,
        changes=changes,
        ip=client_ip(request),
    )
    return JSONResponse(content=envelope(_timeline_to_public(result), request))


# ---- tracks -----------------------------------------------------------


@router.post("/tracks", status_code=status.HTTP_201_CREATED)
async def create_track(
    project_id: UUID,
    body: TrackCreateRequest,
    request: Request,
    current_user: CurrentUserDep,
    use_case: CreateTrackDep,
) -> JSONResponse:
    """Append a track to the caller's project timeline.

    ``version`` is optional (a child create cannot be harmfully stale). Returns
    201 with the ``TrackPublic`` and the new aggregate token in
    ``meta.timeline_version``. 404 (project/timeline not visible), 409 (z_index
    already used), 412 (stale version, if sent).
    """
    result = await use_case.execute(
        project_id=project_id,
        owner_user_id=current_user.id,
        tenant_id=current_user.tenant_id,
        kind=body.kind,
        z_index=body.z_index,
        name=body.name,
        locked=body.locked,
        muted=body.muted,
        expected_version=body.version,
        ip=client_ip(request),
    )
    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content=envelope(
            _track_to_public(result.track),
            request,
            extra_meta={"timeline_version": result.timeline_version},
        ),
    )


@router.get("/tracks")
async def list_tracks(
    project_id: UUID,
    request: Request,
    current_user: CurrentUserDep,
    use_case: ListTracksDep,
) -> JSONResponse:
    """List the caller's project-timeline tracks, ordered by ``z_index``.

    404 if the project or its timeline is missing/not the caller's. The aggregate
    token is surfaced in ``meta.timeline_version``.
    """
    result = await use_case.execute(
        project_id=project_id,
        owner_user_id=current_user.id,
        tenant_id=current_user.tenant_id,
    )
    return JSONResponse(
        content=envelope(
            [_track_to_public(t) for t in result.tracks],
            request,
            extra_meta={"timeline_version": result.timeline.version},
        )
    )


@router.patch("/tracks/{track_id}")
async def update_track(
    project_id: UUID,
    track_id: UUID,
    body: TrackUpdateRequest,
    request: Request,
    current_user: CurrentUserDep,
    use_case: UpdateTrackDep,
) -> JSONResponse:
    """Partially update one track (fenced on the timeline version).

    The body carries the timeline's ``version`` plus any subset of the mutable
    track fields. 404 (project/timeline/track not visible), 409 (z_index
    collision), 412 (stale version), 422 (empty patch / bad field). The new token
    is returned in ``meta.timeline_version``.
    """
    changes = body.model_dump(exclude_unset=True, exclude={"version"})
    result = await use_case.execute(
        project_id=project_id,
        track_id=track_id,
        owner_user_id=current_user.id,
        tenant_id=current_user.tenant_id,
        expected_version=body.version,
        changes=changes,
        ip=client_ip(request),
    )
    return JSONResponse(
        content=envelope(
            _track_to_public(result.track),
            request,
            extra_meta={"timeline_version": result.timeline_version},
        )
    )


@router.delete("/tracks/{track_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_track(
    project_id: UUID,
    track_id: UUID,
    request: Request,
    current_user: CurrentUserDep,
    use_case: DeleteTrackDep,
    version: int = Query(ge=1),
) -> Response:
    """Soft-delete one track (fenced on the timeline version).

    The expected timeline ``version`` is a **required** query parameter (Q13).
    Returns 204. Idempotent-by-404 (404-before-412): a missing/already-deleted
    track is 404 regardless of the token; only a live track with a stale token is
    412. Deleting a track under another user's project is the same 404.
    """
    await use_case.execute(
        project_id=project_id,
        track_id=track_id,
        owner_user_id=current_user.id,
        tenant_id=current_user.tenant_id,
        expected_version=version,
        ip=client_ip(request),
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
