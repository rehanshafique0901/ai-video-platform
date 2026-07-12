"""Unit tests for ``GetTimeline`` (Slice α6.3a).

Coverage map (α6.3 pre-flight):

* U1 — happy path: returns the timeline + its tracks ordered by ``z_index``.
* U2 — no tracks: returns the timeline with an empty track list.
* U3 — unknown project (not the caller's) → ``NotFoundError`` (404).
* U4 — project without a provisioned timeline → ``NotFoundError`` (404).
* U5 — read-only: no commit.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.application.use_cases.timeline.get_timeline import GetTimeline
from app.core.errors import NotFoundError
from tests.unit.application.use_cases.timeline._helpers import (
    build_env,
    seed_timeline,
    seed_track,
)


@pytest.mark.unit
async def test_u1_happy_path_returns_tracks_ordered_by_z_index() -> None:
    env = build_env()
    timeline = await seed_timeline(env)
    await seed_track(env, timeline, z_index=2, name="B")
    await seed_track(env, timeline, z_index=0, name="A")
    await seed_track(env, timeline, z_index=1, name="middle")
    uc = GetTimeline(uow=env.uow)

    result = await uc.execute(
        project_id=env.project_id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
    )

    assert result.timeline.id == timeline.id
    assert [t.z_index for t in result.tracks] == [0, 1, 2]


@pytest.mark.unit
async def test_u2_no_tracks_returns_empty_list() -> None:
    env = build_env()
    await seed_timeline(env)
    uc = GetTimeline(uow=env.uow)

    result = await uc.execute(
        project_id=env.project_id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
    )

    assert result.tracks == []


@pytest.mark.unit
async def test_u3_unknown_project_raises_404() -> None:
    env = build_env()
    await seed_timeline(env)
    uc = GetTimeline(uow=env.uow)

    with pytest.raises(NotFoundError):
        await uc.execute(
            project_id=uuid4(),
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
        )


@pytest.mark.unit
async def test_u4_no_timeline_raises_404() -> None:
    env = build_env()  # project exists, no timeline provisioned
    uc = GetTimeline(uow=env.uow)

    with pytest.raises(NotFoundError):
        await uc.execute(
            project_id=env.project_id,
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
        )


@pytest.mark.unit
async def test_u5_read_only_no_commit() -> None:
    env = build_env()
    await seed_timeline(env)
    uc = GetTimeline(uow=env.uow)

    await uc.execute(
        project_id=env.project_id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
    )

    assert env.uow.commits == 0
