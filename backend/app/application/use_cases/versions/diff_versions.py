"""``DiffProjectVersions`` use case (Slice α5d.2).

Contract (API_CONTRACT §3.3):

    GET /api/v1/projects/{project_id}/versions/{version_id}/diff?against={other}
      → 200  { data: ProjectVersionDiff, meta }
      → 404  { error: { code: NOT_FOUND, ... } }         (project or either version missing / not yours)
      → 422  { error: { code: VALIDATION_FAILED, ... } } (missing / malformed ``against``)
      → 401  { error: { code: UNAUTHENTICATED, ... } }   (via CurrentUserDep)

Compares two versions and reports what changed, computed **on demand** from
the two stored snapshots — nothing is persisted (the α5d.1 ``diff_summary``
column stays ``null``). ``target`` is the path ``version_id``; ``base`` is the
``against`` query version (Q8: changes are base → target). Two-level gate on
**both** versions (each must belong to the caller's owned project → 404, Q9).

The diff is a pure function of the two snapshots, so it lives here (not in the
repository): the use case already fetches both via ``versions.get_owned`` for
the ownership gate, and computing the coarse summary from them needs no further
persistence access. Field-level detail is deferred (α5d.3+).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.application.use_cases.versions.results import VersionDiffResult
from app.core.errors import NotFoundError

# Project business columns compared for ``project_changed`` — everything in the
# snapshot's project block EXCEPT the ``id`` (stable) and ``version`` (the OCC
# token, which always differs between two captures and is not "content").
_PROJECT_BUSINESS_KEYS: tuple[str, ...] = (
    "name",
    "description",
    "aspect_ratio",
    "duration_seconds",
    "language",
    "style",
    "settings",
)


def _project_changed(base: dict[str, Any], target: dict[str, Any]) -> bool:
    """True if any project business column differs between the two snapshots."""
    return any(base.get(k) != target.get(k) for k in _PROJECT_BUSINESS_KEYS)


def _scene_counts(
    base_scenes: list[dict[str, Any]], target_scenes: list[dict[str, Any]]
) -> tuple[int, int, int]:
    """Return ``(added, removed, modified)`` scene counts, keyed on scene ``id``.

    ``added`` = ids only in target, ``removed`` = ids only in base, ``modified``
    = ids in both whose captured scene dicts differ (any column — including
    ``scene_number``, so a reorder counts as a modification).
    """
    base_by_id = {s["id"]: s for s in base_scenes}
    target_by_id = {s["id"]: s for s in target_scenes}
    base_ids = set(base_by_id)
    target_ids = set(target_by_id)
    added = len(target_ids - base_ids)
    removed = len(base_ids - target_ids)
    modified = sum(1 for sid in base_ids & target_ids if base_by_id[sid] != target_by_id[sid])
    return added, removed, modified


class DiffProjectVersions:
    """Coarse base→target diff of two versions under the caller's own project."""

    def __init__(self, uow: IUnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self,
        *,
        project_id: UUID,
        version_id: UUID,
        against_version_id: UUID,
        owner_user_id: UUID,
        tenant_id: UUID,
    ) -> VersionDiffResult:
        async with self._uow:
            project = await self._uow.projects.get_owned(
                project_id=project_id,
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
            )
            if project is None:
                raise NotFoundError(
                    "project not found",
                    details={"project_id": str(project_id)},
                )

            target = await self._uow.versions.get_owned(project_id, version_id)
            if target is None:
                raise NotFoundError(
                    "version not found",
                    details={"version_id": str(version_id)},
                )
            base = await self._uow.versions.get_owned(project_id, against_version_id)
            if base is None:
                raise NotFoundError(
                    "version not found",
                    details={"version_id": str(against_version_id)},
                )

        base_snap = base.snapshot
        target_snap = target.snapshot
        added, removed, modified = _scene_counts(
            base_snap.get("scenes", []), target_snap.get("scenes", [])
        )
        return VersionDiffResult(
            base_version_number=base.version_number,
            target_version_number=target.version_number,
            project_changed=_project_changed(base_snap["project"], target_snap["project"]),
            scenes_added=added,
            scenes_removed=removed,
            scenes_modified=modified,
        )
