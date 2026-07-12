"""Unit tests for ``UpdateTimeline`` (Slice α6.3a).

Coverage map (α6.3 pre-flight):

* U1 — happy path: a real change bumps ``timelines.version`` by 1, commits once;
  does NOT bump ``projects.version``.
* U2 — same-value patch → 200 no-op (no write, no version bump, no commit).
* U3 — stale version → ``VersionConflictError`` (412), no write.
* U4 — unknown project → ``NotFoundError`` (404), before the fence.
* U5 — project without a timeline → ``NotFoundError`` (404).
* U6 — returned result carries the current tracks.
* U7 — ``timeline.updated`` (INFO) emitted with the changed-field set.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
import structlog

from app.application.use_cases.timeline.update_timeline import UpdateTimeline
from app.core.errors import NotFoundError, VersionConflictError
from tests.unit.application.use_cases.timeline._helpers import (
    build_env,
    seed_timeline,
    seed_track,
)


@pytest.mark.unit
async def test_u1_happy_path_bumps_version_commits() -> None:
    env = build_env()
    timeline = await seed_timeline(env, frame_rate=30)
    uc = UpdateTimeline(uow=env.uow)

    result = await uc.execute(
        project_id=env.project_id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
        expected_version=timeline.version,
        changes={"frame_rate": 60},
    )

    assert result.timeline.frame_rate == 60
    assert result.timeline.version == 2
    assert env.uow.commits == 1
    assert env.projects._rows[env.project_id].version == 1


@pytest.mark.unit
async def test_u2_same_value_patch_is_noop() -> None:
    env = build_env()
    timeline = await seed_timeline(env, frame_rate=30)
    uc = UpdateTimeline(uow=env.uow)

    result = await uc.execute(
        project_id=env.project_id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
        expected_version=timeline.version,
        changes={"frame_rate": 30},  # unchanged
    )

    assert result.timeline.version == 1
    assert env.uow.commits == 0


@pytest.mark.unit
async def test_u3_stale_version_raises_412_no_write() -> None:
    env = build_env()
    timeline = await seed_timeline(env, frame_rate=30)
    uc = UpdateTimeline(uow=env.uow)

    with pytest.raises(VersionConflictError):
        await uc.execute(
            project_id=env.project_id,
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
            expected_version=timeline.version + 5,  # stale
            changes={"frame_rate": 60},
        )

    assert env.uow.commits == 0
    assert env.timeline._timelines[timeline.id].frame_rate == 30
    assert env.timeline._timelines[timeline.id].version == 1


@pytest.mark.unit
async def test_u4_unknown_project_raises_404() -> None:
    env = build_env()
    await seed_timeline(env)
    uc = UpdateTimeline(uow=env.uow)

    with pytest.raises(NotFoundError):
        await uc.execute(
            project_id=uuid4(),
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
            expected_version=1,
            changes={"frame_rate": 60},
        )


@pytest.mark.unit
async def test_u5_no_timeline_raises_404() -> None:
    env = build_env()
    uc = UpdateTimeline(uow=env.uow)

    with pytest.raises(NotFoundError):
        await uc.execute(
            project_id=env.project_id,
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
            expected_version=1,
            changes={"frame_rate": 60},
        )


@pytest.mark.unit
async def test_u6_result_carries_current_tracks() -> None:
    env = build_env()
    timeline = await seed_timeline(env)
    await seed_track(env, timeline, z_index=0)
    await seed_track(env, timeline, z_index=1)
    uc = UpdateTimeline(uow=env.uow)

    result = await uc.execute(
        project_id=env.project_id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
        expected_version=timeline.version,
        changes={"background_color": "#ffffff"},
    )

    assert [t.z_index for t in result.tracks] == [0, 1]


@pytest.mark.unit
async def test_u7_updated_log_carries_changed_fields() -> None:
    env = build_env()
    timeline = await seed_timeline(env, background_color="#000000")
    uc = UpdateTimeline(uow=env.uow)

    with structlog.testing.capture_logs() as logs:
        await uc.execute(
            project_id=env.project_id,
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
            expected_version=timeline.version,
            changes={"background_color": "#123456"},
            ip="203.0.113.7",
        )

    events = [e for e in logs if e.get("event") == "timeline.updated"]
    assert len(events) == 1
    ev = events[0]
    assert ev["changed_fields"] == ["background_color"]
    assert ev["previous_version"] == 1
    assert ev["new_version"] == 2
