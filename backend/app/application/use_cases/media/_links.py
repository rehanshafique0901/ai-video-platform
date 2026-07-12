"""Shared optional-link validation for the Media use cases (α6.2 Q5).

A media asset may link to a project, a scene, a prompt, and/or a model. Each
**present** link must be valid *for the caller*:

* ``project_id`` — a live project owned by the caller.
* ``scene_id`` — a live scene in that project (**requires** ``project_id``).
* ``prompt_id`` — a live prompt in that project (**requires** ``project_id``).
* ``model_id`` — a linkable ``ai_models`` row (exists + not ``retired``).

Any failure raises :class:`ValidationFailedError` (→ ``422``): the *body* is
invalid, the route target (the caller's own media namespace) is fine — this is
NOT a ``404`` (contrast the project/prompt/scene *route* gates). Shared by
:class:`RegisterMedia` (validates the whole provided set) and
:class:`UpdateMedia` (validates the *effective* set when any link changes).
"""

from __future__ import annotations

from uuid import UUID

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.core.errors import ValidationFailedError


async def validate_media_links(
    uow: IUnitOfWork,
    *,
    tenant_id: UUID,
    owner_user_id: UUID,
    project_id: UUID | None,
    scene_id: UUID | None,
    prompt_id: UUID | None,
    model_id: UUID | None,
    validate_model: bool,
) -> None:
    """Validate the (effective) link set; raise ``ValidationFailedError`` on any bad link.

    ``validate_model`` lets the caller skip the model check when ``model_id`` is
    untouched (``UpdateMedia`` only re-checks a model the client actually
    (re)linked; ``RegisterMedia`` always validates the provided ``model_id``).
    """
    if project_id is not None:
        project = await uow.projects.get_owned(
            project_id=project_id,
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
        )
        if project is None:
            raise ValidationFailedError(
                "project_id does not reference a live project you own",
                details={"field": "project_id", "project_id": str(project_id)},
            )

    if scene_id is not None:
        if project_id is None:
            raise ValidationFailedError(
                "scene_id requires a project_id",
                details={"field": "scene_id", "scene_id": str(scene_id)},
            )
        scene = await uow.scenes.get_owned_scene(project_id, scene_id)
        if scene is None:
            raise ValidationFailedError(
                "scene_id does not reference a live scene in this project",
                details={"field": "scene_id", "scene_id": str(scene_id)},
            )

    if prompt_id is not None:
        if project_id is None:
            raise ValidationFailedError(
                "prompt_id requires a project_id",
                details={"field": "prompt_id", "prompt_id": str(prompt_id)},
            )
        prompt = await uow.prompts.get_owned(project_id, prompt_id)
        if prompt is None:
            raise ValidationFailedError(
                "prompt_id does not reference a live prompt in this project",
                details={"field": "prompt_id", "prompt_id": str(prompt_id)},
            )

    if validate_model and model_id is not None and not await uow.media.model_is_linkable(model_id):
        raise ValidationFailedError(
            "model_id does not reference a linkable model",
            details={"field": "model_id", "model_id": str(model_id)},
        )
