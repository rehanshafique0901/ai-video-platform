"""``/api/v1/projects/{project_id}/versions/*`` HTTP router (α5d.1).

Three endpoints, all authenticated via :data:`CurrentUserDep` and all nested
under a project path:

* ``POST …/versions``               → 201, capture an immutable snapshot.
* ``GET  …/versions``               → 200, list version metadata (newest first).
* ``GET  …/versions/{version_id}``  → 200, read one version WITH its snapshot.

Every handler resolves ``project_id`` from the path prefix and delegates to a
use case, which runs the project ownership gate (→ 404) before touching the
append-only ledger. The router stays thin: DTO projection + envelope; the
lock, numbering, lineage, and current-pointer advance live in the repository.
Errors (``NotFoundError`` → 404) are rendered by the centralized handlers in
``app.core.errors``; this module has no try / except.

Response shapes (α5d Q4): the LIST returns metadata-only
:class:`ProjectVersionPublic`; create + single GET return the full
:class:`ProjectVersionDetail` (metadata + snapshot). Versions are addressed by
UUID ``id`` (α5d Q3).
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse

from app.api.v1.deps import (
    CreateProjectVersionDep,
    CurrentUserDep,
    DiffProjectVersionsDep,
    GetProjectVersionDep,
    ListProjectVersionsDep,
    RestoreProjectVersionDep,
)
from app.api.v1.helpers import client_ip, envelope
from app.api.v1.schemas.versions import (
    ProjectVersionCreateRequest,
    ProjectVersionDetail,
    ProjectVersionDiff,
    ProjectVersionPublic,
    ProjectVersionRestoreRequest,
    SceneChangeCounts,
)
from app.application.use_cases.versions.results import VersionDiffResult
from app.domain.versions.project_version import ProjectVersion, ProjectVersionSummary

router = APIRouter(prefix="/projects/{project_id}/versions", tags=["versions"])


def _to_public(
    summary: ProjectVersionSummary, current_version_id: UUID | None
) -> ProjectVersionPublic:
    """Project a version-summary read model into the metadata wire DTO."""
    return ProjectVersionPublic(
        id=summary.id,
        project_id=summary.project_id,
        version_number=summary.version_number,
        reason=summary.reason,
        parent_version_id=summary.parent_version_id,
        created_by_user_id=summary.created_by_user_id,
        created_at=summary.created_at,
        is_current=summary.id == current_version_id,
    )


def _to_detail(version: ProjectVersion, current_version_id: UUID | None) -> ProjectVersionDetail:
    """Project a full domain ``ProjectVersion`` (with snapshot) into the wire DTO."""
    return ProjectVersionDetail(
        id=version.id,
        project_id=version.project_id,
        version_number=version.version_number,
        reason=version.reason,
        parent_version_id=version.parent_version_id,
        created_by_user_id=version.created_by_user_id,
        created_at=version.created_at,
        is_current=version.id == current_version_id,
        snapshot=version.snapshot,
        diff_summary=version.diff_summary,
    )


def _to_diff(result: VersionDiffResult) -> ProjectVersionDiff:
    """Project a ``VersionDiffResult`` into the coarse diff wire DTO (α5d.2 §7)."""
    return ProjectVersionDiff(
        base_version_number=result.base_version_number,
        target_version_number=result.target_version_number,
        project_changed=result.project_changed,
        scene_changes=SceneChangeCounts(
            added=result.scenes_added,
            removed=result.scenes_removed,
            modified=result.scenes_modified,
        ),
    )


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_version(
    project_id: UUID,
    request: Request,
    current_user: CurrentUserDep,
    use_case: CreateProjectVersionDep,
    body: ProjectVersionCreateRequest | None = None,
) -> JSONResponse:
    """Capture an immutable snapshot of the caller's project.

    Returns 201 with the created ``ProjectVersionDetail`` (next
    ``version_number``, ``reason = manual_save``, snapshot included). 404 if
    the project is missing or not the caller's. The body carries no fields in
    α5d.1 (``reason`` is server-set); any supplied key is a 422.
    """
    result = await use_case.execute(
        project_id=project_id,
        owner_user_id=current_user.id,
        tenant_id=current_user.tenant_id,
        ip=client_ip(request),
    )
    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content=envelope(_to_detail(result.version, result.current_version_id), request),
    )


@router.get("")
async def list_versions(
    project_id: UUID,
    request: Request,
    current_user: CurrentUserDep,
    use_case: ListProjectVersionsDep,
) -> JSONResponse:
    """List the caller's project's version history (metadata, newest first).

    404 if the project is missing or not the caller's. Snapshot bodies are
    omitted (α5d Q4); fetch a single version to read its snapshot.
    """
    result = await use_case.execute(
        project_id=project_id,
        owner_user_id=current_user.id,
        tenant_id=current_user.tenant_id,
    )
    return JSONResponse(
        content=envelope(
            [_to_public(v, result.current_version_id) for v in result.versions],
            request,
        )
    )


@router.get("/{version_id}")
async def get_version(
    project_id: UUID,
    version_id: UUID,
    request: Request,
    current_user: CurrentUserDep,
    use_case: GetProjectVersionDep,
) -> JSONResponse:
    """Read one version (with its immutable snapshot) under the caller's project.

    A project or version that is missing or not the caller's yields a uniform
    ``404`` (two-level visibility gate). Addressed by UUID ``id`` (α5d Q3).
    """
    result = await use_case.execute(
        project_id=project_id,
        version_id=version_id,
        owner_user_id=current_user.id,
        tenant_id=current_user.tenant_id,
    )
    return JSONResponse(
        content=envelope(_to_detail(result.version, result.current_version_id), request)
    )


@router.post("/{version_id}/restore")
async def restore_version(
    project_id: UUID,
    version_id: UUID,
    body: ProjectVersionRestoreRequest,
    request: Request,
    current_user: CurrentUserDep,
    use_case: RestoreProjectVersionDep,
) -> JSONResponse:
    """Restore a historical snapshot into the caller's project's live state.

    Appends a new ``reason=restore`` version and repoints the current pointer
    (history is never rewritten — ADR-0035 D2). Version-fenced on the
    **aggregate** ``projects.version`` (§4 Aggregate OCC Rule): ``body.version``
    is the token the client last observed. Returns ``200`` with the new head as
    ``ProjectVersionDetail``. ``404`` project/version gate (two-level, before
    the fence); ``412`` on a stale fence (no writes); ``422`` bad body.
    """
    result = await use_case.execute(
        project_id=project_id,
        version_id=version_id,
        owner_user_id=current_user.id,
        tenant_id=current_user.tenant_id,
        expected_version=body.version,
        ip=client_ip(request),
    )
    return JSONResponse(
        content=envelope(_to_detail(result.version, result.current_version_id), request)
    )


@router.get("/{version_id}/diff")
async def diff_versions(
    project_id: UUID,
    version_id: UUID,
    against: UUID,
    request: Request,
    current_user: CurrentUserDep,
    use_case: DiffProjectVersionsDep,
) -> JSONResponse:
    """Coarse change summary between two versions (base ``against`` → target path).

    Computed on demand from the two stored snapshots (nothing persisted). Both
    versions must belong to the caller's owned project (two-level gate on each
    → ``404``); ``against`` is a required query param and a malformed UUID is a
    ``422``. Returns ``200`` with ``ProjectVersionDiff`` (Q8/Q9).
    """
    result = await use_case.execute(
        project_id=project_id,
        version_id=version_id,
        against_version_id=against,
        owner_user_id=current_user.id,
        tenant_id=current_user.tenant_id,
    )
    return JSONResponse(content=envelope(_to_diff(result), request))
