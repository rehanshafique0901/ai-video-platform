"""``DeleteMedia`` use case (Slice α6.2).

Contract (API_CONTRACT §3.2.3):

    DELETE /api/v1/media/{media_id}
      → 204  (no body)                                   (soft-deleted)
      → 404  { error: { code: NOT_FOUND, ... } }         (missing / not yours / already deleted)
      → 401  { error: { code: UNAUTHENTICATED, ... } }   (via CurrentUserDep)

Owner-scoped **soft** delete (α6.2 D4, mirroring α6.1 ``DeletePrompt``):
``media.soft_delete_owned`` sets ``deleted_at`` on the caller's live asset and
reports whether a live row was marked. ``False`` (missing, another owner's
asset, or already soft-deleted) → ``404``, so a repeat delete — and any
GET/PATCH after delete — is a uniform ``404`` (idempotent-by-404). No version
fence. Per ADR-0037 this does **NOT** bump ``projects.version``. Soft-delete
trips none of the downstream ``SET NULL`` FKs (they fire on hard delete only).
"""

from __future__ import annotations

from uuid import UUID

import structlog

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.core.errors import NotFoundError

_LOGGER = structlog.get_logger(__name__)


class DeleteMedia:
    """Soft-delete a media asset owned by the caller; 404 otherwise."""

    def __init__(self, uow: IUnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self,
        *,
        media_id: UUID,
        owner_user_id: UUID,
        tenant_id: UUID,
        ip: str | None = None,
    ) -> None:
        async with self._uow:
            deleted = await self._uow.media.soft_delete_owned(media_id, tenant_id, owner_user_id)
            if not deleted:
                _LOGGER.warning(
                    "media.delete_rejected",
                    reason="not_visible",
                    media_id=str(media_id),
                    owner_user_id=str(owner_user_id),
                    ip=ip,
                )
                raise NotFoundError(
                    "media asset not found",
                    details={"media_id": str(media_id)},
                )
            # No aggregate OCC bump (ADR-0037).
            await self._uow.commit()

        _LOGGER.info(
            "media.deleted",
            media_id=str(media_id),
            owner_user_id=str(owner_user_id),
            ip=ip,
        )
