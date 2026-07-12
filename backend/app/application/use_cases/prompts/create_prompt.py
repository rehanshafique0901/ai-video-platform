"""``CreatePrompt`` use case (Slice α6.1).

Contract (API_CONTRACT §3.4):

    POST /api/v1/projects/{project_id}/prompts
      body:  { kind, text_content, scene_id?, model_id?, extra? }
      → 201  { data: PromptPublic, meta }
      → 404  { error: { code: NOT_FOUND, ... } }         (project missing / not yours)
      → 422  { error: { code: VALIDATION_FAILED, ... } }  (bad body OR foreign scene / bad model)
      → 401  { error: { code: UNAUTHENTICATED, ... } }    (via CurrentUserDep)

Flow:

1. **Project gate** (α6.1 D2) — ``projects.get_owned`` → ``None`` → 404. The
   caller must own a live project; ``owner_user_id`` / ``tenant_id`` come from
   ``CurrentUserDep``, never the body.
2. **Scene-link validation** (Q3) — a non-``None`` ``scene_id`` must reference a
   **live scene in this project** (``scenes.get_owned_scene``); a foreign /
   unknown / soft-deleted scene → ``422`` (not 404 — the *body* is invalid, the
   route target project is fine).
3. **Model-link validation** (Q4) — a non-``None`` ``model_id`` must be linkable
   (``prompts.model_is_linkable`` — exists + not ``retired``); else ``422``.
4. **Insert** and commit. Per ADR-0036 (Q1 = A) this does **NOT** bump
   ``projects.version`` and the prompt is **NOT** captured in any snapshot.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import structlog

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.core.errors import NotFoundError, ValidationFailedError
from app.domain.prompts.prompt import Prompt

_LOGGER = structlog.get_logger(__name__)


class CreatePrompt:
    """Create a prompt under the caller's own project (optional scene/model link)."""

    def __init__(self, uow: IUnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self,
        *,
        project_id: UUID,
        owner_user_id: UUID,
        tenant_id: UUID,
        kind: str,
        text_content: str,
        scene_id: UUID | None = None,
        model_id: UUID | None = None,
        extra: dict[str, Any] | None = None,
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

            if scene_id is not None:
                scene = await self._uow.scenes.get_owned_scene(project_id, scene_id)
                if scene is None:
                    raise ValidationFailedError(
                        "scene_id does not reference a live scene in this project",
                        details={"field": "scene_id", "scene_id": str(scene_id)},
                    )

            if model_id is not None and not await self._uow.prompts.model_is_linkable(model_id):
                raise ValidationFailedError(
                    "model_id does not reference a linkable model",
                    details={"field": "model_id", "model_id": str(model_id)},
                )

            prompt = await self._uow.prompts.add(
                project_id=project_id,
                scene_id=scene_id,
                kind=kind,
                text_content=text_content,
                model_id=model_id,
                extra=extra or {},
            )
            # No aggregate OCC bump (ADR-0036 / Q1 = A): a prompt is a
            # generation input, not versioned editorial content.
            await self._uow.commit()

        _LOGGER.info(
            "prompt.created",
            prompt_id=str(prompt.id),
            project_id=str(project_id),
            scene_id=None if scene_id is None else str(scene_id),
            kind=kind,
            model_id=None if model_id is None else str(model_id),
            owner_user_id=str(owner_user_id),
            tenant_id=str(tenant_id),
            ip=ip,
        )
        return prompt
