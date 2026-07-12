"""``GetMedia`` use case (Slice α6.2).

Contract (API_CONTRACT §3.2.3):

    GET /api/v1/media/{media_id}
      → 200  { data: MediaPublic, meta }
      → 404  { error: { code: NOT_FOUND, ... } }        (missing / not yours / deleted)
      → 401  { error: { code: UNAUTHENTICATED, ... } }  (via CurrentUserDep)

Single **owner** gate (α6.2 D2/Q4): the asset must be live and owned by the
caller (``media.get_owned`` scoped on ``tenant_id`` + ``owner_user_id``). A
missing / soft-deleted / other-owner asset is an indistinguishable ``404``
(anti-enumeration). Unlike prompts/scenes there is no project route gate — media
is an owner-level artefact.
"""

from __future__ import annotations

from uuid import UUID

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.core.errors import NotFoundError
from app.domain.media.media_asset import MediaAsset


class GetMedia:
    """Fetch one media asset owned by the caller."""

    def __init__(self, uow: IUnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self,
        *,
        media_id: UUID,
        owner_user_id: UUID,
        tenant_id: UUID,
    ) -> MediaAsset:
        async with self._uow:
            media = await self._uow.media.get_owned(media_id, tenant_id, owner_user_id)
            if media is None:
                raise NotFoundError(
                    "media asset not found",
                    details={"media_id": str(media_id)},
                )
        return media
