"""``GetPrompt`` use case (Slice α6.1).

Contract (API_CONTRACT §3.4):

    GET /api/v1/projects/{project_id}/prompts/{prompt_id}
      → 200  { data: PromptPublic, meta }
      → 404  { error: { code: NOT_FOUND, ... } }        (project or prompt missing / not yours)
      → 401  { error: { code: UNAUTHENTICATED, ... } }  (via CurrentUserDep)

Two-level visibility gate (α6.1 D2): the project must be owned by the caller
(``projects.get_owned`` → 404), then the prompt must be live and belong to that
project (``prompts.get_owned`` → 404). Both 404s are indistinguishable from
"never existed" (anti-enumeration).
"""

from __future__ import annotations

from uuid import UUID

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.core.errors import NotFoundError
from app.domain.prompts.prompt import Prompt


class GetPrompt:
    """Fetch one prompt under the caller's own project."""

    def __init__(self, uow: IUnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self,
        *,
        project_id: UUID,
        prompt_id: UUID,
        owner_user_id: UUID,
        tenant_id: UUID,
    ) -> Prompt:
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
            prompt = await self._uow.prompts.get_owned(project_id, prompt_id)
            if prompt is None:
                raise NotFoundError(
                    "prompt not found",
                    details={"prompt_id": str(prompt_id)},
                )
        return prompt
