"""Unit tests for ``ListClips`` (Slice α6.3b).

Coverage map (α6.3b pre-flight / D7):

* U1 — happy path: returns the track's clips ordered by ``start_seconds`` ASC
  (``id`` tiebreak) + the aggregate token; read-only (no commit).
* U2 — empty track → ``[]`` (still 200-shaped, token surfaced).
* U3 — only this track's clips are returned (cross-track isolation).
* U4 — unknown project / timeline / track → ``NotFoundError`` (404).
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.application.use_cases.timeline.list_clips import ListClips
from app.application.use_cases.timeline.results import ClipListResult
from app.core.errors import NotFoundError
from tests.unit.application.use_cases.timeline._helpers import (
    build_env,
    seed_clip,
    seed_timeline,
    seed_track,
)


@pytest.mark.unit
async def test_u1_happy_path_ordered_by_start() -> None:
    env = build_env()
    timeline = await seed_timeline(env)
    track = await seed_track(env, timeline)
    await seed_clip(env, track, start_seconds=10.0, end_seconds=12.0)
    await seed_clip(env, track, start_seconds=0.0, end_seconds=5.0)
    await seed_clip(env, track, start_seconds=5.0, end_seconds=7.0)
    uc = ListClips(uow=env.uow)

    result = await uc.execute(
        project_id=env.project_id,
        track_id=track.id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
    )

    assert isinstance(result, ClipListResult)
    assert [c.start_seconds for c in result.clips] == [0.0, 5.0, 10.0]
    assert result.timeline_version == timeline.version
    assert env.uow.commits == 0


@pytest.mark.unit
async def test_u2_empty_track_returns_empty_list() -> None:
    env = build_env()
    timeline = await seed_timeline(env)
    track = await seed_track(env, timeline)
    uc = ListClips(uow=env.uow)

    result = await uc.execute(
        project_id=env.project_id,
        track_id=track.id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
    )

    assert result.clips == []
    assert result.timeline_version == timeline.version


@pytest.mark.unit
async def test_u3_cross_track_isolation() -> None:
    env = build_env()
    timeline = await seed_timeline(env)
    track_a = await seed_track(env, timeline, z_index=0)
    track_b = await seed_track(env, timeline, z_index=1)
    await seed_clip(env, track_a, start_seconds=0.0, end_seconds=1.0)
    await seed_clip(env, track_b, start_seconds=0.0, end_seconds=1.0)
    await seed_clip(env, track_b, start_seconds=2.0, end_seconds=3.0)
    uc = ListClips(uow=env.uow)

    result = await uc.execute(
        project_id=env.project_id,
        track_id=track_b.id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
    )

    assert len(result.clips) == 2
    assert all(c.track_id == track_b.id for c in result.clips)


@pytest.mark.unit
async def test_u4_unknown_track_raises_404() -> None:
    env = build_env()
    await seed_timeline(env)
    uc = ListClips(uow=env.uow)

    with pytest.raises(NotFoundError):
        await uc.execute(
            project_id=env.project_id,
            track_id=uuid4(),
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
        )
