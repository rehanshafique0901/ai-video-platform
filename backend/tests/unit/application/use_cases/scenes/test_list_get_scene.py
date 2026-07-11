"""Unit tests for ``ListScenes`` + ``GetScene`` (Slice α5c).

Coverage map (α5c pre-flight §5.1):

* U5 — list returns the project's scenes ordered by ``scene_number`` ASC;
  empty project → ``[]`` (and creates no storyboard — read-only, D8).
* U6 — list on an unowned project → ``NotFoundError``.
* U7 — get returns the scene + its 1-based ``position``.
* U8 — get on an unowned project → ``NotFoundError`` (project gate).
* U9 — get of an unknown / other-project scene under an owned project →
  ``NotFoundError`` (scene gate — the second level of the visibility gate).
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.application.use_cases.scenes.get_scene import GetScene
from app.application.use_cases.scenes.list_scenes import ListScenes
from app.core.errors import NotFoundError
from tests.unit.application.use_cases.scenes._helpers import build_env, seed_scenes


@pytest.mark.unit
async def test_u5_list_returns_ordered_scenes_empty_when_none() -> None:
    env = build_env()
    uc = ListScenes(uow=env.uow)

    # No scenes yet → empty list, no storyboard created.
    empty = await uc.execute(
        project_id=env.project_id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
    )
    assert empty == []
    assert env.scenes._default_sb == {}

    ids = await seed_scenes(env, 3)
    scenes = await uc.execute(
        project_id=env.project_id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
    )
    assert [s.id for s in scenes] == ids  # insertion == scene_number order
    assert [s.scene_number for s in scenes] == sorted(s.scene_number for s in scenes)


@pytest.mark.unit
async def test_u6_list_unowned_project_raises_not_found() -> None:
    env = build_env()
    uc = ListScenes(uow=env.uow)

    with pytest.raises(NotFoundError):
        await uc.execute(
            project_id=uuid4(),
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
        )


@pytest.mark.unit
async def test_u7_get_returns_scene_and_position() -> None:
    env = build_env()
    ids = await seed_scenes(env, 3)
    uc = GetScene(uow=env.uow)

    result = await uc.execute(
        project_id=env.project_id,
        scene_id=ids[1],
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
    )
    assert result.scene.id == ids[1]
    assert result.position == 2


@pytest.mark.unit
async def test_u8_get_unowned_project_raises_not_found() -> None:
    env = build_env()
    ids = await seed_scenes(env, 1)
    uc = GetScene(uow=env.uow)

    with pytest.raises(NotFoundError):
        await uc.execute(
            project_id=uuid4(),
            scene_id=ids[0],
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
        )


@pytest.mark.unit
async def test_u9_get_unknown_scene_under_owned_project_raises_not_found() -> None:
    env = build_env()
    await seed_scenes(env, 1)
    uc = GetScene(uow=env.uow)

    with pytest.raises(NotFoundError):
        await uc.execute(
            project_id=env.project_id,
            scene_id=uuid4(),  # not a scene of this project
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
        )
