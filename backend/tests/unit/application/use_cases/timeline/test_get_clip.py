"""Unit tests for ``GetClip`` (Slice α6.3b).

Coverage map (α6.3b pre-flight / D3):

* U1 — happy path: returns the clip + the aggregate token; read-only (no commit).
* U2 — unknown project → ``NotFoundError`` (404).
* U3 — project without a timeline → ``NotFoundError`` (404).
* U4 — unknown track → ``NotFoundError`` (404).
* U5 — unknown clip → ``NotFoundError`` (404).
* U6 — clip of another track → ``NotFoundError`` (404) (cross-parent isolation).
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.application.use_cases.timeline.get_clip import GetClip
from app.application.use_cases.timeline.results import ClipResult
from app.core.errors import NotFoundError
from tests.unit.application.use_cases.timeline._helpers import (
    build_env,
    seed_clip,
    seed_timeline,
    seed_track,
)


@pytest.mark.unit
async def test_u1_happy_path_returns_clip_and_token() -> None:
    env = build_env()
    timeline = await seed_timeline(env)
    track = await seed_track(env, timeline)
    clip = await seed_clip(env, track)
    uc = GetClip(uow=env.uow)

    result = await uc.execute(
        project_id=env.project_id,
        track_id=track.id,
        clip_id=clip.id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
    )

    assert isinstance(result, ClipResult)
    assert result.clip.id == clip.id
    assert result.timeline_version == timeline.version
    assert env.uow.commits == 0


@pytest.mark.unit
async def test_u2_unknown_project_raises_404() -> None:
    env = build_env()
    timeline = await seed_timeline(env)
    track = await seed_track(env, timeline)
    clip = await seed_clip(env, track)
    uc = GetClip(uow=env.uow)

    with pytest.raises(NotFoundError):
        await uc.execute(
            project_id=uuid4(),
            track_id=track.id,
            clip_id=clip.id,
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
        )


@pytest.mark.unit
async def test_u3_no_timeline_raises_404() -> None:
    env = build_env()
    uc = GetClip(uow=env.uow)

    with pytest.raises(NotFoundError):
        await uc.execute(
            project_id=env.project_id,
            track_id=uuid4(),
            clip_id=uuid4(),
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
        )


@pytest.mark.unit
async def test_u4_unknown_track_raises_404() -> None:
    env = build_env()
    await seed_timeline(env)
    uc = GetClip(uow=env.uow)

    with pytest.raises(NotFoundError):
        await uc.execute(
            project_id=env.project_id,
            track_id=uuid4(),
            clip_id=uuid4(),
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
        )


@pytest.mark.unit
async def test_u5_unknown_clip_raises_404() -> None:
    env = build_env()
    timeline = await seed_timeline(env)
    track = await seed_track(env, timeline)
    uc = GetClip(uow=env.uow)

    with pytest.raises(NotFoundError):
        await uc.execute(
            project_id=env.project_id,
            track_id=track.id,
            clip_id=uuid4(),
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
        )


@pytest.mark.unit
async def test_u6_clip_of_another_track_raises_404() -> None:
    env = build_env()
    timeline = await seed_timeline(env)
    track_a = await seed_track(env, timeline, z_index=0)
    track_b = await seed_track(env, timeline, z_index=1)
    clip = await seed_clip(env, track_a)
    uc = GetClip(uow=env.uow)

    with pytest.raises(NotFoundError):
        await uc.execute(
            project_id=env.project_id,
            track_id=track_b.id,  # clip belongs to track_a
            clip_id=clip.id,
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
        )
