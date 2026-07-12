"""Aggregate OCC Rule regression tests (α5d.2 Q1 / Option A).

``projects.version`` is the optimistic-concurrency token for the *entire*
Project aggregate (``PROJECT_AGGREGATE.md`` §6): every scene mutation that
changes observable project state MUST advance it, and a no-op MUST NOT. These
tests pin that invariant on all four α5c scene paths so a future refactor that
drops a ``projects.touch_version`` call fails loudly (R1 — coverage risk).
"""

from __future__ import annotations

import pytest

from app.application.use_cases.scenes.create_scene import CreateScene
from app.application.use_cases.scenes.delete_scene import DeleteScene
from app.application.use_cases.scenes.move_scene import MoveScene
from app.application.use_cases.scenes.update_scene import UpdateScene
from tests.unit.application.use_cases.scenes._helpers import build_env, seed_scenes

pytestmark = pytest.mark.unit


def _project_version(env) -> int:
    return env.projects._rows[env.project_id].version


async def test_create_scene_bumps_project_version() -> None:
    env = build_env()
    before = _project_version(env)

    await CreateScene(env.uow).execute(
        project_id=env.project_id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
        title="Scene 1",
        duration_seconds=5.0,
    )

    assert _project_version(env) == before + 1


async def test_update_scene_real_change_bumps_project_version() -> None:
    env = build_env()
    [scene_id] = await seed_scenes(env, 1)  # seeding bypasses use cases → no bump
    before = _project_version(env)

    await UpdateScene(env.uow).execute(
        project_id=env.project_id,
        scene_id=scene_id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
        expected_version=1,
        changes={"title": "Renamed"},
    )

    assert _project_version(env) == before + 1


async def test_update_scene_same_value_noop_does_not_bump_project_version() -> None:
    env = build_env()
    [scene_id] = await seed_scenes(env, 1)
    before = _project_version(env)

    # Same-value PATCH: the use case resolves it as a no-op before any write.
    await UpdateScene(env.uow).execute(
        project_id=env.project_id,
        scene_id=scene_id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
        expected_version=1,
        changes={"title": "Scene 1"},
    )

    assert _project_version(env) == before


async def test_move_scene_real_move_bumps_project_version() -> None:
    env = build_env()
    ids = await seed_scenes(env, 3)
    before = _project_version(env)

    # Move the first scene to the last position (a real reorder).
    await MoveScene(env.uow).execute(
        project_id=env.project_id,
        scene_id=ids[0],
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
        expected_version=1,
        position=3,
    )

    assert _project_version(env) == before + 1


async def test_move_scene_noop_does_not_bump_project_version() -> None:
    env = build_env()
    ids = await seed_scenes(env, 3)
    before = _project_version(env)

    # Move the first scene to position 1 (its current slot) — a no-op.
    await MoveScene(env.uow).execute(
        project_id=env.project_id,
        scene_id=ids[0],
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
        expected_version=1,
        position=1,
    )

    assert _project_version(env) == before


async def test_delete_scene_bumps_project_version() -> None:
    env = build_env()
    [scene_id] = await seed_scenes(env, 1)
    before = _project_version(env)

    await DeleteScene(env.uow).execute(
        project_id=env.project_id,
        scene_id=scene_id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
    )

    assert _project_version(env) == before + 1
