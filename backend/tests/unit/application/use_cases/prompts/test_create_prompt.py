"""Unit tests for ``CreatePrompt`` (Slice α6.1).

Coverage map (α6.1 pre-flight §5.1):

* U1 — happy path: creates a project-level prompt, commits once, returns the
  entity with server ids; ``scene_id`` / ``model_id`` are ``None``.
* U2 — scene-link valid: a live scene of the same project is accepted + echoed.
* U3 — scene-link foreign / unknown → ``ValidationFailedError`` (422), no write.
* U4 — model-link valid: a linkable ``model_id`` is accepted + echoed.
* U5 — model-link unknown / retired → ``ValidationFailedError`` (422), no write.
* U6 — project not owned → ``NotFoundError`` (404), no write, no commit.
* U7 — ``prompt.created`` (INFO) emitted with the field set; ``text_content``
  (user content) is never in the log. Prompts do NOT bump ``projects.version``.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
import structlog

from app.application.use_cases.prompts.create_prompt import CreatePrompt
from app.core.errors import NotFoundError, ValidationFailedError
from app.domain.prompts.prompt import Prompt
from tests.unit.application.use_cases.prompts._helpers import (
    build_env,
    register_model,
    seed_scene,
)


@pytest.mark.unit
async def test_u1_happy_path_project_level_prompt_commits() -> None:
    env = build_env()
    uc = CreatePrompt(uow=env.uow)

    prompt = await uc.execute(
        project_id=env.project_id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
        kind="image",
        text_content="a red bicycle at dawn",
    )

    assert isinstance(prompt, Prompt)
    assert prompt.project_id == env.project_id
    assert prompt.scene_id is None
    assert prompt.model_id is None
    assert prompt.kind == "image"
    assert prompt.extra == {}
    assert env.uow.commits == 1
    # ADR-0036 / Q1=A: a prompt create does NOT bump the project OCC token.
    assert env.projects._rows[env.project_id].version == 1


@pytest.mark.unit
async def test_u2_scene_link_valid_is_accepted() -> None:
    env = build_env()
    scene_id = await seed_scene(env)
    uc = CreatePrompt(uow=env.uow)

    prompt = await uc.execute(
        project_id=env.project_id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
        kind="motion",
        text_content="slow dolly-in",
        scene_id=scene_id,
    )

    assert prompt.scene_id == scene_id
    assert env.uow.commits == 1


@pytest.mark.unit
async def test_u3_foreign_scene_link_raises_422_no_write() -> None:
    env = build_env()
    uc = CreatePrompt(uow=env.uow)

    with pytest.raises(ValidationFailedError):
        await uc.execute(
            project_id=env.project_id,
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
            kind="image",
            text_content="x",
            scene_id=uuid4(),  # no such scene in this project
        )

    assert env.uow.commits == 0
    assert env.prompts._prompts == {}


@pytest.mark.unit
async def test_u4_model_link_valid_is_accepted() -> None:
    env = build_env()
    model_id = register_model(env)
    uc = CreatePrompt(uow=env.uow)

    prompt = await uc.execute(
        project_id=env.project_id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
        kind="image",
        text_content="x",
        model_id=model_id,
    )

    assert prompt.model_id == model_id
    assert env.uow.commits == 1


@pytest.mark.unit
async def test_u5_unknown_model_link_raises_422_no_write() -> None:
    env = build_env()
    uc = CreatePrompt(uow=env.uow)

    with pytest.raises(ValidationFailedError):
        await uc.execute(
            project_id=env.project_id,
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
            kind="image",
            text_content="x",
            model_id=uuid4(),  # unknown / retired
        )

    assert env.uow.commits == 0
    assert env.prompts._prompts == {}


@pytest.mark.unit
async def test_u6_unowned_project_raises_404_no_commit() -> None:
    env = build_env()
    uc = CreatePrompt(uow=env.uow)

    with pytest.raises(NotFoundError):
        await uc.execute(
            project_id=uuid4(),  # not the seeded project
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
            kind="image",
            text_content="x",
        )

    assert env.uow.commits == 0
    assert env.prompts._prompts == {}


@pytest.mark.unit
async def test_u7_created_log_omits_text_content() -> None:
    env = build_env()
    uc = CreatePrompt(uow=env.uow)

    with structlog.testing.capture_logs() as logs:
        prompt = await uc.execute(
            project_id=env.project_id,
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
            kind="style",
            text_content="Secret Prompt Body",
            ip="203.0.113.7",
        )

    created = [e for e in logs if e.get("event") == "prompt.created"]
    assert len(created) == 1
    ev = created[0]
    assert ev["log_level"] == "info"
    assert ev["prompt_id"] == str(prompt.id)
    assert ev["project_id"] == str(env.project_id)
    assert ev["kind"] == "style"
    assert ev["scene_id"] is None
    assert ev["ip"] == "203.0.113.7"
    # text_content is user content — never logged.
    assert "Secret Prompt Body" not in str(ev)
