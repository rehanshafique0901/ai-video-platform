"""Unit tests for ``DeletePrompt`` (Slice α6.1).

Coverage map (α6.1 pre-flight §5.1): happy (soft delete, commit) ·
idempotent-by-404 (second delete → 404) · project not owned → 404.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.application.use_cases.prompts.create_prompt import CreatePrompt
from app.application.use_cases.prompts.delete_prompt import DeletePrompt
from app.application.use_cases.prompts.get_prompt import GetPrompt
from app.core.errors import NotFoundError
from tests.unit.application.use_cases.prompts._helpers import build_env


async def _make(env):  # type: ignore[no-untyped-def]
    return await CreatePrompt(uow=env.uow).execute(
        project_id=env.project_id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
        kind="image",
        text_content="x",
    )


@pytest.mark.unit
async def test_u1_happy_soft_delete_commits() -> None:
    env = build_env()
    created = await _make(env)
    commits_before = env.uow.commits

    await DeletePrompt(uow=env.uow).execute(
        project_id=env.project_id,
        prompt_id=created.id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
    )

    assert env.uow.commits == commits_before + 1
    with pytest.raises(NotFoundError):
        await GetPrompt(uow=env.uow).execute(
            project_id=env.project_id,
            prompt_id=created.id,
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
        )


@pytest.mark.unit
async def test_u2_idempotent_by_404() -> None:
    env = build_env()
    created = await _make(env)
    await DeletePrompt(uow=env.uow).execute(
        project_id=env.project_id,
        prompt_id=created.id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
    )
    with pytest.raises(NotFoundError):
        await DeletePrompt(uow=env.uow).execute(
            project_id=env.project_id,
            prompt_id=created.id,
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
        )


@pytest.mark.unit
async def test_u3_unowned_project_raises_404() -> None:
    env = build_env()
    created = await _make(env)
    with pytest.raises(NotFoundError):
        await DeletePrompt(uow=env.uow).execute(
            project_id=uuid4(),
            prompt_id=created.id,
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
        )
