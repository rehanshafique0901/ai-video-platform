"""Unit tests for ``ListPrompts`` (Slice α6.1).

Coverage map (α6.1 pre-flight §5.1): newest-first order · kind filter ·
scene filter · empty → [] · project not owned → 404.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.application.use_cases.prompts.create_prompt import CreatePrompt
from app.application.use_cases.prompts.list_prompts import ListPrompts
from app.core.errors import NotFoundError
from tests.unit.application.use_cases.prompts._helpers import build_env, seed_scene


async def _make(env, **kw):  # type: ignore[no-untyped-def]
    return await CreatePrompt(uow=env.uow).execute(
        project_id=env.project_id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
        **kw,
    )


@pytest.mark.unit
async def test_u1_newest_first_order() -> None:
    env = build_env()
    p1 = await _make(env, kind="image", text_content="one")
    p2 = await _make(env, kind="image", text_content="two")
    p3 = await _make(env, kind="image", text_content="three")

    listed = await ListPrompts(uow=env.uow).execute(
        project_id=env.project_id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
    )

    assert [p.id for p in listed] == [p3.id, p2.id, p1.id]


@pytest.mark.unit
async def test_u2_kind_filter() -> None:
    env = build_env()
    await _make(env, kind="image", text_content="i")
    vid = await _make(env, kind="video", text_content="v")

    listed = await ListPrompts(uow=env.uow).execute(
        project_id=env.project_id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
        kind="video",
    )

    assert [p.id for p in listed] == [vid.id]


@pytest.mark.unit
async def test_u3_scene_filter() -> None:
    env = build_env()
    scene_id = await seed_scene(env)
    await _make(env, kind="image", text_content="project-level")
    scoped = await _make(env, kind="image", text_content="scene-scoped", scene_id=scene_id)

    listed = await ListPrompts(uow=env.uow).execute(
        project_id=env.project_id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
        scene_id=scene_id,
    )

    assert [p.id for p in listed] == [scoped.id]


@pytest.mark.unit
async def test_u4_empty_returns_empty_list() -> None:
    env = build_env()
    listed = await ListPrompts(uow=env.uow).execute(
        project_id=env.project_id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
    )
    assert listed == []


@pytest.mark.unit
async def test_u5_unowned_project_raises_404() -> None:
    env = build_env()
    with pytest.raises(NotFoundError):
        await ListPrompts(uow=env.uow).execute(
            project_id=uuid4(),
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
        )
