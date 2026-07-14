"""Unit tests for ``CreateClip`` (Slice α6.3b).

Coverage map (α6.3b pre-flight / D1 / D4 / Q13):

* U1 — happy path (no version): appends a clip, bumps the aggregate token
  unconditionally, commits once; does NOT bump ``projects.version``.
* U2 — with a matching ``version`` fence: accepted, bumps the token.
* U3 — with a stale ``version`` fence → ``VersionConflictError`` (412), no write.
* U4 — valid ``media_asset_id`` (owned + live): accepted.
* U5 — foreign / unknown ``media_asset_id`` → ``ValidationFailedError`` (422).
* U6 — unknown project → ``NotFoundError`` (404).
* U7 — project without a timeline → ``NotFoundError`` (404).
* U8 — unknown track → ``NotFoundError`` (404).
* U9 — returns ``ClipResult`` with the new ``timeline_version`` token.
* U10 — ``clip.created`` (INFO) emitted with the field set.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
import structlog

from app.application.use_cases.timeline.create_clip import CreateClip
from app.application.use_cases.timeline.results import ClipResult
from app.core.errors import NotFoundError, ValidationFailedError, VersionConflictError
from tests.unit.application.use_cases.timeline._helpers import (
    build_env,
    seed_media_asset,
    seed_timeline,
    seed_track,
)


@pytest.mark.unit
async def test_u1_happy_path_no_version_bumps_token_commits() -> None:
    env = build_env()
    timeline = await seed_timeline(env)
    track = await seed_track(env, timeline)
    uc = CreateClip(uow=env.uow)

    result = await uc.execute(
        project_id=env.project_id,
        track_id=track.id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
        media_asset_id=None,
        start_seconds=0.0,
        end_seconds=5.0,
        source_start_seconds=0.0,
        source_end_seconds=0.0,
        volume=1.0,
        locked=False,
    )

    assert result.clip.start_seconds == 0.0
    assert result.clip.end_seconds == 5.0
    assert result.timeline_version == 2  # unconditional bump from 1
    assert env.uow.commits == 1
    assert env.projects._rows[env.project_id].version == 1


@pytest.mark.unit
async def test_u2_matching_version_fence_accepted() -> None:
    env = build_env()
    timeline = await seed_timeline(env)
    track = await seed_track(env, timeline)
    uc = CreateClip(uow=env.uow)

    result = await uc.execute(
        project_id=env.project_id,
        track_id=track.id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
        media_asset_id=None,
        start_seconds=1.0,
        end_seconds=2.0,
        source_start_seconds=0.0,
        source_end_seconds=0.0,
        volume=1.0,
        locked=False,
        expected_version=timeline.version,
    )

    assert result.timeline_version == 2
    assert env.uow.commits == 1


@pytest.mark.unit
async def test_u3_stale_version_fence_raises_412_no_write() -> None:
    env = build_env()
    timeline = await seed_timeline(env)
    track = await seed_track(env, timeline)
    uc = CreateClip(uow=env.uow)

    with pytest.raises(VersionConflictError):
        await uc.execute(
            project_id=env.project_id,
            track_id=track.id,
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
            media_asset_id=None,
            start_seconds=0.0,
            end_seconds=5.0,
            source_start_seconds=0.0,
            source_end_seconds=0.0,
            volume=1.0,
            locked=False,
            expected_version=timeline.version + 5,  # stale
        )

    assert env.uow.commits == 0
    assert env.timeline._clips == {}
    assert env.timeline._timelines[timeline.id].version == 1


@pytest.mark.unit
async def test_u4_valid_media_asset_accepted() -> None:
    env = build_env()
    timeline = await seed_timeline(env)
    track = await seed_track(env, timeline)
    asset = await seed_media_asset(env)
    uc = CreateClip(uow=env.uow)

    result = await uc.execute(
        project_id=env.project_id,
        track_id=track.id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
        media_asset_id=asset.id,
        start_seconds=0.0,
        end_seconds=5.0,
        source_start_seconds=0.0,
        source_end_seconds=0.0,
        volume=1.0,
        locked=False,
    )

    assert result.clip.media_asset_id == asset.id
    assert env.uow.commits == 1


@pytest.mark.unit
async def test_u5_unknown_media_asset_raises_422_no_write() -> None:
    env = build_env()
    timeline = await seed_timeline(env)
    track = await seed_track(env, timeline)
    uc = CreateClip(uow=env.uow)

    with pytest.raises(ValidationFailedError):
        await uc.execute(
            project_id=env.project_id,
            track_id=track.id,
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
            media_asset_id=uuid4(),  # not owned / does not exist
            start_seconds=0.0,
            end_seconds=5.0,
            source_start_seconds=0.0,
            source_end_seconds=0.0,
            volume=1.0,
            locked=False,
        )

    assert env.uow.commits == 0
    assert env.timeline._clips == {}
    assert env.timeline._timelines[timeline.id].version == 1


@pytest.mark.unit
async def test_u6_unknown_project_raises_404() -> None:
    env = build_env()
    timeline = await seed_timeline(env)
    track = await seed_track(env, timeline)
    uc = CreateClip(uow=env.uow)

    with pytest.raises(NotFoundError):
        await uc.execute(
            project_id=uuid4(),
            track_id=track.id,
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
            media_asset_id=None,
            start_seconds=0.0,
            end_seconds=5.0,
            source_start_seconds=0.0,
            source_end_seconds=0.0,
            volume=1.0,
            locked=False,
        )


@pytest.mark.unit
async def test_u7_no_timeline_raises_404() -> None:
    env = build_env()
    uc = CreateClip(uow=env.uow)

    with pytest.raises(NotFoundError):
        await uc.execute(
            project_id=env.project_id,
            track_id=uuid4(),
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
            media_asset_id=None,
            start_seconds=0.0,
            end_seconds=5.0,
            source_start_seconds=0.0,
            source_end_seconds=0.0,
            volume=1.0,
            locked=False,
        )


@pytest.mark.unit
async def test_u8_unknown_track_raises_404() -> None:
    env = build_env()
    await seed_timeline(env)
    uc = CreateClip(uow=env.uow)

    with pytest.raises(NotFoundError):
        await uc.execute(
            project_id=env.project_id,
            track_id=uuid4(),  # no such track
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
            media_asset_id=None,
            start_seconds=0.0,
            end_seconds=5.0,
            source_start_seconds=0.0,
            source_end_seconds=0.0,
            volume=1.0,
            locked=False,
        )


@pytest.mark.unit
async def test_u9_returns_clip_result_with_token() -> None:
    env = build_env()
    timeline = await seed_timeline(env)
    track = await seed_track(env, timeline)
    uc = CreateClip(uow=env.uow)

    result = await uc.execute(
        project_id=env.project_id,
        track_id=track.id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
        media_asset_id=None,
        start_seconds=2.0,
        end_seconds=8.0,
        source_start_seconds=1.0,
        source_end_seconds=7.0,
        volume=2.5,
        locked=True,
    )

    assert isinstance(result, ClipResult)
    assert result.clip.volume == 2.5
    assert result.clip.locked is True
    assert result.clip.source_start_seconds == 1.0
    assert result.clip.source_end_seconds == 7.0
    assert result.timeline_version == 2


@pytest.mark.unit
async def test_u10_created_log_emitted() -> None:
    env = build_env()
    timeline = await seed_timeline(env)
    track = await seed_track(env, timeline)
    uc = CreateClip(uow=env.uow)

    with structlog.testing.capture_logs() as logs:
        result = await uc.execute(
            project_id=env.project_id,
            track_id=track.id,
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
            media_asset_id=None,
            start_seconds=0.0,
            end_seconds=5.0,
            source_start_seconds=0.0,
            source_end_seconds=0.0,
            volume=1.0,
            locked=False,
            ip="203.0.113.7",
        )

    events = [e for e in logs if e.get("event") == "clip.created"]
    assert len(events) == 1
    ev = events[0]
    assert ev["clip_id"] == str(result.clip.id)
    assert ev["track_id"] == str(track.id)
    assert ev["new_version"] == 2
