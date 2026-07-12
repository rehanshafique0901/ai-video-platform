"""Unit tests for ``CreateTrack`` (Slice α6.3a).

Coverage map (α6.3 pre-flight / Q5 / Q13):

* U1 — happy path (no version): appends a track, bumps the aggregate token
  unconditionally, commits once; does NOT bump ``projects.version``.
* U2 — with a matching ``version`` fence: accepted, bumps the token.
* U3 — with a stale ``version`` fence → ``VersionConflictError`` (412), no write.
* U4 — ``z_index`` collision → ``ConflictError`` (409), no version bump / commit.
* U5 — unknown project → ``NotFoundError`` (404).
* U6 — project without a timeline → ``NotFoundError`` (404).
* U7 — returns ``TrackResult`` with the new ``timeline_version`` token.
* U8 — ``track.created`` (INFO) emitted with the field set.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
import structlog

from app.application.use_cases.timeline.create_track import CreateTrack
from app.application.use_cases.timeline.results import TrackResult
from app.core.errors import ConflictError, NotFoundError, VersionConflictError
from tests.unit.application.use_cases.timeline._helpers import (
    build_env,
    seed_timeline,
    seed_track,
)


@pytest.mark.unit
async def test_u1_happy_path_no_version_bumps_token_commits() -> None:
    env = build_env()
    await seed_timeline(env)
    uc = CreateTrack(uow=env.uow)

    result = await uc.execute(
        project_id=env.project_id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
        kind="video",
        z_index=0,
        name="Video 1",
    )

    assert result.track.kind == "video"
    assert result.track.z_index == 0
    assert result.timeline_version == 2  # unconditional bump from 1
    assert env.uow.commits == 1
    assert env.projects._rows[env.project_id].version == 1


@pytest.mark.unit
async def test_u2_matching_version_fence_accepted() -> None:
    env = build_env()
    timeline = await seed_timeline(env)
    uc = CreateTrack(uow=env.uow)

    result = await uc.execute(
        project_id=env.project_id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
        kind="audio",
        z_index=1,
        name="Audio 1",
        expected_version=timeline.version,
    )

    assert result.timeline_version == 2
    assert env.uow.commits == 1


@pytest.mark.unit
async def test_u3_stale_version_fence_raises_412_no_write() -> None:
    env = build_env()
    timeline = await seed_timeline(env)
    uc = CreateTrack(uow=env.uow)

    with pytest.raises(VersionConflictError):
        await uc.execute(
            project_id=env.project_id,
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
            kind="video",
            z_index=0,
            name="Video 1",
            expected_version=timeline.version + 5,  # stale
        )

    assert env.uow.commits == 0
    assert env.timeline._tracks == {}
    assert env.timeline._timelines[timeline.id].version == 1


@pytest.mark.unit
async def test_u4_z_index_collision_raises_409_no_bump() -> None:
    env = build_env()
    timeline = await seed_timeline(env)
    await seed_track(env, timeline, z_index=2)
    uc = CreateTrack(uow=env.uow)

    with pytest.raises(ConflictError):
        await uc.execute(
            project_id=env.project_id,
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
            kind="video",
            z_index=2,  # collision
            name="Video 2",
        )

    assert env.uow.commits == 0
    assert env.timeline._timelines[timeline.id].version == 1
    assert len(env.timeline._tracks) == 1


@pytest.mark.unit
async def test_u5_unknown_project_raises_404() -> None:
    env = build_env()
    await seed_timeline(env)
    uc = CreateTrack(uow=env.uow)

    with pytest.raises(NotFoundError):
        await uc.execute(
            project_id=uuid4(),
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
            kind="video",
            z_index=0,
            name="Video 1",
        )


@pytest.mark.unit
async def test_u6_no_timeline_raises_404() -> None:
    env = build_env()
    uc = CreateTrack(uow=env.uow)

    with pytest.raises(NotFoundError):
        await uc.execute(
            project_id=env.project_id,
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
            kind="video",
            z_index=0,
            name="Video 1",
        )


@pytest.mark.unit
async def test_u7_returns_track_result_with_token() -> None:
    env = build_env()
    await seed_timeline(env)
    uc = CreateTrack(uow=env.uow)

    result = await uc.execute(
        project_id=env.project_id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
        kind="subtitle",
        z_index=3,
        name="Subs",
        locked=True,
        muted=True,
    )

    assert isinstance(result, TrackResult)
    assert result.track.locked is True
    assert result.track.muted is True
    assert result.timeline_version == 2


@pytest.mark.unit
async def test_u8_created_log_emitted() -> None:
    env = build_env()
    await seed_timeline(env)
    uc = CreateTrack(uow=env.uow)

    with structlog.testing.capture_logs() as logs:
        result = await uc.execute(
            project_id=env.project_id,
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
            kind="effect",
            z_index=4,
            name="FX",
            ip="203.0.113.7",
        )

    events = [e for e in logs if e.get("event") == "track.created"]
    assert len(events) == 1
    ev = events[0]
    assert ev["track_id"] == str(result.track.id)
    assert ev["kind"] == "effect"
    assert ev["z_index"] == 4
    assert ev["new_version"] == 2
