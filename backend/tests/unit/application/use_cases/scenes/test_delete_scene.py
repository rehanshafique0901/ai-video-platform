"""Unit tests for ``DeleteScene`` (Slice α5c).

Coverage map (α5c pre-flight §5.1):

* U21 — happy path: soft-deletes the scene, commits once, emits
  ``scene.deleted``. The scene is no longer listable.
* U22 — a second delete (idempotent-by-404): the now-gone scene → 404,
  and the delete is not committed.
* U23 — delete on an unowned project → ``NotFoundError`` (project gate),
  the scene is left intact.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
import structlog

from app.application.use_cases.scenes.delete_scene import DeleteScene
from app.core.errors import NotFoundError
from tests.unit.application.use_cases.scenes._helpers import build_env, seed_scenes


@pytest.mark.unit
async def test_u21_happy_path_soft_deletes_and_commits() -> None:
    env = build_env()
    ids = await seed_scenes(env, 2)
    uc = DeleteScene(uow=env.uow)

    with structlog.testing.capture_logs() as logs:
        await uc.execute(
            project_id=env.project_id,
            scene_id=ids[0],
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
        )

    assert ids[0] not in env.scenes._scenes  # gone from the live view
    assert env.uow.commits == 1
    assert [e for e in logs if e.get("event") == "scene.deleted"]


@pytest.mark.unit
async def test_u22_second_delete_is_404() -> None:
    env = build_env()
    ids = await seed_scenes(env, 1)
    uc = DeleteScene(uow=env.uow)

    await uc.execute(
        project_id=env.project_id,
        scene_id=ids[0],
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
    )
    commits_after_first = env.uow.commits

    with pytest.raises(NotFoundError):
        await uc.execute(
            project_id=env.project_id,
            scene_id=ids[0],
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
        )
    assert env.uow.commits == commits_after_first  # the 404 did not commit


@pytest.mark.unit
async def test_u23_delete_unowned_project_raises_not_found() -> None:
    env = build_env()
    ids = await seed_scenes(env, 1)
    uc = DeleteScene(uow=env.uow)

    with pytest.raises(NotFoundError):
        await uc.execute(
            project_id=uuid4(),
            scene_id=ids[0],
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
        )
    assert ids[0] in env.scenes._scenes  # untouched
