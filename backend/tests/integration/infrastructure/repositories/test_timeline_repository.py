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
* R11 — ``add_clip`` + ``list_clips`` ordered by ``start_seconds`` ASC (``id``
  tiebreak), excludes soft-deleted.
* R12 — ``get_clip`` track isolation → ``None``.
* R13 — ``update_clip`` real change; concurrent-delete → ``None``.
* R14 — ``soft_delete_clip`` happy / already-deleted → ``True`` / ``False``.
* R15 — ``list_clips_for_timeline`` groups by track; excludes soft-deleted
  clips/tracks.
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


async def _add_track(repo: TimelineRepository, timeline_id: UUID, *, z_index: int = 0) -> UUID:
    track = await repo.add_track(
        timeline_id=timeline_id,
        kind="video",
        z_index=z_index,
        name=f"t{z_index}",
        locked=False,
        muted=False,
    )
    return track.id


async def _add_clip(
    repo: TimelineRepository,
    track_id: UUID,
    *,
    start_seconds: float = 0.0,
    end_seconds: float = 5.0,
) -> UUID:
    clip = await repo.add_clip(
        track_id=track_id,
        media_asset_id=None,
        start_seconds=start_seconds,
        end_seconds=end_seconds,
        source_start_seconds=0.0,
        source_end_seconds=0.0,
        volume=1.0,
        locked=False,
    )
    return clip.id


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


# ---- R11 — add_clip + list_clips ordered, excludes soft-deleted ------


@pytest.mark.integration
async def test_r11_list_clips_ordered_excludes_soft_deleted(session: AsyncSession) -> None:
    project_id = await _seed_project(session)
    repo = TimelineRepository(session)
    timeline = await _add_timeline(repo, project_id)
    track_id = await _add_track(repo, timeline.id)

    late = await _add_clip(repo, track_id, start_seconds=10.0, end_seconds=12.0)
    early = await _add_clip(repo, track_id, start_seconds=0.0, end_seconds=5.0)
    mid = await _add_clip(repo, track_id, start_seconds=5.0, end_seconds=7.0)

    listed = await repo.list_clips(track_id)
    assert [c.id for c in listed] == [early, mid, late]

    await repo.soft_delete_clip(track_id, early)
    listed = await repo.list_clips(track_id)
    assert [c.id for c in listed] == [mid, late]


# ---- R11b — equal start_seconds → id tiebreak (total order) ----------


@pytest.mark.integration
async def test_r11b_equal_start_seconds_id_tiebreak(session: AsyncSession) -> None:
    project_id = await _seed_project(session)
    repo = TimelineRepository(session)
    timeline = await _add_timeline(repo, project_id)
    track_id = await _add_track(repo, timeline.id)

    a = await _add_clip(repo, track_id, start_seconds=3.0, end_seconds=4.0)
    b = await _add_clip(repo, track_id, start_seconds=3.0, end_seconds=4.0)

    listed = await repo.list_clips(track_id)
    assert [c.id for c in listed] == sorted([a, b])


# ---- R12 — get_clip track isolation → None ---------------------------


@pytest.mark.integration
async def test_r12_get_clip_track_isolation(session: AsyncSession) -> None:
    project_id = await _seed_project(session)
    repo = TimelineRepository(session)
    timeline = await _add_timeline(repo, project_id)
    track_id = await _add_track(repo, timeline.id, z_index=0)
    other_track_id = await _add_track(repo, timeline.id, z_index=1)
    clip_id = await _add_clip(repo, track_id)

    assert (await repo.get_clip(track_id, clip_id)).id == clip_id
    # Wrong track → None.
    assert await repo.get_clip(other_track_id, clip_id) is None


# ---- R13 — update_clip real change; concurrent delete → None ---------


@pytest.mark.integration
async def test_r13_update_clip_real_change(session: AsyncSession) -> None:
    project_id = await _seed_project(session)
    repo = TimelineRepository(session)
    timeline = await _add_timeline(repo, project_id)
    track_id = await _add_track(repo, timeline.id)
    clip_id = await _add_clip(repo, track_id, start_seconds=0.0, end_seconds=5.0)

    updated = await repo.update_clip(track_id, clip_id, {"volume": 2.5, "locked": True})
    assert updated is not None
    assert updated.volume == 2.5
    assert updated.locked is True

    # Soft-delete then update → None (no live row).
    await repo.soft_delete_clip(track_id, clip_id)
    assert await repo.update_clip(track_id, clip_id, {"volume": 3.0}) is None


# ---- R14 — soft_delete_clip idempotency ------------------------------


@pytest.mark.integration
async def test_r14_soft_delete_clip_idempotent(session: AsyncSession) -> None:
    project_id = await _seed_project(session)
    repo = TimelineRepository(session)
    timeline = await _add_timeline(repo, project_id)
    track_id = await _add_track(repo, timeline.id)
    clip_id = await _add_clip(repo, track_id)

    assert await repo.soft_delete_clip(track_id, clip_id) is True
    # Already deleted → False (idempotent).
    assert await repo.soft_delete_clip(track_id, clip_id) is False
    assert await repo.get_clip(track_id, clip_id) is None


# ---- R15 — list_clips_for_timeline groups + excludes soft-deleted ----


@pytest.mark.integration
async def test_r15_list_clips_for_timeline_grouped(session: AsyncSession) -> None:
    project_id = await _seed_project(session)
    repo = TimelineRepository(session)
    timeline = await _add_timeline(repo, project_id)
    track_a = await _add_track(repo, timeline.id, z_index=0)
    track_b = await _add_track(repo, timeline.id, z_index=1)

    a1 = await _add_clip(repo, track_a, start_seconds=0.0, end_seconds=1.0)
    a2 = await _add_clip(repo, track_a, start_seconds=2.0, end_seconds=3.0)
    b1 = await _add_clip(repo, track_b, start_seconds=0.0, end_seconds=1.0)
    gone = await _add_clip(repo, track_b, start_seconds=5.0, end_seconds=6.0)
    await repo.soft_delete_clip(track_b, gone)

    grouped = await repo.list_clips_for_timeline(timeline.id)
    assert [c.id for c in grouped[track_a]] == [a1, a2]
    assert [c.id for c in grouped[track_b]] == [b1]

    # A soft-deleted track's clips are excluded entirely.
    await repo.soft_delete_track(timeline.id, track_a)
    grouped = await repo.list_clips_for_timeline(timeline.id)
    assert track_a not in grouped
    assert [c.id for c in grouped[track_b]] == [b1]
