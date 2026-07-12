"""``UpdatePrompt`` use case (Slice α6.1).

Contract (API_CONTRACT §3.4):

    PATCH /api/v1/projects/{project_id}/prompts/{prompt_id}
      body:  { text_content?, kind?, model_id?, extra? }   (tri-state)
      → 200  { data: PromptPublic (updated_at advanced on real change), meta }
      → 404  { error: { code: NOT_FOUND, ... } }         (project/prompt missing / not yours)
      → 422  { error: { code: VALIDATION_FAILED, ... } } (bad body OR bad model_id)
      → 401  { error: { code: UNAUTHENTICATED, ... } }   (via CurrentUserDep)

**No version fence / no 412** (ADR-0036 / Q1 = Option A): prompts have no OCC
column, so a PATCH is last-writer-wins. Only **content** fields are mutable
(``text_content`` / ``kind`` / ``model_id`` / ``extra`` — Q10); ``scene_id`` is
immutable (no re-parenting in α6.1) and never appears in ``changes``. A
non-``None`` ``model_id`` in the patch is validated linkable first (Q4 → 422). A
same-value patch is a no-op (no write, ``updated_at`` unchanged); the empty
patch is rejected upstream by the DTO (422). ``changes`` is the tri-state
mapping the router built from ``model_fields_set`` (explicit ``model_id: null``
clears the link; absent means unchanged).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

import structlog

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.core.errors import NotFoundError, ValidationFailedError
from app.domain.prompts.prompt import Prompt

_LOGGER = structlog.get_logger(__name__)


class UpdatePrompt:
    """Partial content update of a prompt under the caller's project (no OCC)."""

    def __init__(self, uow: IUnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self,
        *,
        project_id: UUID,
        prompt_id: UUID,
        owner_user_id: UUID,
        tenant_id: UUID,
        changes: Mapping[str, Any],
        ip: str | None = None,
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
                _LOGGER.warning(
                    "prompt.update_rejected",
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

            # Validate a (re)linked model before writing (Q4). An explicit
            # ``model_id: null`` clears the link and skips validation.
            new_model_id = changes.get("model_id")
            if (
                "model_id" in changes
                and new_model_id is not None
                and not await self._uow.prompts.model_is_linkable(new_model_id)
            ):
                raise ValidationFailedError(
                    "model_id does not reference a linkable model",
                    details={"field": "model_id", "model_id": str(new_model_id)},
                )

            effective = {k: v for k, v in changes.items() if getattr(prompt, k) != v}
            if not effective:
                _LOGGER.info(
                    "prompt.update_rejected",
                    reason="same_value_noop",
                    prompt_id=str(prompt.id),
                    project_id=str(project_id),
                    owner_user_id=str(owner_user_id),
                    ip=ip,
                )
                return prompt

            updated = await self._uow.prompts.update_owned(project_id, prompt_id, effective)
            if updated is None:
                # No OCC fence: a None here means the row was soft-deleted
                # between the visibility gate and the write → uniform 404.
                _LOGGER.warning(
                    "prompt.update_rejected",
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
            "prompt.updated",
            prompt_id=str(updated.id),
            project_id=str(project_id),
            changed_fields=sorted(effective.keys()),
            owner_user_id=str(owner_user_id),
            ip=ip,
        )
        return updated
