"""``ListMedia`` use case (Slice α6.2).

Contract (API_CONTRACT §3.2.3):

    GET /api/v1/media?kind=&source=&project_id=&scene_id=
      → 200  { data: [MediaPublic, ...] (newest-first), meta }
      → 422  { error: { code: VALIDATION_FAILED, ... } } (bad filter — via DTO)
      → 401  { error: { code: UNAUTHENTICATED, ... } }  (via CurrentUserDep)

Returns the caller's live media assets ordered by ``(created_at, id)`` DESC,
with optional ``kind`` / ``source`` / ``project_id`` / ``scene_id`` filters
(combined = AND, Q10). **Owner-scoped** (``tenant_id`` + ``owner_user_id`` from
``CurrentUserDep``) — there is no project ownership gate because media is an
owner-level artefact (Q1); ``project_id`` is a filter, not a route gate. Not
paginated in α6.2. Read-only and side-effect-free.
"""

from __future__ import annotations

from uuid import UUID

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.domain.media.media_asset import MediaAsset


class ListMedia:
    """List the caller's media assets, newest-first, optionally filtered."""

    def __init__(self, uow: IUnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self,
        *,
        owner_user_id: UUID,
        tenant_id: UUID,
        kind: str | None = None,
        source: str | None = None,
        project_id: UUID | None = None,
        scene_id: UUID | None = None,
    ) -> list[MediaAsset]:
        async with self._uow:
            return await self._uow.media.list_owned(
                tenant_id,
                owner_user_id,
                kind=kind,
                source=source,
                project_id=project_id,
                scene_id=scene_id,
            )
