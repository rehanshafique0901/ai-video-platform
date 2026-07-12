"""``ListPrompts`` use case (Slice α6.1).

Contract (API_CONTRACT §3.4):

    GET /api/v1/projects/{project_id}/prompts?kind=&scene_id=
      → 200  { data: [PromptPublic, ...] (newest-first), meta }
      → 404  { error: { code: NOT_FOUND, ... } }        (project missing / not yours)
      → 422  { error: { code: VALIDATION_FAILED, ... } } (bad ?kind / ?scene_id — via DTO)
      → 401  { error: { code: UNAUTHENTICATED, ... } }  (via CurrentUserDep)

Returns the caller's project's live prompts ordered by ``(created_at, id)``
DESC, with optional ``kind`` / ``scene_id`` filters (combined = AND, Q9). Not
paginated in α6.1. Read-only and side-effect-free — no lazy parent creation
(prompts have no implicit storyboard-style parent). Empty project → ``[]``.
"""

from __future__ import annotations

from uuid import UUID

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.core.errors import NotFoundError
from app.domain.prompts.prompt import Prompt


class ListPrompts:
    """List the caller's project's prompts, newest-first, optionally filtered."""

    def __init__(self, uow: IUnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self,
        *,
        project_id: UUID,
        owner_user_id: UUID,
        tenant_id: UUID,
        kind: str | None = None,
        scene_id: UUID | None = None,
    ) -> list[Prompt]:
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
            return await self._uow.prompts.list_owned(project_id, kind=kind, scene_id=scene_id)
