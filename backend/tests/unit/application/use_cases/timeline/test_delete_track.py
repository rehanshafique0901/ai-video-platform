"""Unit tests for ``DeleteTrack`` (Slice α6.3a).

Coverage map (α6.3 pre-flight / Q13):

* U1 — happy path: soft-deletes the track, bumps the aggregate token, commits;
  does NOT bump ``projects.version``. Returns the new token.
* U2 — a repeat delete is ``NotFoundError`` (404) — idempotent-by-404.
* U3 — stale timeline version on a live track → ``VersionConflictError`` (412).
* U4 — unknown track → ``NotFoundError`` (404), before the fence (404-before-412).
* U5 — unknown project → ``NotFoundError`` (404).
* U6 — deletion frees the ``z_index`` slot (re-create at the same z_index).
* U7 — ``track.deleted`` (INFO) emitted with the field set.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
import structlog

from app.application.use_cases.timeline.create_track import CreateTrack
from app.application.use_cases.timeline.delete_track import DeleteTrack
from app.core.errors import NotFoundError, VersionConflictError
from tests.unit.application.use_cases.timeline._helpers import (
    build_env,
    seed_timeline,
    seed_track,
)


@pytest.mark.unit
async def test_u1_happy_path_soft_deletes_bumps_token() -> None:
    env = build_env()
    timeline = await seed_timeline(env)
    track = await seed_track(env, timeline, z_index=0)
    uc = DeleteTrack(uow=env.uow)

    new_version = await uc.execute(
        project_id=env.project_id,
        track_id=track.id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
        expected_version=timeline.version,
    )

    assert new_version == 2
    assert track.id not in env.timeline._tracks
    assert env.uow.commits == 1
    assert env.projects._rows[env.project_id].version == 1


@pytest.mark.unit
async def test_u2_repeat_delete_is_404() -> None:
    env = build_env()
    timeline = await seed_timeline(env)
    track = await seed_track(env, timeline)
    uc = DeleteTrack(uow=env.uow)

    await uc.execute(
        project_id=env.project_id,
        track_id=track.id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
        expected_version=timeline.version,
    )

    # The aggregate token advanced to 2; a repeat delete is still 404 (not 412).
    with pytest.raises(NotFoundError):
        await uc.execute(
            project_id=env.project_id,
            track_id=track.id,
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
            expected_version=2,
        )


@pytest.mark.unit
async def test_u3_stale_version_on_live_track_raises_412() -> None:
    env = build_env()
    timeline = await seed_timeline(env)
    track = await seed_track(env, timeline)
    uc = DeleteTrack(uow=env.uow)

    with pytest.raises(VersionConflictError):
        await uc.execute(
            project_id=env.project_id,
            track_id=track.id,
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
            expected_version=timeline.version + 5,  # stale
        )

    assert env.uow.commits == 0
    assert track.id in env.timeline._tracks


@pytest.mark.unit
async def test_u4_unknown_track_raises_404_before_fence() -> None:
    env = build_env()
    timeline = await seed_timeline(env)
    uc = DeleteTrack(uow=env.uow)

    with pytest.raises(NotFoundError):
        await uc.execute(
            project_id=env.project_id,
            track_id=uuid4(),
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
            expected_version=timeline.version + 5,  # stale, but track missing → 404
        )


@pytest.mark.unit
async def test_u5_unknown_project_raises_404() -> None:
    env = build_env()
    timeline = await seed_timeline(env)
    track = await seed_track(env, timeline)
    uc = DeleteTrack(uow=env.uow)

    with pytest.raises(NotFoundError):
        await uc.execute(
            project_id=uuid4(),
            track_id=track.id,
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
            expected_version=timeline.version,
        )


@pytest.mark.unit
async def test_u6_delete_frees_z_index_slot() -> None:
    env = build_env()
    timeline = await seed_timeline(env)
    track = await seed_track(env, timeline, z_index=0)
    delete_uc = DeleteTrack(uow=env.uow)
    create_uc = CreateTrack(uow=env.uow)

    new_version = await delete_uc.execute(
        project_id=env.project_id,
        track_id=track.id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
        expected_version=timeline.version,
    )

    # z_index 0 is now free — re-create at the same slot.
    result = await create_uc.execute(
        project_id=env.project_id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
        kind="video",
        z_index=0,
        name="reused",
        expected_version=new_version,
    )

    assert result.track.z_index == 0
    assert result.timeline_version == new_version + 1


@pytest.mark.unit
async def test_u7_deleted_log_emitted() -> None:
    env = build_env()
    timeline = await seed_timeline(env)
    track = await seed_track(env, timeline)
    uc = DeleteTrack(uow=env.uow)

    with structlog.testing.capture_logs() as logs:
        await uc.execute(
            project_id=env.project_id,
            track_id=track.id,
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
            expected_version=timeline.version,
            ip="203.0.113.7",
        )

    events = [e for e in logs if e.get("event") == "track.deleted"]
    assert len(events) == 1
    ev = events[0]
    assert ev["track_id"] == str(track.id)
    assert ev["previous_version"] == 1
    assert ev["new_version"] == 2
