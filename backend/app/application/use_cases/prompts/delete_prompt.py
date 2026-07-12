"""``DeletePrompt`` use case (Slice α6.1).

Contract (API_CONTRACT §3.4):

    DELETE /api/v1/projects/{project_id}/prompts/{prompt_id}
      → 204  (no body)                                   (soft-deleted)
      → 404  { error: { code: NOT_FOUND, ... } }         (project/prompt missing / not yours / already deleted)
      → 401  { error: { code: UNAUTHENTICATED, ... } }   (via CurrentUserDep)

Project-scoped **soft** delete (α6.1 D4, mirroring α5c ``DeleteScene``): after
the project ownership gate (→ 404), ``prompts.soft_delete_owned`` sets
``deleted_at`` on the caller's live prompt and reports whether a live prompt was
marked. ``False`` (missing, another project's prompt, or already soft-deleted)
→ 404, so a repeat delete — and any GET/PATCH after delete — is a uniform 404
(idempotent-by-404). No version fence. Per ADR-0036 (Q1 = A) this does **NOT**
bump ``projects.version``.
"""

from __future__ import annotations

from uuid import UUID

import structlog

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.core.errors import NotFoundError

_LOGGER = structlog.get_logger(__name__)


class DeletePrompt:
    """Soft-delete a prompt under the caller's own project; 404 otherwise."""

    def __init__(self, uow: IUnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self,
        *,
        project_id: UUID,
        prompt_id: UUID,
        owner_user_id: UUID,
        tenant_id: UUID,
        ip: str | None = None,
    ) -> None:
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

            deleted = await self._uow.prompts.soft_delete_owned(project_id, prompt_id)
            if not deleted:
                _LOGGER.warning(
                    "prompt.delete_rejected",
                    reason="not_visible",
                    prompt_id=str(prompt_id),
                    project_id=str(project_id),
                    owner_user_id=str(owner_user_id),
                    ip=ip,
                )
                raise NotFoundError(
                    "prompt not found",
                    details={"prompt_id": str(prompt_id)},
                )
            # No aggregate OCC bump (ADR-0036 / Q1 = A).
            await self._uow.commit()

        _LOGGER.info(
            "prompt.deleted",
            prompt_id=str(prompt_id),
            project_id=str(project_id),
            owner_user_id=str(owner_user_id),
            ip=ip,
        )
