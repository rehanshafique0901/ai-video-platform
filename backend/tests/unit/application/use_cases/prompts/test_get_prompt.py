"""Unit tests for ``GetPrompt`` (Slice α6.1).

Coverage map (α6.1 pre-flight §5.1): happy · prompt not visible → 404 ·
project not owned → 404.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.application.use_cases.prompts.create_prompt import CreatePrompt
from app.application.use_cases.prompts.get_prompt import GetPrompt
from app.core.errors import NotFoundError
from tests.unit.application.use_cases.prompts._helpers import build_env


@pytest.mark.unit
async def test_u1_happy_path() -> None:
    env = build_env()
    created = await CreatePrompt(uow=env.uow).execute(
        project_id=env.project_id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
        kind="image",
        text_content="x",
    )

    got = await GetPrompt(uow=env.uow).execute(
        project_id=env.project_id,
        prompt_id=created.id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
    )

    assert got.id == created.id


@pytest.mark.unit
async def test_u2_unknown_prompt_raises_404() -> None:
    env = build_env()
    with pytest.raises(NotFoundError):
        await GetPrompt(uow=env.uow).execute(
            project_id=env.project_id,
            prompt_id=uuid4(),
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
        )


@pytest.mark.unit
async def test_u3_unowned_project_raises_404() -> None:
    env = build_env()
    created = await CreatePrompt(uow=env.uow).execute(
        project_id=env.project_id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
        kind="image",
        text_content="x",
    )
    with pytest.raises(NotFoundError):
        await GetPrompt(uow=env.uow).execute(
            project_id=uuid4(),
            prompt_id=created.id,
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
        )
