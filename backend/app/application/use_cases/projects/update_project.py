"""``UpdateProject`` use case (Slice α5b).

Contract (API_CONTRACT §3.2):

    PATCH /api/v1/projects/{project_id}
      body:  { version, name?, description?, language?, style?, settings? }
      → 200  { data: ProjectPublic (version incremented on real change), meta }
      → 404  { error: { code: NOT_FOUND, ... } }         (missing / not yours / deleted)
      → 412  { error: { code: VERSION_CONFLICT, ... } }  (stale version)
      → 409  { error: { code: CONFLICT, ... } }          (rename collides w/ live name)
      → 422  { error: { code: VALIDATION_FAILED, ... } } (via DTO)
      → 401  { error: { code: UNAUTHENTICATED, ... } }   (via CurrentUserDep)

**404-before-412 — the α5b pattern (pre-flight §D3).** Unlike α4's
``PATCH /users/me`` (whose target is always "self", so "not found"
collapses to 412), a project is addressed by a path ``id`` that may
belong to no-one visible to the caller. This use case therefore
establishes *visibility* before *concurrency*:

    1. ``get_owned(id, tenant, owner)`` → ``None`` → ``NotFoundError``
       (404). Missing / out-of-scope / soft-deleted are indistinguishable
       (α5a D5 anti-enumeration) — a caller can NEVER learn a project
       exists via a 412.
    2. Fetched ``version`` != caller's ``expected_version`` →
       ``VersionConflictError`` (412).
    3. No field actually changes value → same-value no-op: return the
       unchanged entity (200), no write, ``version`` unchanged (mirrors α4
       §D6a).
    4. Real change → CAS ``update_owned(..., expected_version, changes)``.
       A ``None`` return means a concurrent writer bumped ``version`` or
       soft-deleted the row between steps 1–4 → ``VersionConflictError``
       (412). The benign delete-race resolving to 412 rather than 404 is
       accepted (the state genuinely changed under the caller; "retry with
       a fresh read" is the correct client action).

``changes`` is a tri-state mapping built by the router from the DTO's
``model_fields_set`` (absent field → not a key; explicit ``null`` → key
with value ``None`` clearing a nullable column). This use case owns the
same-value-no-op filtering; the repository CAS only ever sees columns
whose value truly differs.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import structlog

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.core.errors import NotFoundError, VersionConflictError
from app.domain.projects.project import Project

_LOGGER = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class UpdateProjectResult:
    """Outcome of a successful ``UpdateProject.execute``.

    ``changed`` distinguishes the two success paths (both 200):

    * ``True`` — the persisted row was updated; ``project.version`` is one
      greater than ``expected_version`` and ``updated_at`` was bumped.
    * ``False`` — same-value no-op; ``project.version`` equals
      ``expected_version`` and ``updated_at`` is unchanged. The wire
      response is byte-identical to ``changed=True``; only the server-side
      log records the distinction. The API layer does not inspect this
      field — it returns ``project`` either way.
    """

    project: Project
    changed: bool


class UpdateProject:
    """Version-fenced partial update of the caller's own project (404-before-412)."""

    def __init__(self, uow: IUnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self,
        *,
        project_id: UUID,
        owner_user_id: UUID,
        tenant_id: UUID,
        expected_version: int,
        changes: Mapping[str, Any],
        ip: str | None = None,
    ) -> UpdateProjectResult:
        async with self._uow:
            # Step 1 — visibility (α5b D3). None = missing / out-of-scope /
            # soft-deleted, all a uniform 404 (never a 412 — no existence
            # leak).
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

            # Step 2 — version fence. The row IS visible; a stale version is
            # a genuine 412 (the caller's optimistic view is behind).
            if project.version != expected_version:
                _LOGGER.warning(
                    "project.update_rejected",
                    reason="version_mismatch",
                    project_id=str(project_id),
                    owner_user_id=str(owner_user_id),
                    expected_version=expected_version,
                    ip=ip,
                )
                raise VersionConflictError("Resource has been modified.")

            # Step 3 — same-value no-op. Keep only fields whose value truly
            # differs from what's persisted (α5b D3 step 3 / α4 §D6a). An
            # empty effective set means the client asked us to store what we
            # already have: no write, no version bump, return the current
            # row. (``settings`` compares by dict equality — whole-object.)
            effective = {k: v for k, v in changes.items() if getattr(project, k) != v}
            if not effective:
                _LOGGER.info(
                    "project.update_rejected",
                    reason="same_value_noop",
                    project_id=str(project.id),
                    owner_user_id=str(owner_user_id),
                    ip=ip,
                )
                return UpdateProjectResult(project=project, changed=False)

            # Step 4 — real change → CAS. None = concurrent bump/delete
            # after step 1 → 412 (accepted delete-race; D3). A rename
            # collision raises ConflictError (→ 409) and propagates.
            updated = await self._uow.projects.update_owned(
                project_id=project_id,
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                expected_version=expected_version,
                changes=effective,
            )
            if updated is None:
                _LOGGER.warning(
                    "project.update_rejected",
                    reason="version_mismatch",
                    project_id=str(project_id),
                    owner_user_id=str(owner_user_id),
                    expected_version=expected_version,
                    ip=ip,
                )
                raise VersionConflictError("Resource has been modified.")

            await self._uow.commit()

        _LOGGER.info(
            "project.updated",
            project_id=str(updated.id),
            owner_user_id=str(updated.owner_user_id),
            tenant_id=str(updated.tenant_id),
            changed_fields=sorted(effective.keys()),
            previous_version=expected_version,
            new_version=updated.version,
            ip=ip,
        )
        return UpdateProjectResult(project=updated, changed=True)
