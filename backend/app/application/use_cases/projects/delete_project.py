"""``DeleteProject`` use case (Slice α5b).

Contract (API_CONTRACT §3.2):

    DELETE /api/v1/projects/{project_id}
      → 204  (no body)                                   (soft-deleted)
      → 404  { error: { code: NOT_FOUND, ... } }         (missing / not yours / already deleted)
      → 401  { error: { code: UNAUTHENTICATED, ... } }   (via CurrentUserDep)

Owner-scoped **soft** delete (α5b D6): the repository sets ``deleted_at =
now()`` on the caller's own live row and reports whether a live owned row
was actually marked. ``False`` (row missing, out-of-scope, or already
soft-deleted) maps to ``NotFoundError`` (404). Therefore the first delete
succeeds and every subsequent delete — plus any GET/PATCH — returns 404
(idempotent-by-404, consistent with the α5a anti-enumeration model).

No version fence (α5b D8): a soft delete is not a partial overwrite, so
requiring the client's ``version`` adds friction without lost-update
safety; the 404-idempotency already makes repeat-delete safe. Soft (not
hard) delete preserves auditability, future restore capability, and
referential integrity for the child aggregates (assets/renders) arriving
in α6/α7.
"""

from __future__ import annotations

from uuid import UUID

import structlog

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.core.errors import NotFoundError

_LOGGER = structlog.get_logger(__name__)


class DeleteProject:
    """Soft-delete the caller's own live project; 404 otherwise."""

    def __init__(self, uow: IUnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self,
        *,
        project_id: UUID,
        owner_user_id: UUID,
        tenant_id: UUID,
        ip: str | None = None,
    ) -> None:
        async with self._uow:
            deleted = await self._uow.projects.soft_delete_owned(
                project_id=project_id,
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
            )
            if not deleted:
                # Missing / out-of-scope / already soft-deleted — all a
                # uniform 404 (α5b D6 idempotent-by-404; α5a D5
                # anti-enumeration). Logged at WARN for operational
                # visibility; the client sees only the generic 404.
                _LOGGER.warning(
                    "project.delete_rejected",
                    reason="not_visible",
                    project_id=str(project_id),
                    owner_user_id=str(owner_user_id),
                    ip=ip,
                )
                raise NotFoundError(
                    "project not found",
                    details={"project_id": str(project_id)},
                )
            await self._uow.commit()

        _LOGGER.info(
            "project.deleted",
            project_id=str(project_id),
            owner_user_id=str(owner_user_id),
            tenant_id=str(tenant_id),
            ip=ip,
        )
