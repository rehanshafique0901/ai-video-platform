"""Unit tests for ``RestoreProjectVersion`` + ``DiffProjectVersions`` (α5d.2).

Coverage map (pre-flight §12 step 7):

Restore:
* R1 — appends a ``reason=restore`` head, parent = source version, advances
  ``current_version_id``, bumps the aggregate ``projects.version`` by one.
* R2 — scene reconcile: restoring an older version makes the live scene set
  equal that version's snapshot (by id), reviving a since-deleted scene and
  dropping a since-added one.
* R3 — empty snapshot clears all live scenes.
* R4 — stale aggregate fence → ``VersionConflictError`` (412), no writes.
* R5 — unowned project / unknown version → ``NotFoundError`` (404), before the
  fence (404-before-412).

Diff:
* D1 — added / removed / modified scene counts (base → target).
* D2 — ``project_changed`` reflects a business-column change.
* D3 — unknown / cross-project version on either side → ``NotFoundError``.
"""

from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

import pytest

from app.application.use_cases.scenes.create_scene import CreateScene
from app.application.use_cases.scenes.delete_scene import DeleteScene
from app.application.use_cases.scenes.update_scene import UpdateScene
from app.application.use_cases.versions.create_version import CreateProjectVersion
from app.application.use_cases.versions.diff_versions import DiffProjectVersions
from app.application.use_cases.versions.restore_version import RestoreProjectVersion
from app.core.errors import NotFoundError, VersionConflictError
from tests.unit.application.use_cases.versions._helpers import build_env, seed_scenes

pytestmark = pytest.mark.unit


def _project_version(env) -> int:
    return env.projects._rows[env.project_id].version


async def _capture(env):
    return await CreateProjectVersion(env.uow).execute(
        project_id=env.project_id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
    )


# ---- Restore ----------------------------------------------------------


async def test_r1_restore_appends_head_parents_source_and_advances_current() -> None:
    env = build_env()
    await seed_scenes(env, 2)
    v1 = (await _capture(env)).version  # current → v1

    fence = _project_version(env)
    commits_before = env.uow.commits
    result = await RestoreProjectVersion(env.uow).execute(
        project_id=env.project_id,
        version_id=v1.id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
        expected_version=fence,
    )

    assert result.version.reason == "restore"
    assert result.version.parent_version_id == v1.id
    assert result.version.version_number == v1.version_number + 1
    # New head is current + is_current-able against the returned pointer.
    assert result.current_version_id == result.version.id
    assert env.projects._rows[env.project_id].current_version_id == result.version.id
    # Restore is exactly one aggregate bump.
    assert _project_version(env) == fence + 1
    # Restore commits exactly once (over and above the capture's own commit).
    assert env.uow.commits == commits_before + 1


async def test_r2_restore_reconciles_scene_set_to_snapshot() -> None:
    env = build_env()
    ids = await seed_scenes(env, 2)
    v1 = (await _capture(env)).version  # snapshot has exactly ids[0], ids[1]

    # Mutate away from the snapshot: delete one seeded scene, add a new one.
    await DeleteScene(env.uow).execute(
        project_id=env.project_id,
        scene_id=ids[0],
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
    )
    await CreateScene(env.uow).execute(
        project_id=env.project_id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
        title="Added later",
        duration_seconds=5.0,
    )

    await RestoreProjectVersion(env.uow).execute(
        project_id=env.project_id,
        version_id=v1.id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
        expected_version=_project_version(env),
    )

    live = await env.scenes.list_by_project(env.project_id)
    # Reviving ids[0] and dropping the added scene → exactly the snapshot set.
    assert {s.id for s in live} == {ids[0], ids[1]}


async def test_r3_restore_empty_snapshot_clears_scenes() -> None:
    env = build_env()
    v_empty = (await _capture(env)).version  # captured with zero scenes

    await seed_scenes(env, 3)  # seed bypasses the version bump
    # Re-capture to move the fence forward would add scenes to a later
    # snapshot; instead mutate through a use case so the fence advances.
    await CreateScene(env.uow).execute(
        project_id=env.project_id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
        title="Extra",
        duration_seconds=5.0,
    )

    await RestoreProjectVersion(env.uow).execute(
        project_id=env.project_id,
        version_id=v_empty.id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
        expected_version=_project_version(env),
    )

    assert await env.scenes.list_by_project(env.project_id) == []


async def test_r4_stale_fence_raises_version_conflict_and_does_not_commit() -> None:
    env = build_env()
    await seed_scenes(env, 1)
    v1 = (await _capture(env)).version
    commits_before = env.uow.commits

    with pytest.raises(VersionConflictError):
        await RestoreProjectVersion(env.uow).execute(
            project_id=env.project_id,
            version_id=v1.id,
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
            expected_version=_project_version(env) - 1,  # stale
        )

    assert env.uow.commits == commits_before


async def test_r5_unowned_project_and_unknown_version_are_404_before_fence() -> None:
    env = build_env()
    v1 = (await _capture(env)).version

    # Unowned project → 404.
    with pytest.raises(NotFoundError):
        await RestoreProjectVersion(env.uow).execute(
            project_id=uuid4(),
            version_id=v1.id,
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
            expected_version=_project_version(env),
        )

    # Unknown version (even with a stale fence) → 404, not 412.
    with pytest.raises(NotFoundError):
        await RestoreProjectVersion(env.uow).execute(
            project_id=env.project_id,
            version_id=uuid4(),
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
            expected_version=_project_version(env) - 5,
        )


# ---- Diff -------------------------------------------------------------


async def test_d1_diff_counts_added_removed_modified() -> None:
    env = build_env()
    ids = await seed_scenes(env, 2)
    v1 = (await _capture(env)).version

    # Modify one scene, delete another, add a new one, then capture v2.
    await UpdateScene(env.uow).execute(
        project_id=env.project_id,
        scene_id=ids[0],
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
        expected_version=1,
        changes={"title": "Modified"},
    )
    await DeleteScene(env.uow).execute(
        project_id=env.project_id,
        scene_id=ids[1],
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
    )
    await CreateScene(env.uow).execute(
        project_id=env.project_id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
        title="Brand new",
        duration_seconds=5.0,
    )
    v2 = (await _capture(env)).version

    diff = await DiffProjectVersions(env.uow).execute(
        project_id=env.project_id,
        version_id=v2.id,
        against_version_id=v1.id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
    )

    assert diff.base_version_number == v1.version_number
    assert diff.target_version_number == v2.version_number
    assert diff.scenes_added == 1
    assert diff.scenes_removed == 1
    assert diff.scenes_modified == 1
    assert diff.project_changed is False


async def test_d2_diff_project_changed_on_business_column() -> None:
    env = build_env()
    v1 = (await _capture(env)).version

    # Change a project business column, then capture v2.
    project = env.projects._rows[env.project_id]
    env.projects._rows[env.project_id] = replace(project, name="Renamed project")
    v2 = (await _capture(env)).version

    diff = await DiffProjectVersions(env.uow).execute(
        project_id=env.project_id,
        version_id=v2.id,
        against_version_id=v1.id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
    )

    assert diff.project_changed is True


async def test_d3_unknown_version_on_either_side_is_404() -> None:
    env = build_env()
    v1 = (await _capture(env)).version

    with pytest.raises(NotFoundError):
        await DiffProjectVersions(env.uow).execute(
            project_id=env.project_id,
            version_id=uuid4(),
            against_version_id=v1.id,
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
        )

    with pytest.raises(NotFoundError):
        await DiffProjectVersions(env.uow).execute(
            project_id=env.project_id,
            version_id=v1.id,
            against_version_id=uuid4(),
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
        )
