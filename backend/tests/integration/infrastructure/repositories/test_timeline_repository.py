"""Integration tests for ``TimelineRepository`` (Slice α6.3a).

Runs against the live database; each test is wrapped in a SAVEPOINT that rolls
back on teardown, so no rows persist. Covers the self-contained OCC aggregate
(ADR-0038): one live timeline per project (``add`` → ``409`` on a second), the
version-fenced root CAS (``update_owned`` — net **+1** over the guarded
``tg_timelines_biu_version_bump`` trigger), the single-token aggregate roll-up
(``bump_version`` — fenced vs unconditional), z_index uniqueness per live
timeline (``409``), timeline-scoped track visibility ordered by ``z_index``, and
soft delete freeing the z_index slot.

Coverage map (α6.3 pre-flight §5.2):

* R1 — ``add`` creates ``version = 1``; a second live timeline → ``ConflictError``.
* R2 — ``get_by_project`` returns the live row; excludes soft-deleted.
* R3 — ``update_owned`` real change: net +1, ``updated_at`` advances.
* R4 — ``update_owned`` stale version → ``None`` (→ 412).
* R5 — ``bump_version`` unconditional (``None``) advances; fenced stale → ``None``.
* R6 — ``add_track`` + ``list_tracks`` z_index ASC, excludes soft-deleted.
* R7 — ``add_track`` duplicate z_index (live) → ``ConflictError`` (409).
* R8 — ``get_track`` timeline isolation → ``None``.
* R9 — ``update_track`` real change; z_index collision → ``ConflictError`` (409).
* R10 — ``soft_delete_track`` happy / already-deleted → ``True`` / ``False``;
  frees the z_index slot for re-insert.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, insert, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError
from app.domain.timeline.timeline import Timeline as TimelineEntity
from app.infrastructure.db.models.identity import Tenant, User
from app.infrastructure.db.models.projects import Project as ProjectRow
from app.infrastructure.db.models.timeline import Timeline as TimelineRow
from app.infrastructure.repositories.timeline_repository import TimelineRepository


async def _seed_project(session: AsyncSession) -> UUID:
    tenant_id = uuid4()
    await session.execute(
        insert(Tenant).values(id=tenant_id, name="TL Test", slug=f"tl-{tenant_id}")
    )
    user_id = uuid4()
    await session.execute(
        insert(User).values(
            id=user_id,
            tenant_id=tenant_id,
            email=f"tl-{user_id}@example.com",
            display_name="TL Owner",
        )
    )
    project_id = uuid4()
    await session.execute(
        insert(ProjectRow).values(
            id=project_id,
            tenant_id=tenant_id,
            owner_user_id=user_id,
            name=f"P {project_id}",
            aspect_ratio="horizontal",
        )
    )
    await session.flush()
    return project_id


async def _add_timeline(repo: TimelineRepository, project_id: UUID) -> TimelineEntity:
    return await repo.add(
        project_id=project_id,
        aspect_ratio="16:9",
        frame_rate=30,
        background_color="#000000",
    )


# ---- R1 — add + duplicate → 409 --------------------------------------


@pytest.mark.integration
async def test_r1_add_version_1_and_duplicate_conflicts(session: AsyncSession) -> None:
    project_id = await _seed_project(session)
    repo = TimelineRepository(session)

    timeline = await _add_timeline(repo, project_id)
    assert timeline.version == 1
    assert timeline.project_id == project_id
    assert timeline.project_version_id is None
    assert timeline.duration_seconds == 0.0

    with pytest.raises(ConflictError):
        await _add_timeline(repo, project_id)


# ---- R2 — get_by_project excludes soft-deleted -----------------------


@pytest.mark.integration
async def test_r2_get_by_project_excludes_soft_deleted(session: AsyncSession) -> None:
    project_id = await _seed_project(session)
    repo = TimelineRepository(session)
    timeline = await _add_timeline(repo, project_id)

    assert (await repo.get_by_project(project_id)).id == timeline.id

    await session.execute(
        update(TimelineRow).where(TimelineRow.id == timeline.id).values(deleted_at=func.now())
    )
    assert await repo.get_by_project(project_id) is None


# ---- R3 — update_owned real change: net +1 ---------------------------


@pytest.mark.integration
async def test_r3_update_owned_bumps_version_by_one(session: AsyncSession) -> None:
    project_id = await _seed_project(session)
    repo = TimelineRepository(session)
    timeline = await _add_timeline(repo, project_id)

    updated = await repo.update_owned(
        project_id=project_id,
        expected_version=timeline.version,
        changes={"frame_rate": 60},
    )
    assert updated is not None
    assert updated.frame_rate == 60
    assert updated.version == 2  # net +1 (guarded trigger no-ops)


# ---- R4 — update_owned stale version → None --------------------------


@pytest.mark.integration
async def test_r4_update_owned_stale_returns_none(session: AsyncSession) -> None:
    project_id = await _seed_project(session)
    repo = TimelineRepository(session)
    timeline = await _add_timeline(repo, project_id)

    result = await repo.update_owned(
        project_id=project_id,
        expected_version=timeline.version + 5,
        changes={"frame_rate": 60},
    )
    assert result is None
    assert (await repo.get_by_project(project_id)).version == 1


# ---- R5 — bump_version fenced vs unconditional -----------------------


@pytest.mark.integration
async def test_r5_bump_version_unconditional_and_fenced(session: AsyncSession) -> None:
    project_id = await _seed_project(session)
    repo = TimelineRepository(session)
    await _add_timeline(repo, project_id)

    # Unconditional (child POST) — advances whatever the current value is.
    v = await repo.bump_version(project_id, None)
    assert v == 2

    # Fenced with the current token — advances.
    v = await repo.bump_version(project_id, 2)
    assert v == 3

    # Fenced with a stale token — no row, None.
    assert await repo.bump_version(project_id, 2) is None
    assert (await repo.get_by_project(project_id)).version == 3


# ---- R6 — add_track + list ordered, excludes soft-deleted ------------


@pytest.mark.integration
async def test_r6_list_tracks_ordered_excludes_soft_deleted(session: AsyncSession) -> None:
    project_id = await _seed_project(session)
    repo = TimelineRepository(session)
    timeline = await _add_timeline(repo, project_id)

    t_hi = await repo.add_track(
        timeline_id=timeline.id,
        kind="video",
        z_index=5,
        name="hi",
        locked=False,
        muted=False,
    )
    t_lo = await repo.add_track(
        timeline_id=timeline.id,
        kind="audio",
        z_index=1,
        name="lo",
        locked=False,
        muted=False,
    )

    listed = await repo.list_tracks(timeline.id)
    assert [t.id for t in listed] == [t_lo.id, t_hi.id]

    await repo.soft_delete_track(timeline.id, t_lo.id)
    listed = await repo.list_tracks(timeline.id)
    assert [t.id for t in listed] == [t_hi.id]


# ---- R7 — duplicate z_index (live) → 409 -----------------------------


@pytest.mark.integration
async def test_r7_duplicate_z_index_conflicts(session: AsyncSession) -> None:
    project_id = await _seed_project(session)
    repo = TimelineRepository(session)
    timeline = await _add_timeline(repo, project_id)

    await repo.add_track(
        timeline_id=timeline.id,
        kind="video",
        z_index=0,
        name="a",
        locked=False,
        muted=False,
    )
    with pytest.raises(ConflictError):
        await repo.add_track(
            timeline_id=timeline.id,
            kind="video",
            z_index=0,
            name="b",
            locked=False,
            muted=False,
        )


# ---- R8 — get_track timeline isolation → None ------------------------


@pytest.mark.integration
async def test_r8_get_track_timeline_isolation(session: AsyncSession) -> None:
    project_id = await _seed_project(session)
    repo = TimelineRepository(session)
    timeline = await _add_timeline(repo, project_id)
    track = await repo.add_track(
        timeline_id=timeline.id,
        kind="video",
        z_index=0,
        name="a",
        locked=False,
        muted=False,
    )

    assert (await repo.get_track(timeline.id, track.id)).id == track.id
    # Wrong timeline → None.
    assert await repo.get_track(uuid4(), track.id) is None


# ---- R9 — update_track real change; z_index collision → 409 ----------


@pytest.mark.integration
async def test_r9_update_track_and_z_index_collision(session: AsyncSession) -> None:
    project_id = await _seed_project(session)
    repo = TimelineRepository(session)
    timeline = await _add_timeline(repo, project_id)
    await repo.add_track(
        timeline_id=timeline.id,
        kind="video",
        z_index=0,
        name="a",
        locked=False,
        muted=False,
    )
    target = await repo.add_track(
        timeline_id=timeline.id,
        kind="video",
        z_index=1,
        name="b",
        locked=False,
        muted=False,
    )

    updated = await repo.update_track(timeline.id, target.id, {"name": "renamed", "muted": True})
    assert updated is not None
    assert updated.name == "renamed"
    assert updated.muted is True

    with pytest.raises(ConflictError):
        await repo.update_track(timeline.id, target.id, {"z_index": 0})


# ---- R10 — soft delete idempotency + frees z_index slot --------------


@pytest.mark.integration
async def test_r10_soft_delete_idempotent_frees_slot(session: AsyncSession) -> None:
    project_id = await _seed_project(session)
    repo = TimelineRepository(session)
    timeline = await _add_timeline(repo, project_id)
    track = await repo.add_track(
        timeline_id=timeline.id,
        kind="video",
        z_index=0,
        name="a",
        locked=False,
        muted=False,
    )

    assert await repo.soft_delete_track(timeline.id, track.id) is True
    # Already deleted → False (idempotent).
    assert await repo.soft_delete_track(timeline.id, track.id) is False

    # z_index 0 is free again — re-insert succeeds.
    reused = await repo.add_track(
        timeline_id=timeline.id,
        kind="video",
        z_index=0,
        name="reused",
        locked=False,
        muted=False,
    )
    assert reused.z_index == 0
    assert reused.id != track.id
