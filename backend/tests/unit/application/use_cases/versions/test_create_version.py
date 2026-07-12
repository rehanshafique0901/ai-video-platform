"""Unit tests for ``CreateProjectVersion`` (Slice α5d.1).

Coverage map (α5d pre-flight §7 / §8):

* U1 — happy path on an empty project: captures ``version_number == 1``,
  ``reason == manual_save``, ``parent_version_id is None``, ``scenes == []``,
  ``schema_version == 1``, commits once, and advances the project's
  ``current_version_id`` (+ bumps ``projects.version``).
* U2 — a second capture assigns ``version_number == 2`` and links its
  ``parent_version_id`` to the first version's id (lineage chain).
* U3 — the snapshot reflects the project's live scenes in order, preserving
  scene ids verbatim (α5c Q8 / α5d D-Q8).
* U4 — a project the caller does not own → ``NotFoundError``, no commit, no
  version written.
* U5 — happy path emits ``project_version.created`` (INFO) with the metadata
  fields.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
import structlog

from app.application.use_cases.versions.create_version import CreateProjectVersion
from app.core.errors import NotFoundError
from app.domain.versions.project_version import ProjectVersion
from tests.unit.application.use_cases.versions._helpers import build_env, seed_scenes


@pytest.mark.unit
async def test_u1_first_capture_of_empty_project() -> None:
    env = build_env()
    uc = CreateProjectVersion(uow=env.uow)

    result = await uc.execute(
        project_id=env.project_id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
    )
    version = result.version

    assert isinstance(version, ProjectVersion)
    assert version.version_number == 1
    assert version.reason == "manual_save"
    assert version.parent_version_id is None
    assert version.created_by_user_id == env.owner_user_id
    assert version.diff_summary is None
    assert version.snapshot["schema_version"] == 1
    assert version.snapshot["scenes"] == []
    assert version.snapshot["storyboard"] is None
    assert env.uow.commits == 1
    # The just-captured version is current (drives the is_current wire flag).
    assert result.current_version_id == version.id
    # Current pointer advanced + project version bumped (α5d Q6).
    project = env.projects._rows[env.project_id]
    assert project.current_version_id == version.id
    assert project.version == 2


@pytest.mark.unit
async def test_u2_second_capture_increments_and_links_parent() -> None:
    env = build_env()
    uc = CreateProjectVersion(uow=env.uow)

    first = (
        await uc.execute(
            project_id=env.project_id,
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
        )
    ).version
    second = (
        await uc.execute(
            project_id=env.project_id,
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
        )
    ).version

    assert second.version_number == 2
    assert second.parent_version_id == first.id
    assert env.uow.commits == 2
    assert env.projects._rows[env.project_id].current_version_id == second.id


@pytest.mark.unit
async def test_u3_snapshot_captures_scenes_in_order_with_stable_ids() -> None:
    env = build_env()
    scene_ids = await seed_scenes(env, 3)
    uc = CreateProjectVersion(uow=env.uow)

    version = (
        await uc.execute(
            project_id=env.project_id,
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
        )
    ).version

    snap_scenes = version.snapshot["scenes"]
    assert [s["id"] for s in snap_scenes] == [str(sid) for sid in scene_ids]
    # scene_number is monotonic in capture order (gap-based).
    numbers = [s["scene_number"] for s in snap_scenes]
    assert numbers == sorted(numbers)
    assert version.snapshot["storyboard"] is not None


@pytest.mark.unit
async def test_u4_unowned_project_raises_not_found_no_commit() -> None:
    env = build_env()
    uc = CreateProjectVersion(uow=env.uow)

    with pytest.raises(NotFoundError):
        await uc.execute(
            project_id=uuid4(),  # not the seeded project
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
        )

    assert env.uow.commits == 0
    assert env.versions._versions == {}


@pytest.mark.unit
async def test_u5_happy_path_emits_created_log() -> None:
    env = build_env()
    await seed_scenes(env, 2)
    uc = CreateProjectVersion(uow=env.uow)

    with structlog.testing.capture_logs() as logs:
        version = (
            await uc.execute(
                project_id=env.project_id,
                owner_user_id=env.owner_user_id,
                tenant_id=env.tenant_id,
                ip="203.0.113.7",
            )
        ).version

    created = [e for e in logs if e.get("event") == "project_version.created"]
    assert len(created) == 1
    ev = created[0]
    assert ev["log_level"] == "info"
    assert ev["version_id"] == str(version.id)
    assert ev["version_number"] == 1
    assert ev["scene_count"] == 2
    assert ev["project_id"] == str(env.project_id)
    assert ev["ip"] == "203.0.113.7"
