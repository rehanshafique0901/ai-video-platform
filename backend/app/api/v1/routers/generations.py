"""``/api/v1/generations/*`` router — creator-triggered video generation (α9.7 / ADR-0052).

The endpoint the platform's core capability was missing: until this slice, ``GenerateVideo``
could only be invoked from a script. Four endpoints, all authenticated and owner-scoped:

* ``POST /generations``            → 201 (or 200 on idempotent replay), queue a generation.
* ``GET  /generations/{id}``       → 200, poll one generation.
* ``GET  /generations``            → 200, keyset page of the caller's generations, newest first.
* ``POST /generations/{id}/cancel``→ 200, cancel one that has not been claimed yet.

Flat, not project-nested (D5-C): a generation is owned by a *user*, and its association with a
project happens later and explicitly, at promotion. Distinct from ``/workflow-runs``, which
drives the unrelated α7.6 orchestration pipeline.

``POST`` returns in milliseconds — it records intent, it does not generate. Execution belongs to
the generation worker, and progress is observed by polling ``GET`` (D3-A): a run takes minutes,
so sub-second update latency would buy nothing that a poll does not already give deterministically.

The router stays thin: DTO projection, envelope, and the 201/200 split. Ownership, idempotency,
and cancel semantics live in the use cases.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Query, Request, status
from fastapi.responses import JSONResponse

from app.api.v1.deps import (
    CancelGenerationDep,
    CreateGenerationDep,
    CurrentUserDep,
    GetGenerationDep,
    ListGenerationsDep,
)
from app.api.v1.helpers import envelope
from app.api.v1.schemas.generations import GenerationCreateRequest, GenerationPublic
from app.application.interfaces.generation_job_store import GenerationView
from app.application.use_cases.generation.create_generation import resolve_seed
from app.application.use_cases.generation.request_codec import GenerationRequestSpec
from app.core.errors import ValidationFailedError
from app.domain.generation.execution_state import ExecutionStatus
from app.domain.generation.identity import GlobalStyle

router = APIRouter(prefix="/generations", tags=["generations"])

_STATUS_VALUES = {s.value for s in ExecutionStatus}


def _to_public(view: GenerationView) -> GenerationPublic:
    """Project the curated view onto the wire (no runtime internals — ADR-0052 D3)."""
    return GenerationPublic(
        id=view.id,
        status=view.status,
        prompt=view.prompt,
        title=view.title,
        aspect_ratio=view.aspect_ratio,
        target_platform=view.target_platform,
        width=view.width,
        height=view.height,
        fps=view.fps,
        shot_count=view.shot_count,
        shots_accepted=view.shots_accepted,
        duration_seconds=view.duration_seconds,
        failure_reason=view.failure_reason,
        promotable=view.promotable,
        created_at=view.created_at,
        started_at=view.started_at,
        finished_at=view.finished_at,
    )


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_generation(
    body: GenerationCreateRequest,
    request: Request,
    current_user: CurrentUserDep,
    use_case: CreateGenerationDep,
) -> JSONResponse:
    """Queue a generation for the caller.

    Returns ``201`` with a ``queued`` generation, or ``200`` when ``idempotency_key`` replays an
    earlier request — the render-job split. The seed is resolved here and persisted, so a replay
    returns the identical generation rather than a similar one.
    """
    requested_global_style = body.global_style.value if body.global_style is not None else None
    spec = GenerationRequestSpec(
        prompt=body.prompt,
        seed=resolve_seed(body.seed),
        title=body.title,
        execution_mode=body.execution_mode.value,
        global_style=requested_global_style or GlobalStyle.PIXAR.value,
        aspect_ratio=body.aspect_ratio,
        target_platform=body.target_platform,
        target_duration_seconds=body.target_duration_seconds,
        per_shot_seconds=body.per_shot_seconds,
        width=body.width,
        height=body.height,
        fps=body.fps,
    )
    result = await use_case.execute(
        tenant_id=current_user.tenant_id,
        owner_user_id=current_user.id,
        spec=spec,
        identity_id=body.identity_id,
        requested_seed=body.seed,
        requested_global_style=requested_global_style,
        idempotency_key=body.idempotency_key,
    )
    code = status.HTTP_201_CREATED if result.created else status.HTTP_200_OK
    return JSONResponse(status_code=code, content=envelope(_to_public(result.generation), request))


@router.get("/{generation_id}")
async def get_generation(
    generation_id: UUID,
    request: Request,
    current_user: CurrentUserDep,
    use_case: GetGenerationDep,
) -> JSONResponse:
    """Poll one of the caller's generations (uniform ``404`` if missing / not owned)."""
    view = await use_case.execute(
        tenant_id=current_user.tenant_id,
        owner_user_id=current_user.id,
        generation_id=generation_id,
    )
    return JSONResponse(content=envelope(_to_public(view), request))


@router.get("")
async def list_generations(
    request: Request,
    current_user: CurrentUserDep,
    use_case: ListGenerationsDep,
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = Query(default=None),
    generation_status: str | None = Query(default=None, alias="status"),
) -> JSONResponse:
    """List the caller's generations, newest first, keyset-paginated.

    ``cursor`` is the opaque token from a prior ``meta.next_cursor``; a malformed one is a 422.
    ``status`` filters to a single lifecycle state; an unrecognised value is a 422 rather than a
    silently empty page.
    """
    if generation_status is not None and generation_status not in _STATUS_VALUES:
        raise ValidationFailedError(
            "unknown generation status",
            details={"status": generation_status, "allowed": sorted(_STATUS_VALUES)},
        )
    page = await use_case.execute(
        tenant_id=current_user.tenant_id,
        owner_user_id=current_user.id,
        limit=limit,
        cursor_token=cursor,
        status=generation_status,
    )
    return JSONResponse(
        content=envelope([_to_public(v) for v in page.items], request, next_cursor=page.next_cursor)
    )


@router.post("/{generation_id}/cancel")
async def cancel_generation(
    generation_id: UUID,
    request: Request,
    current_user: CurrentUserDep,
    use_case: CancelGenerationDep,
) -> JSONResponse:
    """Cancel a generation that has not been claimed yet.

    ``409`` once a worker has claimed it: mid-run cancellation is a deliberate future capability
    (it needs the pipeline to poll a flag between shots), and reporting success without stopping
    the work — or the spend — would be a lie.
    """
    view = await use_case.execute(
        tenant_id=current_user.tenant_id,
        owner_user_id=current_user.id,
        generation_id=generation_id,
    )
    return JSONResponse(content=envelope(_to_public(view), request))
