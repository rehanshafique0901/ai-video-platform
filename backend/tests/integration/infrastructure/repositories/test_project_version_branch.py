"""Integration tests for ``ProjectVersionRepository.branch`` (Slice α5d.3).

Runs against the live database; each test is wrapped in a SAVEPOINT that rolls
back on teardown. Covers the load-bearing fork behaviours the unit fakes cannot
prove against a real schema:

* BR1 — fork fidelity: capture (with fat cinematography columns) → branch → a
  NEW independent project is created (fresh id, caller-owned, ``name`` =
  requested, root inherited from the source snapshot); its ``v1`` is
  ``reason=branch`` / ``version_number=1`` / ``parent=NULL`` with a
  ``branched_from`` provenance block; its ``current_version_id`` points at v1 and
  the aggregate ``version`` ends at 2. The v1 snapshot's scenes equal the source
  snapshot's **modulo scene id** (fat fields + decimal-string durations +
  ordering survive; ids are freshly minted — Q5).
* BR2 — the SOURCE project is untouched: version, current pointer, live scene
  set, and the source version row are all unchanged (no source OCC bump — Q8;
  history immutability).
* BR3 — one transaction / all-or-nothing (§6): an injected mid-branch failure
  (after the scene inserts) rolls back to leave zero writes — no new project, no
  new scenes, no new version; the source is unchanged.
* BR4 — a duplicate live project name for the owner → ``ConflictError`` (→ 409),
  raised before any child rows persist.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError
from app.infrastructure.db.models.identity import Tenant, User
from app.infrastructure.db.models.projects import Project as ProjectRow
from app.infrastructure.db.models.scenes import (
    Scene as SceneRow,
    Storyboard as StoryboardRow,
)
from app.infrastructure.repositories.project_version_repository import (
    ProjectVersionRepository,
)
from app.infrastructure.repositories.scene_repository import SceneRepository


async def _seed_owner(session: AsyncSession) -> tuple[UUID, UUID]:
    tenant_id = uuid4()
    await session.execute(
        insert(Tenant).values(id=tenant_id, name="BR Test", slug=f"br-{tenant_id}")
    )
    user_id = uuid4()
    await session.execute(
        insert(User).values(
            id=user_id,
            tenant_id=tenant_id,
            email=f"br-{user_id}@example.com",
            display_name="BR Owner",
        )
    )
    return tenant_id, user_id


async def _insert_project(
    session: AsyncSession, *, tenant_id: UUID, owner_user_id: UUID, name: str | None = None
) -> UUID:
    project_id = uuid4()
    await session.execute(
        insert(ProjectRow).values(
            id=project_id,
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            name=name or f"P {project_id}",
            aspect_ratio="horizontal",
        )
    )
    return project_id


async def _seed_scenes(
    session: AsyncSession, *, project_id: UUID, durations: list[float]
) -> list[UUID]:
    scenes_repo = SceneRepository(session)
    sb, _ = await scenes_repo.ensure_default_storyboard(project_id)
    ids: list[UUID] = []
    for i, dur in enumerate(durations):
        scene = await scenes_repo.add(
            storyboard_id=sb,
            title=f"Scene {i + 1}",
            duration_seconds=dur,
            narration=None,
            subtitle=None,
        )
        ids.append(scene.id)
    return ids


async def _project(session: AsyncSession, project_id: UUID) -> ProjectRow:
    return (
        await session.execute(select(ProjectRow).where(ProjectRow.id == project_id))
    ).scalar_one()


async def _live_scene_ids(session: AsyncSession, project_id: UUID) -> set[UUID]:
    result = await session.execute(
        select(SceneRow.id)
        .where(SceneRow.deleted_at.is_(None))
        .where(
            SceneRow.storyboard_id.in_(
                select(StoryboardRow.id).where(StoryboardRow.project_id == project_id)
            )
        )
    )
    return set(result.scalars().all())


def _strip(scene: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in scene.items() if k != "id"}


# ---- BR1 — fork fidelity + new independent project + provenance -------


@pytest.mark.integration
async def test_br1_branch_forks_new_project_with_fidelity_and_provenance(
    session: AsyncSession,
) -> None:
    tenant_id, owner_id = await _seed_owner(session)
    project_id = await _insert_project(session, tenant_id=tenant_id, owner_user_id=owner_id)
    scene_ids = await _seed_scenes(session, project_id=project_id, durations=[4.5, 2.0])
    # Fat cinematography columns no α5c API surface can produce.
    await session.execute(
        update(SceneRow)
        .where(SceneRow.id == scene_ids[0])
        .values(
            emotion="joyful",
            camera_angle="wide",
            lens="35mm",
            extra={"mood": "bright", "n": 3},
        )
    )
    repo = ProjectVersionRepository(session)
    v1 = await repo.create_snapshot(
        project_id=project_id, created_by_user_id=owner_id, reason="manual_save"
    )

    new_project, new_v1 = await repo.branch(
        source_project_id=project_id,
        source_version_id=v1.id,
        source_version_number=v1.version_number,
        source_snapshot=v1.snapshot,
        new_project_name="Forked project",
        tenant_id=tenant_id,
        owner_user_id=owner_id,
    )

    # A genuinely new, caller-owned aggregate.
    assert new_project.id != project_id
    assert new_project.name == "Forked project"
    assert new_project.owner_user_id == owner_id
    assert new_project.tenant_id == tenant_id
    assert new_project.aspect_ratio == "horizontal"
    # created + first capture → version 2, pointer at v1.
    assert new_project.version == 2
    db_new = await _project(session, new_project.id)
    assert db_new.current_version_id == new_v1.id
    assert db_new.version == 2

    # v1 lineage + provenance.
    assert new_v1.reason == "branch"
    assert new_v1.version_number == 1
    assert new_v1.parent_version_id is None
    assert new_v1.snapshot["branched_from"] == {
        "project_id": str(project_id),
        "version_id": str(v1.id),
        "version_number": v1.version_number,
    }

    # Scenes equal the source modulo id (fat fields + durations + order survive),
    # with freshly-minted ids (Q5).
    assert [_strip(s) for s in new_v1.snapshot["scenes"]] == [
        _strip(s) for s in v1.snapshot["scenes"]
    ]
    new_scene_ids = {s["id"] for s in new_v1.snapshot["scenes"]}
    src_scene_ids = {s["id"] for s in v1.snapshot["scenes"]}
    assert new_scene_ids.isdisjoint(src_scene_ids)
    assert new_v1.snapshot["scenes"][0]["emotion"] == "joyful"
    assert new_v1.snapshot["scenes"][0]["extra"] == {"mood": "bright", "n": 3}

    # Project root inherited modulo id/version/name.
    def _root(p: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in p.items() if k not in {"id", "version", "name"}}

    assert _root(new_v1.snapshot["project"]) == _root(v1.snapshot["project"])


# ---- BR2 — source project untouched -----------------------------------


@pytest.mark.integration
async def test_br2_branch_leaves_source_untouched(session: AsyncSession) -> None:
    tenant_id, owner_id = await _seed_owner(session)
    project_id = await _insert_project(session, tenant_id=tenant_id, owner_user_id=owner_id)
    scene_ids = await _seed_scenes(session, project_id=project_id, durations=[3.0, 3.0])
    repo = ProjectVersionRepository(session)
    v1 = await repo.create_snapshot(
        project_id=project_id, created_by_user_id=owner_id, reason="manual_save"
    )
    source_snapshot_before = v1.snapshot
    version_before = (await _project(session, project_id)).version
    live_before = await _live_scene_ids(session, project_id)

    await repo.branch(
        source_project_id=project_id,
        source_version_id=v1.id,
        source_version_number=v1.version_number,
        source_snapshot=v1.snapshot,
        new_project_name="Independent fork",
        tenant_id=tenant_id,
        owner_user_id=owner_id,
    )

    # No source OCC bump, pointer unchanged, scene set unchanged (Q8).
    src = await _project(session, project_id)
    assert src.version == version_before
    assert src.current_version_id == v1.id
    assert await _live_scene_ids(session, project_id) == live_before == set(scene_ids)
    # Source version row is immutable — unchanged by the fork.
    reread = await repo.get_owned(project_id, v1.id)
    assert reread is not None
    assert reread.reason == "manual_save"
    assert reread.snapshot == source_snapshot_before
    assert "branched_from" not in reread.snapshot
    # The source project still has exactly its own single version.
    assert [s.version_number for s in await repo.list_by_project(project_id)] == [1]


# ---- BR3 — one transaction / all-or-nothing ---------------------------


@pytest.mark.integration
async def test_br3_injected_failure_rolls_back_all_writes(session: AsyncSession) -> None:
    tenant_id, owner_id = await _seed_owner(session)
    project_id = await _insert_project(session, tenant_id=tenant_id, owner_user_id=owner_id)
    await _seed_scenes(session, project_id=project_id, durations=[2.0, 2.0])
    repo = ProjectVersionRepository(session)
    v1 = await repo.create_snapshot(
        project_id=project_id, created_by_user_id=owner_id, reason="manual_save"
    )
    version_before = (await _project(session, project_id)).version
    projects_before = (
        await session.execute(select(func.count()).select_from(ProjectRow))
    ).scalar_one()

    # Inject a failure AFTER the new project + storyboard + scene inserts (the
    # ``_live_scenes`` re-read in step 3, before the v1 insert + pointer advance).
    async def _boom(_storyboard_id: UUID) -> list[SceneRow]:
        raise RuntimeError("injected mid-branch failure")

    repo._live_scenes = _boom  # type: ignore[method-assign]

    with pytest.raises(RuntimeError):
        async with session.begin_nested():
            await repo.branch(
                source_project_id=project_id,
                source_version_id=v1.id,
                source_version_number=v1.version_number,
                source_snapshot=v1.snapshot,
                new_project_name="Doomed fork",
                tenant_id=tenant_id,
                owner_user_id=owner_id,
            )

    # Everything rolled back: no new project row, source unchanged.
    projects_after = (
        await session.execute(select(func.count()).select_from(ProjectRow))
    ).scalar_one()
    assert projects_after == projects_before
    doomed = (
        await session.execute(
            select(func.count()).select_from(ProjectRow).where(ProjectRow.name == "Doomed fork")
        )
    ).scalar_one()
    assert doomed == 0
    assert (await _project(session, project_id)).version == version_before
    assert [s.version_number for s in await repo.list_by_project(project_id)] == [1]


# ---- BR4 — duplicate live name → ConflictError ------------------------


@pytest.mark.integration
async def test_br4_duplicate_name_raises_conflict(session: AsyncSession) -> None:
    tenant_id, owner_id = await _seed_owner(session)
    project_id = await _insert_project(session, tenant_id=tenant_id, owner_user_id=owner_id)
    await _seed_scenes(session, project_id=project_id, durations=[2.0])
    repo = ProjectVersionRepository(session)
    v1 = await repo.create_snapshot(
        project_id=project_id, created_by_user_id=owner_id, reason="manual_save"
    )
    # Pre-existing live project owned by the caller with the target name.
    await _insert_project(session, tenant_id=tenant_id, owner_user_id=owner_id, name="Taken name")

    with pytest.raises(ConflictError):
        async with session.begin_nested():
            await repo.branch(
                source_project_id=project_id,
                source_version_id=v1.id,
                source_version_number=v1.version_number,
                source_snapshot=v1.snapshot,
                new_project_name="Taken name",
                tenant_id=tenant_id,
                owner_user_id=owner_id,
            )
