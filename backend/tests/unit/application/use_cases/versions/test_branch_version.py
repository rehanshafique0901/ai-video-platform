"""Unit tests for ``BranchProjectVersion`` (α5d.3 — fork to a new project).

Coverage map (pre-flight §9 step 6):

* B1 — happy fork: a NEW independent project is created (fresh id, owned by the
  caller, ``name`` = requested, root fields seeded from the source snapshot),
  its ``v1`` is ``reason=branch`` / ``version_number=1`` / ``parent=NULL`` with
  a structured ``branched_from`` provenance block, and the new project's
  aggregate ``version`` ends at 2 (created + first capture). Commits once.
* B2 — scenes materialize with FRESH ids in ``scene_number`` order, content
  preserved (titles/durations), disjoint from the source's scene ids (Q5).
* B3 — the SOURCE project is untouched (its ``current_version_id`` / ``version``
  / scene set are unchanged; no source OCC bump — Q8).
* B4 — duplicate live project name for this owner → ``ConflictError`` (409).
* B5 — unowned project / unknown source version → ``NotFoundError`` (404),
  before any create (404-before-anything, anti-enumeration).
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.application.use_cases.versions.branch_version import BranchProjectVersion
from app.application.use_cases.versions.create_version import CreateProjectVersion
from app.core.errors import ConflictError, NotFoundError
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


def _new_v1(env, new_project_id):
    matches = [v for v in env.versions._versions.values() if v.project_id == new_project_id]
    assert len(matches) == 1, "expected exactly one version (v1) on the branched project"
    return matches[0]


async def test_b1_branch_forks_a_new_project_with_branch_v1_and_provenance() -> None:
    env = build_env()
    await seed_scenes(env, 2)
    src_v1 = (await _capture(env)).version

    commits_before = env.uow.commits
    result = await BranchProjectVersion(env.uow).execute(
        project_id=env.project_id,
        version_id=src_v1.id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
        name="Forked project",
    )

    new_project = result.project
    # A genuinely new, caller-owned aggregate seeded from the source root.
    assert new_project.id != env.project_id
    assert new_project.name == "Forked project"
    assert new_project.owner_user_id == env.owner_user_id
    assert new_project.tenant_id == env.tenant_id
    assert new_project.aspect_ratio == "horizontal"  # inherited from the snapshot
    # created + first capture → aggregate version ends at 2.
    assert new_project.version == 2

    v1 = _new_v1(env, new_project.id)
    assert v1.reason == "branch"
    assert v1.version_number == 1
    assert v1.parent_version_id is None  # fresh root (R1)
    assert v1.snapshot["branched_from"] == {
        "project_id": str(env.project_id),
        "version_id": str(src_v1.id),
        "version_number": src_v1.version_number,
    }
    # Branch commits exactly once (over the capture's own commit).
    assert env.uow.commits == commits_before + 1


async def test_b2_scenes_materialize_with_fresh_ids_in_order() -> None:
    env = build_env()
    src_ids = await seed_scenes(env, 3)
    src_v1 = (await _capture(env)).version

    result = await BranchProjectVersion(env.uow).execute(
        project_id=env.project_id,
        version_id=src_v1.id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
        name="Fork with scenes",
    )

    new_scenes = await env.scenes.list_by_project(result.project.id)
    # Content preserved, ordered by scene_number.
    assert [s.title for s in new_scenes] == ["Scene 1", "Scene 2", "Scene 3"]
    assert all(s.duration_seconds == 5.0 for s in new_scenes)
    # Fresh identity space — no scene id is shared with the source (Q5).
    assert {s.id for s in new_scenes}.isdisjoint(set(src_ids))


async def test_b3_source_project_is_untouched() -> None:
    env = build_env()
    src_ids = await seed_scenes(env, 2)
    src_v1 = (await _capture(env)).version
    fence = _project_version(env)

    await BranchProjectVersion(env.uow).execute(
        project_id=env.project_id,
        version_id=src_v1.id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
        name="Independent fork",
    )

    src_project = env.projects._rows[env.project_id]
    # No source mutation: pointer + aggregate version unchanged (no fence, Q8).
    assert src_project.current_version_id == src_v1.id
    assert src_project.version == fence
    # Source scene set unchanged.
    src_scenes = await env.scenes.list_by_project(env.project_id)
    assert {s.id for s in src_scenes} == set(src_ids)


async def test_b4_duplicate_name_raises_conflict() -> None:
    env = build_env()
    src_v1 = (await _capture(env)).version

    await BranchProjectVersion(env.uow).execute(
        project_id=env.project_id,
        version_id=src_v1.id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
        name="Duplicate name",
    )

    # A second branch to the same live name for this owner → 409.
    with pytest.raises(ConflictError):
        await BranchProjectVersion(env.uow).execute(
            project_id=env.project_id,
            version_id=src_v1.id,
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
            name="Duplicate name",
        )


async def test_b5_unowned_project_and_unknown_version_are_404() -> None:
    env = build_env()
    src_v1 = (await _capture(env)).version

    # Unowned project → 404.
    with pytest.raises(NotFoundError):
        await BranchProjectVersion(env.uow).execute(
            project_id=uuid4(),
            version_id=src_v1.id,
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
            name="From nowhere",
        )

    # Unknown source version → 404.
    with pytest.raises(NotFoundError):
        await BranchProjectVersion(env.uow).execute(
            project_id=env.project_id,
            version_id=uuid4(),
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
            name="From unknown version",
        )
