"""Unit tests for ``MoveScene`` (Slice α5c).

Coverage map (α5c pre-flight §5.1):

* U16 — move to a later position reorders the list and bumps the moved
  scene's ``version``; the display order reflects the new position.
* U17 — move to the current slot is a no-op (unchanged version, still 200).
* U18 — stale ``expected_version`` → ``VersionConflictError`` (412).
* U19 — an out-of-range ``position`` is clamped into ``[1, N]`` (moving to
  position 999 lands the scene last).
* U20 — move on an unowned project → ``NotFoundError`` (project gate).
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from app.application.use_cases.scenes.list_scenes import ListScenes
from app.application.use_cases.scenes.move_scene import MoveScene
from app.core.errors import NotFoundError, VersionConflictError
from tests.unit.application.use_cases.scenes._helpers import Env, build_env, seed_scenes


async def _order(env: Env) -> list[UUID]:
    scenes = await ListScenes(uow=env.uow).execute(
        project_id=env.project_id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
    )
    return [s.id for s in scenes]


@pytest.mark.unit
async def test_u16_move_to_later_position_reorders_and_bumps_version() -> None:
    env = build_env()
    ids = await seed_scenes(env, 3)  # order: [0, 1, 2]
    uc = MoveScene(uow=env.uow)

    result = await uc.execute(
        project_id=env.project_id,
        scene_id=ids[0],
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
        expected_version=1,
        position=3,
    )
    assert result.position == 3
    assert result.scene.version == 2  # moved scene bumped
    assert await _order(env) == [ids[1], ids[2], ids[0]]


@pytest.mark.unit
async def test_u17_move_to_current_slot_is_noop() -> None:
    env = build_env()
    ids = await seed_scenes(env, 3)
    uc = MoveScene(uow=env.uow)

    result = await uc.execute(
        project_id=env.project_id,
        scene_id=ids[1],
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
        expected_version=1,
        position=2,  # already there
    )
    assert result.scene.version == 1  # unchanged
    assert await _order(env) == ids


@pytest.mark.unit
async def test_u18_stale_version_raises_conflict() -> None:
    env = build_env()
    ids = await seed_scenes(env, 2)
    uc = MoveScene(uow=env.uow)

    with pytest.raises(VersionConflictError):
        await uc.execute(
            project_id=env.project_id,
            scene_id=ids[0],
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
            expected_version=99,
            position=2,
        )


@pytest.mark.unit
async def test_u19_out_of_range_position_is_clamped() -> None:
    env = build_env()
    ids = await seed_scenes(env, 3)
    uc = MoveScene(uow=env.uow)

    result = await uc.execute(
        project_id=env.project_id,
        scene_id=ids[0],
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
        expected_version=1,
        position=999,  # clamps to last
    )
    assert result.position == 3
    assert await _order(env) == [ids[1], ids[2], ids[0]]


@pytest.mark.unit
async def test_u20_move_unowned_project_raises_not_found() -> None:
    env = build_env()
    ids = await seed_scenes(env, 2)
    uc = MoveScene(uow=env.uow)

    with pytest.raises(NotFoundError):
        await uc.execute(
            project_id=uuid4(),
            scene_id=ids[0],
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
            expected_version=1,
            position=2,
        )
