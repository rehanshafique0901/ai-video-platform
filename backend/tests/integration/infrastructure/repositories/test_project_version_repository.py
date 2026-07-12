"""Integration tests for ``ProjectVersionRepository`` (Slice α5d.1).

Runs against the live database; each test is wrapped in a SAVEPOINT that
rolls back on teardown, so no rows persist. Covers snapshot assembly
(canonical, restore-ready), monotonic ``version_number`` + lineage, the
``current_version_id`` pointer advance (+ guarded project ``version`` bump),
metadata-only listing, UUID-addressed reads with cross-project isolation, and
the DB-enforced immutability of the ledger.

Note (transaction-constant ``now()``): the SAVEPOINT fixture holds one
transaction, so ``created_at`` is constant across a test. These tests assert
on ``version_number`` / lineage / snapshot content / the ``version`` bump —
never on wall-clock movement.

Coverage map (α5d pre-flight §7 / §8):

* V1 — ``create_snapshot`` on an empty project: ``version_number == 1``,
  parent ``None``, ``scenes == []``, ``storyboard`` ``None``,
  ``schema_version == 1``; advances ``current_version_id`` and bumps
  ``projects.version`` by exactly 1 (α5d Q6).
* V2 — a second capture: ``version_number == 2``, ``parent_version_id`` links
  to the first; pointer advanced again.
* V3 — snapshot captures live scenes in ``scene_number`` order with stable
  ids, fat columns, and ``Numeric`` durations serialized as strings (Q7).
* V4 — ``list_by_project`` returns metadata newest-first (no snapshot bodies).
* V5 — ``get_owned`` returns the full snapshot; ``None`` for a foreign project
  and for an unknown id.
* V6 — the ledger is immutable: a direct UPDATE of a ``project_versions`` row
  is rejected by the DB trigger (DS7).
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlalchemy import insert, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.db.models.identity import Tenant, User
from app.infrastructure.db.models.projects import (
    Project as ProjectRow,
    ProjectVersion as ProjectVersionRow,
)
from app.infrastructure.repositories.project_version_repository import (
    ProjectVersionRepository,
)
from app.infrastructure.repositories.scene_repository import SceneRepository


async def _seed_owner(session: AsyncSession) -> tuple[UUID, UUID]:
    tenant_id = uuid4()
    await session.execute(
        insert(Tenant).values(id=tenant_id, name="PV Test", slug=f"pv-{tenant_id}")
    )
    user_id = uuid4()
    await session.execute(
        insert(User).values(
            id=user_id,
            tenant_id=tenant_id,
            email=f"pv-{user_id}@example.com",
            display_name="PV Owner",
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
    """Append scenes to ``project_id`` via the real SceneRepository; return ids."""
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


async def _project_row(session: AsyncSession, project_id: UUID) -> ProjectRow:
    return (
        await session.execute(select(ProjectRow).where(ProjectRow.id == project_id))
    ).scalar_one()


# ---- V1 — first capture of an empty project ---------------------------


@pytest.mark.integration
async def test_v1_first_capture_empty_project(session: AsyncSession) -> None:
    tenant_id, owner_id = await _seed_owner(session)
    project_id = await _insert_project(session, tenant_id=tenant_id, owner_user_id=owner_id)
    repo = ProjectVersionRepository(session)

    version = await repo.create_snapshot(
        project_id=project_id, created_by_user_id=owner_id, reason="manual_save"
    )

    assert version.version_number == 1
    assert version.parent_version_id is None
    assert version.reason == "manual_save"
    assert version.diff_summary is None
    assert version.snapshot["schema_version"] == 1
    assert version.snapshot["scenes"] == []
    assert version.snapshot["storyboard"] is None

    project = await _project_row(session, project_id)
    assert project.current_version_id == version.id
    assert project.version == 2  # guarded trigger bumped it by exactly 1


# ---- V2 — second capture increments + links parent --------------------


@pytest.mark.integration
async def test_v2_second_capture_links_parent(session: AsyncSession) -> None:
    tenant_id, owner_id = await _seed_owner(session)
    project_id = await _insert_project(session, tenant_id=tenant_id, owner_user_id=owner_id)
    repo = ProjectVersionRepository(session)

    first = await repo.create_snapshot(
        project_id=project_id, created_by_user_id=owner_id, reason="manual_save"
    )
    second = await repo.create_snapshot(
        project_id=project_id, created_by_user_id=owner_id, reason="manual_save"
    )

    assert second.version_number == 2
    assert second.parent_version_id == first.id
    project = await _project_row(session, project_id)
    assert project.current_version_id == second.id
    assert project.version == 3  # two captures → two bumps


# ---- V3 — snapshot captures scenes canonically ------------------------


@pytest.mark.integration
async def test_v3_snapshot_captures_scenes_in_order(session: AsyncSession) -> None:
    tenant_id, owner_id = await _seed_owner(session)
    project_id = await _insert_project(session, tenant_id=tenant_id, owner_user_id=owner_id)
    scene_ids = await _seed_scenes(session, project_id=project_id, durations=[4.5, 2.0, 1.25])
    repo = ProjectVersionRepository(session)

    version = await repo.create_snapshot(
        project_id=project_id, created_by_user_id=owner_id, reason="manual_save"
    )

    snap_scenes = version.snapshot["scenes"]
    assert [s["id"] for s in snap_scenes] == [str(sid) for sid in scene_ids]
    # Numeric durations serialized as lossless strings (α5d Q7).
    assert snap_scenes[0]["duration_seconds"] == "4.500"
    # Fat columns present even though the α5c API exposes a slim subset.
    assert "emotion" in snap_scenes[0]
    assert "extra" in snap_scenes[0]
    assert version.snapshot["storyboard"] is not None
    # Ordering key monotonic.
    numbers = [s["scene_number"] for s in snap_scenes]
    assert numbers == sorted(numbers)


# ---- V4 — list metadata newest-first ----------------------------------


@pytest.mark.integration
async def test_v4_list_metadata_newest_first(session: AsyncSession) -> None:
    tenant_id, owner_id = await _seed_owner(session)
    project_id = await _insert_project(session, tenant_id=tenant_id, owner_user_id=owner_id)
    repo = ProjectVersionRepository(session)
    for _ in range(3):
        await repo.create_snapshot(
            project_id=project_id, created_by_user_id=owner_id, reason="manual_save"
        )

    summaries = await repo.list_by_project(project_id)
    assert [s.version_number for s in summaries] == [3, 2, 1]
    # Summary is metadata-only — it has no snapshot attribute.
    assert not hasattr(summaries[0], "snapshot")


# ---- V5 — get scoping --------------------------------------------------


@pytest.mark.integration
async def test_v5_get_owned_scoping(session: AsyncSession) -> None:
    tenant_id, owner_id = await _seed_owner(session)
    project_id = await _insert_project(session, tenant_id=tenant_id, owner_user_id=owner_id)
    other_project = await _insert_project(session, tenant_id=tenant_id, owner_user_id=owner_id)
    repo = ProjectVersionRepository(session)
    version = await repo.create_snapshot(
        project_id=project_id, created_by_user_id=owner_id, reason="manual_save"
    )

    got = await repo.get_owned(project_id, version.id)
    assert got is not None
    assert got.snapshot["schema_version"] == 1
    # Not visible under a different project.
    assert (await repo.get_owned(other_project, version.id)) is None
    # Unknown id.
    assert (await repo.get_owned(project_id, uuid4())) is None


# ---- V6 — ledger immutability (DB trigger) ----------------------------


@pytest.mark.integration
async def test_v6_ledger_is_immutable(session: AsyncSession) -> None:
    tenant_id, owner_id = await _seed_owner(session)
    project_id = await _insert_project(session, tenant_id=tenant_id, owner_user_id=owner_id)
    repo = ProjectVersionRepository(session)
    version = await repo.create_snapshot(
        project_id=project_id, created_by_user_id=owner_id, reason="manual_save"
    )

    # A direct UPDATE of the append-only ledger must be rejected by the
    # ``reject_mutation`` trigger (α5d DS7). Isolate the abort in a nested
    # savepoint so the outer test transaction stays usable for teardown.
    with pytest.raises(DBAPIError):
        async with session.begin_nested():
            await session.execute(
                update(ProjectVersionRow)
                .where(ProjectVersionRow.id == version.id)
                .values(reason="autosave")
            )
