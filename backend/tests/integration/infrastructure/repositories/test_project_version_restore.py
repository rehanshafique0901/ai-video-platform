"""Integration tests for ``ProjectVersionRepository.restore`` (Slice α5d.2).

Runs against the live database; each test is wrapped in a SAVEPOINT that rolls
back on teardown. Covers the load-bearing restore behaviours the unit fakes
cannot prove against a real schema:

* RS1 — round-trip fidelity: capture (with fat cinematography columns) →
  mutate → restore → the restore version's snapshot equals the source snapshot
  **modulo ``project.version``** (fat fields + decimal-string durations +
  ordering all survive). Restore appends ``reason=restore`` parented on the
  source, advances ``current_version_id``, and bumps ``projects.version`` by
  exactly one (Aggregate OCC Rule).
* RS2 — revive-soft-deleted (Q3): a scene soft-deleted after capture is revived
  **in place** (same UUID) by restore, not re-inserted.
* RS3 — stale aggregate fence → ``None`` (→ 412 at the use case), no writes.
* RS4 — one transaction / all-or-nothing (§9): an injected mid-restore failure
  (after the scene reconcile writes) rolls back to leave zero writes — live
  scenes, project version, and the ledger are all unchanged.
* RS5 — history immutability: restore never mutates the source version row.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

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
        insert(Tenant).values(id=tenant_id, name="RS Test", slug=f"rs-{tenant_id}")
    )
    user_id = uuid4()
    await session.execute(
        insert(User).values(
            id=user_id,
            tenant_id=tenant_id,
            email=f"rs-{user_id}@example.com",
            display_name="RS Owner",
        )
    )
    return tenant_id, user_id


async def _insert_project(session: AsyncSession, *, tenant_id: UUID, owner_user_id: UUID) -> UUID:
    project_id = uuid4()
    await session.execute(
        insert(ProjectRow).values(
            id=project_id,
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            name=f"P {project_id}",
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


# ---- RS1 — round-trip fidelity + lineage + aggregate bump -------------


@pytest.mark.integration
async def test_rs1_restore_round_trip_fidelity(session: AsyncSession) -> None:
    tenant_id, owner_id = await _seed_owner(session)
    project_id = await _insert_project(session, tenant_id=tenant_id, owner_user_id=owner_id)
    scene_ids = await _seed_scenes(session, project_id=project_id, durations=[4.5, 2.0])
    # Set fat cinematography columns that no α5c API surface can produce.
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

    # Mutate away from the snapshot: retitle scene 0, add a new scene.
    scenes_repo = SceneRepository(session)
    await scenes_repo.update_owned(
        project_id, scene_ids[0], expected_version=1, changes={"title": "Retitled"}
    )
    sb, _ = await scenes_repo.ensure_default_storyboard(project_id)
    await scenes_repo.add(
        storyboard_id=sb, title="Added", duration_seconds=1.0, narration=None, subtitle=None
    )

    fence = (await _project(session, project_id)).version
    restored = await repo.restore(
        project_id=project_id,
        source_version_id=v1.id,
        restored_by_user_id=owner_id,
        expected_project_version=fence,
    )
    assert restored is not None

    # Lineage + reason + pointer + exactly-one aggregate bump.
    assert restored.reason == "restore"
    assert restored.parent_version_id == v1.id
    assert restored.version_number == v1.version_number + 1
    project = await _project(session, project_id)
    assert project.current_version_id == restored.id
    assert project.version == fence + 1

    # Snapshot equality modulo project.version (fat fields + durations + order).
    assert restored.snapshot["scenes"] == v1.snapshot["scenes"]
    p_before = {k: v for k, v in v1.snapshot["project"].items() if k != "version"}
    p_after = {k: v for k, v in restored.snapshot["project"].items() if k != "version"}
    assert p_before == p_after
    # Fat columns actually survived the round-trip.
    assert restored.snapshot["scenes"][0]["emotion"] == "joyful"
    assert restored.snapshot["scenes"][0]["extra"] == {"mood": "bright", "n": 3}
    # Live set equals the snapshot set (added scene dropped).
    assert await _live_scene_ids(session, project_id) == set(scene_ids)


# ---- RS2 — revive a soft-deleted scene by upsert-on-id ----------------


@pytest.mark.integration
async def test_rs2_restore_revives_soft_deleted_scene(session: AsyncSession) -> None:
    tenant_id, owner_id = await _seed_owner(session)
    project_id = await _insert_project(session, tenant_id=tenant_id, owner_user_id=owner_id)
    scene_ids = await _seed_scenes(session, project_id=project_id, durations=[3.0, 3.0])
    repo = ProjectVersionRepository(session)
    v1 = await repo.create_snapshot(
        project_id=project_id, created_by_user_id=owner_id, reason="manual_save"
    )

    # Soft-delete one scene after capture.
    scenes_repo = SceneRepository(session)
    assert await scenes_repo.soft_delete_owned(project_id, scene_ids[1]) is True
    assert scene_ids[1] not in await _live_scene_ids(session, project_id)

    fence = (await _project(session, project_id)).version
    restored = await repo.restore(
        project_id=project_id,
        source_version_id=v1.id,
        restored_by_user_id=owner_id,
        expected_project_version=fence,
    )
    assert restored is not None
    # The soft-deleted scene is revived IN PLACE (same UUID), not re-inserted.
    assert await _live_scene_ids(session, project_id) == set(scene_ids)


# ---- RS3 — stale fence → None, no writes ------------------------------


@pytest.mark.integration
async def test_rs3_stale_fence_returns_none_no_writes(session: AsyncSession) -> None:
    tenant_id, owner_id = await _seed_owner(session)
    project_id = await _insert_project(session, tenant_id=tenant_id, owner_user_id=owner_id)
    scene_ids = await _seed_scenes(session, project_id=project_id, durations=[2.0])
    repo = ProjectVersionRepository(session)
    v1 = await repo.create_snapshot(
        project_id=project_id, created_by_user_id=owner_id, reason="manual_save"
    )
    live_before = await _live_scene_ids(session, project_id)
    version_before = (await _project(session, project_id)).version

    result = await repo.restore(
        project_id=project_id,
        source_version_id=v1.id,
        restored_by_user_id=owner_id,
        expected_project_version=version_before - 1,  # stale
    )

    assert result is None
    # No writes: no new version, project version unchanged, scenes unchanged.
    assert [s.version_number for s in await repo.list_by_project(project_id)] == [1]
    assert (await _project(session, project_id)).version == version_before
    assert await _live_scene_ids(session, project_id) == live_before
    assert live_before == set(scene_ids)


# ---- RS4 — one transaction / all-or-nothing ---------------------------


@pytest.mark.integration
async def test_rs4_injected_failure_rolls_back_all_writes(session: AsyncSession) -> None:
    tenant_id, owner_id = await _seed_owner(session)
    project_id = await _insert_project(session, tenant_id=tenant_id, owner_user_id=owner_id)
    await _seed_scenes(session, project_id=project_id, durations=[2.0, 2.0])
    repo = ProjectVersionRepository(session)
    v1 = await repo.create_snapshot(
        project_id=project_id, created_by_user_id=owner_id, reason="manual_save"
    )
    # Add a scene so the reconcile has real soft-delete work to do before the
    # injected failure.
    scenes_repo = SceneRepository(session)
    sb, _ = await scenes_repo.ensure_default_storyboard(project_id)
    await scenes_repo.add(
        storyboard_id=sb, title="Doomed", duration_seconds=1.0, narration=None, subtitle=None
    )
    live_before = await _live_scene_ids(session, project_id)
    version_before = (await _project(session, project_id)).version

    # Inject a failure AFTER the scene reconcile writes (step 5 re-read).
    async def _boom(_storyboard_id: UUID) -> list[SceneRow]:
        raise RuntimeError("injected mid-restore failure")

    repo._live_scenes = _boom  # type: ignore[method-assign]

    with pytest.raises(RuntimeError):
        async with session.begin_nested():
            await repo.restore(
                project_id=project_id,
                source_version_id=v1.id,
                restored_by_user_id=owner_id,
                expected_project_version=version_before,
            )

    # Everything rolled back: scenes, project version, ledger all unchanged.
    assert await _live_scene_ids(session, project_id) == live_before
    assert (await _project(session, project_id)).version == version_before
    assert [s.version_number for s in await repo.list_by_project(project_id)] == [1]


# ---- RS5 — history immutability ---------------------------------------


@pytest.mark.integration
async def test_rs5_restore_does_not_mutate_source_version(session: AsyncSession) -> None:
    tenant_id, owner_id = await _seed_owner(session)
    project_id = await _insert_project(session, tenant_id=tenant_id, owner_user_id=owner_id)
    await _seed_scenes(session, project_id=project_id, durations=[2.0])
    repo = ProjectVersionRepository(session)
    v1 = await repo.create_snapshot(
        project_id=project_id, created_by_user_id=owner_id, reason="manual_save"
    )
    source_snapshot_before = v1.snapshot

    fence = (await _project(session, project_id)).version
    await repo.restore(
        project_id=project_id,
        source_version_id=v1.id,
        restored_by_user_id=owner_id,
        expected_project_version=fence,
    )

    reread = await repo.get_owned(project_id, v1.id)
    assert reread is not None
    assert reread.reason == "manual_save"  # unchanged
    assert reread.snapshot == source_snapshot_before  # snapshot untouched
