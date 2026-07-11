"""Unit tests for ``CreateScene`` (Slice α5c).

Coverage map (α5c pre-flight §5.1):

* U1 — happy path: appends a scene (``version == 1``), computes its
  1-based ``position``, commits once. First scene of a project auto-creates
  the default storyboard and emits ``storyboard.default_created``.
* U2 — the second create reuses the same storyboard (no second
  ``storyboard.default_created``) and appends at ``position = 2`` with a
  larger ``scene_number`` (gap append).
* U3 — a project the caller does not own → ``NotFoundError``, no commit.
* U4 — happy path emits ``scene.created`` (INFO) with the field set; the
  scene ``title`` (user content) is never in the log.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
import structlog

from app.application.use_cases.scenes.create_scene import CreateScene
from app.application.use_cases.scenes.results import SceneResult
from app.core.errors import NotFoundError
from tests.unit.application.use_cases.scenes._helpers import build_env


@pytest.mark.unit
async def test_u1_happy_path_appends_version_one_and_commits() -> None:
    env = build_env()
    uc = CreateScene(uow=env.uow)

    with structlog.testing.capture_logs() as logs:
        result = await uc.execute(
            project_id=env.project_id,
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
            title="Opening shot",
            duration_seconds=4.5,
        )

    assert isinstance(result, SceneResult)
    assert result.scene.version == 1
    assert result.scene.title == "Opening shot"
    assert result.scene.duration_seconds == 4.5
    assert result.position == 1
    assert result.scene.scene_number == 1000  # first slot
    assert env.uow.commits == 1
    # First scene auto-creates the default storyboard.
    assert [e for e in logs if e.get("event") == "storyboard.default_created"]


@pytest.mark.unit
async def test_u2_second_create_reuses_storyboard_and_appends() -> None:
    env = build_env()
    uc = CreateScene(uow=env.uow)

    first = await uc.execute(
        project_id=env.project_id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
        title="One",
        duration_seconds=1.0,
    )
    with structlog.testing.capture_logs() as logs:
        second = await uc.execute(
            project_id=env.project_id,
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
            title="Two",
            duration_seconds=2.0,
        )

    assert second.position == 2
    assert second.scene.scene_number > first.scene.scene_number
    assert first.scene.storyboard_id == second.scene.storyboard_id
    # Storyboard already existed → no second creation event.
    assert not [e for e in logs if e.get("event") == "storyboard.default_created"]


@pytest.mark.unit
async def test_u3_unowned_project_raises_not_found_no_commit() -> None:
    env = build_env()
    uc = CreateScene(uow=env.uow)

    with pytest.raises(NotFoundError):
        await uc.execute(
            project_id=uuid4(),  # not the seeded project
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
            title="X",
            duration_seconds=1.0,
        )

    assert env.uow.commits == 0
    assert env.scenes._scenes == {}


@pytest.mark.unit
async def test_u4_happy_path_emits_scene_created_log_without_content() -> None:
    env = build_env()
    uc = CreateScene(uow=env.uow)

    with structlog.testing.capture_logs() as logs:
        result = await uc.execute(
            project_id=env.project_id,
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
            title="Secret Title",
            duration_seconds=3.0,
            ip="203.0.113.7",
        )

    created = [e for e in logs if e.get("event") == "scene.created"]
    assert len(created) == 1
    ev = created[0]
    assert ev["log_level"] == "info"
    assert ev["scene_id"] == str(result.scene.id)
    assert ev["project_id"] == str(env.project_id)
    assert ev["owner_user_id"] == str(env.owner_user_id)
    assert ev["position"] == 1
    assert ev["ip"] == "203.0.113.7"
    # Scene title is user content — never logged.
    assert "Secret Title" not in str(ev)
