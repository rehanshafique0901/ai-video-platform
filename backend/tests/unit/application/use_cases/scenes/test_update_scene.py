"""Unit tests for ``UpdateScene`` (Slice α5c).

Coverage map (α5c pre-flight §5.1):

* U10 — happy path: applies content changes, bumps ``version`` by exactly
  1, commits once, emits ``scene.updated`` with ``changed_fields``.
* U11 — stale ``expected_version`` → ``VersionConflictError`` (412), no
  commit (fenced before the write).
* U12 — same-value change is a no-op: returns 200 with the unchanged
  version, does NOT write, emits ``scene.update_rejected`` (same_value_noop).
* U13 — clearing a nullable field (explicit ``narration=None``) persists.
* U14 — project gate 404 precedes the version fence (404-before-412): a
  wrong owner on a stale version still raises ``NotFoundError``.
* U15 — unknown scene under an owned project → ``NotFoundError``.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
import structlog

from app.application.use_cases.scenes.update_scene import UpdateScene
from app.core.errors import NotFoundError, VersionConflictError
from tests.unit.application.use_cases.scenes._helpers import build_env, seed_scenes


@pytest.mark.unit
async def test_u10_happy_path_bumps_version_and_commits() -> None:
    env = build_env()
    ids = await seed_scenes(env, 1)
    uc = UpdateScene(uow=env.uow)

    with structlog.testing.capture_logs() as logs:
        result = await uc.execute(
            project_id=env.project_id,
            scene_id=ids[0],
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
            expected_version=1,
            changes={"title": "Renamed"},
        )

    assert result.scene.title == "Renamed"
    assert result.scene.version == 2  # exactly +1
    assert env.uow.commits == 1
    updated = [e for e in logs if e.get("event") == "scene.updated"]
    assert len(updated) == 1
    assert updated[0]["changed_fields"] == ["title"]


@pytest.mark.unit
async def test_u11_stale_version_raises_conflict_no_commit() -> None:
    env = build_env()
    ids = await seed_scenes(env, 1)
    uc = UpdateScene(uow=env.uow)

    with pytest.raises(VersionConflictError):
        await uc.execute(
            project_id=env.project_id,
            scene_id=ids[0],
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
            expected_version=99,
            changes={"title": "Nope"},
        )
    assert env.uow.commits == 0


@pytest.mark.unit
async def test_u12_same_value_is_noop_returns_unchanged() -> None:
    env = build_env()
    ids = await seed_scenes(env, 1)
    uc = UpdateScene(uow=env.uow)

    scene = env.scenes._scenes[ids[0]]
    with structlog.testing.capture_logs() as logs:
        result = await uc.execute(
            project_id=env.project_id,
            scene_id=ids[0],
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
            expected_version=1,
            changes={"title": scene.title},  # unchanged
        )

    assert result.scene.version == 1  # no bump
    assert env.uow.commits == 0  # no write
    noop = [
        e
        for e in logs
        if e.get("event") == "scene.update_rejected" and e.get("reason") == "same_value_noop"
    ]
    assert len(noop) == 1


@pytest.mark.unit
async def test_u13_clear_nullable_field_persists() -> None:
    env = build_env()
    storyboard_id, _ = await env.scenes.ensure_default_storyboard(env.project_id)
    scene = await env.scenes.add(
        storyboard_id=storyboard_id,
        title="Has narration",
        duration_seconds=2.0,
        narration="to be cleared",
        subtitle=None,
    )
    uc = UpdateScene(uow=env.uow)

    result = await uc.execute(
        project_id=env.project_id,
        scene_id=scene.id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
        expected_version=1,
        changes={"narration": None},
    )
    assert result.scene.narration is None
    assert result.scene.version == 2


@pytest.mark.unit
async def test_u14_project_gate_404_precedes_version_fence() -> None:
    env = build_env()
    ids = await seed_scenes(env, 1)
    uc = UpdateScene(uow=env.uow)

    # Wrong owner AND stale version — the project gate wins (404, not 412).
    with pytest.raises(NotFoundError):
        await uc.execute(
            project_id=env.project_id,
            scene_id=ids[0],
            owner_user_id=uuid4(),  # not the owner
            tenant_id=env.tenant_id,
            expected_version=99,
            changes={"title": "X"},
        )


@pytest.mark.unit
async def test_u15_unknown_scene_raises_not_found() -> None:
    env = build_env()
    await seed_scenes(env, 1)
    uc = UpdateScene(uow=env.uow)

    with pytest.raises(NotFoundError):
        await uc.execute(
            project_id=env.project_id,
            scene_id=uuid4(),
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
            expected_version=1,
            changes={"title": "X"},
        )
