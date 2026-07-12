"""Unit tests for ``UpdateTrack`` (Slice α6.3a).

Coverage map (α6.3 pre-flight / Q5 / Q13):

* U1 — happy path: a real change updates the track + bumps the aggregate token
  by 1, commits once; does NOT bump ``projects.version``.
* U2 — same-value patch → 200 no-op (no write, no bump, no commit).
* U3 — stale timeline version → ``VersionConflictError`` (412), no write.
* U4 — ``z_index`` collision with another live track → ``ConflictError`` (409).
* U5 — unknown track → ``NotFoundError`` (404), before the fence (404-before-412).
* U6 — unknown project / timeline → ``NotFoundError`` (404).
* U7 — ``track.updated`` (INFO) emitted with the changed-field set.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
import structlog

from app.application.use_cases.timeline.update_track import UpdateTrack
from app.core.errors import ConflictError, NotFoundError, VersionConflictError
from tests.unit.application.use_cases.timeline._helpers import (
    build_env,
    seed_timeline,
    seed_track,
)


@pytest.mark.unit
async def test_u1_happy_path_updates_and_bumps_token() -> None:
    env = build_env()
    timeline = await seed_timeline(env)
    track = await seed_track(env, timeline, z_index=0, name="old")
    uc = UpdateTrack(uow=env.uow)

    result = await uc.execute(
        project_id=env.project_id,
        track_id=track.id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
        expected_version=timeline.version,
        changes={"name": "new"},
    )

    assert result.track.name == "new"
    assert result.timeline_version == 2
    assert env.uow.commits == 1
    assert env.projects._rows[env.project_id].version == 1


@pytest.mark.unit
async def test_u2_same_value_patch_is_noop() -> None:
    env = build_env()
    timeline = await seed_timeline(env)
    track = await seed_track(env, timeline, name="same")
    uc = UpdateTrack(uow=env.uow)

    result = await uc.execute(
        project_id=env.project_id,
        track_id=track.id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
        expected_version=timeline.version,
        changes={"name": "same"},
    )

    assert result.timeline_version == 1
    assert env.uow.commits == 0


@pytest.mark.unit
async def test_u3_stale_version_raises_412_no_write() -> None:
    env = build_env()
    timeline = await seed_timeline(env)
    track = await seed_track(env, timeline, name="old")
    uc = UpdateTrack(uow=env.uow)

    with pytest.raises(VersionConflictError):
        await uc.execute(
            project_id=env.project_id,
            track_id=track.id,
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
            expected_version=timeline.version + 5,  # stale
            changes={"name": "new"},
        )

    assert env.uow.commits == 0
    assert env.timeline._tracks[track.id].name == "old"
    assert env.timeline._timelines[timeline.id].version == 1


@pytest.mark.unit
async def test_u4_z_index_collision_raises_409() -> None:
    env = build_env()
    timeline = await seed_timeline(env)
    await seed_track(env, timeline, z_index=0, name="a")
    target = await seed_track(env, timeline, z_index=1, name="b")
    uc = UpdateTrack(uow=env.uow)

    with pytest.raises(ConflictError):
        await uc.execute(
            project_id=env.project_id,
            track_id=target.id,
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
            expected_version=timeline.version,
            changes={"z_index": 0},  # collides with the other track
        )

    assert env.uow.commits == 0


@pytest.mark.unit
async def test_u5_unknown_track_raises_404_before_fence() -> None:
    env = build_env()
    timeline = await seed_timeline(env)
    uc = UpdateTrack(uow=env.uow)

    # A stale version is supplied, but the missing track must be 404 (404-before-412).
    with pytest.raises(NotFoundError):
        await uc.execute(
            project_id=env.project_id,
            track_id=uuid4(),
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
            expected_version=timeline.version + 5,
            changes={"name": "new"},
        )


@pytest.mark.unit
async def test_u6_unknown_project_raises_404() -> None:
    env = build_env()
    timeline = await seed_timeline(env)
    track = await seed_track(env, timeline)
    uc = UpdateTrack(uow=env.uow)

    with pytest.raises(NotFoundError):
        await uc.execute(
            project_id=uuid4(),
            track_id=track.id,
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
            expected_version=timeline.version,
            changes={"name": "new"},
        )


@pytest.mark.unit
async def test_u7_updated_log_carries_changed_fields() -> None:
    env = build_env()
    timeline = await seed_timeline(env)
    track = await seed_track(env, timeline, muted=False)
    uc = UpdateTrack(uow=env.uow)

    with structlog.testing.capture_logs() as logs:
        await uc.execute(
            project_id=env.project_id,
            track_id=track.id,
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
            expected_version=timeline.version,
            changes={"muted": True},
            ip="203.0.113.7",
        )

    events = [e for e in logs if e.get("event") == "track.updated"]
    assert len(events) == 1
    ev = events[0]
    assert ev["changed_fields"] == ["muted"]
    assert ev["previous_version"] == 1
    assert ev["new_version"] == 2
