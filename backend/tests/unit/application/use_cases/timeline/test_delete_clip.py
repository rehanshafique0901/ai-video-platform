"""Unit tests for ``DeleteClip`` (Slice α6.3b).

Coverage map (α6.3b pre-flight / D1 / Q3):

* U1 — happy path: soft-deletes the clip, bumps the aggregate token, commits;
  does NOT bump ``projects.version``.
* U2 — repeat delete → ``NotFoundError`` (404) (idempotent-by-404).
* U3 — stale ``version`` on a live clip → ``VersionConflictError`` (412).
* U4 — 404-before-412: a missing clip with a stale token is 404, not 412.
* U5 — unknown project / timeline / track → ``NotFoundError`` (404).
* U6 — ``clip.deleted`` (INFO) emitted with the field set.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
import structlog

from app.application.use_cases.timeline.delete_clip import DeleteClip
from app.core.errors import NotFoundError, VersionConflictError
from tests.unit.application.use_cases.timeline._helpers import (
    build_env,
    seed_clip,
    seed_timeline,
    seed_track,
)


@pytest.mark.unit
async def test_u1_happy_path_soft_deletes_bumps_commits() -> None:
    env = build_env()
    timeline = await seed_timeline(env)
    track = await seed_track(env, timeline)
    clip = await seed_clip(env, track)
    uc = DeleteClip(uow=env.uow)

    new_version = await uc.execute(
        project_id=env.project_id,
        track_id=track.id,
        clip_id=clip.id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
        expected_version=timeline.version,
    )

    assert new_version == 2
    assert env.uow.commits == 1
    assert env.timeline._clips == {}
    assert env.projects._rows[env.project_id].version == 1


@pytest.mark.unit
async def test_u2_repeat_delete_raises_404() -> None:
    env = build_env()
    timeline = await seed_timeline(env)
    track = await seed_track(env, timeline)
    clip = await seed_clip(env, track)
    uc = DeleteClip(uow=env.uow)

    first = await uc.execute(
        project_id=env.project_id,
        track_id=track.id,
        clip_id=clip.id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
        expected_version=timeline.version,
    )

    with pytest.raises(NotFoundError):
        await uc.execute(
            project_id=env.project_id,
            track_id=track.id,
            clip_id=clip.id,
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
            expected_version=first,  # even with the fresh token → 404 (gone)
        )


@pytest.mark.unit
async def test_u3_stale_version_on_live_clip_raises_412() -> None:
    env = build_env()
    timeline = await seed_timeline(env)
    track = await seed_track(env, timeline)
    clip = await seed_clip(env, track)
    uc = DeleteClip(uow=env.uow)

    with pytest.raises(VersionConflictError):
        await uc.execute(
            project_id=env.project_id,
            track_id=track.id,
            clip_id=clip.id,
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
            expected_version=timeline.version + 9,  # stale
        )

    assert env.uow.commits == 0
    assert clip.id in env.timeline._clips


@pytest.mark.unit
async def test_u4_missing_clip_stale_token_is_404_not_412() -> None:
    env = build_env()
    timeline = await seed_timeline(env)
    track = await seed_track(env, timeline)
    uc = DeleteClip(uow=env.uow)

    with pytest.raises(NotFoundError):
        await uc.execute(
            project_id=env.project_id,
            track_id=track.id,
            clip_id=uuid4(),  # no such clip
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
            expected_version=timeline.version + 9,  # stale — but 404 wins
        )


@pytest.mark.unit
async def test_u5_unknown_track_raises_404() -> None:
    env = build_env()
    await seed_timeline(env)
    uc = DeleteClip(uow=env.uow)

    with pytest.raises(NotFoundError):
        await uc.execute(
            project_id=env.project_id,
            track_id=uuid4(),
            clip_id=uuid4(),
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
            expected_version=1,
        )


@pytest.mark.unit
async def test_u6_deleted_log_emitted() -> None:
    env = build_env()
    timeline = await seed_timeline(env)
    track = await seed_track(env, timeline)
    clip = await seed_clip(env, track)
    uc = DeleteClip(uow=env.uow)

    with structlog.testing.capture_logs() as logs:
        await uc.execute(
            project_id=env.project_id,
            track_id=track.id,
            clip_id=clip.id,
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
            expected_version=timeline.version,
            ip="203.0.113.7",
        )

    events = [e for e in logs if e.get("event") == "clip.deleted"]
    assert len(events) == 1
    assert events[0]["clip_id"] == str(clip.id)
    assert events[0]["new_version"] == 2
