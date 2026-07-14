"""Unit tests for ``UpdateClip`` (Slice α6.3b).

Coverage map (α6.3b pre-flight / D1 / D4 / D5 / Q4):

* U1 — happy path: applies changes, bumps the aggregate token once, commits;
  does NOT bump ``projects.version``.
* U2 — same-value patch → 200 no-op (no write, no bump, no commit).
* U3 — stale ``version`` → ``VersionConflictError`` (412) (404-before-412 passed).
* U4 — unknown project / timeline / track / clip → ``NotFoundError`` (404).
* U5 — (re)link a valid ``media_asset_id`` → accepted.
* U6 — (re)link an unknown ``media_asset_id`` → ``ValidationFailedError`` (422).
* U7 — explicit unlink (``media_asset_id=None``) → accepted (no link check).
* U8 — merged-range violation (partial patch) → ``ValidationFailedError`` (422).
* U9 — ``clip.updated`` (INFO) emitted with the changed-field set.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
import structlog

from app.application.use_cases.timeline.results import ClipResult
from app.application.use_cases.timeline.update_clip import UpdateClip
from app.core.errors import NotFoundError, ValidationFailedError, VersionConflictError
from tests.unit.application.use_cases.timeline._helpers import (
    build_env,
    seed_clip,
    seed_media_asset,
    seed_timeline,
    seed_track,
)


@pytest.mark.unit
async def test_u1_happy_path_bumps_token_commits() -> None:
    env = build_env()
    timeline = await seed_timeline(env)
    track = await seed_track(env, timeline)
    clip = await seed_clip(env, track, volume=1.0)
    uc = UpdateClip(uow=env.uow)

    result = await uc.execute(
        project_id=env.project_id,
        track_id=track.id,
        clip_id=clip.id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
        expected_version=timeline.version,
        changes={"volume": 2.0},
    )

    assert isinstance(result, ClipResult)
    assert result.clip.volume == 2.0
    assert result.timeline_version == 2
    assert env.uow.commits == 1
    assert env.projects._rows[env.project_id].version == 1


@pytest.mark.unit
async def test_u2_same_value_noop_no_bump() -> None:
    env = build_env()
    timeline = await seed_timeline(env)
    track = await seed_track(env, timeline)
    clip = await seed_clip(env, track, volume=1.0)
    uc = UpdateClip(uow=env.uow)

    result = await uc.execute(
        project_id=env.project_id,
        track_id=track.id,
        clip_id=clip.id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
        expected_version=timeline.version,
        changes={"volume": 1.0},  # unchanged
    )

    assert result.timeline_version == 1  # no bump
    assert env.uow.commits == 0


@pytest.mark.unit
async def test_u3_stale_version_raises_412() -> None:
    env = build_env()
    timeline = await seed_timeline(env)
    track = await seed_track(env, timeline)
    clip = await seed_clip(env, track)
    uc = UpdateClip(uow=env.uow)

    with pytest.raises(VersionConflictError):
        await uc.execute(
            project_id=env.project_id,
            track_id=track.id,
            clip_id=clip.id,
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
            expected_version=timeline.version + 3,  # stale
            changes={"volume": 2.0},
        )

    assert env.uow.commits == 0


@pytest.mark.unit
async def test_u4_unknown_clip_raises_404() -> None:
    env = build_env()
    timeline = await seed_timeline(env)
    track = await seed_track(env, timeline)
    uc = UpdateClip(uow=env.uow)

    with pytest.raises(NotFoundError):
        await uc.execute(
            project_id=env.project_id,
            track_id=track.id,
            clip_id=uuid4(),  # no such clip
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
            expected_version=timeline.version,
            changes={"volume": 2.0},
        )


@pytest.mark.unit
async def test_u5_relink_valid_media_accepted() -> None:
    env = build_env()
    timeline = await seed_timeline(env)
    track = await seed_track(env, timeline)
    clip = await seed_clip(env, track)
    asset = await seed_media_asset(env)
    uc = UpdateClip(uow=env.uow)

    result = await uc.execute(
        project_id=env.project_id,
        track_id=track.id,
        clip_id=clip.id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
        expected_version=timeline.version,
        changes={"media_asset_id": asset.id},
    )

    assert result.clip.media_asset_id == asset.id
    assert env.uow.commits == 1


@pytest.mark.unit
async def test_u6_relink_unknown_media_raises_422() -> None:
    env = build_env()
    timeline = await seed_timeline(env)
    track = await seed_track(env, timeline)
    clip = await seed_clip(env, track)
    uc = UpdateClip(uow=env.uow)

    with pytest.raises(ValidationFailedError):
        await uc.execute(
            project_id=env.project_id,
            track_id=track.id,
            clip_id=clip.id,
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
            expected_version=timeline.version,
            changes={"media_asset_id": uuid4()},  # not owned
        )

    assert env.uow.commits == 0


@pytest.mark.unit
async def test_u7_explicit_unlink_accepted() -> None:
    env = build_env()
    timeline = await seed_timeline(env)
    track = await seed_track(env, timeline)
    asset = await seed_media_asset(env)
    clip = await seed_clip(env, track, media_asset_id=asset.id)
    uc = UpdateClip(uow=env.uow)

    result = await uc.execute(
        project_id=env.project_id,
        track_id=track.id,
        clip_id=clip.id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
        expected_version=timeline.version,
        changes={"media_asset_id": None},  # unlink
    )

    assert result.clip.media_asset_id is None
    assert env.uow.commits == 1


@pytest.mark.unit
async def test_u8_merged_range_violation_raises_422() -> None:
    env = build_env()
    timeline = await seed_timeline(env)
    track = await seed_track(env, timeline)
    clip = await seed_clip(env, track, start_seconds=0.0, end_seconds=5.0)
    uc = UpdateClip(uow=env.uow)

    with pytest.raises(ValidationFailedError):
        await uc.execute(
            project_id=env.project_id,
            track_id=track.id,
            clip_id=clip.id,
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
            expected_version=timeline.version,
            changes={"start_seconds": 6.0},  # now start (6) >= stored end (5)
        )

    assert env.uow.commits == 0


@pytest.mark.unit
async def test_u9_updated_log_emitted() -> None:
    env = build_env()
    timeline = await seed_timeline(env)
    track = await seed_track(env, timeline)
    clip = await seed_clip(env, track, locked=False)
    uc = UpdateClip(uow=env.uow)

    with structlog.testing.capture_logs() as logs:
        await uc.execute(
            project_id=env.project_id,
            track_id=track.id,
            clip_id=clip.id,
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
            expected_version=timeline.version,
            changes={"locked": True},
            ip="203.0.113.7",
        )

    events = [e for e in logs if e.get("event") == "clip.updated"]
    assert len(events) == 1
    assert events[0]["changed_fields"] == ["locked"]
    assert events[0]["new_version"] == 2
